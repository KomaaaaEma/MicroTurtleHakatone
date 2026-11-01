from copy import deepcopy
from datetime import datetime
import time

import pytest
from pydantic import ValidationError

from app.engine import Engine
from app.models import Device, DeviceLocation
from app.properties import PropertyService
from app.seed import seed_devices, seed_rules
from app.store import Store


@pytest.fixture
def properties(tmp_path):
    store = Store(tmp_path / "properties.sqlite")
    for item in seed_devices():
        device = Device.model_validate(item).model_dump()
        store.save("devices", device["id"], device)
    service = PropertyService(store)
    service.migrate()
    # Other apartments are test fixtures, not invented user addresses in seed data.
    building = service.get_building("building-1")
    building["entrances"][0]["apartments"].append({"number": "3008", "floor": 1})
    building["entrances"].append({"number": "2", "name": "", "apartments": [{"number": "44", "floor": 1}]})
    service.save_building(building, "building-1")
    yield service
    store.close()


def resident(apartment="3007", building_id="building-1", entrance="1"):
    return {"id": "resident-" + apartment, "role": "resident", "location": {
        "building_id": building_id, "entrance": entrance, "apartment": apartment,
    }}


def test_initial_address_is_explicitly_unknown_and_locations_real_fields(properties):
    building = properties.catalog()["buildings"][0]
    assert building["address"] == "г. Пермь, шоссе Космонавтов, 111д"
    device = properties.store.get("devices", "leak-1")
    assert device["location"] == {"building_id": "building-1", "entrance": "1", "apartment": "3007", "floor": None, "scope": "apartment"}
    label = properties.location_label(device)
    assert "шоссе Космонавтов, 111д" in label
    assert "кв. 3007" in label and "Ванная" in label


def test_resident_never_gets_common_devices_or_another_apartment(properties):
    all_devices = properties.store.list("devices")
    admin_devices = properties.filter_devices(all_devices, {"role": "admin"})
    resident_devices = properties.filter_devices(all_devices, resident())
    assert len(admin_devices) == 17
    assert len(resident_devices) == 17
    assert len([item for item in resident_devices if item["location"]["scope"] == "apartment"]) == 12
    assert len(properties.filter_devices(all_devices, resident("3008"), {"scope": "apartment"})) == 0
    assert not properties.filter_devices(all_devices, resident(), {"apartment": "3008"})
    assert len(properties.filter_devices(all_devices, resident(), {"scope": "common"})) == 5
    assert not properties.can_access(None, all_devices[0])


def test_administrator_filters_house_entrance_apartment_and_common_scope(properties):
    devices = properties.store.list("devices")
    admin = {"role": "admin"}
    common = properties.filter_devices(devices, admin, {"building_id": "building-1", "scope": "common"})
    assert {item["id"] for item in common} == {"barrier-1", "elevator-1", "corridor-meter-1", "intercom-1", "common-camera-1"}
    entrance_common = properties.filter_devices(devices, admin, {"entrance": "1", "scope": "common"})
    assert {item["id"] for item in entrance_common} == {"elevator-1", "corridor-meter-1", "intercom-1", "common-camera-1"}
    assert len(properties.filter_devices(devices, admin, {"apartment": "3007"})) == 12
    assert not properties.filter_devices(devices, admin, {"building_id": "other-house"})
    with pytest.raises(ValueError, match="категория"):
        properties.filter_devices(devices, admin, {"scope": "invalid"})


def test_user_catalog_excludes_other_apartments_and_entrances(properties):
    catalog = properties.catalog(resident())["buildings"]
    assert len(catalog) == 1
    assert [item["number"] for item in catalog[0]["entrances"]] == ["1"]
    assert [item["number"] for item in catalog[0]["entrances"][0]["apartments"]] == ["3007"]
    assert not properties.catalog(resident(building_id="missing"))["buildings"]


def test_address_edit_updates_device_display_without_recreating_it(properties):
    before = properties.store.get("devices", "leak-1")
    building = properties.get_building("building-1")
    building["address"] = "Тестовый адрес, дом 10"
    properties.save_building(building, "building-1")
    after = properties.store.get("devices", "leak-1")
    assert before == after
    assert properties.location_label(after).startswith("Тестовый адрес, дом 10")


