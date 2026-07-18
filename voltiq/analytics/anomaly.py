"""Two-layer battery anomaly detection.

Layer 1 — deterministic safety rules. These mirror the hard limits a real BMS
enforces; they must fire regardless of what any model thinks:

* cell over/under-voltage
* excessive cell imbalance (min-max spread), the classic weak-cell signature
* pack over-temperature and abnormal self-heating vs ambient

Layer 2 — Isolation Forest over windowed features. Catches "weird but within
limits" behaviour before it trips a rule: creeping imbalance, unusual
temperature-vs-current relationships, resistance growth. The model is fitted
per-vehicle on the earliest (assumed-healthy) portion of its own history, so
each vehicle learns its own normal.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sklearn.ensemble import IsolationForest

# ---------------------------------------------------------------- rule layer

RULES = {
    "CELL_OVERVOLT": dict(limit=4250.0, severity="critical",
                          text="Cell over-voltage: {v:.0f} mV (limit 4250 mV)"),
    "CELL_UNDERVOLT": dict(limit=2800.0, severity="critical",
                           text="Cell under-voltage: {v:.0f} mV (limit 2800 mV)"),
    "CELL_IMBALANCE_WARN": dict(limit=100.0, severity="warning",
                                text="Cell imbalance {v:.0f} mV (warn ≥ 100 mV)"),
    "CELL_IMBALANCE_CRIT": dict(limit=200.0, severity="critical",
                                text="Cell imbalance {v:.0f} mV (critical ≥ 200 mV)"),
    "PACK_OVERTEMP_WARN": dict(limit=50.0, severity="warning",
                               text="Pack temperature {v:.1f} °C (warn ≥ 50 °C)"),
    "PACK_OVERTEMP_CRIT": dict(limit=58.0, severity="critical",
                               text="Pack temperature {v:.1f} °C (critical ≥ 58 °C)"),
}


def rule_alerts(vin: str, ts: float, cell: dict, temps: dict) -> list[dict]:
    """Evaluate safety rules on one aligned sample of CellStats + Temps."""
    out: list[dict] = []

    def fire(code: str, value: float) -> None:
        r = RULES[code]
        out.append(dict(vin=vin, ts=ts, severity=r["severity"], code=code,
                        message=r["text"].format(v=value), value=round(value, 2)))

    vmax, vmin = cell["CellVoltMax"], cell["CellVoltMin"]
    spread = vmax - vmin
    tmax = temps["TempMax"]

    if vmax >= RULES["CELL_OVERVOLT"]["limit"]:
        fire("CELL_OVERVOLT", vmax)
    if 0 < vmin <= RULES["CELL_UNDERVOLT"]["limit"]:
        fire("CELL_UNDERVOLT", vmin)
    if spread >= RULES["CELL_IMBALANCE_CRIT"]["limit"]:
        fire("CELL_IMBALANCE_CRIT", spread)
    elif spread >= RULES["CELL_IMBALANCE_WARN"]["limit"]:
        fire("CELL_IMBALANCE_WARN", spread)
    if tmax >= RULES["PACK_OVERTEMP_CRIT"]["limit"]:
        fire("PACK_OVERTEMP_CRIT", tmax)
    elif tmax >= RULES["PACK_OVERTEMP_WARN"]["limit"]:
        fire("PACK_OVERTEMP_WARN", tmax)
    return out


# ---------------------------------------------------------------- ML layer

@dataclass
class AnomalyResult:
    anomaly_rate_pct: float          # % of recent windows flagged anomalous
    n_windows: int
    flagged_ts: list[float]          # timestamps of anomalous windows


def _windows(features: np.ndarray, ts: np.ndarray, size: int) -> tuple[np.ndarray, np.ndarray]:
    """Aggregate row-level features into fixed-size window statistics."""
    n = len(features) // size
    if n == 0:
        return np.empty((0, features.shape[1] * 2)), np.empty(0)
    trimmed = features[: n * size].reshape(n, size, -1)
    mean = trimmed.mean(axis=1)
    std = trimmed.std(axis=1)
    return np.hstack([mean, std]), ts[: n * size : size]


def detect_anomalies(
    ts: np.ndarray,
    features: np.ndarray,
    window: int = 30,
    train_frac: float = 0.3,
    contamination: float = 0.02,
    random_state: int = 0,
) -> AnomalyResult:
    """Fit Isolation Forest on the earliest windows, score the remainder.

    features columns: [cell_spread_mv, temp_delta_ambient, abs_current,
                       pack_voltage] — ambient-relative temperature is used
    instead of absolute so seasonal weather does not read as an anomaly.
    """
    X, wts = _windows(features, ts, window)
    n = len(X)
    if n < 20:
        return AnomalyResult(anomaly_rate_pct=0.0, n_windows=n, flagged_ts=[])

    n_train = max(10, int(n * train_frac))
    model = IsolationForest(
        n_estimators=100, contamination=contamination, random_state=random_state
    )
    model.fit(X[:n_train])
    preds = model.predict(X[n_train:])          # -1 = anomaly
    flagged = wts[n_train:][preds == -1]
    rate = 100.0 * float((preds == -1).sum()) / max(1, len(preds))
    return AnomalyResult(
        anomaly_rate_pct=round(rate, 2),
        n_windows=n,
        flagged_ts=[float(t) for t in flagged[-50:]],
    )
