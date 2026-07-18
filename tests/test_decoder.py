"""DBC codec round-trip tests."""

import pytest

from voltiq.decoder.dbc import DbcCodec


@pytest.fixture(scope="module")
def codec() -> DbcCodec:
    return DbcCodec()


def test_all_messages_present(codec):
    assert set(codec.message_names) == {
        "BMS_PackStatus", "BMS_CellStats", "BMS_Temps", "BMS_Energy",
    }


def test_pack_status_roundtrip(codec):
    signals = {"PackVoltage": 385.2, "PackCurrent": -142.7,
               "StateOfCharge": 76.4, "ChargingState": 1}
    arb_id, data = codec.encode("BMS_PackStatus", signals)
    assert arb_id == 0x155
    assert len(data) == 8
    name, decoded = codec.decode(arb_id, data)
    assert name == "BMS_PackStatus"
    assert decoded["PackVoltage"] == pytest.approx(385.2, abs=0.1)
    assert decoded["PackCurrent"] == pytest.approx(-142.7, abs=0.1)
    assert decoded["StateOfCharge"] == pytest.approx(76.4, abs=0.1)
    assert decoded["ChargingState"] == 1


def test_negative_temperature_roundtrip(codec):
    signals = {"TempMin": -25.0, "TempMax": -20.0, "TempAvg": -22.0, "AmbientTemp": -30.0}
    arb_id, data = codec.encode("BMS_Temps", signals)
    _, decoded = codec.decode(arb_id, data)
    assert decoded["AmbientTemp"] == pytest.approx(-30.0)


def test_unknown_id_returns_none(codec):
    assert codec.decode(0x7FF, b"\x00" * 8) is None


def test_out_of_range_rejected(codec):
    with pytest.raises(Exception):
        codec.encode("BMS_PackStatus", {"PackVoltage": 9999.0, "PackCurrent": 0,
                                        "StateOfCharge": 50, "ChargingState": 0})
