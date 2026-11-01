"""Public, versioned device and scenario description language."""
from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Capability(Model):
    key: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z][a-zA-Z0-9_]*$")
    label: str = Field(min_length=1, max_length=120)
    type: Literal["boolean", "number", "string"] = "number"
    role: Literal["measurement", "event", "command", "property"] = "measurement"
    unit: str = Field(default="", max_length=20)
    min: float | None = None
    max: float | None = None
    default: Any = None

    @model_validator(mode="after")
    def valid_default(self):
        if self.min is not None and not math.isfinite(self.min):
            raise ValueError("Минимум должен быть конечным числом")
        if self.max is not None and not math.isfinite(self.max):
            raise ValueError("Максимум должен быть конечным числом")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("Минимум больше максимума")
        if self.default is None:
            self.default = {"number": 0, "boolean": False, "string": ""}[self.type]
        validate_value(self.model_dump(), self.default)
        return self


def validate_value(cap: dict, value: Any):
    """No truthiness coercion: 'false', NaN, bool-as-number are rejected."""
    kind = cap["type"]
    if kind == "boolean" and type(value) is not bool:
        raise ValueError(f"{cap['key']}: ожидается true или false")
    if kind == "number":
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f"{cap['key']}: ожидается конечное число")
        if cap.get("min") is not None and value < cap["min"]:
            raise ValueError(f"{cap['key']}: меньше {cap['min']}")
        if cap.get("max") is not None and value > cap["max"]:
            raise ValueError(f"{cap['key']}: больше {cap['max']}")
    if kind == "string" and (not isinstance(value, str) or len(value) > 2000):
        raise ValueError(f"{cap['key']}: ожидается строка до 2000 символов")
    return value


class UjinMapping(Model):
    serialnumber: str = Field(default="", max_length=100)
    ext_guid: str = Field(default="", max_length=200)
    model: str = Field(default="parkcloud_device", max_length=100)
    device_class: str = Field(default="integrations_devices", max_length=100)
    signal_map: dict[str, str] = Field(default_factory=dict, max_length=100)


class DeviceLocation(Model):
    """Physical location is independent from the user-facing room label."""
    building_id: str = Field(default="building-1", min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    entrance: str = Field(default="1", max_length=30)
    apartment: str = Field(default="3007", max_length=30)
    floor: int | None = Field(default=None, ge=-10, le=200)
    scope: Literal["apartment", "common"] = "apartment"

    @model_validator(mode="after")
    def valid_scope(self):
        self.entrance = self.entrance.strip()
        self.apartment = self.apartment.strip()
        if self.scope == "apartment" and (not self.entrance or not self.apartment):
            raise ValueError("Для квартирного устройства укажите подъезд и квартиру")
        if self.scope == "common" and self.apartment:
            raise ValueError("Общедомовое устройство не может быть привязано к квартире")
        return self


class CommonAccess(Model):
    resident_read: bool = True
    resident_commands: list[str] = Field(default_factory=list, max_length=100)


class Meter(Model):
    type: Literal["cold_water", "hot_water", "electricity", "heating"] = "cold_water"
    unit: str = Field(default="", max_length=20)
    manual_allowed: bool = True
    stale_after_hours: float = Field(default=48, gt=0, le=8760)
    max_daily_usage: float = Field(default=10, gt=0, le=1000000)

    @model_validator(mode="after")
    def consistent_unit(self):
        expected = {"cold_water": "м³", "hot_water": "м³", "electricity": "кВт·ч", "heating": "Гкал"}[self.type]
        if self.unit and self.unit != expected:
            raise ValueError("Единица измерения не соответствует типу счётчика")
        self.unit = expected
        return self


class Device(Model):
    id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=160)
    kind: str = Field(default="custom", min_length=1, max_length=60)
    room: str = Field(default="Квартира", max_length=160)
    online: bool = True
    capabilities: list[Capability] = Field(min_length=1, max_length=100)
    state: dict[str, Any] = Field(default_factory=dict)
    ujin: UjinMapping = Field(default_factory=UjinMapping)
    location: DeviceLocation = Field(default_factory=DeviceLocation)
    common_access: CommonAccess = Field(default_factory=CommonAccess)
    meter: Meter | None = None
    last_seen: str | None = Field(default=None, max_length=80, json_schema_extra={"readOnly": True})
    last_seen_source: str | None = Field(default=None, max_length=60, json_schema_extra={"readOnly": True})

    @model_validator(mode="after")
    def state_matches_description(self):
        if self.last_seen:
            try:
                observed = datetime.fromisoformat(self.last_seen.replace("Z", "+00:00"))
                if observed.tzinfo is None:
                    raise ValueError
            except ValueError:
                raise ValueError("Время последнего отклика должно быть ISO-датой с часовым поясом") from None
        if not self.last_seen:
            self.last_seen_source = None
        keys = [c.key for c in self.capabilities]
        if len(set(keys)) != len(keys):
            raise ValueError("Ключи возможностей должны быть уникальны")
        if set(self.state) - set(keys):
            raise ValueError("Состояние содержит неизвестные параметры")
        for cap in self.capabilities:
            value = self.state.get(cap.key, cap.default)
            self.state[cap.key] = validate_value(cap.model_dump(), value)
        if set(self.ujin.signal_map) - set(keys):
            raise ValueError("Маппинг Ujin содержит неизвестные параметры")
        cap_by_key = {item.key: item for item in self.capabilities}
        if len(self.common_access.resident_commands) != len(set(self.common_access.resident_commands)):
            raise ValueError("Команды доступа жителей должны быть уникальны")
        for key in self.common_access.resident_commands:
            if key not in cap_by_key or cap_by_key[key].role not in ("command", "property"):
                raise ValueError("Доступ жителям можно разрешить только для существующей команды или свойства")
        if self.meter and (self.kind != "meter" or "total" not in cap_by_key or cap_by_key["total"].type != "number"):
            raise ValueError("Для счётчика необходим числовой параметр total и тип meter")
        return self


