"""State-of-Health estimation from telemetry.

Primary method: **partial-discharge coulomb counting**. For each drive
segment we integrate current over time and divide by the SOC swing:

    C_est = ∫ I dt / ΔSOC

which yields an estimate of usable capacity from ordinary driving data — no
full reference discharge needed. Segment estimates are noisy, so we take a
median-filtered trend over the vehicle's history.

SOH = C_est / C_rated. The same capacity-vs-cycles history feeds the RUL
extrapolation in :mod:`voltiq.analytics.rul`.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

MIN_SOC_SWING_PCT = 8.0      # ignore segments with tiny SOC change (noise-dominated)
MIN_SEGMENT_SAMPLES = 10
MAX_GAP_S = 300.0            # a sampling gap longer than this splits a segment


@dataclass
class CapacityPoint:
    ts: float
    cycles: float
    capacity_ah: float


@dataclass
class SohEstimate:
    soh_pct: float
    capacity_ah: float
    n_segments: int
    history: list[CapacityPoint]


def _discharge_segments(ts: np.ndarray, current: np.ndarray) -> list[tuple[int, int]]:
    """Contiguous index ranges where the pack is discharging (I > 5 A).

    Segments are also split on sampling gaps (vehicle parked, telemetry
    dropout): integrating current across a gap would badly overestimate
    charge throughput.
    """
    active = current > 5.0
    segments: list[tuple[int, int]] = []
    start: int | None = None

    def close(end: int) -> None:
        nonlocal start
        if start is not None and end - start >= MIN_SEGMENT_SAMPLES:
            segments.append((start, end))
        start = None

    for i, a in enumerate(active):
        if i > 0 and ts[i] - ts[i - 1] > MAX_GAP_S:
            close(i)
        if a and start is None:
            start = i
        elif not a:
            close(i)
    close(len(active))
    return segments


def estimate_soh(
    ts: np.ndarray,
    current: np.ndarray,
    soc_pct: np.ndarray,
    cycles: np.ndarray,
    rated_capacity_ah: float,
) -> SohEstimate | None:
    """Estimate SOH from aligned PackStatus series (positive I = discharge)."""
    points: list[CapacityPoint] = []
    for i0, i1 in _discharge_segments(ts, current):
        d_soc = soc_pct[i0] - soc_pct[i1 - 1]
        if d_soc < MIN_SOC_SWING_PCT:
            continue
        # Trapezoidal ∫I dt over the segment, in amp-hours
        ah = float(np.trapezoid(current[i0:i1], ts[i0:i1]) / 3600.0)
        cap = ah / (d_soc / 100.0)
        if 0.3 * rated_capacity_ah < cap < 1.2 * rated_capacity_ah:  # sanity gate
            points.append(CapacityPoint(
                ts=float(ts[i1 - 1]),
                cycles=float(cycles[min(i1 - 1, len(cycles) - 1)]),
                capacity_ah=cap,
            ))

    if len(points) < 3:
        return None

    # Median filter (k=5) to suppress segment-level noise
    caps = np.array([p.capacity_ah for p in points])
    k = min(5, len(caps) if len(caps) % 2 == 1 else len(caps) - 1)
    smoothed = caps.copy()
    half = k // 2
    for i in range(len(caps)):
        lo, hi = max(0, i - half), min(len(caps), i + half + 1)
        smoothed[i] = np.median(caps[lo:hi])
    for p, c in zip(points, smoothed):
        p.capacity_ah = float(c)

    current_cap = float(np.median(smoothed[-5:]))
    return SohEstimate(
        soh_pct=round(100.0 * current_cap / rated_capacity_ah, 2),
        capacity_ah=round(current_cap, 2),
        n_segments=len(points),
        history=points,
    )
