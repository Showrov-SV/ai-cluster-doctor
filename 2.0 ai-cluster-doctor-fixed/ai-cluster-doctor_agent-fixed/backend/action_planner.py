"""
action_planner.py
Rule-based only (no model). Looks at whichever metric caused the largest
threshold deduction (reusing ml.THRESHOLDS / ml._threshold_deduction) and
maps it to one concrete next step with a priority: NOW (critical) / TODAY
(warning) / LATER (anomaly-only). Returns None for a healthy node - nothing
to plan.
"""

from __future__ import annotations
from typing import Dict, Optional

from ml import _threshold_deduction

ACTIONS = {
    "temperature_c": {
        "critical": "Throttle workload and inspect cooling/thermal paste immediately",
        "warn": "Schedule a cooling/airflow inspection today",
    },
    "memory_utilization_pct": {
        "critical": "Investigate for a memory leak and restart the offending process now",
        "warn": "Review memory usage trend and plan a process restart",
    },
    "power_watts": {
        "critical": "Check PSU health and investigate abnormal power draw immediately",
        "warn": "Monitor power draw and inspect PSU/wiring today",
    },
    "fan_speed_pct": {
        "critical": "Replace or reseat the failing fan immediately - cooling failure risk",
        "warn": "Inspect fan for dust/wear and plan replacement today",
    },
}


def plan_action(root_cause: Optional[str], latest: Dict, health_score: float) -> Optional[Dict]:
    if health_score >= 85 or not latest:
        return None

    worst_feature, worst_severity, worst_deduction = None, "ok", 0.0
    for feature in ["temperature_c", "memory_utilization_pct", "power_watts", "fan_speed_pct"]:
        value = latest.get(feature)
        if value is None:
            continue
        deduction, severity = _threshold_deduction(feature, value)
        if deduction > worst_deduction:
            worst_deduction = deduction
            worst_feature = feature
            worst_severity = severity

    if worst_feature:
        priority = "NOW" if worst_severity == "critical" else "TODAY"
        action = ACTIONS[worst_feature][worst_severity]
        reason = root_cause or f"{worst_severity.upper()} deduction on {worst_feature}"
    else:
        # no threshold breach but score is still down - anomaly-only signal
        priority = "LATER"
        action = "Monitor node closely; no single metric breached threshold"
        reason = root_cause or "Anomalous multi-metric pattern relative to this node's baseline"

    return {"action": action, "priority": priority, "reason": reason}
