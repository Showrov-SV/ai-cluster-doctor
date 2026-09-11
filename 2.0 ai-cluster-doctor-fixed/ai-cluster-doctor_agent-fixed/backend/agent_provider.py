"""
agent_provider.py
AgentProvider - the Host-side TelemetryProvider for real hardware reporting
in over HTTP via installed Agents (see POST /api/agent/telemetry in
main.py). Agents never run AI - they only push raw readings here.

This buffers what's been reported per device_id in the same shape
SimulatorProvider already produces, so ml.py and the rest of main.py work
unchanged regardless of which provider a node came from.
"""

from __future__ import annotations
import time
from collections import deque
from typing import Dict, List

from providers import TelemetryProvider

HISTORY_MAXLEN = 300


class AgentProvider(TelemetryProvider):
    """State is populated by incoming HTTP pushes from Agents, not
    generated locally - `tick()` is a no-op since there's nothing to
    advance between pushes."""

    def __init__(self):
        self._hostnames: Dict[str, str] = {}
        self._history: Dict[str, deque] = {}
        self._last_seen: Dict[str, float] = {}

    def tick(self) -> None:
        pass  # agents push asynchronously; nothing to advance here

    def ingest(self, device_id: str, hostname: str, reading: dict) -> None:
        """Called by POST /api/agent/telemetry when an Agent reports in."""
        if device_id not in self._history:
            self._history[device_id] = deque(maxlen=HISTORY_MAXLEN)
        self._hostnames[device_id] = hostname
        reading = dict(reading)
        reading.setdefault("timestamp", time.time())
        self._history[device_id].append(reading)
        self._last_seen[device_id] = time.time()

    def get_node_ids(self) -> List[str]:
        return list(self._history.keys())

    def node_exists(self, node_id: str) -> bool:
        return node_id in self._history

    def get_node_info(self, node_id: str) -> Dict:
        return {"name": self._hostnames.get(node_id, node_id), "gpu_model": "Agent-reported"}

    def get_history(self, node_id: str, limit: int = 300) -> List[dict]:
        return list(self._history.get(node_id, []))[-limit:]
