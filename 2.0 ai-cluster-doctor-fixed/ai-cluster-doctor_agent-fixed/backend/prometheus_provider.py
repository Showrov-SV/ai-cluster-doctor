"""
prometheus_provider.py
TelemetryProvider backed by a Prometheus server that's already scraping
dcgm-exporter (or any exporter using the same metric names) across one or
more real GPU hosts.

This is a pull provider: tick() runs one instant PromQL query per metric
(covering every GPU Prometheus knows about at once) and caches the results
per node, so get_node_ids/get_node_info/get_history stay fast and don't hit
the network on every API request - the same shape as
SimulatorProvider/AgentProvider, so main.py and ml.py never need to know
the difference.

Metric mapping (standard DCGM Prometheus metric names -> our frozen 5-key
contract):
    DCGM_FI_DEV_GPU_TEMP        -> temperature_c
    DCGM_FI_DEV_GPU_UTIL        -> gpu_utilization_pct
    DCGM_FI_DEV_MEM_COPY_UTIL   -> memory_utilization_pct
    DCGM_FI_DEV_POWER_USAGE     -> power_watts
    fan_speed_pct has no standard DCGM field - most datacenter GPUs rely on
    server chassis fans rather than exposing a per-GPU fan sensor - so it's
    always reported as 0.0. This is an honest placeholder, not a fabricated
    reading, matching the convention already used in agent_core.py.

Configuration: PROMETHEUS_URL env var (e.g. "http://prometheus:9090"),
defaults to "http://localhost:9090".

Not wired into main.py's active provider list by default - see the Phase 5
notes for how to add it once you have a real Prometheus server reachable.
"""

from __future__ import annotations
import os
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


def _node_key(labels: dict) -> str:
    """Hostname+gpu-index uniquely identifies a physical GPU across a
    fleet; falls back to the `instance` label if Hostname isn't present."""
    host = labels.get("Hostname") or labels.get("instance", "unknown")
    gpu = labels.get("gpu", "0")
    return f"{host}:gpu{gpu}"


class PrometheusProvider(TelemetryProvider):
    """Pulls the latest DCGM metrics for every GPU known to a Prometheus
    server via its HTTP query API, buffering them locally exactly like
    SimulatorProvider/AgentProvider do."""

    def __init__(self, base_url: Optional[str] = None, timeout: float = 10.0):
        self.base_url = (base_url or os.environ.get("PROMETHEUS_URL", "http://localhost:9090")).rstrip("/")
        self.timeout = timeout
        self._history: Dict[str, deque] = {}
        self._hostnames: Dict[str, str] = {}

    def _query(self, promql: str) -> list:
        resp = requests.get(f"{self.base_url}/api/v1/query", params={"query": promql}, timeout=self.timeout)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("status") != "success":
            raise RuntimeError(f"Prometheus query failed: {payload}")
        return payload["data"]["result"]

    def tick(self) -> None:
        per_node_values: Dict[str, dict] = {}
        for our_key, promql_metric in METRIC_MAP.items():
            for series in self._query(promql_metric):
                key = _node_key(series["metric"])
                per_node_values.setdefault(key, {})[our_key] = float(series["value"][1])
                self._hostnames[key] = series["metric"].get("Hostname") or key

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
        return {"name": self._hostnames.get(node_id, node_id), "gpu_model": "DCGM (via Prometheus)"}

    def get_history(self, node_id: str, limit: int = 300) -> List[dict]:
        return list(self._history.get(node_id, []))[-limit:]
