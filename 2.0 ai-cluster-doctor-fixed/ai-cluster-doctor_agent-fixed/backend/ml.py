"""
ml.py
Real (if lightweight) machine-learning logic for AI Cluster Doctor:

1. Anomaly detection  -> sklearn IsolationForest trained per-node on its own
   recent telemetry window, so "normal" is learned per-node rather than
   using one global threshold.
2. Health score        -> rule-based deductions (thresholds informed by
   common NVIDIA DCGM operating guidance) combined with the anomaly score
   from (1), scaled to 0-100.
3. Root cause analysis -> simple, explainable rule-matching against the
   feature that deviates most, so the system can say *why* a node's score
   dropped rather than just *that* it dropped.
4. Failure prediction (short-term) -> linear trend extrapolation on
   temperature/memory over the recent window to estimate whether a
   threshold breach is imminent (~2 min lookahead).
5. Failure prediction (long-term)  -> XGBoost regression trained on
   windowed lags of this node's own history, predicting the metric value
   several minutes further out than (4) can reach, to catch slower-building
   degradation trends the short linear window would miss. This coexists
   with (4) - neither replaces the other; a node can show a short-term
   trend, a long-term trend, both, or neither.
"""

from __future__ import annotations
import numpy as np
from sklearn.ensemble import IsolationForest
from xgboost import XGBRegressor
from typing import List, Dict, Optional

FEATURES = [
    "temperature_c",
    "gpu_utilization_pct",
    "memory_utilization_pct",
    "power_watts",
    "fan_speed_pct",
]

MIN_SAMPLES_FOR_MODEL = 40

THRESHOLDS = {
    "temperature_c": {"warn": 78, "critical": 88},
    "memory_utilization_pct": {"warn": 88, "critical": 96},
    "power_watts": {"warn": 320, "critical": 380},
    "fan_speed_pct": {"warn": 20, "critical": 10, "inverse": True},  # low fan is bad
}

# Features that get both short-term (linear) and long-term (XGBoost)
# predictions. Kept identical between the two so the two predictors are
# directly comparable for the same metric.
PREDICTED_FEATURES = ["temperature_c", "memory_utilization_pct", "fan_speed_pct"]

# --- long-term (XGBoost) predictor configuration ---
LONG_TERM_WINDOW = 10  # how many recent raw readings feed each prediction
LONG_TERM_HORIZONS = (60, 150)  # ticks ahead: ~2 min and ~5 min at a 2s tick
LONG_TERM_MIN_TRAIN_PAIRS = 20  # need at least this many windowed examples to train


def _to_matrix(history: List[dict]) -> np.ndarray:
    return np.array([[row[f] for f in FEATURES] for row in history], dtype=float)


def detect_anomaly(history: List[dict]) -> Dict:
    """Fit an IsolationForest on this node's own recent history and score the
    latest reading against it. Returns anomaly flag + normalized score."""
    if len(history) < MIN_SAMPLES_FOR_MODEL:
        return {"is_anomaly": False, "anomaly_score": 0.0, "confidence": "low_data"}

    X = _to_matrix(history)
    X_train = X[:-1]
    x_latest = X[-1:].copy()

    model = IsolationForest(
        n_estimators=100,
        contamination=0.08,
        random_state=42,
    )
    model.fit(X_train)

    raw_score = model.decision_function(x_latest)[0]  # >0 normal, <0 anomalous
    is_anomaly = bool(model.predict(x_latest)[0] == -1)

    # normalize roughly into a 0-1 "abnormality" score for blending into health score
    abnormality = float(np.clip((0.15 - raw_score) / 0.30, 0, 1))

    return {
        "is_anomaly": is_anomaly,
        "anomaly_score": round(abnormality, 3),
        "confidence": "ok",
    }


def predict_trend(history: List[dict], feature: str, lookahead_ticks: int = 60) -> Optional[Dict]:
    """Simple linear regression on the recent window to estimate whether a
    metric is trending toward its critical threshold, and roughly how many
    ticks away that is. This is intentionally simple (explainable > fancy)."""
    if len(history) < 20 or feature not in THRESHOLDS:
        return None

    y = np.array([row[feature] for row in history[-60:]], dtype=float)
    x = np.arange(len(y))
    slope, intercept = np.polyfit(x, y, 1)

    critical = THRESHOLDS[feature]["critical"]
    inverse = THRESHOLDS[feature].get("inverse", False)

    # minimum slope magnitude (per tick) required before we treat this as a
    # real trend rather than random noise around a flat baseline
    MIN_SLOPE = {
        "temperature_c": 0.08,
        "memory_utilization_pct": 0.08,
        "fan_speed_pct": 0.08,
    }.get(feature, 0.05)

    current = y[-1]
    trending_toward_failure = (slope > MIN_SLOPE and not inverse) or (slope < -MIN_SLOPE and inverse)

    if not trending_toward_failure:
        return {"trending": False}

    ticks_to_critical = (critical - current) / slope
    if ticks_to_critical <= 0 or ticks_to_critical > lookahead_ticks:
        # trend exists but won't reach critical within our lookahead window -
        # not worth surfacing as an actionable prediction
        return {"trending": False}

    return {
        "trending": True,
        "eta_ticks": round(ticks_to_critical, 1),
        "slope_per_tick": round(float(slope), 3),
    }


