from copy import deepcopy

import pytest

from app.engine import DeviceOffline, Engine
from app.models import Device
from app.seed import seed_devices, seed_rules
from app.store import Store


@pytest.fixture
def engine(tmp_path):
    store = Store(tmp_path / "test.sqlite")
    engine = Engine(store)
    for d in seed_devices():
        item = Device.model_validate(d).model_dump()
        store.save("devices", item["id"], item)
    for r in seed_rules():
        item = engine.validate_rule(r)
        store.save("rules", item["id"], item)
    yield engine
    store.close()


def test_leak_closes_valve_and_notifies_two_roles(engine):
    result = engine.update_state("leak-1", {"leak": True})
    assert engine.device("valve-1")["state"]["closed"] is True
    assert len(result["runs"]) == 1
    assert result["runs"][0]["status"] == "success"
    assert {n["target"] for n in engine.store.list("notifications")} == {"resident", "concierge"}
    assert len({e["trace_id"] for e in engine.store.list("events") if e.get("kind") == "state"}) == 1


def test_duplicate_external_event_survives_process_restart(engine):
    first = engine.update_state("leak-1", {"leak": True}, event_id="event-42")
    engine.reset_states()
    restarted = Engine(engine.store)
    second = restarted.update_state("leak-1", {"leak": True}, event_id="event-42")
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert len(engine.store.list("runs")) == 1
    assert engine.device("leak-1")["state"]["leak"] is False


def test_unchanged_state_does_not_trigger(engine):
    engine.update_state("leak-1", {"leak": False})
    assert not engine.store.list("runs")


def test_disabled_rule_not_executed(engine):
    rule = engine.store.get("rules", "rule-leak")
    rule["enabled"] = False
    engine.save_rule(rule)
    engine.update_state("leak-1", {"leak": True})
    assert not engine.store.list("runs")
    assert engine.device("valve-1")["state"]["closed"] is False


def test_conditions_all_any_and_offline_inputs(engine):
    rule = engine.store.get("rules", "rule-leak")
    rule["conditions"] = [dict(device_id="thermostat-1", signal="temperature", operator="gt", value=25), dict(device_id="light-1", signal="power", operator="eq", value=True)]
    rule["cooldown_seconds"] = 0
    engine.save_rule(rule)
    engine.update_state("leak-1", {"leak": True})
    assert engine.store.list("runs")[0]["status"] == "skipped"
    rule["condition_mode"] = "any"
    engine.save_rule(rule)
    engine.update_state("leak-1", {"leak": False})
    engine.update_state("leak-1", {"leak": True})
    assert engine.store.list("runs")[0]["status"] == "success"


def test_offline_action_continues_notifications_and_retries(engine):
    device = engine.device("valve-1")
    device["online"] = False
    engine.store.save("devices", device["id"], device)
    result = engine.update_state("leak-1", {"leak": True})
    run = result["runs"][0]
    assert run["status"] == "error"
    assert run["steps"][0]["attempts"] == 2
    assert len(engine.store.list("notifications")) == 2
    assert "недоступно" in run["steps"][0]["message"]


def test_stop_policy_does_not_send_following_actions(engine):
    rule = engine.store.get("rules", "rule-leak")
    rule["on_error"] = "stop"
    engine.save_rule(rule)
    engine.store.update_device("valve-1", {"online": False})
    result = engine.update_state("leak-1", {"leak": True})
    assert result["runs"][0]["status"] == "error"
    assert len(result["runs"][0]["steps"]) == 1
    assert not engine.store.list("notifications")


def test_invalid_batch_is_atomic(engine):
    with pytest.raises(ValueError):
        engine.update_state("thermostat-1", {"temperature": 28, "humidity": 105})
    assert engine.device("thermostat-1")["state"]["temperature"] == 23.4
    assert not engine.store.list("events")


@pytest.mark.parametrize("value", ["false", 0, 1, None])
def test_boolean_is_not_truthiness_coerced(engine, value):
    with pytest.raises(ValueError):
        engine.update_state("leak-1", {"leak": value})


def test_read_only_command_and_nan_rejected(engine):
    with pytest.raises(ValueError):
        engine.command("thermostat-1", "temperature", 25)
    with pytest.raises(ValueError):
        engine.update_state("thermostat-1", {"temperature": float("nan")})


def test_device_reference_and_capability_changes_protected(engine):
    with pytest.raises(ValueError):
        engine.delete_device("valve-1")
    device = engine.device("valve-1")
    device["capabilities"][0]["role"] = "measurement"
    with pytest.raises(ValueError):
        engine.save_device(device)
    assert engine.device("valve-1")["capabilities"][0]["role"] == "command"


def test_cooldown_prevents_repeat_trigger(engine):
    engine.update_state("leak-1", {"leak": True})
    engine.update_state("leak-1", {"leak": False})
    engine.update_state("leak-1", {"leak": True})
    assert len(engine.store.list("runs")) == 1


def test_cascading_rules_execute_once_per_trace(engine):
    common = deepcopy(engine.store.get("rules", "rule-leak"))
    common.update(id="cascade", cooldown_seconds=0, trigger=dict(device_id="valve-1", signal="closed", value=True, operator="eq"), actions=[dict(type="command", device_id="light-1", signal="power", value=False)])
    engine.save_rule(common)
    engine.update_state("leak-1", {"leak": True})
    assert engine.device("light-1")["state"]["power"] is False
    assert {r["rule_id"] for r in engine.store.list("runs")} == {"rule-leak", "cascade"}


def test_manual_run_checks_conditions_but_bypasses_trigger(engine):
    run = engine.run_rule("rule-leak")
    assert run["manual"] is True
    assert engine.device("leak-1")["state"]["leak"] is False
    assert engine.device("valve-1")["state"]["closed"] is True


def test_barrier_obstacle_safety(engine):
    engine.update_state("barrier-1", {"obstacle": True, "open": True})
    with pytest.raises(ValueError, match="препятствие"):
        engine.command("barrier-1", "open", False)
    assert engine.device("barrier-1")["state"]["open"] is True


def test_physics_and_persistence(engine, tmp_path):
    engine.command("thermostat-1", "mode", "cool")
    engine.tick(2)
    assert engine.device("thermostat-1")["state"]["temperature"] < 23.4
    path = tmp_path / "persist.sqlite"
    store = Store(path)
    store.save("devices", "x", {"id": "x", "state": {"value": 7}})
    store.close()
    reopened = Store(path)
    assert reopened.get("devices", "x")["state"]["value"] == 7
    reopened.close()


def test_water_meter_stops_when_valve_closes_and_resumes(engine):
    engine.command("valve-1", "closed", True)
    before = engine.device("meter-1")["state"]["total"]
    engine.tick(60)
    state = engine.device("meter-1")["state"]
    assert state["total"] == before
    assert state["flow"] == 0
    engine.command("valve-1", "closed", False)
    engine.tick(60)
    resumed = engine.device("meter-1")["state"]
    assert resumed["flow"] > 0
    assert resumed["total"] > before
