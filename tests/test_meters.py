from copy import deepcopy
from datetime import datetime, timezone
import time

from fastapi import HTTPException
from pydantic import ValidationError
import pytest

from app.engine import Engine
from app.meters import MeterService
from app.models import Device
from app.properties import PropertyService
from app.seed import seed_devices
from app.store import Store


@pytest.fixture
def meters(tmp_path):
    store = Store(tmp_path / "meters.sqlite")
    for item in seed_devices():
        device = Device.model_validate(item).model_dump()
        store.save("devices", device["id"], device)
    properties = PropertyService(store)
    properties.migrate()
    clock = [time.time()]
    engine = Engine(store, clock=lambda: clock[0])
    service = MeterService(store, properties, engine, clock=lambda: clock[0])
    service.test_clock = clock
    yield service
    store.close()


def resident(apartment="3007"):
    return {"id": "resident-" + apartment, "role": "resident", "location": {
        "building_id": "building-1", "entrance": "1", "apartment": apartment,
    }}


def test_four_personal_meter_types_and_explicit_demo_history(meters):
    data = meters.list(resident())["meters"]
    assert {item["type"] for item in data} == {"cold_water", "hot_water", "electricity", "heating"}
    assert {item["unit"] for item in data} == {"м³", "кВт·ч", "Гкал"}
    assert all(len(item["readings"]) == 7 for item in data)
    assert all(item["history_synthetic"] for item in data)
    assert all(row["source"] == "demo" and row["synthetic"] for item in data for row in item["readings"])


def test_get_does_not_append_readings_or_refresh_old_measurement(meters):
    first = meters.list(resident())["meters"][0]
    count = len(meters.store.list("meter_readings"))
    meters.test_clock[0] += 49 * 3600
    second = meters.list(resident())["meters"][0]
    assert len(meters.store.list("meter_readings")) == count
    assert second["last_reading_at"] == first["last_reading_at"]
    assert second["freshness"]["status"] == "stale"
    assert second["anomalies"][0]["code"] == "stale"


def test_meter_visibility_and_common_readonly_permission(meters):
    assert not meters.list(resident("3008"))["meters"]
    assert len(meters.list({"role": "admin"})["meters"]) == 5
    common = meters.list(resident(), {"scope": "common"})["meters"]
    assert len(common) == 1 and common[0]["id"] == "corridor-meter-1"
    assert common[0]["manual_allowed"] is False
    with pytest.raises(HTTPException) as forbidden:
        meters.add_reading("corridor-meter-1", {"value": 531}, resident())
    assert forbidden.value.status_code == 403
    with pytest.raises(HTTPException) as missing:
        meters.add_reading("meter-1", {"value": 125}, resident("3008"))
    assert missing.value.status_code == 404


def test_manual_entry_updates_current_state_and_history_once(meters):
    before = meters.list(resident())["meters"][0]
    value = before["value"] + 0.5
    meters.test_clock[0] += 1
    result = meters.add_reading(before["id"], {"value": value, "request_id": "submission-1"}, resident())
    assert result["value"] == value
    assert result["readings"][-1]["source"] == "manual"
    assert result["readings"][-1]["synthetic"] is False
    assert len(result["readings"]) == 8
    duplicate = meters.add_reading(before["id"], {"value": value, "request_id": "submission-1"}, resident())
    assert len(duplicate["readings"]) == 8
    assert meters.store.get("devices", before["id"])["state"]["total"] == value


def test_invalid_or_decreasing_manual_value_is_rejected_without_mutation(meters):
    first = meters.list(resident())["meters"][0]
    for value in (-1, float("nan"), float("inf"), True, "124.32"):
        with pytest.raises(ValidationError):
            meters.add_reading(first["id"], {"value": value}, resident())
    with pytest.raises(ValueError, match="меньше"):
        meters.add_reading(first["id"], {"value": first["value"] - 1}, resident())
    assert meters.store.get("devices", first["id"])["state"]["total"] == first["value"]


def test_manual_timestamp_validation_and_usage_anomaly(meters):
    first = meters.list(resident())["meters"][0]
    future = datetime.fromtimestamp(meters.test_clock[0] + 3600, timezone.utc).isoformat()
    with pytest.raises(ValueError, match="будущем"):
        meters.add_reading(first["id"], {"value": first["value"] + 1, "timestamp": future}, resident())
    meters.test_clock[0] += 3600
    result = meters.add_reading(first["id"], {"value": first["value"] + 50}, resident())
    assert result["anomalies"][0]["code"] == "usage_jump"


def test_bound_device_does_not_get_fabricated_history_and_observation_is_recorded(meters):
    device = meters.store.get("devices", "meter-1")
    device["ujin"]["serialnumber"] = "owned-device"
    meters.store.save("devices", device["id"], device)
    bound = next(item for item in meters.list(resident())["meters"] if item["id"] == "meter-1")
    assert not bound["readings"]
    assert bound["freshness"]["status"] == "no_data"
    meters.engine.update_state("meter-1", {"total": 125}, source="ujin")
    bound = next(item for item in meters.list(resident())["meters"] if item["id"] == "meter-1")
    assert bound["readings"][-1]["source"] == "ujin"
    assert bound["readings"][-1]["synthetic"] is False


def test_manual_counter_reading_does_not_claim_offline_device_became_online(meters):
    meter = meters.list(resident())["meters"][0]
    device = meters.store.get("devices", meter["id"])
    device["online"] = False
    meters.store.save("devices", device["id"], device)
    meters.test_clock[0] += 1
    updated = meters.add_reading(device["id"], {"value": meter["value"] + 1}, resident())
    assert updated["readings"][-1]["source"] == "manual"
    assert meters.store.get("devices", device["id"])["online"] is False
    assert meters.store.get("devices", device["id"])["last_seen"] is None