def predict_long_term(history: List[dict], feature: str, horizon_ticks: int) -> Optional[Dict]:
    """XGBoost-based long-term prediction for a single feature/horizon.

    Trains entirely on this node's own already-observed history: for every
    past tick i, features are simple trend/summary stats of a short window
    ending at i, and the label is the metric's value `horizon_ticks` later -
    which has *already happened* by the time this runs, since it's read
    straight out of the history buffer. No future/unseen data is used.

    This deliberately reuses THRESHOLDS (the same critical levels
    predict_trend checks against) so the two predictors are directly
    comparable, but is otherwise fully independent of predict_trend - it
    doesn't call it, share its slope calculation, or gate on its result.
    """
    if feature not in THRESHOLDS:
        return None

    values = [row[feature] for row in history]
    n = len(values)
    window = LONG_TERM_WINDOW

    X, y = [], []
    for i in range(window - 1, n - horizon_ticks):
        w = values[i - window + 1: i + 1]
        mean = sum(w) / window
        std = (sum((v - mean) ** 2 for v in w) / window) ** 0.5
        slope = (w[-1] - w[0]) / window
        X.append([w[-1], mean, std, slope])
        # Predict the *delta* over the horizon, not the absolute future value.
        # Tree-based models like XGBoost can't output a value outside the
        # range of labels they were trained on (leaves only average training
        # labels) - if we trained on absolute values, the model could never
        # predict a genuinely new maximum for a monotonic trend, which would
        # silently defeat the whole point of a long-term predictor. Deltas
        # stay roughly constant for a steady trend regardless of the
        # absolute level, so predicting the delta and adding it to the
        # current reading correctly extrapolates past the observed range.
        y.append(values[i + horizon_ticks] - w[-1])

    if len(X) < LONG_TERM_MIN_TRAIN_PAIRS:
        return None  # not enough history yet for this horizon - stay silent rather than guess

    model = XGBRegressor(n_estimators=30, max_depth=3, learning_rate=0.3, verbosity=0)
    model.fit(np.array(X), np.array(y))

    w = values[-window:]
    mean = sum(w) / window
    std = (sum((v - mean) ** 2 for v in w) / window) ** 0.5
    slope = (w[-1] - w[0]) / window
    predicted_delta = float(model.predict(np.array([[w[-1], mean, std, slope]]))[0])
    predicted = w[-1] + predicted_delta

    cfg = THRESHOLDS[feature]
    inverse = cfg.get("inverse", False)
    critical = cfg["critical"]
    would_breach = (predicted <= critical) if inverse else (predicted >= critical)
    if not would_breach:
        return None

    return {
        "trending": True,
        "predicted_value": round(predicted, 2),
        "horizon_ticks": horizon_ticks,
        "model": "xgboost",
    }


def _threshold_deduction(feature: str, value: float) -> (float, str):
    """Returns (deduction_points, severity_label) for a single metric."""
    cfg = THRESHOLDS.get(feature)
    if not cfg:
        return 0.0, "ok"

    inverse = cfg.get("inverse", False)
    if inverse:
        if value <= cfg["critical"]:
            return 30.0, "critical"
        if value <= cfg["warn"]:
            return 12.0, "warn"
        return 0.0, "ok"
    else:
        if value >= cfg["critical"]:
            return 30.0, "critical"
        if value >= cfg["warn"]:
            return 12.0, "warn"
        return 0.0, "ok"


def compute_health(history: List[dict]) -> Dict:
    """Combine threshold-based deductions with the ML anomaly score into a
    single 0-100 health score, plus a root-cause explanation."""
    if not history:
        return {
            "health_score": 100.0,
            "status": "unknown",
            "root_cause": None,
            "anomaly": {"is_anomaly": False, "anomaly_score": 0.0, "confidence": "no_data"},
            "predictions": {},
            "long_term_predictions": {},
        }

    latest = history[-1]
    score = 100.0
    worst_feature = None
    worst_severity = "ok"
    worst_deduction = 0.0

    for feature in ["temperature_c", "memory_utilization_pct", "power_watts", "fan_speed_pct"]:
        deduction, severity = _threshold_deduction(feature, latest[feature])
        score -= deduction
        if deduction > worst_deduction:
            worst_deduction = deduction
            worst_feature = feature
            worst_severity = severity

    anomaly = detect_anomaly(history)
    score -= anomaly["anomaly_score"] * 25  # blend ML anomaly signal into score
    score = max(0.0, min(100.0, score))

    if score >= 85:
        status = "healthy"
    elif score >= 60:
        status = "warning"
    else:
        status = "critical"

    root_cause = None
    if worst_feature:
        readable = {
            "temperature_c": "elevated GPU temperature",
            "memory_utilization_pct": "high memory utilization",
            "power_watts": "abnormal power draw",
            "fan_speed_pct": "insufficient fan speed / cooling failure",
        }[worst_feature]
        root_cause = f"{worst_severity.upper()}: {readable} ({latest[worst_feature]})"
    elif anomaly["is_anomaly"]:
        root_cause = "Anomalous multi-metric pattern detected relative to this node's own baseline"

    predictions = {}
    for feature in ["temperature_c", "memory_utilization_pct", "fan_speed_pct"]:
        pred = predict_trend(history, feature)
        if pred and pred.get("trending"):
            predictions[feature] = pred

    long_term_predictions = {}
    for feature in PREDICTED_FEATURES:
        per_horizon = {}
        for horizon in LONG_TERM_HORIZONS:
            pred = predict_long_term(history, feature, horizon)
            if pred:
                per_horizon[str(horizon)] = pred
        if per_horizon:
            long_term_predictions[feature] = per_horizon

    return {
        "health_score": round(score, 1),
        "status": status,
        "root_cause": root_cause,
        "anomaly": anomaly,
        "predictions": predictions,
        "long_term_predictions": long_term_predictions,
    }
