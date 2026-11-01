from copy import deepcopy
import time

import pytest

from app.engine import Engine
from app.models import Device
from app.seed import seed_devices, seed_rules
from app.store import Store


@pytest.fixture
def engine(tmp_path):
    store = Store(tmp_path / "delays.sqlite")
    clock = [time.time()]
    engine = Engine(store, clock=lambda: clock[0])
    engine.test_clock = clock
    for item in seed_devices():
        saved = Device.model_validate(item).model_dump()
        store.save("devices", saved["id"], saved)
    for item in seed_rules():
        saved = engine.validate_rule(item)
        store.save("rules", saved["id"], saved)
    yield engine
    store.close()


def delayed_rule(engine, actions=None):
    rule = engine.store.get("rules", "rule-leak")
    rule["actions"] = actions or [
        {"type": "delay", "seconds": 10},
        {"type": "command", "device_id": "valve-1", "signal": "closed", "value": True},
        {"type": "notification", "target": "resident", "message": "Завершено после задержки"},
    ]
    return engine.save_rule(rule)


def test_delayed_rule_returns_immediately_and_resumes_when_due(engine, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: pytest.fail("Delay must not sleep"))
    rule = delayed_rule(engine)
    run = engine.run_rule(rule["id"])
    assert run["status"] == "waiting"
    assert engine.device("valve-1")["state"]["closed"] is False
    assert not engine.process_due_jobs()
    engine.test_clock[0] += 10
    finished = engine.process_due_jobs()
    assert len(finished) == 1 and finished[0]["status"] == "success"
    assert engine.device("valve-1")["state"]["closed"] is True
    assert len(engine.store.list("notifications")) == 1
    assert not engine.process_due_jobs()


def test_delay_continuation_survives_new_engine_instance(engine):
    rule = delayed_rule(engine)
    original = engine.run_rule(rule["id"])
    engine.test_clock[0] += 11
    restarted = Engine(engine.store, clock=lambda: engine.test_clock[0])
    finished = restarted.process_due_jobs()
    assert finished[0]["id"] == original["id"]
    assert finished[0]["status"] == "success"
    assert engine.store.list("scenario_jobs")[0]["state"] == "done"


def test_two_delays_do_not_repeat_completed_commands_or_notifications(engine):
    rule = delayed_rule(engine, [
        {"type": "notification", "target": "resident", "message": "Первое"},
        {"type": "delay", "seconds": 3},
        {"type": "notification", "target": "resident", "message": "Второе"},
        {"type": "delay", "seconds": 4},
        {"type": "notification", "target": "resident", "message": "Третье"},
    ])
    run = engine.run_rule(rule["id"])
    engine.test_clock[0] += 3
    middle = engine.process_due_jobs()[0]
    assert middle["status"] == "waiting"
    assert len(engine.store.list("notifications")) == 2
    # Recovered old claim sees the second delay and must leave its own job alone.
    old_job = engine.store.list("scenario_jobs")[0]
    old_job["state"] = "running"
    engine.store.save("scenario_jobs", old_job["id"], old_job)
    engine.process_due_jobs()
    assert len(engine.store.list("notifications")) == 2
    assert engine.store.get("runs", run["id"])["status"] == "waiting"
    engine.test_clock[0] += 4
    engine.process_due_jobs()
    assert engine.store.get("runs", run["id"])["status"] == "success"
    assert len(engine.store.list("notifications")) == 3


def test_recovered_notification_uses_stable_id_not_duplicate(engine):
    rule = delayed_rule(engine)
    run = engine.run_rule(rule["id"])
    engine.store.append("notifications", {"id": f"{run['id']}:2", "message": "Before crash", "target": "resident"})
    engine.test_clock[0] += 10
    engine.process_due_jobs()
    assert len(engine.store.list("notifications")) == 1


def test_disabled_automatic_run_is_cancelled_but_explicit_manual_run_allowed(engine):
    rule = delayed_rule(engine)
    automatic = engine.update_state("leak-1", {"leak": True})["runs"][0]
    rule["enabled"] = False
    engine.save_rule(rule)
    assert engine.store.get("runs", automatic["id"])["status"] == "cancelled"
    manual = engine.run_rule(rule["id"])
    engine.test_clock[0] += 10
    engine.process_due_jobs()
    assert engine.store.get("runs", manual["id"])["status"] == "success"


