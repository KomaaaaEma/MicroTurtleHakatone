"""Offline protocol checks: no test accesses the real Ujin stand."""
import io
import json
from copy import deepcopy
from urllib.parse import parse_qs, urlparse

import pytest

from app.store import Store
from app.ujin import IntegrationError, UjinAdapter, UjinTransport, from_wire


class Engine:
    def __init__(self, store):
        self.store = store
        self.calls = []
        self.fail_once = False

    def log(self, kind, message, **extra):
        self.store.log(kind, message, **extra)

    def update_state(self, device_id, values, source="emulator"):
        self.calls.append(("state", device_id, values, source))
        device = self.store.get("devices", device_id)
        device["state"].update(values)
        self.store.save("devices", device_id, device)

    def command(self, device_id, signal, value, source="manual"):
        if self.fail_once:
            self.fail_once = False
            raise ValueError("offline")
        self.calls.append(("command", device_id, signal, value, source))
        self.update_state(device_id, {signal: value}, source)


def device():
    return {"id": "light-1", "kind": "light", "name": "Свет", "online": True,
            "capabilities": [{"key": "power", "label": "Питание", "type": "boolean", "role": "command"}],
            "state": {"power": False},
            "ujin": {"serialnumber": "12345", "ext_guid": "team_light_1", "device_class": "integrations_devices",
                     "model": "parkcloud_device", "signal_map": {"power": "integrations_devices_lamp"}}}


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("UJIN_UST", "ust-private-test")
    monkeypatch.setenv("UJIN_APARTMENT_ID", "42")
    monkeypatch.delenv("UJIN_CREDENTIALS", raising=False)
    monkeypatch.delenv("UJIN_BASE_URL", raising=False)
    store = Store(":memory:")
    item = device()
    store.save("devices", item["id"], item)
    engine = Engine(store)
    yield store, engine
    store.close()


def test_json_contract_auth_is_query_and_zero_sentinel():
    requests = []

    def open_request(request, timeout):
        requests.append(request)
        return io.BytesIO(b'{"error":0,"data":{}}')

    client = UjinTransport("https://api-uae-test.ujin.tech/api", "secret", "42", opener=open_request)
    rows = [{"sn": "team_light_1", "s": "integrations_devices_lamp", "v": 0.001, "c": "integrations_devices"}]
    client.publish("team_light_1", rows)
    request = requests[0]
    assert request.method == "POST"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data) == {"data": rows}
    assert parse_qs(urlparse(request.full_url).query) == {"token": ["secret"], "apartment_id": ["42"], "serialnumber": ["team_light_1"]}
    assert b"secret" not in request.data
    client.info("12345")
    assert parse_qs(urlparse(requests[-1].full_url).query)["serialnumber"] == ["12345"]


def test_remote_error_does_not_echo_token():
    client = UjinTransport("https://api-uae-test.ujin.tech", "hidden-token", "42",
                           opener=lambda *args, **kwargs: io.BytesIO(b'{"error":1,"message":"hidden-token"}'))
    with pytest.raises(IntegrationError) as error:
        client.info("123")
    assert "hidden-token" not in str(error.value)


def test_rejects_unapproved_host():
    with pytest.raises(IntegrationError):
        UjinTransport("https://unexpected.example", "token", "42")
    with pytest.raises(IntegrationError):
        UjinTransport("http://api-uae-test.ujin.tech", "token", "42")


class FakeTransport:
    def __init__(self):
        self.posts = []
        self.value = 0.001
        self.task = None
        self.task_dt = "task-1"
        self.empty_readback = False

    def publish(self, ext, rows):
        self.posts.append((ext, rows))
        self.value = rows[0]["v"] if rows else self.value
        return {"error": 0}

    def info(self, serial):
        if self.empty_readback:
            return {"error": 0, "data": {"signals": []}}
        return {"error": 0, "data": {"signals": [{"name": "integrations_devices_lamp", "state": {"value": self.value},
                                                     "task": self.task, "signal_task_dt": self.task_dt}]}}


def adapter(setup, transport=None):
    store, engine = setup
    transport = transport or FakeTransport()
    delays = []
    result = UjinAdapter(store, engine, transport_factory=lambda *args: transport, sleep=delays.append)
    result.configure({"mode": "live"})
    return result, transport, delays


def test_two_phase_publication_no_double_prefix_and_readback(setup):
    integration, transport, delays = adapter(setup)
    result = integration.sync()
    assert [post[1][0]["s"] for post in transport.posts] == ["lamp", "integrations_devices_lamp"]
    assert all(post[1][0]["v"] == 0.001 for post in transport.posts)
    assert delays == [2]
    assert result["published"] == 1 and result["verified"] == 1
    assert integration.summary()["pending"] == 0
    assert "ust-private-test" not in json.dumps(integration.summary())
    integration.sync()
    assert len(transport.posts) == 3  # dictionary init persisted, full publish only