class Predicate(Model):
    device_id: str = Field(min_length=1, max_length=100)
    signal: str = Field(min_length=1, max_length=64)
    operator: Literal["eq", "ne", "gt", "gte", "lt", "lte", "changed"] = "eq"
    value: Any = None


class Action(Model):
    type: Literal["command", "notification", "delay"] = "command"
    device_id: str | None = None
    signal: str | None = None
    value: Any = None
    target: Literal["resident", "concierge"] = "resident"
    message: str = Field(default="", max_length=2000)
    seconds: float = Field(default=0, ge=0, le=300)

    @model_validator(mode="after")
    def required_fields(self):
        if self.type == "command" and (not self.device_id or not self.signal):
            raise ValueError("Для команды нужны устройство и параметр")
        if self.type == "notification" and not self.message.strip():
            raise ValueError("Укажите текст уведомления")
        return self


class Rule(Model):
    id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=2000)
    enabled: bool = True
    trigger: Predicate
    conditions: list[Predicate] = Field(default_factory=list, max_length=50)
    condition_mode: Literal["all", "any"] = "all"
    actions: list[Action] = Field(min_length=1, max_length=50)
    cooldown_seconds: float = Field(default=3, ge=0, le=86400)
    on_error: Literal["continue", "stop"] = "continue"
    retries: int = Field(default=1, ge=0, le=3)
    owner_id: str | None = Field(default=None, max_length=100)


class StateUpdate(Model):
    values: dict[str, Any] = Field(min_length=1, max_length=100)
    event_id: str | None = Field(default=None, max_length=120)


class Command(Model):
    signal: str
    value: Any


class OnlineUpdate(Model):
    online: bool


class EmulationUpdate(Model):
    enabled: bool


class Configuration(Model):
    schema_version: Literal[1] = 1
    devices: list[Device] = Field(max_length=500)
    rules: list[Rule] = Field(max_length=500)
    organizations: list[dict] | None = Field(default=None, max_length=1000)
    complexes: list[dict] | None = Field(default=None, max_length=1000)
    buildings: list[dict] | None = Field(default=None, max_length=1000)