def test_building_crud_and_occupied_reference_protection(properties):
    building = properties.save_building({"name": "Второй дом", "address": "", "entrances": [{"number": "1", "apartments": [{"number": "1", "floor": 2}]}]})
    assert properties.get_building(building["id"])["name"] == "Второй дом"
    properties.delete_building(building["id"])
    with pytest.raises(ValueError, match="не найден"):
        properties.get_building(building["id"])
    with pytest.raises(ValueError, match="есть устройства"):
        properties.delete_building("building-1")
    original = properties.get_building("building-1")
    changed = deepcopy(original)
    changed["entrances"] = []
    with pytest.raises(ValueError, match="Подъезд"):
        properties.save_building(changed, "building-1")
    assert properties.get_building("building-1") == original


def test_occupied_resident_location_cannot_be_removed(properties):
    properties.store.save("users", "resident-43", resident("3008"))
    building = properties.get_building("building-1")
    building["entrances"][0]["apartments"] = [item for item in building["entrances"][0]["apartments"] if item["number"] != "3008"]
    with pytest.raises(ValueError, match="Квартира"):
        properties.save_building(building, "building-1")


def test_locations_are_validated_against_directory(properties):
    assert properties.validate_resident_location(resident()["location"])["apartment"] == "3007"
    with pytest.raises(ValueError, match="Квартира"):
        properties.validate_resident_location(resident("999")["location"])
    with pytest.raises(ValueError, match="Подъезд"):
        properties.validate_resident_location(resident(entrance="99")["location"])
    device = properties.store.get("devices", "leak-1")
    building = properties.get_building("building-1")
    building["entrances"][0]["apartments"][0]["floor"] = 1
    properties.save_building(building, "building-1")
    device["location"]["floor"] = 2
    with pytest.raises(ValueError, match="Этаж"):
        properties.validate_device_location(device)
    with pytest.raises(ValidationError, match="Общедомовое"):
        DeviceLocation(scope="common", apartment="3007")


def test_health_red_alarms_offline_and_recovery_are_derived(properties):
    device = properties.store.get("devices", "leak-1")
    assert properties.health(device)["level"] == "ok"
    device["state"]["leak"] = True
    health = properties.health(device)
    assert health["level"] == "critical"
    assert health["issues"][0]["code"] == "leak"
    device["online"] = False
    assert properties.health(device)["level"] == "offline"
    device["online"] = True
    device["state"]["leak"] = False
    assert properties.health(device)["level"] == "ok"


def test_warnings_are_not_confused_with_normal_commanded_states(properties):
    valve = properties.store.get("devices", "valve-1")
    valve["state"]["closed"] = True
    assert properties.health(valve)["level"] == "ok"
    light = properties.store.get("devices", "light-1")
    light["state"]["power"] = False
    assert properties.health(light)["level"] == "ok"
    leak = properties.store.get("devices", "leak-1")
    leak["state"]["battery"] = 15
    assert properties.health(leak)["level"] == "warning"
    ventilation = properties.store.get("devices", "ventilation-1")
    ventilation["state"]["co2"] = 1500
    assert properties.health(ventilation)["issues"][0]["code"] == "co2"
    barrier = properties.store.get("devices", "barrier-1")
    barrier["state"]["obstacle"] = True
    assert properties.health(barrier)["level"] == "warning"
    fire = properties.store.get("devices", "fire-1")
    fire["state"]["smoke"] = True
    assert properties.health(fire)["level"] == "critical"


def test_migration_preserves_existing_state_and_bindings_and_is_idempotent(tmp_path):
    store = Store(tmp_path / "old.sqlite")
    old = seed_devices()[0]
    del old["location"]
    old["state"]["leak"] = True
    old["ujin"]["serialnumber"] = "owned-existing-serial"
    store.save("devices", old["id"], old)
    service = PropertyService(store)
    service.migrate()
    first = store.list("devices")
    service.migrate()
    assert store.list("devices") == first
    upgraded = store.get("devices", "leak-1")
    assert upgraded["state"]["leak"] is True
    assert upgraded["ujin"]["serialnumber"] == "owned-existing-serial"
    assert upgraded["location"]["apartment"] == "3007"
    assert store.get("devices", "corridor-meter-1")["location"]["scope"] == "common"
    store.close()


def test_water_meter_only_reacts_to_valve_in_its_own_apartment(properties):
    engine = Engine(properties.store)
    own_valve = properties.store.get("devices", "valve-1")
    own_valve["state"]["closed"] = True
    properties.store.save("devices", own_valve["id"], own_valve)
    other_meter = deepcopy(properties.store.get("devices", "meter-1"))
    other_meter["id"] = "other-meter"
    other_meter["location"]["apartment"] = "3008"
    properties.store.save("devices", other_meter["id"], other_meter)
    engine.tick(2)
    assert engine.device("meter-1")["state"]["flow"] == 0
    assert engine.device("other-meter")["state"]["flow"] > 0
    assert engine.device("corridor-meter-1")["state"]["total"] > 530.4


