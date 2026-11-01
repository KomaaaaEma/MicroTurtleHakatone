"""Scoped meter histories with explicit demo provenance and manual readings."""
from __future__ import annotations

from datetime import datetime, timezone
import math
import time
from typing import Any

from fastapi import HTTPException
from pydantic import Field

from .models import Model

DAY = 86400
UNITS = {"cold_water": "м³", "hot_water": "м³", "electricity": "кВт·ч", "heating": "Гкал"}


def iso_at(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="milliseconds")


def unix_at(timestamp):
    parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Время показания должно содержать часовой пояс")
    return parsed.timestamp()


def record_state_reading(store, device, source, *, timestamp=None):
    """Called only for an observed change, never to make a stale value look fresh."""
    if device.get("kind") != "meter" or not device.get("meter"):
        return None
    value = device.get("state", {}).get("total")
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return None
    return store.append("meter_readings", {
        "device_id": device["id"], "timestamp": timestamp or iso_at(time.time()), "value": value,
        "unit": device["meter"]["unit"], "source": source,
        "synthetic": source in ("demo", "emulator", "reset"),
        "location": dict(device.get("location") or {}),
    })


class ManualReading(Model):
    value: float = Field(strict=True, ge=0, le=1e12)
    timestamp: str | None = Field(default=None, max_length=80)
    request_id: str | None = Field(default=None, max_length=120)


