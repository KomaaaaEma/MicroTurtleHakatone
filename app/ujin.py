"""Ujin test-stand adapter. Credentials never leave this server process.

Contract: microturtle/emulators/ujin_client.py (31 Oct 2026 UAE ust mode).
Device publication is two-phase JSON; error=0 is acceptance, not proof of
storage. Read-back verification and a durable latest-state outbox expose that
important distinction. The local rule engine remains independent of Ujin.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from contextlib import nullcontext
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_BASE = "https://api-uae-test.ujin.tech"
GROUP_PATH = "/api/import_device/group-update-device-signal/"
INFO_PATH = "/api/bmsdevice/get-device-info/"
SAFE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9_]{1,150}$")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class IntegrationError(RuntimeError):
    """Messages must be safe for the UI: never include a request URL or token."""


class IntegrationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str = Field(default="dry_run", pattern="^(dry_run|live)$")
    poll_interval: float = Field(default=5.0, ge=2, le=300)


class UjinTransport:
    """Small injectable synchronous transport, suitable for offline contract tests."""

    def __init__(self, base_url: str, token: str, apartment_id: str,
                 opener: Callable | None = None, timeout: float = 8):
        parsed = urllib.parse.urlparse(base_url)
        # Only an operator-controlled environment variable can select another host.
        explicit = os.environ.get("UJIN_BASE_URL", "").rstrip("/").removesuffix("/api")
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise IntegrationError("Адрес Ujin должен быть HTTPS без параметров и учётных данных")
        if parsed.netloc != "api-uae-test.ujin.tech" and base_url != explicit:
            raise IntegrationError("Другой стенд задаётся только переменной сервера UJIN_BASE_URL")
        self.base_url = base_url.rstrip("/").removesuffix("/api")
        self.token = token
        self.bms_token = ""
        self.apartment_id = str(apartment_id)
        self.opener = opener or urllib.request.urlopen
        self.timeout = timeout

    def request(self, path: str, *, serial: str, rows: list | None = None) -> dict:
        query = {"token": (self.bms_token or self.token) if rows is None else self.token,
                 "serialnumber": str(serial)}
        if rows is not None:
            query["apartment_id"] = self.apartment_id
        url = self.base_url + path + "?" + urllib.parse.urlencode(query)
        body = None if rows is None else json.dumps({"data": rows}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="GET" if body is None else "POST")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        return self._send(req)

    def _send(self, req) -> dict:
        try:
            with self.opener(req, timeout=self.timeout) as response:
                result = json.loads(response.read(2_000_001).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise IntegrationError(f"Ujin вернул HTTP {exc.code}") from None
        except (OSError, ValueError, UnicodeError):
            raise IntegrationError("Стенд Ujin недоступен или вернул некорректный JSON") from None
        if not isinstance(result, dict):
            raise IntegrationError("Некорректная структура ответа Ujin")
        if "error" not in result or str(result.get("error")).lower() not in ("0", "false"):
            # Remote messages can reflect credentials; do not relay them.
            raise IntegrationError("Ujin отклонил запрос; проверьте доступ и привязку устройства")
        return result

    def authenticate_employee(self, employee: dict) -> str:
        body = urllib.parse.urlencode({key: employee[key] for key in ("login", "password", "app")
                                       if employee.get(key)}).encode("utf-8")
        request = urllib.request.Request(self.base_url + "/api/auth/authenticate/", data=body, method="POST")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        result = self._send(request)
        data = result.get("data") or {}
        token = data.get("token") if isinstance(data, dict) else None
        if not token and isinstance(data, dict) and isinstance(data.get("user"), dict):
            token = data["user"].get("token")
        token = token or result.get("token")
        if not isinstance(token, str) or not token:
            raise IntegrationError("Вход УК не вернул токен; проверьте выданный аккаунт сотрудника")
        self.bms_token = token
        return token

    def info(self, serial: str) -> dict:
        return self.request(INFO_PATH, serial=serial)

    def publish(self, ext_guid: str, rows: list) -> dict:
        # UAE reference uses ext_guid as serialnumber in the import_device gate.
        return self.request(GROUP_PATH, serial=ext_guid, rows=rows)


def remote_signals(info: dict) -> list[dict]:
    data = info.get("data") or {}
    if not isinstance(data, dict):
        return []
    signals = data.get("signals") or []
    if isinstance(signals, dict):
        return [dict(value, name=key) if isinstance(value, dict) else {"name": key, "value": value}
                for key, value in signals.items()]
    return [row for row in signals if isinstance(row, dict)] if isinstance(signals, list) else []


def signal_value(row: dict) -> Any:
    state = row.get("state") or {}
    if not isinstance(state, dict):
        state = {"value": state}
    return state.get("value", row.get("value", row.get("signal_value")))


def pending_task(row: dict) -> tuple[Any, str] | None:
    state = row.get("state") or {}
    if not isinstance(state, dict):
        state = {}
    value = row.get("signal_task_value", row.get("task", state.get("task", row.get("task_value"))))
    if value is None or value == "":
        return None
    dt = row.get("signal_task_dt") or row.get("task_dt") or state.get("task_dt")
    # Without a task timestamp, name+value is the only stable identity available.
    identity = str(dt) if dt is not None and dt != "" else json.dumps(value, sort_keys=True)
    return value, identity


def to_wire(value: Any) -> Any:
    if isinstance(value, bool):
        return 1 if value else 0.001
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise IntegrationError("Сигнал содержит недопустимое число")
        return 0.001 if value == 0 else value
    return value


def from_wire(value: Any, capability: dict) -> Any:
    kind = capability.get("type")
    if kind == "boolean":
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        try:
            return float(value) >= 0.5
        except (ValueError, TypeError):
            raise IntegrationError("Некорректный логический сигнал Ujin") from None
    if kind == "number":
        try:
            number = float(value)
            if not math.isfinite(number):
                raise ValueError
            # The supplied stand uses 0.001 as a wire sentinel for numerical zero.
            return 0 if number == 0.001 else number
        except (ValueError, TypeError):
            raise IntegrationError("Некорректный числовой сигнал Ujin") from None
    if kind == "string":
        if capability.get("key") == "mode":
            try:
                return {0: "off", 0.001: "off", 1: "heat", 2: "cool", 3: "auto"}.get(float(value), str(value))
            except (TypeError, ValueError):
                pass
        return str(value)
    return value


class UjinAdapter:
    def __init__(self, store, engine, *, transport_factory=None, sleep=time.sleep):
        self.store = store
        self.engine = engine
        self.transport_factory = transport_factory or UjinTransport
        self.sleep = sleep
        self._lock = threading.RLock()
        self._sync_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._connected = False
        self._last_success = None
        self._last_verified = None
        self._last_error = None
        self._last_exchange = None
        self._backoff = 0
        self._bms_token = ""
        self._settings = IntegrationSettings.model_validate(self._read("ujin-settings", {})).model_dump()
        self._outbox = self._read("ujin-outbox", {})
        self._seen = self._read("ujin-seen", {})
        self._initialized = set(self._read("ujin-initialized", {}).get("keys", []))
        self.router = APIRouter(prefix="/api/integration", tags=["Ujin"])
        self._routes()

    def _read(self, key: str, default):
        doc = self.store.get("meta", key)
        if doc is None:
            return default
        return doc.get("value", default)

    def _write(self, key: str, value):
        self.store.save("meta", key, {"id": key, "value": value})

    def _credentials(self) -> tuple[str, str, str, dict]:
        data: dict = {}
        filename = os.environ.get("UJIN_CREDENTIALS")
        if filename:
            try:
                loaded = json.loads(Path(filename).read_text(encoding="utf-8-sig"))
                if not isinstance(loaded, dict):
                    raise ValueError
                data = loaded
            except (OSError, ValueError):
                raise IntegrationError("Не удалось прочитать файл UJIN_CREDENTIALS на сервере") from None
        try:
            index = int(os.environ.get("UJIN_RESIDENT_INDEX", "0"))
            residents = data.get("residents") or []
            resident = residents[index] if residents else {}
            if not isinstance(resident, dict):
                raise TypeError
        except (ValueError, IndexError, TypeError):
            raise IntegrationError("Проверьте UJIN_RESIDENT_INDEX на сервере") from None
        token = os.environ.get("UJIN_UST") or resident.get("ust", "")
        apartment = os.environ.get("UJIN_APARTMENT_ID") or resident.get("apartment_id") or data.get("team_apartment", "")
        base = os.environ.get("UJIN_BASE_URL") or data.get("api_base") or DEFAULT_BASE
        base = str(base).rstrip("/").removesuffix("/api")
        if not token or not apartment:
            raise IntegrationError("Для живого режима задайте UJIN_CREDENTIALS либо UJIN_UST и UJIN_APARTMENT_ID")
        return base, str(token), str(apartment), data

    def _transport(self, authenticate: bool = False):
        base, token, apartment, data = self._credentials()
        client = self.transport_factory(base, token, apartment)
        client.bms_token = os.environ.get("UJIN_BMS_UST") or self._bms_token
        employee = data.get("employee") or {}
        if authenticate and not client.bms_token and isinstance(employee, dict) and employee.get("login") and employee.get("password"):
            # Employee authentication is lazy and the returned session lives in memory only.
            # Resident authorization is retained separately for publication.
            self._bms_token = client.authenticate_employee(employee)
        return client

    def _mapped_devices(self):
        return [device for device in self.store.list("devices")
                if (device.get("ujin") or {}).get("serialnumber") and (device.get("ujin") or {}).get("ext_guid")]

    def summary(self) -> dict:
        with self._lock:
            try:
                base, _, _, _ = self._credentials()
                available = True
            except IntegrationError:
                base, available = DEFAULT_BASE, False
            parsed = urllib.parse.urlparse(base)
            # Summary exposes only the origin, never an arbitrary path/query from config.
            base = (parsed.scheme + "://" + parsed.netloc) if (
                parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password
            ) else DEFAULT_BASE
            mapped = self._mapped_devices()
            return {**self._settings, "connected": self._connected and self._settings["mode"] == "live",
                    "credentials_available": available, "base_url": base,
                    "bms_credentials_available": bool(os.environ.get("UJIN_BMS_UST") or self._bms_token),
                    "mapped_devices": len(mapped), "pending": len(self._outbox),
                    "failed": sum(1 for item in self._outbox.values() if item.get("attempts", 0) >= 5),
                    "last_success": self._last_success, "last_verified": self._last_verified,
                    "last_error": self._last_error, "last_exchange": self._last_exchange,
                    "warning": "Уведомления жителю и консьержу отображаются в Ujin Studio; внешняя доставка не настроена. Команды Ujin доступны только для rw/w-сигналов стенда."}

    def configure(self, payload: dict) -> dict:
        settings = IntegrationSettings.model_validate({**self._settings, **payload}).model_dump()
        if settings["mode"] == "live":
            self._transport()  # reject invalid credentials/base before changing persisted mode
            if not self._mapped_devices():
                raise IntegrationError("Сначала загрузите привязки устройств или задайте SN и ext_guid в конструкторе")
        with self._lock:
            self._settings = settings
            self._write("ujin-settings", settings)
            self._connected = False
            self._last_error = None
        self.engine.log("integration", "Режим Ujin: " + settings["mode"])
        return self.summary()

    def on_state(self, device_id: str, values: dict):
        """Durable latest-state outbox. Called by engine hook; performs no network I/O."""
        device = self.store.get("devices", device_id)
        if not device or not (device.get("ujin") or {}).get("ext_guid"):
            return
        with self._lock:
            previous = self._outbox.get(device_id, {})
            pending = {**previous.get("values", {}), **values}
            self._outbox[device_id] = {"values": pending, "revision": previous.get("revision", 0) + 1,
                                       "token": str(uuid4()), "attempts": 0, "queued_at": utcnow()}
            self._write("ujin-outbox", self._outbox)

    @staticmethod
    def _same_item(current, snapshot):
        if not current:
            return False
        if current.get("token") or snapshot.get("token"):
            return current.get("token") == snapshot.get("token")
        return current.get("revision") == snapshot.get("revision")

    def on_configuration_change(self, old_devices, new_devices):
        """Discard pending writes that belong to a replaced local description.

        Called with the engine lock held. A request already in flight keeps its
        original remote binding; its response cannot mutate a replacement device.
        Seen task IDs remain valid when importing the same remote binding.
        """
        old = old_devices if isinstance(old_devices, dict) else {d["id"]: d for d in old_devices}
        new = new_devices if isinstance(new_devices, dict) else {d["id"]: d for d in new_devices}
        with self._lock:
            for device_id, previous in old.items():
                current = new.get(device_id)
                if current != previous:
                    self._outbox.pop(device_id, None)
                if current is None or current.get("ujin") != previous.get("ujin"):
                    self._seen = {key: value for key, value in self._seen.items() if not key.startswith(device_id + ":")}
                    ext = str((previous.get("ujin") or {}).get("ext_guid", ""))
                    self._initialized = {key for key in self._initialized if not key.startswith(ext + ":")}
            self._write("ujin-outbox", self._outbox)
            self._write("ujin-seen", self._seen)
            self._write("ujin-initialized", {"keys": sorted(self._initialized)})

    @staticmethod
    def _mapping(device: dict) -> tuple[dict, str, str]:
        info = device.get("ujin") or {}
        ext = str(info.get("ext_guid", ""))
        cls = str(info.get("device_class") or "integrations_devices")
        if not SAFE_ID.fullmatch(ext):
            raise IntegrationError("ext_guid должен состоять из букв, цифр и подчёркиваний; дефисы теряются на стенде")
        if not SAFE_NAME.fullmatch(cls):
            raise IntegrationError("Некорректный класс устройства Ujin")
        mapping = info.get("signal_map") or {}
        prefix = cls + "_"
        normalized = {}
        for cap in device.get("capabilities", []):
            local = cap["key"]
            remote = str(mapping.get(local) or local)
            short = remote[len(prefix):] if remote.startswith(prefix) else remote
            if not SAFE_NAME.fullmatch(short):
                raise IntegrationError("Имена сигналов Ujin: латиница, цифры и подчёркивание")
            normalized[local] = prefix + short
        return normalized, ext, cls

    def _rows(self, device: dict, values: dict) -> tuple[list, list]:
        mapping, ext, cls = self._mapping(device)
        rows, new_short = [], []
        for key, value in values.items():
            if key not in mapping:
                continue
            full = mapping[key]
            if device.get("kind") == "thermostat" and key == "mode":
                value = {"off": 0, "heat": 1, "cool": 2, "auto": 3}.get(value, value)
            row = {"sn": ext, "s": full, "v": to_wire(value), "c": cls}
            rows.append(row)
            identity = ext + ":" + full
            if identity not in self._initialized:
                new_short.append({**row, "s": full[len(cls) + 1:]})
        return rows, new_short

    def load_mappings(self) -> dict:
        with getattr(self.engine, "lock", nullcontext()):
            previous = self.store.list("devices")
            result = self._load_mappings()
            self.on_configuration_change(previous, self.store.list("devices"))
            return result

    def _load_mappings(self) -> dict:
        _, _, _, data = self._credentials()
        credentials_devices = data.get("devices") or []
        families = {"leak": "leak-1", "barrier": "barrier-1", "thermostat": "thermostat-1",
                    "counter": "meter-1", "light": "light-1", "fire": "fire-1",
                    "ventilation": "ventilation-1", "elevator": "elevator-1"}
        aliases = {"on": "lamp", "power": "lamp", "enabled": "lamp", "target": "target_temp", "target_temperature": "target_temp",
                   "reading": "total", "consumption": "total", "open": "open_cmd", "is_open": "open_cmd"}
        changed = []
        for remote in credentials_devices:
            if not isinstance(remote, dict):
                continue
            device_id = families.get(remote.get("family"))
            device = self.store.get("devices", device_id) if device_id else None
            if not device:
                continue
            info = {"serialnumber": str(remote.get("sn", "")), "ext_guid": str(remote.get("ext", "")),
                    "model": str(remote.get("model") or "parkcloud_device"),
                    "device_class": str(remote.get("publish_class") or "integrations_devices"),
                    "signal_map": {cap["key"]: aliases.get(cap["key"], cap["key"])
                                   for cap in device.get("capabilities", [])}}
            self._mapping({**device, "ujin": info})
            device["ujin"] = info
            self.store.save("devices", device_id, device)
            changed.append(device_id)
        self.engine.log("integration", "Загружены привязки устройств Ujin", device_ids=changed)
        return {"mapped": changed, "count": len(changed), "integration": self.summary()}

    def _failure(self, message: str):
        self._connected = False
        self._last_error = message
        # A cached employee session can expire; retry lazy authentication on the
        # next scheduled exchange instead of keeping an invalid session forever.
        self._bms_token = ""
        self._backoff = min(60, max(2, self._backoff * 2))
        self.engine.log("integration_error", message)

    def check(self) -> dict:
        try:
            client = self._transport(authenticate=True)
            devices = self._mapped_devices()
            if not devices:
                raise IntegrationError("Нет привязанных устройств; загрузите привязки")
            info = client.info(str(devices[0]["ujin"]["serialnumber"]))
            if not remote_signals(info):
                raise IntegrationError("Стенд ответил, но не вернул сигналы привязанного устройства")
            self._connected = self._settings["mode"] == "live"
            self._last_success = utcnow()
            self._last_error = None
            return {"ok": True, "read_only": True, "device_id": devices[0]["id"], "integration": self.summary()}
        except IntegrationError as exc:
            self._failure(str(exc))
            raise

    def _apply_remote(self, device: dict, info: dict, result: dict):
        with getattr(self.engine, "lock", nullcontext()):
            current = self.store.get("devices", device["id"])
            if current is None or current.get("ujin") != device.get("ujin") or current.get("capabilities") != device.get("capabilities"):
                return {}
            return self._apply_remote_snapshot(device, info, result)

    def _apply_remote_snapshot(self, device: dict, info: dict, result: dict):
        mapping, _, _ = self._mapping(device)
        reverse = {remote: local for local, remote in mapping.items()}
        # Some BMS responses use short signal names instead of prefixed names.
        reverse.update({remote.split(device["ujin"].get("device_class", "integrations_devices") + "_", 1)[-1]: local
                        for local, remote in mapping.items()})
        caps = {cap["key"]: cap for cap in device.get("capabilities", [])}
        updates = {}
        remote_values = {}
        for row in remote_signals(info):
            name = str(row.get("name") or row.get("signal_name") or "")
            local = reverse.get(name)
            if not local:
                continue
            cap = caps[local]
            task = pending_task(row)
            task_applied = False
            if task is not None and cap.get("role") in ("command", "property"):
                value, identity = task
                task_key = device["id"] + ":" + name
                if self._seen.get(task_key) != identity:
                    parsed = from_wire(value, cap)
                    try:
                        self.engine.command(device["id"], local, parsed, source="ujin-command")
                    except (ValueError, KeyError):
                        raise IntegrationError("Команда Ujin не исполнена: устройство недоступно или значение не соответствует описанию") from None
                    self._seen[task_key] = identity
                    self._write("ujin-seen", self._seen)
                    result["commands"] += 1
                    task_applied = True
            raw = signal_value(row)
            if raw is None:
                continue
            value = from_wire(raw, cap)
            remote_values[local] = value
            with self._lock:
                pending = self._outbox.get(device["id"], {}).get("values", {})
                # Do not let stale read-back overwrite a queued local state/command.
                waiting = local in pending and not self._equivalent(raw, pending[local], cap)
            if not waiting and not task_applied:
                updates[local] = value
        if updates:
            try:
                self.engine.update_state(device["id"], updates, source="ujin")
            except (ValueError, KeyError):
                raise IntegrationError("Состояние Ujin не применено: устройство недоступно или значение не соответствует описанию") from None
            result["pulled"] += len(updates)
        return remote_values

    @staticmethod
    def _equivalent(raw: Any, local: Any, cap: dict) -> bool:
        if cap.get("type") == "number":
            try:
                return math.isclose(float(raw), float(to_wire(local)), rel_tol=1e-7, abs_tol=1e-7)
            except (TypeError, ValueError):
                return False
        try:
            return from_wire(raw, cap) == local
        except IntegrationError:
            return False

    def sync(self, force: bool = True) -> dict:
        if not self._sync_lock.acquire(blocking=False):
            return {"busy": True, "integration": self.summary()}
        try:
            result = {"mode": self._settings["mode"], "published": 0, "verified": 0,
                      "pulled": 0, "commands": 0, "dry_run": self._settings["mode"] == "dry_run"}
            devices = self._mapped_devices()
            if force:
                for device in devices:
                    self.on_state(device["id"], device.get("state", {}))
            client = self._transport(authenticate=True) if self._settings["mode"] == "live" else None
            errors = []
            for device in devices:
                if self._stop.is_set():
                    result["stopped"] = True
                    break
                device_id = device["id"]
                if not device.get("online", True):
                    errors.append("Обмен отложен: одно из привязанных устройств недоступно")
                    continue
                with self._lock:
                    item = json.loads(json.dumps(self._outbox.get(device_id, {})))
                if item and item.get("attempts", 0) < 5:
                    try:
                        rows, short = self._rows(device, item.get("values", {}))
                        if client:
                            if short:
                                client.publish(device["ujin"]["ext_guid"], short)
                                if self._stop.is_set():
                                    result["stopped"] = True
                                    break
                                if self.sleep is time.sleep:
                                    self._stop.wait(2)
                                else:
                                    self.sleep(2)
                            if self._stop.is_set():
                                result["stopped"] = True
                                break
                            client.publish(device["ujin"]["ext_guid"], rows)
                        with self._lock:
                            self._last_exchange = {"at": utcnow(), "device_id": device_id,
                                                   "phase": "accepted" if client else "dry_run",
                                                   "rows": rows, "new_signals": len(short)}
                            if client is None and self._same_item(self._outbox.get(device_id), item):
                                del self._outbox[device_id]
                            self._write("ujin-outbox", self._outbox)
                        result["published"] += len(rows)
                    except IntegrationError as exc:
                        errors.append(str(exc))
                if client:
                    if self._stop.is_set():
                        result["stopped"] = True
                        break
                    try:
                        info = client.info(str(device["ujin"]["serialnumber"]))
                        caps = {cap["key"]: cap for cap in device.get("capabilities", [])}
                        mapping, _, _ = self._mapping(device)
                        by_remote = {str(row.get("name") or row.get("signal_name")): signal_value(row)
                                     for row in remote_signals(info)}
                        prefix = (device["ujin"].get("device_class") or "integrations_devices") + "_"
                        by_remote.update({prefix + name: value for name, value in list(by_remote.items())
                                          if not name.startswith(prefix)})
                        with self._lock:
                            for name in by_remote:
                                if name in mapping.values():
                                    self._initialized.add(device["ujin"]["ext_guid"] + ":" + name)
                            self._write("ujin-initialized", {"keys": sorted(self._initialized)})
                        # Verify accepted rows using raw transport values before ingesting.
                        if item:
                            verified = all(mapping.get(key) in by_remote and
                                           self._equivalent(by_remote[mapping[key]], value, caps[key])
                                           for key, value in item.get("values", {}).items() if key in mapping)
                            with self._lock:
                                current = self._outbox.get(device_id)
                                if self._same_item(current, item):
                                    if verified:
                                        del self._outbox[device_id]
                                        result["verified"] += len(item.get("values", {}))
                                        self._last_verified = utcnow()
                                    else:
                                        current["attempts"] = current.get("attempts", 0) + 1
                                        current["last_error"] = "Принято API, но чтение не подтвердило значения"
                                    self._write("ujin-outbox", self._outbox)
                        self._apply_remote(device, info, result)
                        self._last_success = utcnow()
                        self._connected = True
                    except IntegrationError as exc:
                        errors.append(str(exc))
                        with self._lock:
                            current = self._outbox.get(device_id)
                            if self._same_item(current, item):
                                current["attempts"] = current.get("attempts", 0) + 1
                                self._write("ujin-outbox", self._outbox)
            if errors:
                self._failure(errors[0])
                result["errors"] = list(dict.fromkeys(errors))
            else:
                self._last_error = None
                self._backoff = 0
            if result["published"] or result["commands"]:
                self.engine.log("integration", "Обмен с Ujin: " + ("сухой прогон" if client is None else "живой стенд"),
                                published=result["published"], verified=result["verified"], commands=result["commands"])
            result["integration"] = self.summary()
            return result
        finally:
            self._sync_lock.release()

    def _worker(self):
        while not self._stop.wait(max(self._settings["poll_interval"], self._backoff)):
            if self._settings["mode"] != "live":
                continue
            try:
                self.sync(force=False)
            except Exception:
                # The worker must survive validation/device errors and network outages.
                self._failure("Обмен с Ujin прерван; проверьте привязки и доступность устройств")

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._worker, name="ujin-poll", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            # Every individual request has an 8s socket timeout and the loop
            # checks cancellation between requests. Do not close SQLite while
            # a normal in-flight exchange is still completing.
            self._thread.join(timeout=12)
        return self._thread is None or not self._thread.is_alive()

    def _routes(self):
        @self.router.get("")
        def status():
            return self.summary()

        @self.router.post("")
        def settings(payload: IntegrationSettings):
            try:
                return self.configure(payload.model_dump())
            except IntegrationError as exc:
                raise HTTPException(400, str(exc)) from None

        @self.router.post("/check")
        def check():
            try:
                return self.check()
            except IntegrationError as exc:
                raise HTTPException(502, str(exc)) from None

        @self.router.post("/sync")
        def sync():
            try:
                return self.sync(force=True)
            except IntegrationError as exc:
                raise HTTPException(502, str(exc)) from None

        @self.router.post("/load-mappings")
        def mappings():
            try:
                return self.load_mappings()
            except IntegrationError as exc:
                raise HTTPException(400, str(exc)) from None
