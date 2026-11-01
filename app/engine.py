"""Deterministic event-condition-action engine with durable traces and bounded cascades."""
from __future__ import annotations

from collections import deque
from copy import deepcopy
from datetime import datetime
import threading
import time
from uuid import uuid4

from .models import Device, Rule, validate_value
from .seed import TEMPLATES
from .store import now
from .scenarios import ScenarioStorage, timestamp


class NotFound(ValueError):
    pass


class DeviceOffline(ValueError):
    pass


class Engine:
    MAX_CASCADE_EVENTS = 64

    def __init__(self, store, *, clock=time.time):
        self.store = store
        self.clock = clock
        self.scenarios = ScenarioStorage(store, clock=clock)
        self.lock = threading.RLock()
        self.publish_hook = None
        self.integration_summary = lambda: {"mode": "demo", "connected": False}

    def log(self, kind, message, **extra):
        if extra.get("device_id") and "location" not in extra:
            device = self.store.get("devices", extra["device_id"])
            if device and device.get("location"):
                extra["location"] = deepcopy(device["location"])
        return self.store.log(kind, message, **extra)

    def device(self, device_id):
        device = self.store.get("devices", device_id)
        if device is None:
            raise NotFound("Устройство не найдено")
        return device

    def capability(self, device_id, signal, devices=None):
        device = devices.get(device_id) if devices is not None else self.device(device_id)
        if device is None:
            raise ValueError(f"Устройство {device_id} не существует")
        cap = next((c for c in device["capabilities"] if c["key"] == signal), None)
        if cap is None:
            raise ValueError(f"Устройство {device['name']}: неизвестный параметр {signal}")
        return device, cap

    def validate_rule(self, rule, devices=None):
        item = Rule.model_validate(rule).model_dump()
        for p in [item["trigger"], *item["conditions"]]:
            _, cap = self.capability(p["device_id"], p["signal"], devices)
            if p["operator"] != "changed":
                validate_value(cap, p["value"])
            if p["operator"] in ("gt", "gte", "lt", "lte") and cap["type"] != "number":
                raise ValueError("Сравнения больше/меньше доступны только для чисел")
        for action in item["actions"]:
            if action["type"] == "command":
                _, cap = self.capability(action["device_id"], action["signal"], devices)
                if cap["role"] not in ("command", "property"):
                    raise ValueError(f"{cap['label']}: параметр доступен только для чтения")
                validate_value(cap, action["value"])
        return item

    def save_device(self, data, expected_id=None):
        item = Device.model_validate(data).model_dump()
        if expected_id is not None and item["id"] != expected_id:
            raise ValueError("Нельзя изменить ID устройства")
        with self.lock:
            previous = self.store.get("devices", item["id"]) if expected_id is not None else None
            item["last_seen"] = previous.get("last_seen") if previous else None
            item["last_seen_source"] = previous.get("last_seen_source") if previous else None
            devices = {d["id"]: d for d in self.store.list("devices")}
            devices[item["id"]] = item
            for rule in self.store.list("rules"):
                self.validate_rule(rule, devices)
            self.store.save("devices", item["id"], item)
            self.log("device", f"Сохранено устройство: {item['name']}", device_id=item["id"])
        return item

    def save_rule(self, data, expected_id=None):
        with self.lock:
            item = self.validate_rule(data)
            if expected_id is not None and item["id"] != expected_id:
                raise ValueError("Нельзя изменить ID сценария")
            self.store.save("rules", item["id"], item)
            self.scenarios.record_version(item)
            if not item["enabled"]:
                self.cancel_rule_jobs(item["id"], automatic_only=True)
            self.log("rule", f"Сохранён сценарий: {item['name']}", rule_id=item["id"])
            return item

    def rule_versions(self, rule_id):
        with self.lock:
            rule = self.store.get("rules", rule_id)
            if rule is None:
                raise NotFound("Сценарий не найден")
            if not self.scenarios.versions(rule_id):
                self.scenarios.record_version(rule)
            return self.scenarios.versions(rule_id)

    def rollback_rule(self, rule_id, version_id):
        with self.lock:
            current = self.store.get("rules", rule_id)
            if current is None:
                raise NotFound("Сценарий не найден")
            version = self.scenarios.version(rule_id, version_id)
            item = deepcopy(version["rule"])
            item["id"] = rule_id
            item["owner_id"] = current.get("owner_id")
            item = self.validate_rule(item)
            self.store.save("rules", rule_id, item)
            self.scenarios.record_version(item, restored_from=version_id)
            if not item["enabled"]:
                self.cancel_rule_jobs(rule_id, automatic_only=True)
            self.log("rule", "Восстановлена версия сценария: " + item["name"], rule_id=rule_id, version_id=version_id)
            return item

    def cancel_rule_jobs(self, rule_id, automatic_only=False, reason="Сценарий выключен или удалён"):
        with self.lock:
            for job in self.scenarios.pending(rule_id):
                run = self.store.get("runs", job["run_id"])
                if automatic_only and run and run.get("manual"):
                    continue
                self.scenarios.finish(job, "cancelled")
                if run and run["status"] in ("waiting", "running"):
                    run.update(status="cancelled", reason=reason)
                    self.store.save("runs", run["id"], run)
            return {"ok": True}

    def delete_device(self, device_id):
        with self.lock:
            self.device(device_id)
            references = [r["name"] for r in self.store.list("rules") if any(x.get("device_id") == device_id for x in [r["trigger"], *r["conditions"], *r["actions"]])]
            if references:
                raise ValueError("Устройство используется сценариями: " + ", ".join(references))
            self.store.delete("devices", device_id)
            self.log("device", "Устройство удалено", device_id=device_id)

    @staticmethod
    def compare(operator, current, target, previous=None):
        if operator == "changed":
            return current != previous
        if operator == "eq":
            return current == target
        if operator == "ne":
            return current != target
        if current is None or target is None:
            return False
        return {"gt": lambda: current > target, "gte": lambda: current >= target, "lt": lambda: current < target, "lte": lambda: current <= target}[operator]()

    def _matches(self, p, value, previous=None):
        return self.compare(p["operator"], value, p["value"], previous)

    def _record_changes(self, device_id, values, source, trace_id, queue, event_id=None):
        device = self.device(device_id)
        if not device["online"] and source != "manual-reading":
            raise DeviceOffline(f"{device['name']}: устройство недоступно")
        for key, value in values.items():
            _, cap = self.capability(device_id, key)
            validate_value(cap, value)
        # Receipt time is server-owned. An unchanged report is still an actual
        # receipt, but human meter entry is not proof of a device connection.
        if source != "manual-reading":
            device["last_seen"] = timestamp(self.clock())
            device["last_seen_source"] = source
            self.store.save("devices", device_id, device)
        changed = {k: v for k, v in values.items() if device["state"].get(k) != v}
        old = deepcopy(device["state"])
        if changed:
            device["state"].update(changed)
            self.store.save("devices", device_id, device)
            if "total" in changed and source != "manual-reading":
                from .meters import record_state_reading
                record_state_reading(self.store, device, source)
            for i, (key, value) in enumerate(changed.items()):
                item = self.store.append("events", {"id": f"{event_id}:{key}" if event_id else str(uuid4()), "kind": "state", "message": f"{device['name']}: {key} → {value}", "device_id": device_id, "location": deepcopy(device.get("location", {})), "signal": key, "previous": old.get(key), "value": value, "source": source, "trace_id": trace_id})
                queue.append(item)
            if self.publish_hook and source != "ujin":
                try:
                    self.publish_hook(device, changed)
                except Exception as exc:
                    self.log("integration_error", "Не удалось поставить состояние в очередь Ujin", device_id=device_id, error=type(exc).__name__)
        return device

    def update_state(self, device_id, values, source="emulator", event_id=None):
        with self.lock:
            # Receipt survives log rotation; bounded 5000 idempotency markers.
            if event_id and self.store.get("receipts", event_id):
                return {"device": self.device(device_id), "duplicate": True, "runs": []}
            queue = deque()
            trace_id = str(uuid4())
            device = self._record_changes(device_id, values, source, trace_id, queue, event_id)
            if event_id:
                self.store.save("receipts", event_id, {"timestamp": now()})
                self.store.prune("receipts", 5000)
            runs = self._drain(queue, trace_id)
            return {"device": self.device(device_id), "duplicate": False, "trace_id": trace_id, "runs": runs}

    def command(self, device_id, signal, value, source="manual"):
        with self.lock:
            device, cap = self.capability(device_id, signal)
            if cap["role"] not in ("command", "property"):
                raise ValueError("Параметр доступен только для чтения")
            validate_value(cap, value)
            if not device["online"]:
                self.log("error", f"Команда не выполнена: {device['name']} недоступен", device_id=device_id)
                raise DeviceOffline(f"{device['name']}: устройство недоступно")
            if device["kind"] == "barrier" and signal == "open" and value is False and device["state"].get("obstacle"):
                raise ValueError("Шлагбаум не может закрыться: обнаружено препятствие")
            self.log("command", f"Команда: {device['name']} / {cap['label']}", device_id=device_id, signal=signal, value=value, source=source)
            result = self.update_state(device_id, {signal: value}, source=source)
            # Repeat commands still need acknowledgement, even with unchanged state.
            if self.publish_hook and source == "ujin-command" and device["state"].get(signal) == value:
                self.publish_hook(self.device(device_id), {signal: value})
            result["command_status"] = {"stage": "emulator_confirmed", "platform_confirmation": "pending" if device.get("ujin", {}).get("serialnumber") else "not_linked"}
            return result

    def _conditions(self, rule, event):
        checks = []
        for p in rule["conditions"]:
            d = self.device(p["device_id"])
            if not d["online"]:
                checks.append(False)
                continue
            previous = event.get("previous") if event and event.get("device_id") == p["device_id"] and event.get("signal") == p["signal"] else d["state"].get(p["signal"])
            checks.append(self._matches(p, d["state"].get(p["signal"]), previous))
        return True if not checks else (all(checks) if rule["condition_mode"] == "all" else any(checks))

    def _cooldown(self, rule):
        for run in self.store.list("runs"):
            if run["rule_id"] == rule["id"] and run["status"] not in ("skipped",):
                return self.clock() - datetime.fromisoformat(run["timestamp"]).timestamp() < rule["cooldown_seconds"]
        return False

    def _execute(self, rule, event, trace_id, queue, manual=False):
        trigger_device = self.device(rule["trigger"]["device_id"])
        run = {"id": str(uuid4()), "timestamp": timestamp(self.clock()), "rule_id": rule["id"], "rule_name": rule["name"], "trace_id": trace_id, "event_id": event.get("id") if event else None, "location": deepcopy(trigger_device.get("location", {})), "owner_id": rule.get("owner_id"), "status": "running", "steps": [], "manual": manual}
        self.store.save("runs", run["id"], run)
        if not self._conditions(rule, event):
            run.update(status="skipped", reason="Условия не выполнены", duration_ms=0)
            self.store.save("runs", run["id"], run)
            return run
        return self._continue_run(rule, event, trace_id, queue, run)

    def _continue_run(self, rule, event, trace_id, queue, run, start_index=0):
        errors = sum(step["status"] == "error" for step in run["steps"])
        for index, action in enumerate(rule["actions"]):
            if index < start_index:
                continue
            existing = next((step for step in run["steps"] if step["index"] == index), None)
            if existing:
                if existing["status"] == "waiting":
                    # Its own durable job owns the continuation. A recovered old
                    # job must not schedule or execute this delay a second time.
                    run["status"] = "waiting"
                    self.store.save("runs", run["id"], run)
                    return run
                if existing["status"] in ("success", "error"):
                    continue
            step = {"index": index, "type": action["type"], "status": "pending", "timestamp": now(), "action": action, "attempts": 0}
            if action["type"] == "delay" and action.get("seconds", 0) > 0:
                step.update(status="waiting", attempts=1, message=f"Ожидание {action['seconds']:g} с", due_at=self.clock() + action["seconds"])
                run["steps"].append(step)
                run.update(status="waiting", resume_at=timestamp(step["due_at"]))
                self.scenarios.schedule(run, rule, event, index, action["seconds"])
                return run
            for attempt in range(rule["retries"] + 1):
                step["attempts"] = attempt + 1
                try:
                    if action["type"] == "notification":
                        self.store.append("notifications", {"id": f"{run['id']}:{index}", "target": action["target"], "message": action["message"], "read": False, "delivery": "local", "rule_id": rule["id"], "run_id": run["id"], "trace_id": trace_id, "location": deepcopy(run["location"]), "owner_id": rule.get("owner_id")})
                        step["message"] = "Оповещение в интерфейсе: " + ("консьерж" if action["target"] == "concierge" else "житель")
                    elif action["type"] == "delay":
                        step["message"] = "Нулевая задержка — выполнено сразу"
                    else:
                        device, cap = self.capability(action["device_id"], action["signal"])
                        if cap["role"] not in ("command", "property"):
                            raise ValueError("Параметр доступен только для чтения")
                        if device["kind"] == "barrier" and action["signal"] == "open" and action["value"] is False and device["state"].get("obstacle"):
                            raise ValueError("Закрытие заблокировано: препятствие")
                        self._record_changes(device["id"], {action["signal"]: action["value"]}, "scenario", trace_id, queue)
                        step["message"] = f"{device['name']}: {cap['label']} → {action['value']}"
                        step["confirmation"] = "emulator_confirmed"
                        step["platform_confirmation"] = "pending" if device.get("ujin", {}).get("serialnumber") else "not_linked"
                    step["status"] = "success"
                    break
                except (ValueError, KeyError) as exc:
                    step.update(status="error", message=str(exc))
                    # Offline is retried within this bounded run; transport retries
                    # have their own durable Ujin outbox and exponential backoff.
            if step["status"] == "error":
                errors += 1
                self.log("error", step["message"], rule_id=rule["id"], trace_id=trace_id)
            run["steps"].append(step)
            self.store.save("runs", run["id"], run)
            if errors and rule["on_error"] == "stop":
                break
        run.update(status="error" if errors else "success", duration_ms=round((self.clock() - datetime.fromisoformat(run["timestamp"]).timestamp()) * 1000, 2))
        run.pop("resume_at", None)
        self.store.save("runs", run["id"], run)
        self.log("scenario", f"{rule['name']}: " + ("выполнен с ошибками" if errors else "выполнен"), rule_id=rule["id"], run_id=run["id"], trace_id=trace_id, status=run["status"])
        return run

    def process_due_jobs(self, limit=20):
        completed = []
        with self.lock:
            for job in self.scenarios.due(limit):
                run = self.store.get("runs", job["run_id"])
                current_rule = self.store.get("rules", job["rule_id"])
                if not run or run["status"] in ("success", "error", "cancelled", "skipped"):
                    self.scenarios.finish(job)
                    continue
                if not current_rule or (not current_rule["enabled"] and not run.get("manual")):
                    self.cancel_rule_jobs(job["rule_id"])
                    continue
                owner = self.store.get("users", run.get("owner_id")) if run.get("owner_id") else None
                if run.get("owner_id") and (owner is None or owner.get("active") is False):
                    self.cancel_rule_jobs(job["rule_id"], reason="Учётная запись владельца сценария недоступна")
                    continue
                if owner and owner.get("role") == "resident":
                    from .properties import PropertyService
                    properties = PropertyService(self.store)
                    refs = [job["rule"]["trigger"], *job["rule"]["conditions"], *[action for action in job["rule"]["actions"] if action["type"] == "command"]]
                    if any(not properties.can_access(owner, self.device(ref["device_id"])) for ref in refs):
                        self.cancel_rule_jobs(job["rule_id"])
                        continue
                job["state"] = "running"
                self.store.save("scenario_jobs", job["id"], job)
                for step in run["steps"]:
                    if step["index"] == job["delay_index"]:
                        step.update(status="success", message="Задержка завершена")
                run["status"] = "running"
                self.store.save("runs", run["id"], run)
                queue = deque()
                self._continue_run(job["rule"], job.get("event"), job["trace_id"], queue, run, job["next_index"])
                self._drain(queue, job["trace_id"], {job["rule_id"]})
                self.scenarios.finish(job)
                completed.append(run)
        return completed

    def _drain(self, queue, trace_id, executed=None):
        executed = executed or set()
        runs = []
        count = 0
        while queue and count < self.MAX_CASCADE_EVENTS:
            event = queue.popleft()
            count += 1
            for rule in self.store.list("rules"):
                p = rule["trigger"]
                if not rule["enabled"] or rule["id"] in executed or p["device_id"] != event["device_id"] or p["signal"] != event["signal"]:
                    continue
                if not self._matches(p, event["value"], event.get("previous")) or self._cooldown(rule):
                    continue
                executed.add(rule["id"])
                runs.append(self._execute(rule, event, trace_id, queue))
        if queue:
            self.log("error", "Каскад остановлен: превышен лимит событий", trace_id=trace_id)
        return runs

    def run_rule(self, rule_id):
        with self.lock:
            rule = self.store.get("rules", rule_id)
            if rule is None:
                raise NotFound("Сценарий не найден")
            queue = deque()
            trace_id = str(uuid4())
            # Explicit manual run bypasses enable/cooldown/trigger, still checks conditions.
            run = self._execute(rule, None, trace_id, queue, manual=True)
            self._drain(queue, trace_id, {rule_id})
            return run

    def reset_states(self):
        with self.lock:
            for device in self.store.list("devices"):
                device["state"] = {c["key"]: deepcopy(c["default"]) for c in device["capabilities"]}
                device["online"] = True
                self.store.save("devices", device["id"], device)
                if self.publish_hook:
                    try:
                        self.publish_hook(device, device["state"])
                    except Exception as exc:
                        self.log("integration_error", "Не удалось поставить сброс эмулятора в очередь Ujin", device_id=device["id"], error=type(exc).__name__)
            self.log("system", "Эмуляторы возвращены в исходное состояние")

    def demo(self, name):
        if name == "reset":
            self.reset_states()
            return {"ok": True}
        mapping = {"leak": ("leak-1", "leak", True, False), "fire": ("fire-1", "smoke", True, False), "climate": ("thermostat-1", "temperature", 28, 23.4)}
        if name not in mapping:
            raise NotFound("Демонстрация не найдена")
        device_id, key, value, initial = mapping[name]
        with self.lock:
            device = self.device(device_id)
            device["state"][key] = initial
            self.store.save("devices", device_id, device)
            return self.update_state(device_id, {key: value}, source="demo")

    def tick(self, dt=2.0):
        """Simple physical models; only enabled by the user, never random alerts."""
        with self.lock:
            for device in self.store.list("devices"):
                if not device["online"]:
                    continue
                s = device["state"]
                values = {}
                if device["kind"] == "thermostat" and all(k in s for k in ("temperature", "target_temp", "mode")):
                    temp, target = s["temperature"], s["target_temp"]
                    mode = s["mode"]
                    delta = min(abs(temp - target), 0.1 * dt)
                    if mode in ("auto", "cool") and temp > target:
                        temp -= delta
                    elif mode in ("auto", "heat") and temp < target:
                        temp += delta
                    values["temperature"] = round(temp, 2)
                elif device["kind"] == "meter" and all(k in s for k in ("total", "flow")):
                    valves = [d for d in self.store.list("devices") if d["kind"] == "valve" and d["room"] == device["room"]
                              and all((d.get("location") or {}).get(key) == (device.get("location") or {}).get(key)
                                      for key in ("building_id", "entrance", "apartment", "scope"))]
                    default_flow = next(c["default"] for c in device["capabilities"] if c["key"] == "flow")
                    flow = 0 if any(v["state"].get("closed") for v in valves) else (s["flow"] or default_flow)
                    values = {"total": round(s["total"] + flow * dt / 3600, 6), "flow": flow}
                elif device["kind"] == "meter" and all(k in s for k in ("total", "power")):
                    values = {"total": round(s["total"] + s["power"] * dt / 3600, 6)}
                elif device["kind"] in ("leak", "fire") and "battery" in s:
                    values["battery"] = round(max(0, s["battery"] - 0.0001 * dt), 4)
                if values:
                    try:
                        self.update_state(device["id"], values, source="emulator")
                    except ValueError as exc:
                        self.log("emulator_error", str(exc), device_id=device["id"])

    def bootstrap(self):
        with self.lock:
            devices = self.store.list("devices")
            rules = self.store.list("rules")
            runs = self.store.list("runs", 100)
            notifications = self.store.list("notifications", 100)
            return {"devices": devices, "rules": rules, "events": self.store.list("events", 150), "runs": runs, "notifications": notifications, "templates": deepcopy(TEMPLATES), "integration": self.integration_summary(), "emulation": self.store.get("meta", "emulation") or {"enabled": False}, "stats": {"devices": len(devices), "online": sum(d["online"] for d in devices), "rules": len(rules), "active_rules": sum(r["enabled"] for r in rules), "runs": len(runs), "successful_runs": sum(r["status"] == "success" for r in runs), "errors": sum(r["status"] == "error" for r in runs), "notifications": len(notifications), "unread": sum(not n["read"] for n in notifications)}}
