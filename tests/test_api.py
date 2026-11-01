from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "api.sqlite", start_workers=False)
    with TestClient(app) as client:
        assert client.post("/api/auth/setup", json={"username": "admin", "password": "Test-admin-strong-password"}).status_code == 201
        yield client


def test_bootstrap_and_demo(client):
    data = client.get("/api/bootstrap").json()
    assert len(data["devices"]) == 17
    assert len(data["rules"]) == 3
    result = client.post("/api/demo/leak").json()
    assert result["runs"][0]["status"] == "success"
    assert len(client.get("/api/notifications").json()) == 2
    assert client.get("/api/openapi.json").status_code == 200


def test_bad_import_does_not_replace_good_configuration(client):
    before = client.get("/api/export").json()
    broken = deepcopy(before)
    broken["devices"] = [d for d in broken["devices"] if d["id"] != "valve-1"]
    response = client.post("/api/import", json=broken)
    assert response.status_code == 422
    assert client.get("/api/export").json() == before


def test_import_duplicate_ids_rejected(client):
    config = client.get("/api/export").json()
    config["devices"].append(config["devices"][0])
    assert client.post("/api/import", json=config).status_code == 422


def test_crud_declarative_custom_device(client):
    device = {"id": "custom-42", "name": "CO₂", "kind": "custom", "room": "Офис", "capabilities": [{"key": "co2", "label": "CO₂", "type": "number", "role": "measurement", "min": 0, "max": 10000, "default": 400}], "state": {"co2": 500}}
    assert client.post("/api/devices", json=device).status_code == 201
    assert client.post("/api/devices", json=device).status_code == 409
    saved = client.get("/api/devices/custom-42").json()
    saved["name"] = "CO₂ в переговорной"
    assert client.put("/api/devices/custom-42", json=saved).status_code == 200
    assert client.post("/api/devices/custom-42/state", json={"values": {"co2": -1}}).status_code == 422
    assert client.delete("/api/devices/custom-42").status_code == 200
    assert client.get("/api/devices/custom-42").status_code == 404


def test_offline_http_error_and_recovery(client):
    assert client.post("/api/devices/valve-1/online", json={"online": False}).status_code == 200
    assert client.post("/api/devices/valve-1/command", json={"signal": "closed", "value": True}).status_code == 409
    assert client.post("/api/devices/valve-1/online", json={"online": True}).status_code == 200
    assert client.post("/api/devices/valve-1/command", json={"signal": "closed", "value": True}).status_code == 200


def test_referenced_device_cannot_be_deleted(client):
    assert client.delete("/api/devices/valve-1").status_code == 422
    assert client.delete("/api/rules/rule-leak").status_code == 200
    assert client.delete("/api/devices/valve-1").status_code == 200


def test_configuration_roundtrip(client):
    config = client.get("/api/export").json()
    config["devices"][0]["name"] = "Датчик под ванной"
    assert client.post("/api/import", json=config).status_code == 200
    assert client.get("/api/export").json() == config


def test_read_notification_and_emulation(client):
    client.post("/api/demo/fire")
    client.post("/api/notifications/read")
    assert all(n["read"] for n in client.get("/api/notifications").json())
    assert client.post("/api/emulation", json={"enabled": True}).json() == {"enabled": True}
    assert client.get("/api/bootstrap").json()["emulation"]["enabled"] is True


def test_foreign_origin_cannot_trigger_local_automation(client):
    response = client.post("/api/demo/leak", headers={"Origin": "https://untrusted.example"})
    assert response.status_code == 403
    assert client.get("/api/devices/leak-1").json()["state"]["leak"] is False
    assert client.post("/api/demo/leak", headers={"Origin": "http://testserver"}).status_code == 200


def test_import_invalidates_stale_integration_outbox(client):
    app = client.app
    device = app.state.engine.device("leak-1")
    device["ujin"]["serialnumber"] = "12345"
    device["ujin"]["ext_guid"] = "stand_sensor"
    assert client.put("/api/devices/leak-1", json=device).status_code == 200
    client.post("/api/devices/leak-1/state", json={"values": {"leak": True}})
    assert app.state.integration.summary()["pending"] == 1
    config = client.get("/api/export").json()
    config["devices"][0]["ujin"]["ext_guid"] = "replacement_sensor"
    assert client.post("/api/import", json=config).status_code == 200
    assert app.state.integration.summary()["pending"] == 0


def test_reset_replaces_queued_alert_with_default_state(client):
    app = client.app
    device = app.state.engine.device("leak-1")
    device["ujin"].update(serialnumber="12345", ext_guid="stand_sensor")
    client.put("/api/devices/leak-1", json=device)
    client.post("/api/devices/leak-1/state", json={"values": {"leak": True}})
    assert app.state.integration._outbox["leak-1"]["values"]["leak"] is True
    assert client.post("/api/demo/reset").status_code == 200
    assert app.state.integration._outbox["leak-1"]["values"]["leak"] is False


def test_imported_observation_time_is_not_a_new_device_receipt(client):
    client.post("/api/devices/light-1/command", json={"signal": "power", "value": False})
    config = client.get("/api/export").json()
    light = next(d for d in config["devices"] if d["id"] == "light-1")
    assert light["last_seen"]
    assert client.post("/api/import", json=config).status_code == 200
    restored = client.get("/api/devices/light-1").json()
    assert restored["last_seen"] is None and restored["last_seen_source"] is None


def test_chunked_body_cannot_bypass_request_size_limit(client):
    payload = (chunk for chunk in (b'{"title":"', b'x' * 2_000_001, b'"}'))
    response = client.post("/api/tickets", content=payload, headers={"Content-Type": "application/json"})
    assert response.status_code == 413
