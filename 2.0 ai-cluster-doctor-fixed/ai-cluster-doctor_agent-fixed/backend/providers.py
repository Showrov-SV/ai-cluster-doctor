"""
providers.py
Telemetry provider abstraction layer.

This module defines the seam between "how telemetry is produced" and "how
it's consumed" (the API layer in main.py, and the ML logic in ml.py).

Today, only SimulatorProvider exists, and it wraps the original
ClusterSimulator from simulator.py completely unchanged - this is a pure
adapter, not a rewrite. Behavior is identical to the original prototype.

Going forward, new telemetry sources (Agents reporting real hardware
metrics over the network, NVIDIA DCGM, Prometheus) are added by writing a
new class that implements TelemetryProvider - main.py and ml.py never need
to change, since both only ever depend on this interface, not on
ClusterSimulator directly.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Dict, List

from simulator import ClusterSimulator


class TelemetryProvider(ABC):
    """Abstract interface every telemetry source must implement.

    A "node" here means whatever unit of hardware is being monitored -
    today that's a simulated GPU node; later it may be a physical PC
    reporting in via an Agent. The rest of the system (ml.py, main.py)
    only ever talks to this interface.
    """

    @abstractmethod
    def tick(self) -> None:
        """Advance/refresh telemetry for all monitored nodes by one step.

        For the simulator this generates new synthetic readings. For a
        real provider this would poll/ingest the latest reported metrics.
        """
        raise NotImplementedError

    @abstractmethod
    def get_node_ids(self) -> List[str]:
        """Return the ids of every node currently known to this provider."""
        raise NotImplementedError

    @abstractmethod
    def node_exists(self, node_id: str) -> bool:
        """Whether a given node id is known to this provider."""
        raise NotImplementedError

    @abstractmethod
    def get_node_info(self, node_id: str) -> Dict:
        """Static/slow-changing node metadata (display name, GPU model, etc.)."""
        raise NotImplementedError

    @abstractmethod
    def get_history(self, node_id: str, limit: int = 300) -> List[dict]:
        """Return up to `limit` most recent telemetry readings for a node.

        Each reading must be a dict containing at minimum the keys:
        temperature_c, gpu_utilization_pct, memory_utilization_pct,
        power_watts, fan_speed_pct - this is the frozen data contract that
        ml.py depends on.
        """
        raise NotImplementedError


class SimulatorProvider(TelemetryProvider):
    """Wraps the existing ClusterSimulator so it satisfies TelemetryProvider.

    This is a pure adapter around the original, unmodified simulator.py -
    no simulation behavior is changed by introducing this abstraction.
    """

    def __init__(self):
        self._sim = ClusterSimulator()

    def tick(self) -> None:
        self._sim.tick_all()

    def get_node_ids(self) -> List[str]:
        return self._sim.get_node_ids()

    def node_exists(self, node_id: str) -> bool:
        return node_id in self._sim.nodes

    def get_node_info(self, node_id: str) -> Dict:
        node = self._sim.nodes[node_id]
        return {"name": node.name, "gpu_model": node.gpu_model}

    def get_history(self, node_id: str, limit: int = 300) -> List[dict]:
        return self._sim.get_history(node_id, limit)


class CompositeProvider(TelemetryProvider):
    """Combines multiple TelemetryProviders into one, so main.py keeps a
    single `sim: TelemetryProvider` regardless of how many underlying
    sources exist (simulator, agents, future DCGM/Prometheus). Each call is
    routed to whichever sub-provider currently owns that node id."""

    def __init__(self, providers: List[TelemetryProvider]):
        self._providers = providers

    def _owner(self, node_id: str):
        for p in self._providers:
            if p.node_exists(node_id):
                return p
        return None

    def tick(self) -> None:
        for p in self._providers:
            p.tick()

    def get_node_ids(self) -> List[str]:
        ids: List[str] = []
        for p in self._providers:
            ids.extend(p.get_node_ids())
        return ids

    def node_exists(self, node_id: str) -> bool:
        return self._owner(node_id) is not None

    def get_node_info(self, node_id: str) -> Dict:
        owner = self._owner(node_id)
        if owner is None:
            raise KeyError(node_id)
        return owner.get_node_info(node_id)

    def get_history(self, node_id: str, limit: int = 300) -> List[dict]:
        owner = self._owner(node_id)
        return owner.get_history(node_id, limit) if owner else []