def test_error_zero_without_readback_remains_pending_and_stops_retry(setup):
    integration, transport, _ = adapter(setup)
    transport.empty_readback = True
    result = integration.sync()
    assert result["published"] == 1 and result["verified"] == 0
    assert integration.summary()["pending"] == 1
    for _ in range(4):
        integration.sync(force=False)
    assert integration.summary()["failed"] == 1
    count = len(transport.posts)
    integration.sync(force=False)
    assert len(transport.posts) == count


def test_task_dedup_survives_adapter_restart(setup):
    integration, transport, _ = adapter(setup)
    transport.task = 1
    integration.sync(force=False)
    engine = setup[1]
    assert len([call for call in engine.calls if call[0] == "command"]) == 1
    integration.sync(force=False)
    restarted, _, _ = adapter(setup, transport)
    restarted.sync(force=False)
    assert len([call for call in engine.calls if call[0] == "command"]) == 1
    transport.task_dt = "task-2"
    restarted.sync(force=False)
    assert len([call for call in engine.calls if call[0] == "command"]) == 2


def test_failed_task_is_not_marked_seen(setup):
    integration, transport, _ = adapter(setup)
    transport.task = 1
    setup[1].fail_once = True
    first = integration.sync(force=False)
    assert first["commands"] == 0
    assert integration._seen == {}
    second = integration.sync(force=False)
    assert second["commands"] == 1


def test_dry_run_requires_no_credentials_and_does_not_connect(setup, monkeypatch):
    monkeypatch.delenv("UJIN_UST")
    monkeypatch.delenv("UJIN_APARTMENT_ID")
    store, engine = setup
    integration = UjinAdapter(store, engine, transport_factory=lambda *args: pytest.fail("network must not be called"))
    result = integration.sync()
    assert result["dry_run"] is True
    assert result["integration"]["connected"] is False
    assert result["integration"]["credentials_available"] is False


def test_thermostat_mode_and_zero_decode():
    cap = {"key": "mode", "type": "string"}
    assert from_wire(2, cap) == "cool"
    assert from_wire(0.001, cap) == "off"
    assert from_wire(0.001, {"type": "boolean"}) is False
    assert from_wire(0.001, {"type": "number"}) == 0


def test_binding_replacement_discards_queue_and_old_readback(setup):
    from copy import deepcopy
    integration, transport, _ = adapter(setup)
    store, engine = setup
    old = store.get("devices", "light-1")
    integration.on_state("light-1", {"power": True})
    integration._seen["light-1:integrations_devices_lamp"] = "old-task"
    new = deepcopy(old)
    new["ujin"]["serialnumber"] = "replacement"
    new["ujin"]["ext_guid"] = "replacement_light"
    store.save("devices", "light-1", new)
    integration.on_configuration_change([old], [new])
    assert not integration._outbox
    assert not integration._seen
    transport.task = 1
    result = {"commands": 0, "pulled": 0}
    integration._apply_remote(old, transport.info("old"), result)
    assert engine.calls == []
    assert result == {"commands": 0, "pulled": 0}


def test_config_reconciliation_preserves_seen_for_same_binding(setup):
    from copy import deepcopy
    integration, _, _ = adapter(setup)
    store, _ = setup
    old = store.get("devices", "light-1")
    integration._seen["light-1:integrations_devices_lamp"] = "task-1"
    new = deepcopy(old)
    new["name"] = "Свет в холле"
    integration.on_configuration_change([old], [new])
    assert integration._seen["light-1:integrations_devices_lamp"] == "task-1"


def test_queue_tokens_cannot_match_after_replacement(setup):
    integration, _, _ = adapter(setup)
    integration.on_state("light-1", {"power": True})
    old_item = deepcopy(integration._outbox["light-1"])
    integration._outbox.clear()
    integration.on_state("light-1", {"power": False})
    assert integration._outbox["light-1"]["revision"] == old_item["revision"]
    assert not integration._same_item(integration._outbox["light-1"], old_item)


def test_employee_token_nested_response_keeps_resident_publish_auth():
    requests = []

    def open_request(request, timeout):
        requests.append(request)
        payload = ({"error": 0, "data": {"user": {"token": "employee-private"}}}
                   if "/auth/authenticate/" in request.full_url else {"error": 0, "data": {"signals": []}})
        return io.BytesIO(json.dumps(payload).encode())

    client = UjinTransport("https://api-uae-test.ujin.tech", "resident-private", "42", opener=open_request)
    client.authenticate_employee({"login": "employee", "password": "private-password", "app": "bms"})
    client.info("12345")
    client.publish("team_light_1", [{"sn": "team_light_1", "s": "lamp", "v": 1, "c": "integrations_devices"}])
    assert parse_qs(urlparse(requests[1].full_url).query)["token"] == ["employee-private"]
    assert parse_qs(urlparse(requests[2].full_url).query)["token"] == ["resident-private"]
    assert requests[0].get_header("Content-type") == "application/x-www-form-urlencoded"
    assert "/auth/authenticate/" in requests[0].full_url