def test_execution_snapshots_location_for_notifications(properties):
    engine = Engine(properties.store)
    for item in seed_rules():
        rule = engine.validate_rule(item)
        properties.store.save("rules", rule["id"], rule)
    engine.update_state("leak-1", {"leak": True})
    run = properties.store.list("runs")[0]
    assert run["location"]["apartment"] == "3007"
    assert all(item["location"]["apartment"] == "3007" for item in properties.store.list("notifications"))


def test_hierarchy_crud_and_foreign_organization_reference_are_validated(properties):
    organization = properties.save_organization({"name": "Другая организация"})
    complex = properties.save_complex({"name": "Второй комплекс", "organization_id": organization["id"]})
    building = properties.save_building({"name": "Другой дом", "organization_id": organization["id"], "complex_id": complex["id"], "entrances": []})
    with pytest.raises(ValueError, match="другой организации"):
        properties.save_building({"name": "Неверная связь", "organization_id": "organization-1", "complex_id": complex["id"]})
    with pytest.raises(ValueError, match="есть дома"):
        properties.delete_complex(complex["id"])
    with pytest.raises(ValueError, match="жилые комплексы"):
        properties.delete_organization(organization["id"])
    properties.delete_building(building["id"])
    properties.delete_complex(complex["id"])
    properties.delete_organization(organization["id"])


def test_common_commands_are_explicit_and_entrance_scoped(properties):
    user = resident()
    barrier = properties.store.get("devices", "barrier-1")
    elevator = properties.store.get("devices", "elevator-1")
    assert not properties.can_access(user, barrier)
    assert properties.can_read(user, barrier)
    assert properties.can_command(user, barrier, "open")
    assert not properties.can_command(user, barrier, "obstacle")
    assert properties.can_command(user, elevator, "floor")
    assert not properties.can_command(user, elevator, "evacuation")
    assert properties.can_read(resident(entrance="2"), barrier)
    assert not properties.can_read(resident(entrance="2"), elevator)
    assert not properties.can_read(resident(building_id="foreign-house"), barrier)
    barrier["common_access"]["resident_read"] = False
    assert not properties.can_command(user, barrier, "open")


def test_v2_migration_adds_meters_and_common_policy_without_overwriting_custom_denial(tmp_path):
    store = Store(tmp_path / "upgrade-v2.sqlite")
    try:
        for item in [d for d in seed_devices() if d["id"] not in {"intercom-1", "common-camera-1", "hot-water-meter-1", "electricity-meter-1", "heating-meter-1"}]:
            item = Device.model_validate(item).model_dump()
            item.pop("common_access", None)
            item.pop("meter", None)
            if item["location"]["scope"] == "apartment":
                item["location"].update(apartment="42", floor=1)
            store.save("devices", item["id"], item)
        # This explicit administrator policy must survive adding the new defaults.
        elevator = store.get("devices", "elevator-1")
        elevator["common_access"] = {"resident_read": False, "resident_commands": []}
        store.save("devices", elevator["id"], elevator)
        store.save("buildings", "building-1", {"id": "building-1", "name": "Дом MicroTurtles", "address": "", "entrances": [{"number": "1", "name": "", "apartments": [{"number": "42", "floor": 1}]}]})
        store.save("meta", "properties-v2", {"version": 2})
        service = PropertyService(store)
        service.migrate()
        assert len(store.list("devices")) == 17
        assert store.get("devices", "meter-1")["meter"]["type"] == "cold_water"
        assert store.get("devices", "barrier-1")["common_access"]["resident_commands"] == ["open"]
        assert store.get("devices", "elevator-1")["common_access"]["resident_read"] is False
        assert store.get("devices", "leak-1")["location"]["apartment"] == "3007"
        assert store.get("devices", "leak-1")["location"]["floor"] is None
        assert service.get_building("building-1")["address"] == "г. Пермь, шоссе Космонавтов, 111д"
        service.migrate()
        assert store.get("devices", "elevator-1")["common_access"]["resident_read"] is False
        assert len(store.list("devices")) == 17
    finally:
        store.close()


