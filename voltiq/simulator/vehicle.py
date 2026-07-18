"""Vehicle-level simulation: drive cycles, charging, and CAN frame emission.

Each simulated day the vehicle runs a commute-style profile (drive, rest,
drive, then AC or DC charge back to target SOC).  During activity the BMS
broadcasts the four VoltIQ CAN messages, encoded through the same DBC file
the decoder uses — so the whole pipeline round-trips real 8-byte CAN payloads.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Iterator

from voltiq.decoder.dbc import DbcCodec
from voltiq.simulator.battery import BatteryPack, PackConfig

SECONDS_PER_DAY = 86_400


@dataclass
class RawFrame:
    """A raw CAN frame with capture metadata (what a telematics unit uploads)."""

    vin: str
    timestamp: float          # unix epoch seconds
    arbitration_id: int
    data: bytes


@dataclass
class FaultScenario:
    """Fault injected part-way through the simulation."""

    kind: str                 # "weak_cell" | "cooling_degraded"
    start_day: int
    severity: float = 1.0


class VehicleSimulator:
    """Simulates one vehicle's battery telemetry over many days."""

    def __init__(
        self,
        vin: str,
        codec: DbcCodec,
        start_time: float,
        daily_km: float = 55.0,
        seed: int = 0,
        fault: FaultScenario | None = None,
        pack_config: PackConfig | None = None,
    ) -> None:
        self.vin = vin
        self.codec = codec
        self.start_time = start_time
        self.daily_km = daily_km
        self.fault = fault
        self.rng = random.Random(seed)
        self.pack = BatteryPack(pack_config, seed=seed)
        self.odometer_km = 0.0
        self._cooling_nominal = self.pack.cfg.cooling_w_per_k

    # ------------------------------------------------------------ drive plan
    def _day_plan(self, day: int) -> list[tuple[str, float]]:
        """Return (phase, duration_s) segments for one day."""
        drive_s = self.daily_km / 60.0 * 3600.0  # assume 60 km/h average
        charge_s = 2.5 * 3600.0
        return [
            ("drive", drive_s * 0.5),
            ("rest", 600.0),
            ("drive", drive_s * 0.5),
            ("charge", charge_s),
        ]

    def _phase_current(self, phase: str, t_in_phase: float) -> float:
        """Current draw (A) for a phase; positive = discharge."""
        if phase == "drive":
            base = 90.0 + 45.0 * math.sin(t_in_phase / 47.0)  # rolling load
            spikes = 130.0 if self.rng.random() < 0.05 else 0.0  # acceleration
            return base + spikes
        if phase == "charge":
            soc = self.pack.state.soc
            if soc >= 0.9:
                return 0.0
            return -160.0 if soc < 0.8 else -60.0  # CC then taper
        return 0.0

    def _apply_fault(self, day: int) -> None:
        if self.fault is None or day < self.fault.start_day:
            return
        ramp_days = max(1, day - self.fault.start_day + 1)
        if self.fault.kind == "weak_cell":
            # Weak cell sags progressively further below the pack average.
            self.pack.inject_weak_cell(min(250.0, 4.0 * ramp_days * self.fault.severity))
        elif self.fault.kind == "cooling_degraded":
            factor = max(0.25, 1.0 - 0.02 * ramp_days * self.fault.severity)
            self.pack.cfg.cooling_w_per_k = self._cooling_nominal * factor

    # ------------------------------------------------------------- main loop
    def frames(self, days: int, emit_every_s: float = 60.0, dt_s: float = 10.0) -> Iterator[RawFrame]:
        """Yield encoded CAN frames for ``days`` of vehicle life."""
        for day in range(days):
            self._apply_fault(day)
            ambient = 18.0 + 10.0 * math.sin(2 * math.pi * day / 365.0) + self.rng.gauss(0, 2)
            t = self.start_time + day * SECONDS_PER_DAY + 7 * 3600.0  # day starts 07:00
            since_emit = emit_every_s  # emit immediately at day start

            for phase, duration in self._day_plan(day):
                if phase == "rest":
                    t += duration
                    continue
                steps = int(duration / dt_s)
                for _ in range(steps):
                    current = self._phase_current(phase, t % 1000.0)
                    self.pack.step(current, dt_s, ambient_c=ambient)
                    if phase == "drive":
                        self.odometer_km += 60.0 * dt_s / 3600.0
                    t += dt_s
                    since_emit += dt_s
                    if since_emit >= emit_every_s:
                        since_emit = 0.0
                        yield from self._emit(t, current, ambient, phase)

    # ---------------------------------------------------------------- encode
    def _emit(self, t: float, current: float, ambient: float, phase: str) -> Iterator[RawFrame]:
        m = self.pack.measure(current)
        payloads = {
            "BMS_PackStatus": {
                "PackVoltage": max(0.0, m["pack_voltage"]),
                "PackCurrent": m["pack_current"],
                "StateOfCharge": m["soc_pct"],
                "ChargingState": 1 if phase == "charge" else 0,
            },
            "BMS_CellStats": {
                "CellVoltMin": max(0.0, m["cell_mv_min"]),
                "CellVoltMax": max(0.0, m["cell_mv_max"]),
                "CellVoltAvg": max(0.0, m["cell_mv_avg"]),
                "WeakCellIndex": 42 if self.pack.state.weak_cell_offset_mv > 0 else 0,
            },
            "BMS_Temps": {
                "TempMin": m["temp_c"] - 1.5,
                "TempMax": m["temp_c"] + 1.5,
                "TempAvg": m["temp_c"],
                "AmbientTemp": ambient,
            },
            "BMS_Energy": {
                "OdometerKm": self.odometer_km,
                "CycleCount": min(6553.5, m["cycles"]),
                "EstCapacity": m["capacity_ah"],
            },
        }
        for name, signals in payloads.items():
            arb_id, data = self.codec.encode(name, signals)
            yield RawFrame(vin=self.vin, timestamp=t, arbitration_id=arb_id, data=data)
