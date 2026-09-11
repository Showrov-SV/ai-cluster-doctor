"""
simulator.py
Simulates a GPU cluster producing telemetry. In a real deployment this would
be replaced with data pulled from NVIDIA DCGM / Prometheus exporters. Here we
generate physically-plausible values, including a few nodes that slowly drift
towards failure so the prediction/anomaly-detection logic has something real
to catch.
"""

import random
import time
import math
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List


HISTORY_LEN = 300  # keep last N readings per node (~ last N * tick_seconds of data)


@dataclass
class NodeState:
    node_id: str
    name: str
    gpu_model: str
    # baseline "healthy" operating point for this node
    base_temp: float
    base_util: float
    base_mem: float
    base_power: float
    base_fan: float
    # degradation trajectory (0 = healthy, grows towards 1 = failing)
    degradation: float = 0.0
    degrading: bool = False
    history: Deque[dict] = field(default_factory=lambda: deque(maxlen=HISTORY_LEN))

    def tick(self):
        t = time.time()

        # Occasionally start a slow degradation event on a healthy node
        if not self.degrading and random.random() < 0.0015:
            self.degrading = True

        if self.degrading:
            self.degradation = min(1.0, self.degradation + random.uniform(0.002, 0.01))
            if self.degradation >= 1.0 and random.random() < 0.02:
                # node "recovers" after maintenance would reset it; here we
                # occasionally reset so the demo keeps producing fresh events
                self.degradation = 0.0
                self.degrading = False

        noise = lambda scale: random.uniform(-scale, scale)

        temp = self.base_temp + self.degradation * 28 + noise(1.5)
        util = min(100, max(0, self.base_util + self.degradation * 15 + noise(4)))
        mem = min(100, max(0, self.base_mem + self.degradation * 20 + noise(3)))
        power = self.base_power + self.degradation * 60 + noise(8)
        fan = max(0, self.base_fan - self.degradation * 25 + noise(3))  # fan struggles as node degrades

        # occasional sharp spike (transient anomaly) independent of slow drift
        if random.random() < 0.01:
            temp += random.uniform(8, 15)
            power += random.uniform(20, 40)

        reading = {
            "timestamp": t,
            "temperature_c": round(temp, 1),
            "gpu_utilization_pct": round(util, 1),
            "memory_utilization_pct": round(mem, 1),
            "power_watts": round(power, 1),
            "fan_speed_pct": round(fan, 1),
        }
        self.history.append(reading)
        return reading


def _make_nodes() -> Dict[str, NodeState]:
    gpu_models = ["NVIDIA A100 80GB", "NVIDIA H100 80GB", "NVIDIA A100 40GB"]
    nodes = {}
    for i in range(1, 9):
        node_id = f"gpu-node-{i:02d}"
        nodes[node_id] = NodeState(
            node_id=node_id,
            name=node_id,
            gpu_model=random.choice(gpu_models),
            base_temp=random.uniform(45, 58),
            base_util=random.uniform(35, 75),
            base_mem=random.uniform(30, 65),
            base_power=random.uniform(150, 250),
            base_fan=random.uniform(45, 65),
        )
    return nodes


class ClusterSimulator:
    def __init__(self):
        self.nodes: Dict[str, NodeState] = _make_nodes()

    def tick_all(self):
        for node in self.nodes.values():
            node.tick()

    def get_node_ids(self) -> List[str]:
        return list(self.nodes.keys())

    def get_latest(self, node_id: str) -> dict:
        node = self.nodes[node_id]
        return node.history[-1] if node.history else {}

    def get_history(self, node_id: str, limit: int = HISTORY_LEN) -> List[dict]:
        node = self.nodes[node_id]
        return list(node.history)[-limit:]
