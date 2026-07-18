"""Remaining Useful Life prediction.

Model: the empirical square-root capacity-fade law for lithium-ion cells,

    C(n) = C0 · (1 − k·√n)        n = equivalent full cycles

fitted by least squares to the vehicle's own capacity history (produced by
:mod:`voltiq.analytics.soh`). We then solve for the cycle count where capacity
crosses the end-of-life threshold (default 80 % of rated) and convert the
remaining cycles into km and calendar days using the vehicle's observed usage
rate. Fit residuals give a 1-sigma uncertainty band on the EOL crossing.

The sqrt-law linearises as y = (1 − C/C0) = k·√n, so the fit is a one-
parameter linear regression through the origin — robust even with few points.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from voltiq.analytics.soh import CapacityPoint


@dataclass
class RulEstimate:
    rul_cycles: float
    rul_km: float | None
    rul_days: float | None
    eol_cycles: float
    fade_k: float
    sigma_cycles: float          # 1-sigma uncertainty on the EOL crossing


def predict_rul(
    history: list[CapacityPoint],
    rated_capacity_ah: float,
    eol_soh: float = 0.80,
    km_per_cycle: float | None = None,
    seconds_per_cycle: float | None = None,
) -> RulEstimate | None:
    if len(history) < 5:
        return None

    n = np.array([max(p.cycles, 1e-6) for p in history])
    c = np.array([p.capacity_ah for p in history])
    y = 1.0 - c / rated_capacity_ah          # observed fade fraction
    x = np.sqrt(n)

    # Least-squares slope through the origin: k = Σxy / Σx²
    denom = float(np.dot(x, x))
    if denom <= 0:
        return None
    k = float(np.dot(x, y) / denom)
    if k <= 1e-6:                            # no measurable fade yet
        return RulEstimate(
            rul_cycles=float("inf"), rul_km=None, rul_days=None,
            eol_cycles=float("inf"), fade_k=k, sigma_cycles=float("inf"),
        )

    # EOL where fade reaches (1 − eol_soh):  √n_eol = (1 − eol_soh)/k
    eol_cycles = ((1.0 - eol_soh) / k) ** 2
    now_cycles = float(n[-1])
    rul_cycles = max(0.0, eol_cycles - now_cycles)

    # Uncertainty: propagate residual std of the fade fit through the inverse
    resid = y - k * x
    k_sigma = float(np.std(resid) / np.sqrt(denom)) if len(resid) > 2 else 0.0
    if k_sigma > 0:
        lo = ((1.0 - eol_soh) / (k + k_sigma)) ** 2
        hi = ((1.0 - eol_soh) / max(k - k_sigma, 1e-9)) ** 2
        sigma_cycles = (hi - lo) / 2.0
    else:
        sigma_cycles = 0.0

    return RulEstimate(
        rul_cycles=round(rul_cycles, 1),
        rul_km=round(rul_cycles * km_per_cycle, 0) if km_per_cycle else None,
        rul_days=round(rul_cycles * seconds_per_cycle / 86_400.0, 1)
        if seconds_per_cycle else None,
        eol_cycles=round(eol_cycles, 1),
        fade_k=round(k, 6),
        sigma_cycles=round(sigma_cycles, 1),
    )
