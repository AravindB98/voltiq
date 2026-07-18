"""Battery physics model tests."""

import pytest

from voltiq.simulator.battery import BatteryPack, ocv_from_soc


def test_ocv_monotonic():
    socs = [i / 100 for i in range(101)]
    volts = [ocv_from_soc(s) for s in socs]
    assert all(b >= a for a, b in zip(volts, volts[1:]))
    assert volts[0] == pytest.approx(3.0)
    assert volts[-1] == pytest.approx(4.2)


def test_discharge_reduces_soc_and_voltage():
    pack = BatteryPack()
    v0 = pack.measure(0.0)["pack_voltage"]
    for _ in range(360):  # 1 h at 100 A
        pack.step(current_a=100.0, dt_s=10.0)
    m = pack.measure(100.0)
    assert m["soc_pct"] < 90.0
    assert m["pack_voltage"] < v0


def test_charge_increases_soc():
    pack = BatteryPack()
    pack.state.soc = 0.3
    for _ in range(360):
        pack.step(current_a=-150.0, dt_s=10.0)
    assert pack.state.soc > 0.3


def test_soc_bounded():
    pack = BatteryPack()
    for _ in range(10_000):
        pack.step(current_a=500.0, dt_s=60.0)
    assert pack.state.soc == 0.0
    for _ in range(10_000):
        pack.step(current_a=-500.0, dt_s=60.0)
    assert pack.state.soc == 1.0


def test_capacity_fades_with_cycling():
    pack = BatteryPack()
    fresh = pack.capacity_ah
    pack.state.ah_throughput = 2.0 * pack.cfg.rated_capacity_ah * 400  # 400 EFC
    pack.state.equivalent_full_cycles = 400
    assert pack.capacity_ah < fresh
    assert 0.5 < pack.soh < 1.0


def test_resistance_grows_with_age_and_cold():
    pack = BatteryPack()
    fresh_r = pack.resistance_ohm
    pack.state.temp_c = -10.0
    assert pack.resistance_ohm > fresh_r
    pack.state.temp_c = 25.0
    pack.state.equivalent_full_cycles = 500
    pack.state.ah_throughput = 2 * 500 * pack.cfg.rated_capacity_ah
    assert pack.resistance_ohm > fresh_r


def test_joule_heating_raises_temperature():
    pack = BatteryPack()
    t0 = pack.state.temp_c
    for _ in range(60):
        pack.step(current_a=300.0, dt_s=10.0, ambient_c=25.0)
    assert pack.state.temp_c > t0


def test_weak_cell_widens_spread():
    pack = BatteryPack()
    healthy = pack.measure(50.0)
    pack.inject_weak_cell(150.0)
    faulty = pack.measure(50.0)
    healthy_spread = healthy["cell_mv_max"] - healthy["cell_mv_min"]
    faulty_spread = faulty["cell_mv_max"] - faulty["cell_mv_min"]
    assert faulty_spread > healthy_spread + 100.0