class MeterService:
    def __init__(self, store, properties, engine=None, *, clock=time.time):
        self.store = store
        self.properties = properties
        self.engine = engine
        self.clock = clock

    def _history(self, device_id):
        return sorted((item for item in self.store.list("meter_readings") if item.get("device_id") == device_id),
                      key=lambda item: (item["timestamp"], item.get("id", "")))

    def _ensure_demo_history(self, device):
        marker = "meter-demo:" + device["id"]
        if self.store.get("meta", marker) or self._history(device["id"]):
            return
        if (device.get("ujin") or {}).get("serialnumber"):
            # A bound real device needs an observed update, not fabricated data.
            return
        metadata = device.get("meter")
        if not metadata:
            return
        current = device.get("state", {}).get("total")
        if type(current) not in (int, float) or not math.isfinite(current) or current < 0:
            return
        increment = {"cold_water": .22, "hot_water": .15, "electricity": 6.8, "heating": .02}[metadata["type"]]
        now = self.clock()
        for days_back in range(6, -1, -1):
            self.store.append("meter_readings", {
                "device_id": device["id"], "timestamp": iso_at(now - DAY * days_back),
                "value": round(max(0, current - increment * days_back), 6), "unit": metadata["unit"],
                "source": "demo", "synthetic": True, "location": dict(device.get("location") or {}),
            })
        self.store.save("meta", marker, {"synthetic": True, "seeded_at": iso_at(now)})

    def _visible_history(self, device, user):
        rows = self._history(device["id"])
        if user.get("role") != "admin":
            rows = [row for row in rows if self.properties.can_read(user, {"location": row.get("location") or {}, "common_access": device.get("common_access") or {}})]
        return rows[-400:]

    def _meter(self, device, user):
        self._ensure_demo_history(device)
        history = self._visible_history(device, user)
        metadata = device["meter"]
        now = self.clock()
        last = history[-1] if history else None
        age = max(0, (now - unix_at(last["timestamp"])) / 3600) if last else None
        freshness = {"status": "no_data" if last is None else "stale" if age > metadata["stale_after_hours"] else "fresh",
                     "age_hours": round(age, 2) if age is not None else None, "stale_after_hours": metadata["stale_after_hours"]}
        anomalies = []
        if freshness["status"] == "stale":
            anomalies.append({"code": "stale", "severity": "warning", "message": "Показания устарели. Требуется новое показание"})
        if len(history) >= 2:
            previous, latest = history[-2:]
            delta = latest["value"] - previous["value"]
            if delta < 0:
                anomalies.append({"code": "counter_decreased", "severity": "critical", "message": "Показание уменьшилось. Проверьте замену или сброс счётчика"})
            elif delta > metadata["max_daily_usage"]:
                # A large increment is only alarming when the measured interval
                # is no longer than a day; do not compare a monthly bill to a day.
                elapsed = unix_at(latest["timestamp"]) - unix_at(previous["timestamp"])
                if 0 <= elapsed <= DAY:
                    anomalies.append({"code": "usage_jump", "severity": "warning", "message": "Резкий рост расхода за сутки или меньший интервал"})
        return {"id": device["id"], "device_id": device["id"], "name": device["name"],
                "type": metadata["type"], "unit": metadata["unit"], "value": last["value"] if last else device["state"].get("total"),
                "manual_allowed": bool(metadata["manual_allowed"] and (user.get("role") == "admin" or device["location"]["scope"] == "apartment")),
                "location": device["location"], "location_label": self.properties.location_label(device),
                "common": device["location"]["scope"] == "common", "online": device["online"],
                "readings": history, "last_reading_at": last["timestamp"] if last else None,
                "freshness": freshness, "anomalies": anomalies,
                "history_synthetic": bool(history and all(row.get("synthetic") for row in history)),
                "max_daily_usage": metadata["max_daily_usage"]}

    def list(self, user, filters=None):
        if not user:
            raise HTTPException(401, "Войдите в приложение")
        filters = dict(filters or {})
        if user.get("role") == "resident" and not filters.get("scope"):
            filters["scope"] = "apartment"
        devices = self.properties.filter_devices(self.store.list("devices"), user, filters)
        return {"meters": [self._meter(device, user) for device in devices if device.get("kind") == "meter" and device.get("meter")]}

    def add_reading(self, device_id, payload, user):
        if not user:
            raise HTTPException(401, "Войдите в приложение")
        reading = ManualReading.model_validate(payload)
        device = self.store.get("devices", device_id)
        if not device or not device.get("meter") or not self.properties.can_read(user, device):
            raise HTTPException(404, "Счётчик не найден")
        if not device["meter"]["manual_allowed"] or (user.get("role") != "admin" and not self.properties.can_access(user, device)):
            raise HTTPException(403, "Ручной ввод для этого счётчика недоступен")
        now = self.clock()
        timestamp = unix_at(reading.timestamp) if reading.timestamp else now
        if timestamp > now + 300 or timestamp < now - 31 * DAY:
            raise ValueError("Дата показания должна быть в пределах последнего месяца и не в будущем")
        lock = self.engine.lock if self.engine else self.store.lock
        with lock:
            self._ensure_demo_history(device)
            if reading.request_id and self.store.get("meter_receipts", device_id + ":" + reading.request_id):
                return self._meter(device, user)
            history = self._history(device_id)
            if history and timestamp < unix_at(history[-1]["timestamp"]):
                raise ValueError("Новое показание не может быть старше последнего сохранённого")
            baseline = history[-1]["value"] if history else device["state"].get("total", 0)
            if reading.value < baseline:
                raise ValueError("Новое показание не может быть меньше предыдущего")
            # The command/event engine preserves its publish hook and validation.
            # 'manual-reading' rows are recorded once here so a custom timestamp
            # does not get replaced by the current emulator time.
            if self.engine:
                self.engine.update_state(device_id, {"total": reading.value}, source="manual-reading")
            else:
                device["state"]["total"] = reading.value
                self.store.save("devices", device_id, device)
            device = self.store.get("devices", device_id)
            record_state_reading(self.store, device, "manual", timestamp=iso_at(timestamp))
            if reading.request_id:
                self.store.save("meter_receipts", device_id + ":" + reading.request_id, {"at": iso_at(now)})
                self.store.prune("meter_receipts", 5000)
            self.store.log("meter", "Сохранено ручное показание: " + device["name"], device_id=device_id,
                           location=device["location"], value=reading.value, source="manual")
            return self._meter(device, user)
