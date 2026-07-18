"""End-to-end API tests: simulate -> ingest over HTTP -> analytics -> query."""

import time

import pytest
from fastapi.testclient import TestClient

from voltiq.api.app import create_app
from voltiq.decoder.dbc import DbcCodec
from voltiq.simulator.vehicle import FaultScenario, VehicleSimulator

VIN_OK = "5YJ3E1EA0PF100001"
VIN_BAD = "5YJ3E1EA0PF100002"


@pytest.fixture(scope="module")
def client(tmp_path_factory) -> TestClient:
    db = tmp_path_factory.mktemp("db") / "test.db"
    app = create_app(str(db))
    with TestClient(app) as c:
        yield c


def _upload(client: TestClient, vin: str, days: int, fault=None) -> None:
    codec = DbcCodec()
    sim = VehicleSimulator(vin, codec, start_time=time.time() - days * 86_400,
                          seed=hash(vin) % 1000, fault=fault)
    frames = [
        {"vin": f.vin, "timestamp": f.timestamp,
         "arbitration_id": f.arbitration_id, "data_hex": f.data.hex()}
        for f in sim.frames(days=days)
    ]
    for i in range(0, len(frames), 5000):
        r = client.post("/api/v1/ingest/frames", json={"frames": frames[i:i + 5000]})
        assert r.status_code == 200
        body = r.json()
        assert body["decoded"] == body["accepted"]


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_ingest_and_analytics_flow(client):
    _upload(client, VIN_OK, days=40)
    _upload(client, VIN_BAD, days=40, fault=FaultScenario("weak_cell", start_day=10))

    r = client.post("/api/v1/analytics/run")
    assert r.status_code == 200
    assert r.json()["vehicles_analyzed"] == 2

    fleet = client.get("/api/v1/fleet").json()
    assert fleet["vehicles"] == 2
    assert fleet["frames_stored"] > 0

    # faulty vehicle must be flagged worse than the healthy one
    by_vin = {v["vin"]: v for v in fleet["fleet"]}
    assert by_vin[VIN_BAD]["status"] in ("watch", "critical")
    assert by_vin[VIN_BAD]["open_alerts"] > 0

    alerts = client.get(f"/api/v1/vehicles/{VIN_BAD}/alerts").json()
    assert any("IMBALANCE" in a["code"] for a in alerts)

    health = client.get(f"/api/v1/vehicles/{VIN_OK}/health").json()
    assert health["soh_pct"] is None or health["soh_pct"] > 80.0


def test_telemetry_endpoint(client):
    rows = client.get(f"/api/v1/vehicles/{VIN_OK}/telemetry",
                      params={"message": "BMS_PackStatus", "limit": 100}).json()
    assert rows
    assert {"ts", "PackVoltage", "PackCurrent", "StateOfCharge"} <= set(rows[0])


def test_unknown_vin_404(client):
    assert client.get("/api/v1/vehicles/NOPE12345678/health").status_code == 404


def test_rejects_bad_frames(client):
    bad = {"frames": [{"vin": "X", "timestamp": -5,
                       "arbitration_id": 999999999999, "data_hex": "zz"}]}
    assert client.post("/api/v1/ingest/frames", json=bad).status_code == 422


def test_unknown_can_ids_counted_not_fatal(client):
    frames = [{"vin": VIN_OK, "timestamp": time.time(),
               "arbitration_id": 0x7DF, "data_hex": "0011223344556677"}]
    body = client.post("/api/v1/ingest/frames", json={"frames": frames}).json()
    assert body["unknown_ids"] == 1
    assert body["decoded"] == 0


def test_dashboard_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "VoltIQ" in r.text
