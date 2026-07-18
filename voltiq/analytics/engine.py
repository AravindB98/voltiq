"""Analytics engine: runs the full health pipeline for each vehicle.

For every VIN in the store it:

1. loads and time-aligns the decoded message streams,
2. evaluates safety rules on every sample (-> alerts table),
3. runs Isolation Forest anomaly detection over windowed features,
4. estimates SOH via partial-discharge coulomb counting,
5. extrapolates RUL from the capacity-fade trend,
6. rolls everything into a per-vehicle status written to `vehicle_health`.

Status policy:
    critical — any critical alert, or SOH below 80 %
    watch    — warnings, elevated anomaly rate, or SOH below 85 %
    healthy  — everything else
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from voltiq.analytics.anomaly import detect_anomalies, rule_alerts
from voltiq.analytics.rul import predict_rul
from voltiq.analytics.soh import estimate_soh
from voltiq.ingest.store import Store
from voltiq.simulator.battery import PackConfig

log = logging.getLogger(__name__)


@dataclass
class VehicleReport:
    vin: str
    status: str
    soh_pct: float | None
    rul_cycles: float | None
    new_alerts: int
    anomaly_rate_pct: float | None


def _aligned(series_a, series_b, tolerance_s: float = 5.0):
    """Pair samples from two message streams whose timestamps match closely."""
    j = 0
    for ts, sig in series_a:
        while j < len(series_b) and series_b[j][0] < ts - tolerance_s:
            j += 1
        if j < len(series_b) and abs(series_b[j][0] - ts) <= tolerance_s:
            yield ts, sig, series_b[j][1]


class AnalyticsEngine:
    def __init__(self, store: Store, pack_config: PackConfig | None = None,
                 alert_debounce_s: float = 3600.0) -> None:
        self.store = store
        self.pack_cfg = pack_config or PackConfig()
        self.alert_debounce_s = alert_debounce_s

    def run_vehicle(self, vin: str) -> VehicleReport:
        pack = self.store.signal_series(vin, "BMS_PackStatus")
        cells = self.store.signal_series(vin, "BMS_CellStats")
        temps = self.store.signal_series(vin, "BMS_Temps")
        energy = self.store.signal_series(vin, "BMS_Energy")

        # ---- Layer 1: safety rules on aligned CellStats + Temps.
        # Debounce: a given alert code re-fires at most once per hour, so a
        # persistent condition produces a readable trail instead of thousands
        # of duplicates.
        alerts: list[dict] = []
        last_fired: dict[str, float] = {}
        cell_temp = list(_aligned(cells, temps))
        for ts, cell_sig, temp_sig in cell_temp:
            for a in rule_alerts(vin, ts, cell_sig, temp_sig):
                if ts - last_fired.get(a["code"], -1e12) >= self.alert_debounce_s:
                    last_fired[a["code"]] = ts
                    alerts.append(a)
        new_alerts = self.store.insert_alerts(alerts) if alerts else 0

        # ---- Layer 2: Isolation Forest over windowed features
        anomaly_rate = None
        pack_by_ts = dict(pack)
        feats, fts = [], []
        for ts, cell_sig, temp_sig in cell_temp:
            p = pack_by_ts.get(ts)
            if p is None:
                continue
            feats.append([
                cell_sig["CellVoltMax"] - cell_sig["CellVoltMin"],
                temp_sig["TempAvg"] - temp_sig["AmbientTemp"],
                abs(p["PackCurrent"]),
                p["PackVoltage"],
            ])
            fts.append(ts)
        if len(feats) >= 600:
            res = detect_anomalies(np.array(fts), np.array(feats))
            anomaly_rate = res.anomaly_rate_pct

        # ---- SOH from coulomb counting
        soh = None
        rul = None
        if pack and energy:
            ts_arr = np.array([t for t, _ in pack])
            cur_arr = np.array([s["PackCurrent"] for _, s in pack])
            soc_arr = np.array([s["StateOfCharge"] for _, s in pack])
            e_ts = np.array([t for t, _ in energy])
            e_cyc = np.array([s["CycleCount"] for _, s in energy])
            cyc_arr = np.interp(ts_arr, e_ts, e_cyc)
            soh = estimate_soh(ts_arr, cur_arr, soc_arr, cyc_arr,
                               self.pack_cfg.rated_capacity_ah)

        # ---- RUL from capacity-fade extrapolation
        odometer = energy[-1][1]["OdometerKm"] if energy else None
        last_seen = max(t for t, _ in pack) if pack else None
        if soh is not None and energy:
            cycles_now = float(e_cyc[-1])
            km_per_cycle = odometer / cycles_now if cycles_now > 1 else None
            span_s = float(e_ts[-1] - e_ts[0])
            sec_per_cycle = span_s / cycles_now if cycles_now > 1 and span_s > 0 else None
            rul = predict_rul(soh.history, self.pack_cfg.rated_capacity_ah,
                              eol_soh=self.pack_cfg.eol_soh,
                              km_per_cycle=km_per_cycle,
                              seconds_per_cycle=sec_per_cycle)

        # ---- Roll-up status
        has_critical = any(a["severity"] == "critical" for a in alerts)
        has_warning = any(a["severity"] == "warning" for a in alerts)
        soh_pct = soh.soh_pct if soh else None
        if has_critical or (soh_pct is not None and soh_pct < 80.0):
            status = "critical"
        elif has_warning or (soh_pct is not None and soh_pct < 85.0) or (
            anomaly_rate is not None and anomaly_rate > 15.0
        ):
            status = "watch"
        elif pack:
            status = "healthy"
        else:
            status = "unknown"

        def _finite(v):
            return v if v is not None and np.isfinite(v) else None

        self.store.upsert_health(dict(
            vin=vin,
            soh_pct=soh_pct,
            rul_cycles=_finite(rul.rul_cycles) if rul else None,
            rul_km=_finite(rul.rul_km) if rul else None,
            rul_days=_finite(rul.rul_days) if rul else None,
            cycles=float(e_cyc[-1]) if energy else None,
            odometer_km=odometer,
            last_seen=last_seen,
            anomaly_rate_pct=anomaly_rate,
            status=status,
        ))
        log.info("analytics %s: status=%s soh=%s alerts=%d", vin, status, soh_pct, new_alerts)
        return VehicleReport(vin=vin, status=status, soh_pct=soh_pct,
                             rul_cycles=rul.rul_cycles if rul else None,
                             new_alerts=new_alerts, anomaly_rate_pct=anomaly_rate)

    def run_all(self) -> list[VehicleReport]:
        return [self.run_vehicle(vin) for vin in self.store.vins()]