def test_delayed_execution_uses_validated_snapshot_not_later_edit(engine):
    rule = delayed_rule(engine)
    run = engine.run_rule(rule["id"])
    rule["actions"][-1]["message"] = "Поздняя правка"
    engine.save_rule(rule)
    engine.test_clock[0] += 10
    engine.process_due_jobs()
    assert engine.store.list("notifications")[0]["message"] == "Завершено после задержки"
    assert engine.store.get("runs", run["id"])["status"] == "success"


def test_versions_and_rollback_create_new_version_preserving_history(engine):
    first = engine.rule_versions("rule-leak")[0]
    rule = deepcopy(first["rule"])
    rule["name"] = "Новое имя"
    engine.save_rule(rule)
    assert [row["version"] for row in engine.rule_versions(rule["id"])] == [2, 1]
    restored = engine.rollback_rule(rule["id"], first["id"])
    assert restored["name"] == first["rule"]["name"]
    versions = engine.rule_versions(rule["id"])
    assert [row["version"] for row in versions] == [3, 2, 1]
    assert versions[0]["restored_from"] == first["id"]


def test_rollback_revalidates_removed_device_and_does_not_mutate_current_rule(engine):
    first = engine.rule_versions("rule-leak")[0]
    rule = deepcopy(first["rule"])
    rule["actions"] = [{"type": "notification", "target": "resident", "message": "Только сообщение"}]
    current = engine.save_rule(rule)
    engine.delete_device("valve-1")
    with pytest.raises(ValueError):
        engine.rollback_rule(rule["id"], first["id"])
    assert engine.store.get("rules", rule["id"]) == current
    assert len(engine.rule_versions(rule["id"])) == 2


def test_command_confirmation_never_claims_real_device_without_readback(engine):
    response = engine.command("valve-1", "closed", True)
    assert response["command_status"] == {"stage": "emulator_confirmed", "platform_confirmation": "not_linked"}
    device = engine.device("valve-1")
    device["ujin"]["serialnumber"] = "owned-test-device"
    engine.store.save("devices", device["id"], device)
    response = engine.command("valve-1", "closed", False)
    assert response["command_status"]["platform_confirmation"] == "pending"


def test_delayed_job_survives_database_close_and_reopen(tmp_path):
    path = tmp_path / "restart.sqlite"
    store = Store(path)
    clock = [time.time()]
    first = Engine(store, clock=lambda: clock[0])
    for device in seed_devices():
        item = Device.model_validate(device).model_dump()
        store.save("devices", item["id"], item)
    for rule in seed_rules():
        store.save("rules", rule["id"], first.validate_rule(rule))
    rule = delayed_rule(first)
    run = first.run_rule(rule["id"])
    store.close()
    clock[0] += 10
    restored = Store(path)
    try:
        restarted = Engine(restored, clock=lambda: clock[0])
        restarted.process_due_jobs()
        assert restored.get("runs", run["id"])["status"] == "success"
        assert len(restored.list("notifications")) == 1
    finally:
        restored.close()


def test_disabled_owner_cannot_execute_previously_scheduled_actions(engine):
    engine.store.save("users", "inactive-owner", {"id": "inactive-owner", "role": "resident", "active": True,
                                                 "location": {"building_id": "building-1", "entrance": "1", "apartment": "3007"}})
    rule = delayed_rule(engine)
    rule["owner_id"] = "inactive-owner"
    engine.save_rule(rule)
    run = engine.run_rule(rule["id"])
    owner = engine.store.get("users", "inactive-owner")
    owner["active"] = False
    engine.store.save("users", owner["id"], owner)
    engine.test_clock[0] += 10
    assert not engine.process_due_jobs()
    assert engine.store.get("runs", run["id"])["status"] == "cancelled"
    assert engine.device("valve-1")["state"]["closed"] is False
    assert not engine.store.list("notifications")
