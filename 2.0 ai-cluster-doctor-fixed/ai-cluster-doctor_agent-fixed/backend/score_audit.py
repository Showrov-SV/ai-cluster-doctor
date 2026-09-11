"""
score_audit.py
Additive, explainability-only module: re-derives the same per-metric point
deductions ml.compute_health() already computes (reusing ml.THRESHOLDS /
ml._threshold_deduction - not a second set of numbers) into a human-readable
breakdown. Does not change ml.py's scoring or root-cause logic.
"""

from __future__ import annotations
from typing import Dict, Optional

from ml import THRESHOLDS, _threshold_deduction

READABLE = {
    "temperature_c": "GPU temperature",
    "memory_utilization_pct": "Memory utilization",
    "power_watts": "Power draw",
    "fan_speed_pct": "Fan speed",
}


def audit_score(latest: Dict, anomaly_score: float) -> Optional[Dict]:
    """Returns {"health_score", "score_breakdown": [{metric, penalty, reason}]}."""
    if not latest:
        return None

    score = 100.0
    breakdown = []

    for feature in ["temperature_c", "memory_utilization_pct", "power_watts", "fan_speed_pct"]:
        value = latest.get(feature)
        if value is None:
            continue
        deduction, severity = _threshold_deduction(feature, value)
        score -= deduction
        if deduction > 0:
            breakdown.append({
                "metric": feature,
                "penalty": -deduction,
                "reason": f"{severity.upper()}: {READABLE[feature]} at {value}",
            })

    anomaly_penalty = round(anomaly_score * 25, 1)
    score -= anomaly_penalty
    if anomaly_penalty > 0:
        breakdown.append({
            "metric": "anomaly",
            "penalty": -anomaly_penalty,
            "reason": f"Isolation Forest abnormality blend ({anomaly_score:.2f})",
        })

    score = max(0.0, min(100.0, score))

    return {
        "health_score": round(score, 1),
        "score_breakdown": breakdown,
    }
