"""Analytics tests: rules, anomaly detection, SOH and RUL."""

import numpy as np

from voltiq.analytics.anomaly import detect_anomalies, rule_alerts
from voltiq.analytics.rul import predict_rul
from voltiq.analytics.soh import CapacityPoint, estimate_soh

HEALTHY_CELL = {"CellVoltMin": 3700, "CellVoltMax": 3715, "CellVoltAvg": 3707,
                "WeakCellIndex": 0}
HEALTHY_TEMP = {"TempMin": 24, "TempMax": 27, "TempAvg": 25.5, "AmbientTemp": 22}


def test_no_alerts_when_healthy():
    assert rule_alerts("VIN1", 0.0, HEALTHY_CELL, HEALTHY_TEMP) == []


def test_imbalance_alerts_fire():
    cell = dict(HEALTHY_CELL, CellVoltMin=3500, CellVoltMax=3720)
    codes = {a["code"] for a in rule_alerts("VIN1", 0.0, cell, HEALTHY_TEMP)}
    assert "CELL_IMBALANCE_CRIT" in codes


def test_overtemp_severity_ladder():
    warn = rule_alerts("V", 0, HEALTHY_CELL, dict(HEALTHY_TEMP, TempMax=52))
    crit = rule_alerts("V", 0, HEALTHY_CELL, dict(HEALTHY_TEMP, TempMax=60))
    assert any(a["code"] == "PACK_OVERTEMP_WARN" for a in warn)
    assert any(a["code"] == "PACK_OVERTEMP_CRIT" for a in crit)


def test_isolation_forest_flags_shifted_regime():
    rng = np.random.default_rng(0)
    n = 3000
    ts = np.arange(n, dtype=float)
    base = np.column_stack([
        rng.normal(15, 2, n),     # cell spread mV
        rng.normal(3, 0.5, n),    # temp delta above ambient
        rng.normal(90, 10, n),    # |current|
        rng.normal(380, 2, n),    # pack V
    ])
    # inject fault regime in the last quarter: imbalance + self-heating
    base[-n // 4 :, 0] += 120.0
    base[-n // 4 :, 1] += 15.0
    res = detect_anomalies(ts, base)
    assert res.n_windows > 20
    assert res.anomaly_rate_pct > 10.0
    # flagged windows should be concentrated in the fault region
    assert np.median(res.flagged_ts) > ts[n // 2]


def test_soh_recovers_known_capacity():
    """Simulate ideal discharge data for a pack of known capacity -> SOH ≈ truth."""
    true_cap = 200.0  # Ah (rated 230 -> SOH ~87 %)
    rated = 230.0
    ts, cur, soc, cyc = [], [], [], []
    t = 0.0
    soc_now = 95.0
    for _ in range(40):  # 40 discharge segments
        for _ in range(120):
            i = 80.0
            ts.append(t)
            cur.append(i)
            soc.append(soc_now)
            cyc.append(t / 100_000)
            soc_now -= (i * 60.0 / 3600.0) / true_cap * 100.0
            t += 60.0
        # recharge instantly (idle gap)
        ts.append(t)
        cur.append(0.0)
        soc.append(soc_now)
        cyc.append(t / 100_000)
        soc_now = 95.0
        t += 3600.0
    est = estimate_soh(np.array(ts), np.array(cur), np.array(soc),
                       np.array(cyc), rated_capacity_ah=rated)
    assert est is not None
    assert abs(est.capacity_ah - true_cap) / true_cap < 0.05
    assert abs(est.soh_pct - 100.0 * true_cap / rated) < 5.0


def test_rul_from_sqrt_fade_law():
    """History generated from C(n)=C0(1-k√n) -> predictor recovers k and EOL."""
    rated, k = 230.0, 0.008
    history = [
        CapacityPoint(ts=float(n * 1000), cycles=float(n),
                      capacity_ah=rated * (1 - k * np.sqrt(n)))
        for n in range(10, 300, 10)
    ]
    est = predict_rul(history, rated, eol_soh=0.80,
                      km_per_cycle=350.0, seconds_per_cycle=86_400.0)
    assert est is not None
    assert abs(est.fade_k - k) < 1e-3
    true_eol = (0.20 / k) ** 2  # 625 cycles
    assert abs(est.eol_cycles - true_eol) < 30
    assert est.rul_cycles > 0
    assert est.rul_km is not None and est.rul_km > 0


def test_rul_requires_history():
    assert predict_rul([], 230.0) is None
