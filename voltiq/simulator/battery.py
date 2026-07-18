"""Physics-based lithium-ion battery pack model.

The model captures the behaviours a BMS actually observes:

* **Open-circuit voltage (OCV)** as a function of state of charge, using a
  piecewise-linear fit of a typical NMC cell curve.
* **Ohmic voltage drop** through an internal resistance that grows with age
  and rises sharply at low temperature.
* **Lumped thermal model**: Joule heating balanced against convective cooling.
* **Capacity fade** following the square-root-of-throughput law reported for
  NMC/graphite cells (e.g. Wang et al., J. Power Sources 2011): fade is
  proportional to sqrt(equivalent full cycles).
* **Cell imbalance**: per-cell voltage spread that widens with age, plus an
  optional injected weak cell for fault-scenario testing.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

# Typical NMC cell OCV curve: (SOC fraction, cell volts)
_OCV_POINTS: list[tuple[float, float]] = [
    (0.00, 3.00),
    (0.05, 3.35),
    (0.10, 3.45),
    (0.20, 3.55),
    (0.40, 3.65),
    (0.60, 3.80),
    (0.80, 3.98),
    (0.90, 4.08),
    (1.00, 4.20),
]


def ocv_from_soc(soc: float) -> float:
    """Interpolate open-circuit cell voltage from SOC (0..1)."""
    soc = min(1.0, max(0.0, soc))
    for (s0, v0), (s1, v1) in zip(_OCV_POINTS, _OCV_POINTS[1:]):
        if soc <= s1:
            frac = (soc - s0) / (s1 - s0)
            return v0 + frac * (v1 - v0)
    return _OCV_POINTS[-1][1]


@dataclass
class PackConfig:
    """Electrical and thermal parameters of the simulated pack."""

    series_cells: int = 96
    rated_capacity_ah: float = 230.0          # pack-level (parallel group) capacity
    r_internal_ohm: float = 0.045             # fresh pack resistance at 25 degC
    thermal_mass_j_per_k: float = 250_000.0   # lumped heat capacity of the pack
    cooling_w_per_k: float = 350.0            # convective cooling coefficient
    fade_k: float = 0.006                     # capacity fade per sqrt(EFC)
    eol_soh: float = 0.80                     # end-of-life threshold (80 % SOH)


@dataclass
class PackState:
    soc: float = 0.9                 # 0..1
    temp_c: float = 25.0
    equivalent_full_cycles: float = 0.0
    ah_throughput: float = 0.0
    weak_cell_offset_mv: float = 0.0  # injected fault: extra sag on one cell
    imbalance_mv: float = 8.0         # healthy pack spread
    rng: random.Random = field(default_factory=lambda: random.Random(0))


class BatteryPack:
    """Steppable battery pack simulation.

    Sign convention: positive current = discharge, negative = charge.
    """

    def __init__(self, config: PackConfig | None = None, seed: int = 0) -> None:
        self.cfg = config or PackConfig()
        self.state = PackState(rng=random.Random(seed))

    # ------------------------------------------------------------------ aging
    @property
    def soh(self) -> float:
        """State of health = current capacity / rated capacity."""
        fade = self.cfg.fade_k * math.sqrt(max(self.state.equivalent_full_cycles, 0.0))
        return max(0.5, 1.0 - fade)

    @property
    def capacity_ah(self) -> float:
        return self.cfg.rated_capacity_ah * self.soh

    @property
    def resistance_ohm(self) -> float:
        """Internal resistance: grows ~linearly with fade, doubles at -10 degC."""
        aging_factor = 1.0 + 2.0 * (1.0 - self.soh)
        t = self.state.temp_c
        cold_factor = 1.0 + max(0.0, (10.0 - t)) * 0.05
        return self.cfg.r_internal_ohm * aging_factor * cold_factor

    # ------------------------------------------------------------------ step
    def step(self, current_a: float, dt_s: float, ambient_c: float = 25.0) -> None:
        """Advance the pack by ``dt_s`` seconds at ``current_a`` amps."""
        st, cfg = self.state, self.cfg

        # Coulomb counting
        dah = current_a * dt_s / 3600.0
        st.soc = min(1.0, max(0.0, st.soc - dah / self.capacity_ah))
        st.ah_throughput += abs(dah)
        st.equivalent_full_cycles = st.ah_throughput / (2.0 * cfg.rated_capacity_ah)

        # Lumped thermal model
        heat_w = current_a * current_a * self.resistance_ohm
        cool_w = cfg.cooling_w_per_k * (st.temp_c - ambient_c)
        st.temp_c += (heat_w - cool_w) * dt_s / cfg.thermal_mass_j_per_k

        # Imbalance drifts up slowly with age
        st.imbalance_mv = 8.0 + 60.0 * (1.0 - self.soh)

    # ------------------------------------------------------------ measurement
    def measure(self, current_a: float) -> dict[str, float]:
        """Return sensor readings a BMS would report right now."""
        st, cfg = self.state, self.cfg
        cell_ocv = ocv_from_soc(st.soc)
        drop_per_cell = current_a * self.resistance_ohm / cfg.series_cells
        cell_v = cell_ocv - drop_per_cell

        noise = st.rng.gauss(0.0, 1.5)  # mV measurement noise
        avg_mv = cell_v * 1000.0 + noise
        half_spread = st.imbalance_mv / 2.0
        min_mv = avg_mv - half_spread - st.weak_cell_offset_mv
        max_mv = avg_mv + half_spread

        return {
            "pack_voltage": cell_v * cfg.series_cells,
            "pack_current": current_a,
            "soc_pct": st.soc * 100.0,
            "cell_mv_min": min_mv,
            "cell_mv_max": max_mv,
            "cell_mv_avg": avg_mv,
            "temp_c": st.temp_c,
            "soh": self.soh,
            "capacity_ah": self.capacity_ah,
            "cycles": st.equivalent_full_cycles,
        }

    # ------------------------------------------------------------------ fault
    def inject_weak_cell(self, offset_mv: float) -> None:
        """Simulate a degrading cell: its voltage sags ``offset_mv`` below avg."""
        self.state.weak_cell_offset_mv = offset_mv
