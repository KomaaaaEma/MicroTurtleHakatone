"""Building directory, physical device placement and derived operational health.

The initial address was supplied directly by the user. It remains editable.
The entrance and floor were not supplied: entrance 1 is explicitly marked as
requiring confirmation and no floor is inferred from the apartment number.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from .models import DeviceLocation, Model
from .seed import corridor_meter, additional_meters, additional_common_devices


class Apartment(Model):
    number: str = Field(min_length=1, max_length=30)
    floor: int | None = Field(default=None, ge=-10, le=200)

    @model_validator(mode="after")
    def clean_number(self):
        self.number = self.number.strip()
        if not self.number:
            raise ValueError("Укажите номер квартиры")
        return self


class Entrance(Model):
    number: str = Field(min_length=1, max_length=30)
    name: str = Field(default="", max_length=120)
    apartments: list[Apartment] = Field(default_factory=list, max_length=2000)

    @model_validator(mode="after")
    def unique_apartments(self):
        self.number = self.number.strip()
        if not self.number:
            raise ValueError("Укажите номер подъезда")
        numbers = [item.number for item in self.apartments]
        if len(numbers) != len(set(numbers)):
            raise ValueError("Номера квартир в подъезде должны быть уникальными")
        return self


class Building(Model):
    id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=160)
    address: str = Field(default="", max_length=500)
    organization_id: str = Field(default="organization-1", min_length=1, max_length=100)
    complex_id: str = Field(default="complex-1", min_length=1, max_length=100)
    entrances: list[Entrance] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def unique_entrances(self):
        self.name = self.name.strip()
        self.address = self.address.strip()
        if not self.name:
            raise ValueError("Укажите название дома")
        numbers = [item.number for item in self.entrances]
        if len(numbers) != len(set(numbers)):
            raise ValueError("Номера подъездов должны быть уникальными")
        return self


class Organization(Model):
    id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=160)


class Complex(Model):
    id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=160)
    organization_id: str = Field(min_length=1, max_length=100)


def initial_building():
    return {
        "id": "building-1", "name": "Дом MicroTurtles", "address": "г. Пермь, шоссе Космонавтов, 111д",
        "entrances": [
            {"number": "1", "name": "Подъезд 1 (уточнить)", "apartments": [{"number": "3007", "floor": None}]},
        ],
    }


class PropertyService:
    def __init__(self, store):
        self.store = store

    def migrate(self):
        """One-time upgrade preserves existing states, rules, names and bindings."""
        if not self.store.list("organizations"):
            self.store.save("organizations", "organization-1", {"id": "organization-1", "name": "УК MicroTurtles"})
        if not self.store.list("complexes"):
            self.store.save("complexes", "complex-1", {"id": "complex-1", "name": "Жилой комплекс — название не указано", "organization_id": "organization-1"})
        if not self.store.list("buildings"):
            building = Building.model_validate(initial_building()).model_dump()
            self.store.save("buildings", building["id"], building)
        for old in self.store.list("buildings"):
            if "organization_id" not in old or "complex_id" not in old:
                upgraded = Building.model_validate(old).model_dump()
                self.store.save("buildings", upgraded["id"], upgraded)
        marker = self.store.get("meta", "properties-v2")
        seed_building = self.store.get("buildings", "building-1")
        seed_address_upgrade = bool(seed_building and not seed_building.get("address") and not self.store.get("meta", "initial-user-address-v1"))
        if seed_address_upgrade:
            seed_building["address"] = initial_building()["address"]
            entrance = self._find_entrance(seed_building, "1")
            if entrance is None:
                entrance = {"number": "1", "name": "Подъезд 1 (уточнить)", "apartments": []}
                seed_building["entrances"].append(entrance)
            if not entrance.get("name"):
                entrance["name"] = "Подъезд 1 (уточнить)"
            if not any(item["number"] == "3007" for item in entrance["apartments"]):
                entrance["apartments"].append({"number": "3007", "floor": None})
            self.store.save("buildings", "building-1", seed_building)
        self.store.save("meta", "initial-user-address-v1", {"applied": True})
        existing_residents = any(user.get("role") == "resident" for user in self.store.list("users"))
        for device in self.store.list("devices"):
            old_location = device.get("location")
            initial_default = ((not marker or seed_address_upgrade) and not existing_residents and old_location
                               and old_location.get("building_id") == "building-1" and old_location.get("entrance") == "1"
                               and old_location.get("apartment") == "42")
            if old_location and not initial_default:
                continue
            location = {"building_id": "building-1", "entrance": "1", "apartment": "3007", "floor": None, "scope": "apartment"}
            if device.get("kind") in ("barrier", "elevator"):
                location.update(scope="common", apartment="", entrance="" if device["kind"] == "barrier" else "1",
                                floor=None)
            device["location"] = DeviceLocation.model_validate(location).model_dump()
            self.store.save("devices", device["id"], device)
        if not marker and not self.store.get("devices", "corridor-meter-1"):
            item = corridor_meter()
            self.store.save("devices", item["id"], item)
        if not marker:
            rule = self.store.get("rules", "rule-leak")
            if rule:
                changed = False
                for action in rule.get("actions", []):
                    if action.get("message") == "Протечка в квартире 42. Проверьте перекрытие воды и свяжитесь с жителем.":
                        action["message"] = "Обнаружена протечка в квартире. Проверьте расположение датчика и свяжитесь с жителем."
                        changed = True
                if changed:
                    self.store.save("rules", rule["id"], rule)
        self.store.save("meta", "properties-v2", {"version": 2})
        if not self.store.get("meta", "properties-v3"):
            for device in self.store.list("devices"):
                if device["id"] == "meter-1" and not device.get("meter"):
                    device["meter"] = {"type": "cold_water", "unit": "м³", "manual_allowed": True, "stale_after_hours": 48, "max_daily_usage": 5}
                if device["id"] == "corridor-meter-1" and not device.get("meter"):
                    device["meter"] = corridor_meter()["meter"]
                if device["id"] in ("barrier-1", "elevator-1") and not isinstance(device.get("common_access"), dict):
                    device["common_access"] = {"resident_read": True, "resident_commands": ["open"] if device["id"] == "barrier-1" else ["floor"]}
                self.store.save("devices", device["id"], device)
            for item in additional_meters():
                if not self.store.get("devices", item["id"]):
                    self.store.save("devices", item["id"], item)
            self.store.save("meta", "properties-v3", {"version": 3})
        if not self.store.get("meta", "properties-v4-common"):
            for item in additional_common_devices():
                if not self.store.get("devices", item["id"]):
                    self.store.save("devices", item["id"], item)
            self.store.save("meta", "properties-v4-common", {"version": 4})
        for device in self.store.list("devices"):
            if "last_seen" not in device or "last_seen_source" not in device:
                device.setdefault("last_seen", None)
                device.setdefault("last_seen_source", None)
                self.store.save("devices", device["id"], device)
        return self.catalog()

    def catalog(self, user=None):
        buildings = self.store.list("buildings")
        if user is None or user.get("role") == "admin":
            return {"organizations": self.store.list("organizations"), "complexes": self.store.list("complexes"), "buildings": buildings}
        location = user.get("location") or {}
        allowed = []
        for building in buildings:
            if building["id"] != location.get("building_id"):
                continue
            item = deepcopy(building)
            item["entrances"] = [entrance for entrance in item["entrances"] if entrance["number"] == location.get("entrance")]
            for entrance in item["entrances"]:
                entrance["apartments"] = [apt for apt in entrance["apartments"] if apt["number"] == location.get("apartment")]
            allowed.append(item)
        complex_ids = {building["complex_id"] for building in allowed}
        organization_ids = {building["organization_id"] for building in allowed}
        return {"organizations": [item for item in self.store.list("organizations") if item["id"] in organization_ids],
                "complexes": [item for item in self.store.list("complexes") if item["id"] in complex_ids], "buildings": allowed}

    def _directory_item(self, kind, identifier, label):
        item = self.store.get(kind, identifier)
        if item is None:
            raise ValueError(label + " не найден(а) в справочнике")
        return item

    def save_organization(self, payload, expected_id=None):
        item = Organization.model_validate(payload).model_dump()
        if not item["name"].strip():
            raise ValueError("Укажите название организации")
        if expected_id:
            self._directory_item("organizations", expected_id, "Организация")
            if item["id"] != expected_id:
                raise ValueError("Нельзя изменить ID организации")
        elif self.store.get("organizations", item["id"]):
            raise ValueError("Организация с таким ID уже существует")
        self.store.save("organizations", item["id"], item)
        return item

    def delete_organization(self, identifier):
        self._directory_item("organizations", identifier, "Организация")
        if any(item["organization_id"] == identifier for item in self.store.list("complexes")):
            raise ValueError("К организации привязаны жилые комплексы")
        self.store.delete("organizations", identifier)
        return {"ok": True}

    def save_complex(self, payload, expected_id=None):
        item = Complex.model_validate(payload).model_dump()
        if not item["name"].strip():
            raise ValueError("Укажите название жилого комплекса")
        self._directory_item("organizations", item["organization_id"], "Организация")
        if expected_id:
            self._directory_item("complexes", expected_id, "Жилой комплекс")
            if item["id"] != expected_id:
                raise ValueError("Нельзя изменить ID жилого комплекса")
            if any(building["complex_id"] == item["id"] and building["organization_id"] != item["organization_id"] for building in self.store.list("buildings")):
                raise ValueError("Сначала перенесите дома из этого комплекса")
        elif self.store.get("complexes", item["id"]):
            raise ValueError("Жилой комплекс с таким ID уже существует")
        self.store.save("complexes", item["id"], item)
        return item

    def delete_complex(self, identifier):
        self._directory_item("complexes", identifier, "Жилой комплекс")
        if any(item["complex_id"] == identifier for item in self.store.list("buildings")):
            raise ValueError("В жилом комплексе есть дома")
        self.store.delete("complexes", identifier)
        return {"ok": True}

    def get_building(self, building_id):
        building = self.store.get("buildings", building_id)
        if building is None:
            raise ValueError("Дом не найден в справочнике")
        return building

    @staticmethod
    def _find_entrance(building, entrance):
        return next((item for item in building["entrances"] if item["number"] == entrance), None)

    def _check_location(self, location, building=None, *, resident=False):
        if not isinstance(location, dict):
            raise ValueError("Укажите расположение")
        if resident:
            # User profiles have no device-specific floor/scope fields.
            result = {key: str(location.get(key) or "").strip() for key in ("building_id", "entrance", "apartment")}
            if not all(result.values()):
                raise ValueError("Для жителя укажите дом, подъезд и квартиру")
            result["scope"] = "apartment"
        else:
            result = DeviceLocation.model_validate(location).model_dump()
        building = building or self.get_building(result["building_id"])
        if building["id"] != result["building_id"]:
            raise ValueError("Дом не соответствует расположению")
        entrance = self._find_entrance(building, result["entrance"])
        if result["entrance"] and entrance is None:
            raise ValueError("Подъезд отсутствует в выбранном доме")
        if result["scope"] == "apartment":
            apartment = next((item for item in (entrance or {}).get("apartments", []) if item["number"] == result["apartment"]), None)
            if apartment is None:
                raise ValueError("Квартира отсутствует в выбранном подъезде")
            if not resident:
                if result.get("floor") is not None and apartment.get("floor") is not None and result["floor"] != apartment["floor"]:
                    raise ValueError("Этаж устройства не соответствует этажу квартиры в справочнике")
                result["floor"] = apartment.get("floor")
        if resident:
            return {key: result[key] for key in ("building_id", "entrance", "apartment")}
        return result

    def validate_resident_location(self, location):
        return self._check_location(location, resident=True)

    def validate_device_location(self, device):
        return self._check_location(device.get("location") or {})

    def save_building(self, payload, expected_id=None):
        item = Building.model_validate(payload).model_dump()
        self._directory_item("organizations", item["organization_id"], "Организация")
        complex = self._directory_item("complexes", item["complex_id"], "Жилой комплекс")
        if complex["organization_id"] != item["organization_id"]:
            raise ValueError("Жилой комплекс принадлежит другой организации")
        if expected_id is not None:
            self.get_building(expected_id)
            if item["id"] != expected_id:
                raise ValueError("Нельзя изменить ID дома")
        elif self.store.get("buildings", item["id"]):
            raise ValueError("Дом с таким ID уже существует")
        # Do not orphan any device or resident by deleting its entrance/apartment.
        for device in self.store.list("devices"):
            location = device.get("location") or {}
            if location.get("building_id") == item["id"]:
                self._check_location(location, item)
        for user in self.store.list("users"):
            location = user.get("location") or {}
            if user.get("role") == "resident" and location.get("building_id") == item["id"]:
                self._check_location(location, item, resident=True)
        self.store.save("buildings", item["id"], item)
        self.store.log("property", "Сохранён дом: " + item["name"], building_id=item["id"])
        return item

    def delete_building(self, building_id):
        self.get_building(building_id)
        if any((device.get("location") or {}).get("building_id") == building_id for device in self.store.list("devices")):
            raise ValueError("В доме есть устройства. Сначала перенесите или удалите их")
        if any((user.get("location") or {}).get("building_id") == building_id for user in self.store.list("users")):
            raise ValueError("К дому привязаны пользователи. Сначала измените их расположение")
        self.store.delete("buildings", building_id)
        self.store.log("property", "Дом удалён из справочника", building_id=building_id)
        return {"ok": True}

    @staticmethod
    def can_access(user, device):
        if not user:
            return False
        if user.get("role") == "admin":
            return True
        if user.get("role") != "resident":
            return False
        location = device.get("location") or {}
        assigned = user.get("location") or {}
        return location.get("scope") == "apartment" and all(
            bool(assigned.get(key)) and location.get(key) == assigned.get(key)
            for key in ("building_id", "entrance", "apartment"))

    def can_read(self, user, device):
        if self.can_access(user, device):
            return True
        if not user or user.get("role") != "resident":
            return False
        location = device.get("location") or {}
        assigned = user.get("location") or {}
        access = device.get("common_access") or {}
        return (location.get("scope") == "common" and access.get("resident_read", True)
                and bool(assigned.get("building_id")) and location.get("building_id") == assigned.get("building_id")
                and (not location.get("entrance") or location.get("entrance") == assigned.get("entrance")))

    def can_command(self, user, device, signal):
        if self.can_access(user, device):
            return True
        return self.can_read(user, device) and signal in (device.get("common_access") or {}).get("resident_commands", [])

    def common_devices(self, user, filters=None):
        return self.filter_devices(self.store.list("devices"), user, {**(filters or {}), "scope": "common"})

    def filter_devices(self, devices, user, filters=None):
        filters = filters or {}
        scope = filters.get("scope") or "all"
        if scope not in ("all", "apartment", "common"):
            raise ValueError("Неизвестная категория устройств")
        result = []
        for device in devices:
            if not self.can_read(user, device):
                continue
            location = device.get("location") or {}
            building = self.store.get("buildings", location.get("building_id")) or {}
            if any(filters.get(key) and building.get(key) != str(filters[key]) for key in ("organization_id", "complex_id")):
                continue
            if scope != "all" and location.get("scope") != scope:
                continue
            if any(filters.get(key) and location.get(key) != str(filters[key]) for key in ("building_id", "entrance", "apartment")):
                continue
            result.append(device)
        return result

    def location_label(self, device):
        location = device.get("location") or {}
        building = self.store.get("buildings", location.get("building_id")) or {}
        address = building.get("address") or ((building.get("name") or "Дом") + " · адрес не указан")
        parts = [address]
        if location.get("entrance"):
            entrance = self._find_entrance(building, location["entrance"]) if building else None
            parts.append(entrance.get("name") if entrance and entrance.get("name") else "подъезд " + location["entrance"])
        if location.get("floor") is not None:
            parts.append("этаж " + str(location["floor"]))
        if location.get("scope") == "common":
            parts.append("общедомовое")
        elif location.get("apartment"):
            parts.append("кв. " + location["apartment"])
        if device.get("room"):
            parts.append(device["room"])
        return ", ".join(parts)

    def health(self, device):
        state = device.get("state") or {}
        issues = []

        def add(code, message, severity):
            issues.append({"code": code, "message": message, "severity": severity})

        def active(value):
            return value is True or (type(value) in (int, float) and value != 0)

        if not device.get("online", True):
            add("offline", "Нет связи с устройством. Последнее состояние может быть устаревшим", "critical")
        if active(state.get("leak")):
            add("leak", "Обнаружена протечка воды", "critical")
        if active(state.get("smoke")):
            add("smoke", "Обнаружен дым — требуется немедленная проверка", "critical")
        if active(state.get("fault")):
            add("fault", "Устройство сообщает о неисправности", "critical")
        if active(state.get("tamper")):
            add("tamper", "Корпус устройства вскрыт", "warning")
        battery = state.get("battery")
        if type(battery) in (int, float) and battery <= 20:
            add("low_battery", f"Низкий заряд батареи: {battery:g}%", "critical" if battery <= 5 else "warning")
        temperature = state.get("temperature")
        if type(temperature) in (int, float) and (temperature < 5 or temperature > 35):
            add("temperature", f"Температура вне комфортного диапазона: {temperature:g} °C", "warning")
        co2 = state.get("co2")
        if type(co2) in (int, float) and co2 > 1000:
            add("co2", f"Повышенный CO₂: {co2:g} ppm", "warning")
        if active(state.get("obstacle")):
            add("obstacle", "Обнаружено препятствие: закрытие шлагбаума заблокировано", "warning")
        if not device.get("online", True):
            level, label = "offline", "Нет связи"
        elif any(issue["severity"] == "critical" for issue in issues):
            level, label = "critical", "Требует внимания"
        elif issues:
            level, label = "warning", "Предупреждение"
        else:
            level, label = "ok", "Всё в порядке"
        last_seen = device.get("last_seen")
        age_seconds = None
        if last_seen:
            try:
                seen = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
                if seen.tzinfo is not None:
                    age_seconds = max(0, int((datetime.now(timezone.utc) - seen).total_seconds()))
            except (TypeError, ValueError):
                pass
        mapping = device.get("ujin") or {}
        missing = [key for key in ("serialnumber", "ext_guid") if not mapping.get(key)]
        mapping_status = "not_linked" if len(missing) == 2 else "incomplete" if missing else "configured"
        return {"device_id": device["id"], "level": level, "label": label, "issues": issues,
                "location_label": self.location_label(device), "last_seen": last_seen,
                "last_seen_source": device.get("last_seen_source"), "last_seen_age_seconds": age_seconds,
                "connection_status": "offline" if not device.get("online", True) else "observed" if last_seen else "never_seen",
                "mapping_status": mapping_status, "mapping_missing": missing}
