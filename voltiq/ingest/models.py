"""API data contracts (Pydantic v2)."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class CanFrameIn(BaseModel):
    """One raw CAN frame as uploaded by a telematics unit."""

    vin: str = Field(min_length=11, max_length=17)
    timestamp: float = Field(gt=0, description="Unix epoch seconds")
    arbitration_id: int = Field(ge=0, le=0x1FFFFFFF)
    data_hex: str = Field(min_length=2, max_length=16, description="Payload, hex encoded")

    @field_validator("data_hex")
    @classmethod
    def _valid_hex(cls, v: str) -> str:
        bytes.fromhex(v)  # raises ValueError on bad input
        return v.lower()


class FrameBatchIn(BaseModel):
    frames: list[CanFrameIn] = Field(min_length=1, max_length=10_000)


class IngestResult(BaseModel):
    accepted: int
    decoded: int
    unknown_ids: int


class Alert(BaseModel):
    vin: str
    timestamp: float
    severity: str            # "warning" | "critical"
    code: str
    message: str
    value: float


class VehicleHealth(BaseModel):
    vin: str
    soh_pct: float | None
    rul_cycles: float | None
    rul_km: float | None
    rul_days: float | None
    cycles: float | None
    odometer_km: float | None
    last_seen: float | None
    open_alerts: int
    anomaly_rate_pct: float | None
    status: str              # "healthy" | "watch" | "critical" | "unknown"


class FleetSummary(BaseModel):
    vehicles: int
    healthy: int
    watch: int
    critical: int
    frames_stored: int
    fleet: list[VehicleHealth]