def test_last_seen_only_changes_on_receipt_not_bootstrap_health_or_crud(properties):
    clock = [time.time()]
    engine = Engine(properties.store, clock=lambda: clock[0])
    assert engine.device("leak-1")["last_seen"] is None
    engine.update_state("leak-1", {"leak": False}, source="ujin")
    first = engine.device("leak-1")
    assert first["last_seen_source"] == "ujin"
    assert first["last_seen"] is not None
    clock[0] += 10
    engine.bootstrap()
    health = properties.health(first)
    assert health["connection_status"] == "observed"
    assert engine.device("leak-1")["last_seen"] == first["last_seen"]
    edited = deepcopy(first)
    edited["name"] = "Переименованный датчик"
    edited["last_seen"] = "2099-01-01T00:00:00+00:00"
    edited["last_seen_source"] = "ujin"
    saved = engine.save_device(edited, "leak-1")
    assert saved["last_seen"] == first["last_seen"]
    engine.update_state("leak-1", {"leak": False}, source="emulator")
    latest = engine.device("leak-1")
    assert latest["last_seen"] != first["last_seen"]
    assert latest["last_seen_source"] == "emulator"


def test_failed_offline_request_does_not_claim_new_connection(properties):
    clock = [time.time()]
    engine = Engine(properties.store, clock=lambda: clock[0])
    engine.command("valve-1", "closed", False)
    previous = engine.device("valve-1")["last_seen"]
    properties.store.update_device("valve-1", {"online": False})
    clock[0] += 30
    with pytest.raises(ValueError):
        engine.command("valve-1", "closed", True)
    assert engine.device("valve-1")["last_seen"] == previous
    assert properties.health(engine.device("valve-1"))["connection_status"] == "offline"
    assert properties.health(engine.device("valve-1"))["last_seen"] == previous


def test_new_device_cannot_import_fabricated_current_connection(properties):
    engine = Engine(properties.store)
    item = deepcopy(engine.device("leak-1"))
    item.update(id="new-sensor", last_seen="2099-01-01T00:00:00+00:00", last_seen_source="ujin")
    created = engine.save_device(item)
    assert created["last_seen"] is None and created["last_seen_source"] is None
    assert properties.health(created)["connection_status"] == "never_seen"


def test_mapping_diagnostic_does_not_claim_real_platform_confirmation(properties):
    device = properties.store.get("devices", "leak-1")
    assert properties.health(device)["mapping_status"] == "not_linked"
    device["ujin"]["serialnumber"] = "own-device"
    health = properties.health(device)
    assert health["mapping_status"] == "incomplete"
    assert health["mapping_missing"] == ["ext_guid"]
    device["ujin"]["ext_guid"] = "own_device"
    health = properties.health(device)
    assert health["mapping_status"] == "configured"
    assert health["connection_status"] == "never_seen"
    assert health["last_seen"] is None


def test_intercom_and_common_camera_are_virtual_and_explicitly_scoped(properties):
    user = resident()
    intercom = properties.store.get("devices", "intercom-1")
    camera = properties.store.get("devices", "common-camera-1")
    private_camera = properties.store.get("devices", "camera-1")
    assert intercom["kind"] == "intercom" and intercom["location"]["scope"] == "common"
    assert camera["kind"] == "camera" and camera["location"]["scope"] == "common"
    assert private_camera["location"]["scope"] == "apartment"
    assert properties.can_command(user, intercom, "door_open")
    assert not properties.can_command(user, intercom, "call")
    assert properties.can_read(user, camera)
    assert not properties.can_command(user, camera, "recording")
    for device in (intercom, camera):
        assert not properties.can_read(resident(entrance="2"), device)
        assert not properties.can_read(resident(building_id="other-house"), device)
        assert device["last_seen"] is None
        assert not device["ujin"].get("serialnumber")
        before = deepcopy(device)
        properties.health(device)
        properties.common_devices(user)
        assert properties.store.get("devices", device["id"]) == before
    engine = Engine(properties.store)
    engine.command("intercom-1", "door_open", True)
    assert engine.device("intercom-1")["state"]["door_open"] is True
    assert engine.device("intercom-1")["last_seen_source"] == "manual"


def test_common_device_migration_preserves_custom_device_and_does_not_resurrect_deletion(properties):
    intercom = properties.store.get("devices", "intercom-1")
    intercom["name"] = "Мой домофон"
    intercom["common_access"] = {"resident_read": False, "resident_commands": []}
    properties.store.save("devices", intercom["id"], intercom)
    properties.store.delete("devices", "common-camera-1")
    properties.store.delete("meta", "properties-v4-common")
    properties.migrate()
    assert properties.store.get("devices", "intercom-1")["name"] == "Мой домофон"
    assert properties.store.get("devices", "intercom-1")["common_access"]["resident_read"] is False
    assert properties.store.get("devices", "common-camera-1") is not None
    assert len(properties.store.list("devices")) == 17
    properties.store.delete("devices", "common-camera-1")
    properties.migrate()
    assert properties.store.get("devices", "common-camera-1") is None
