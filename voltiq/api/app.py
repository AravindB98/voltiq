"""FastAPI application: telemetry ingestion + fleet health API + dashboard."""

from __future__ import annotations

import os
from importlib import resources
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from voltiq import __version__
from voltiq.analytics.engine import AnalyticsEngine
from voltiq.decoder.dbc import DbcCodec
from voltiq.ingest.models import (
    Alert,
    FleetSummary,
    FrameBatchIn,
    IngestResult,
    VehicleHealth,
)
from voltiq.ingest.pipeline import IngestPipeline
from voltiq.ingest.store import Store

DEFAULT_DB = os.environ.get("VOLTIQ_DB", "voltiq.db")


def _health_from_row(row: dict, open_alerts: int) -> VehicleHealth:
    return VehicleHealth(
        vin=row["vin"], soh_pct=row["soh_pct"], rul_cycles=row["rul_cycles"],
        rul_km=row["rul_km"], rul_days=row["rul_days"], cycles=row["cycles"],
        odometer_km=row["odometer_km"], last_seen=row["last_seen"],
        open_alerts=open_alerts, anomaly_rate_pct=row["anomaly_rate_pct"],
        status=row["status"],
    )


def create_app(db_path: str | None = None) -> FastAPI:
    app = FastAPI(
        title="VoltIQ",
        version=__version__,
        description="EV Battery Intelligence Platform: CAN telemetry ingestion, "
                    "anomaly detection, SOH estimation and RUL prediction.",
    )
    store = Store(db_path or DEFAULT_DB)
    pipeline = IngestPipeline(store, DbcCodec())
    engine = AnalyticsEngine(store)
    app.state.store = store

    # ------------------------------------------------------------- ingestion
    @app.post("/api/v1/ingest/frames", response_model=IngestResult, tags=["ingest"])
    def ingest_frames(batch: FrameBatchIn) -> IngestResult:
        frames = [
            (f.vin, f.timestamp, f.arbitration_id, bytes.fromhex(f.data_hex))
            for f in batch.frames
        ]
        stats = pipeline.ingest(frames)
        return IngestResult(accepted=stats.accepted, decoded=stats.decoded,
                            unknown_ids=stats.unknown_ids)

    # ------------------------------------------------------------- analytics
    @app.post("/api/v1/analytics/run", tags=["analytics"])
    def run_analytics() -> dict:
        reports = engine.run_all()
        return {"vehicles_analyzed": len(reports),
                "new_alerts": sum(r.new_alerts for r in reports)}

    # ------------------------------------------------------------------ fleet
    @app.get("/api/v1/fleet", response_model=FleetSummary, tags=["fleet"])
    def fleet() -> FleetSummary:
        rows = store.health_all()
        items = [_health_from_row(r, store.open_alert_count(r["vin"])) for r in rows]
        by = lambda s: sum(1 for i in items if i.status == s)  # noqa: E731
        return FleetSummary(vehicles=len(items), healthy=by("healthy"),
                            watch=by("watch"), critical=by("critical"),
                            frames_stored=store.frame_count(), fleet=items)

    @app.get("/api/v1/vehicles/{vin}/health", response_model=VehicleHealth, tags=["fleet"])
    def vehicle_health(vin: str) -> VehicleHealth:
        row = store.health_for(vin)
        if row is None:
            raise HTTPException(404, f"unknown VIN {vin!r} (run analytics first)")
        return _health_from_row(row, store.open_alert_count(vin))

    @app.get("/api/v1/vehicles/{vin}/alerts", response_model=list[Alert], tags=["fleet"])
    def vehicle_alerts(vin: str, limit: int = 100) -> list[Alert]:
        return [Alert(**a) for a in store.alerts_for(vin, limit=limit)]

    @app.get("/api/v1/vehicles/{vin}/telemetry", tags=["fleet"])
    def vehicle_telemetry(vin: str, message: str = "BMS_PackStatus",
                          since: float = 0.0, limit: int = 2000) -> list[dict]:
        series = store.signal_series(vin, message, since=since)
        stride = max(1, len(series) // limit)
        return [{"ts": ts, **sig} for ts, sig in series[::stride]]

    # -------------------------------------------------------------- misc/UI
    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict:
        return {"status": "ok", "version": __version__,
                "frames_stored": store.frame_count()}

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard() -> str:
        html = Path(str(resources.files("voltiq").joinpath("dashboard", "index.html")))
        return html.read_text(encoding="utf-8")

    return app
