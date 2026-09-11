"""
dcgm_provider.py
TelemetryProvider that scrapes one or more dcgm-exporter /metrics endpoints
directly over HTTP (Prometheus *text exposition* format), bypassing a
Prometheus server entirely - useful for a small cluster where running a
full Prometheus deployment is unnecessary overhead.

This is a pull provider: tick() scrapes each configured dcgm-exporter
target and caches parsed readings per GPU, mirroring
SimulatorProvider/AgentProvider's shape so main.py and ml.py never need to
know the difference.

Metric mapping: identical to prometheus_provider.py (see its module
docstring for the fan_speed_pct placeholder rationale) - both providers map
the same DCGM field names onto our frozen 5-key contract, since
dcgm-exporter is the ultimate data source either way; this provider just
talks to it directly instead of through a Prometheus server.

Configuration: DCGM_EXPORTER_TARGETS env var, a comma-separated list of
"host:port" addresses of running dcgm-exporter processes, e.g.
    DCGM_EXPORTER_TARGETS=gpu-host-1:9400,gpu-host-2:9400

Not wired into main.py's active provider list by default - see the Phase 5
notes for how to add it once you have real dcgm-exporter targets reachable.
"""

from __future__ import annotations
import os
import re
import time
from collections import deque
from typing import Dict, List, Optional

import requests

from providers import TelemetryProvider

HISTORY_MAXLEN = 300

METRIC_MAP = {
    "temperature_c": "DCGM_FI_DEV_GPU_TEMP",
    "gpu_utilization_pct": "DCGM_FI_DEV_GPU_UTIL",
    "memory_utilization_pct": "DCGM_FI_DEV_MEM_COPY_UTIL",
    "power_watts": "DCGM_FI_DEV_POWER_USAGE",
}
_METRIC_NAME_TO_KEY = {v: k for k, v in METRIC_MAP.items()}

_LINE_RE = re.compile(r"^(\w+)\{([^}]*)\}\s+([0-9eE+\-.]+)\s*$")
_LABEL_RE = re.compile(r'(\w+)="([^"]*)"')


def _parse_labels(label_str: str) -> Dict[str, str]:
    return dict(_LABEL_RE.findall(label_str))


def _parse_exposition_text(text: str):
    """Yields (metric_name, labels, value) for every line matching
    Prometheus text exposition format that's one of our known DCGM metrics;
    HELP/TYPE comments and unrelated metrics are ignored."""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE_RE.match(line)
        if not match:
            continue
        metric_name, label_str, value_str = match.groups()
        if metric_name not in _METRIC_NAME_TO_KEY:
            continue
        yield metric_name, _parse_labels(label_str), float(value_str)


def _node_key(labels: dict, target: str) -> str:
    host = labels.get("Hostname") or target
    gpu = labels.get("gpu", "0")
    return f"{host}:gpu{gpu}"


class DCGMProvider(TelemetryProvider):
    """Scrapes one or more dcgm-exporter /metrics endpoints directly and
    buffers readings locally, exactly like
    SimulatorProvider/AgentProvider."""

    def __init__(self, targets: Optional[List[str]] = None, timeout: float = 10.0):
        env_targets = os.environ.get("DCGM_EXPORTER_TARGETS", "")
        self.targets = targets if targets is not None else [t.strip() for t in env_targets.split(",") if t.strip()]
        self.timeout = timeout
        self._history: Dict[str, deque] = {}
        self._hostnames: Dict[str, str] = {}

    def tick(self) -> None:
        for target in self.targets:
            resp = requests.get(f"http://{target}/metrics", timeout=self.timeout)
            resp.raise_for_status()

            per_node_values: Dict[str, dict] = {}
            for metric_name, labels, value in _parse_exposition_text(resp.text):
                key = _node_key(labels, target)
                our_key = _METRIC_NAME_TO_KEY[metric_name]
                per_node_values.setdefault(key, {})[our_key] = value
                self._hostnames[key] = labels.get("Hostname") or key

            now = time.time()
            for node_id, values in per_node_values.items():
                if node_id not in self._history:
                    self._history[node_id] = deque(maxlen=HISTORY_MAXLEN)
                self._history[node_id].append({
                    "timestamp": now,
                    "temperature_c": values.get("temperature_c", 0.0),
                    "gpu_utilization_pct": values.get("gpu_utilization_pct", 0.0),
                    "memory_utilization_pct": values.get("memory_utilization_pct", 0.0),
                    "power_watts": values.get("power_watts", 0.0),
                    "fan_speed_pct": 0.0,  # no standard DCGM field - see module docstring
                })

    def get_node_ids(self) -> List[str]:
        return list(self._history.keys())

    def node_exists(self, node_id: str) -> bool:
        return node_id in self._history

    def get_node_info(self, node_id: str) -> Dict:
        return {"name": self._hostnames.get(node_id, node_id), "gpu_model": "DCGM (direct exporter scrape)"}

    def get_history(self, node_id: str, limit: int = 300) -> List[dict]:
        return list(self._history.get(node_id, []))[-limit:]
