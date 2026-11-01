"""Persistent incident, maintenance and administration workflows.

All records are scoped on the server. Files remain in SQLite and require the
same account boundary as their parent ticket; audit and backups never include
passwords, sessions, integration credentials or arbitrary HTTP bodies.
"""
from __future__ import annotations
import base64
import binascii
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import PurePath
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from .auth import public_user, require_admin, require_user
from .models import Device, Rule
from .properties import Building
from .store import Store, now

STATUSES = {"new", "in_progress", "waiting", "completed", "cancelled"}
PRIORITIES = {"low": 72, "normal": 24, "high": 4, "urgent": 1}
TRANSITIONS = {"new": {"in_progress", "waiting", "cancelled"},
               "in_progress": {"waiting", "completed", "cancelled"},
               "waiting": {"in_progress", "completed", "cancelled"},
               "completed": {"in_progress"}, "cancelled": {"new"}}

def text(value, limit=5000, required=False):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError("Укажите текст допустимой длины")
    return value.strip()

def when(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result
    except (TypeError, AttributeError, ValueError):
        raise ValueError("Дата должна быть ISO 8601 с часовым поясом") from None

class Operations:
    def __init__(self, store, engine, properties, integration):
        self.store, self.engine, self.properties, self.integration = store, engine, properties, integration
        self.router = APIRouter(prefix="/api", tags=["Operations"])
        self._routes()

    def audit(self, user, action, entity_id="", **details):
        self.store.append("audit", {"actor_id": user.get("id") if user else None,
            "actor_name": user.get("display_name") if user else "Система", "action": action,
            "entity_id": entity_id, "details": details})
        self.store.prune("audit", 10000)

    def notify(self, user_id, channel, message, location, **links):
        recipient = self.store.get("users", user_id)
        if not recipient or recipient["role"] != "resident" or not recipient.get("active", True):
            return None
        if not self.profile(recipient)["notification_preferences"].get(channel, True):
            return None
        return self.store.append("notifications", {"target": "resident", "owner_id": user_id,
            "channel": channel, "message": message, "location": {**deepcopy(location), "scope": location.get("scope", "apartment")}, "read": False,
            "delivery": "local", **links})

    def _scope(self, user, location, common=False):
        if user["role"] == "admin":
            return True
        if not location:
            return False
        if self.properties.can_access(user, {"location": location}):
            return True
        return common and self.properties.can_read(user, {"location": location, "common_access": {"resident_read": True}})

    def _assignee(self, value):
        if not value:
            return None
        user = self.store.get("users", value)
        if not user or user["role"] != "admin" or not user.get("active", True):
            raise ValueError("Выберите действующего сотрудника из списка администраторов")
        return value

    def _object_filter(self, user, item, filters):
        fields = {key: filters.get(key, "") for key in ("organization_id", "complex_id", "building_id", "entrance", "apartment", "scope")}
        if not self.properties.filter_devices([{"location": item.get("location") or {}, "common_access": {"resident_read": True}}], user, fields):
            return False
        return all(not filters.get(key) or str(item.get(key, "")) == str(filters[key]) for key in ("status", "priority", "assignee_id", "category"))

    def _history(self, record, user, action, comment=""):
        record.setdefault("history", []).append({"at": now(), "actor_id": user.get("id"),
            "actor_name": user.get("display_name", "Система"), "action": action, "comment": comment})
        record["updated_at"] = now()

    def refresh_incidents(self):
        with self.engine.lock:
            active = set()
            for device in self.store.list("devices"):
                for issue in self.properties.health(device)["issues"]:
                    key = "device-" + device["id"] + "-" + issue["code"]
                    active.add(key)
                    previous = self.store.get("incidents", key)
                    if previous and previous["status"] != "resolved":
                        continue
                    item = previous or {"id": key, "device_id": device["id"], "code": issue["code"],
                        "created_at": now(), "assignee_id": None, "history": []}
                    item.update(title=issue["message"], location=deepcopy(device["location"]),
                        location_label=self.properties.location_label(device), device_name=device["name"],
                        priority="critical" if issue["severity"] == "critical" else "normal", status="open", active=True)
                    self._history(item, {"display_name": "Система"}, "reopened" if previous else "opened")
                    self.store.save("incidents", key, item)
                    self.audit(None, "incident.opened", key, device_id=device["id"], code=issue["code"])
                    for recipient in self.store.list("users"):
                        if recipient.get("role") == "resident" and self.properties.can_read(recipient, device):
                            self.notify(recipient["id"], "incidents", issue["message"] + ". " + device["name"], device["location"], incident_id=key, device_id=device["id"])
            for item in self.store.list("incidents"):
                if item["id"] not in active and item["status"] != "resolved":
                    item.update(status="resolved", active=False, resolved_at=now())
                    self._history(item, {"display_name": "Система"}, "resolved", "Причина больше не обнаруживается")
                    self.store.save("incidents", item["id"], item)
                    self.audit(None, "incident.resolved", item["id"])

    def incidents(self, user):
        self.refresh_incidents()
        return [item for item in self.store.list("incidents") if self._scope(user, item["location"], common=True)
                and (user["role"] == "admin" or self.properties.can_read(user, self.store.get("devices", item["device_id"]) or {}))]

    def _ticket(self, user, ticket_id):
        item = self.store.get("tickets", ticket_id)
        if not item or (user["role"] != "admin" and (item["owner_id"] != user["id"] or not self._scope(user, item["location"], common=True))):
            raise HTTPException(404, "Заявка не найдена")
        return item

    def _ticket_view(self, user, ticket, detail=False):
        item = deepcopy(ticket)
        item["overdue"] = item["status"] not in ("completed", "cancelled") and when(item["due_at"]) < datetime.now(timezone.utc)
        assignee = self.store.get("users", item.get("assignee_id")) if item.get("assignee_id") else None
        item["assignee_name"] = assignee.get("display_name") if assignee else None
        if detail:
            item["comments"] = [c for c in self.store.list("ticket_comments") if c["ticket_id"] == item["id"]
                                and (user["role"] == "admin" or not c.get("internal"))]
            item["attachments"] = [self._file_meta(a) for a in self.store.list("attachments") if a["ticket_id"] == item["id"]]
        return item

    def create_ticket(self, user, payload, *, order_id=None, persist=True):
        allowed = {"title", "description", "category", "priority", "location", "preferred_time", "device_id", "incident_id"}
        if set(payload) - allowed:
            raise ValueError("Неизвестное поле заявки")
        title, description = text(payload.get("title", ""), 160, True), text(payload.get("description", ""), 10000, True)
        priority = payload.get("priority", "normal")
        if not isinstance(priority, str) or priority not in PRIORITIES:
            raise ValueError("Неизвестный приоритет")
        linked_device = None
        if payload.get("incident_id"):
            incident = next((item for item in self.incidents(user) if item["id"] == payload["incident_id"]), None)
            if not incident:
                raise HTTPException(404, "Инцидент не найден")
            if payload.get("device_id") and payload["device_id"] != incident["device_id"]:
                raise ValueError("Устройство не соответствует инциденту")
            payload = {**payload, "device_id": incident["device_id"]}
        if payload.get("device_id"):
            linked_device = self.store.get("devices", payload["device_id"])
            if not linked_device or not self.properties.can_read(user, linked_device):
                raise HTTPException(404, "Устройство не найдено")
        location = linked_device["location"] if linked_device else (payload.get("location") if user["role"] == "admin" else user.get("location"))
        if not location:
            raise ValueError("Выберите дом, подъезд и квартиру заявки")
        if not isinstance(location, dict):
            raise ValueError("Расположение заявки должно содержать дом, подъезд и квартиру")
        if location.get("scope") == "common":
            location = self.properties.validate_device_location({"location": location})
        else:
            location = {**self.properties.validate_resident_location(location), "scope": "apartment", "floor": None}
        ticket = {"id": str(uuid4()), "owner_id": user["id"], "owner_name": user["display_name"],
            "title": title, "description": description, "category": text(payload.get("category", "other"), 80, True),
            "priority": priority, "location": location, "status": "new", "assignee_id": None,
            "preferred_time": text(payload.get("preferred_time", ""), 300), "created_at": now(),
            "due_at": (datetime.now(timezone.utc) + timedelta(hours=PRIORITIES[priority])).isoformat(),
            "rating": None, "order_id": order_id, "device_id": payload.get("device_id"), "incident_id": payload.get("incident_id"), "history": []}
        self._history(ticket, user, "created")
        if not persist:
            return ticket
        self.store.save("tickets", ticket["id"], ticket)
        self.audit(user, "ticket.created", ticket["id"])
        self.notify(user["id"], "tickets", "Заявка принята: " + title, location, ticket_id=ticket["id"])
        return self._ticket_view(user, ticket, True)

    def patch_ticket(self, user, ticket_id, payload):
        with self.engine.lock:
            ticket = self._ticket(user, ticket_id)
            admin = user["role"] == "admin"
            allowed = {"status", "assignee_id", "priority", "title", "description", "preferred_time", "due_at"} if admin else {"status", "title", "description", "preferred_time"}
            if not payload or set(payload) - allowed:
                raise ValueError("Недопустимое изменение заявки")
            if not admin and any(key != "status" for key in payload) and ticket["status"] != "new":
                raise HTTPException(403, "После начала работ изменяйте описание через комментарии")
            if "status" in payload:
                status = payload["status"]
                if not isinstance(status, str) or status not in STATUSES or (status != ticket["status"] and status not in TRANSITIONS[ticket["status"]]):
                    raise ValueError("Недопустимый переход статуса")
                if not admin and not (status == "cancelled" and ticket["status"] in ("new", "waiting")):
                    raise HTTPException(403, "Житель может отменить только ожидающую заявку")
                ticket["status"] = status
                if status == "completed":
                    ticket["completed_at"] = now()
            if "assignee_id" in payload:
                ticket["assignee_id"] = self._assignee(payload["assignee_id"])
            if "priority" in payload:
                if not isinstance(payload["priority"], str) or payload["priority"] not in PRIORITIES:
                    raise ValueError("Неизвестный приоритет")
                ticket["priority"] = payload["priority"]
                ticket["due_at"] = (datetime.now(timezone.utc) + timedelta(hours=PRIORITIES[ticket["priority"]])).isoformat()
            if "due_at" in payload:
                ticket["due_at"] = when(payload["due_at"]).isoformat()
            for field, limit in (("title", 160), ("description", 10000), ("preferred_time", 300)):
                if field in payload:
                    ticket[field] = text(payload[field], limit, field != "preferred_time")
            self._history(ticket, user, "updated", ", ".join(sorted(payload)))
            self.store.save("tickets", ticket_id, ticket)
            self.audit(user, "ticket.updated", ticket_id, fields=sorted(payload))
            self.notify(ticket["owner_id"], "tickets", "Заявка «" + ticket["title"] + "»: " + ticket["status"], ticket["location"], ticket_id=ticket_id)
            return self._ticket_view(user, ticket, True)

    @staticmethod
    def _file_meta(item):
        return {key: item[key] for key in ("id", "filename", "content_type", "size", "timestamp")} | {"url": "/api/attachments/" + item["id"]}

    @staticmethod
    def decode_file(payload, images_only=False):
        if set(payload) - {"filename", "content_type", "data_base64"}:
            raise ValueError("Неизвестное поле файла")
        filename = text(payload.get("filename", "file"), 180, True).replace("\\", "/").split("/")[-1]
        if any(ord(c) < 32 for c in filename):
            raise ValueError("Некорректное имя файла")
        content_type = payload.get("content_type", "application/octet-stream")
        allowed = {"image/png", "image/jpeg", "image/webp"}
        if not images_only:
            allowed |= {"application/pdf", "text/plain", "application/octet-stream"}
        if not isinstance(content_type, str) or content_type not in allowed:
            raise ValueError("Поддерживаются PNG, JPEG, WebP, PDF и текстовые файлы")
        try:
            data = base64.b64decode(payload.get("data_base64", ""), validate=True)
        except (binascii.Error, TypeError, ValueError):
            raise ValueError("Повреждённое содержимое файла") from None
        if not data or len(data) > 1_048_576:
            raise ValueError("Размер файла: от 1 байта до 1 МБ")
        if images_only:
            valid = ((content_type == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n"))
                     or (content_type == "image/jpeg" and data.startswith(b"\xff\xd8\xff"))
                     or (content_type == "image/webp" and data[:4] == b"RIFF" and data[8:12] == b"WEBP"))
            if not valid:
                raise ValueError("Содержимое не соответствует формату изображения")
        return filename, content_type, data

    def profile(self, user):
        prefs = self.store.get("profiles", user["id"]) or {"notification_preferences": {"incidents": True, "tickets": True, "news": True, "scenarios": True, "marketing": False}}
        return {"user": public_user(user), "notification_preferences": prefs["notification_preferences"]}

    def backup(self, user, name=""):
        snapshot = {"schema_version": 1, "devices": self.store.list("devices"), "rules": self.store.list("rules"),
                    "buildings": self.store.list("buildings"), "organizations": self.store.list("organizations"), "complexes": self.store.list("complexes")}
        item = self.store.append("backups", {"name": text(name or "Резервная копия конфигурации", 160),
            "created_by": user["id"], "snapshot": snapshot,
            "counts": {key: len(snapshot[key]) for key in ("devices", "rules", "buildings")}})
        self.store.prune("backups", 30)
        self.audit(user, "backup.created", item["id"])
        return {key: value for key, value in item.items() if key != "snapshot"}

    def restore(self, user, backup_id):
        backup = self.store.get("backups", backup_id)
        if not backup:
            raise HTTPException(404, "Резервная копия не найдена")
        snapshot = backup["snapshot"]
        scratch = Store(":memory:")
        try:
            # Validate the complete proposed graph before touching live configuration.
            for kind in ("organizations", "complexes", "buildings"):
                for item in snapshot.get(kind, []):
                    if kind == "buildings":
                        item = Building.model_validate(item).model_dump()
                    scratch.save(kind, item["id"], item)
            from .properties import PropertyService
            validator = PropertyService(scratch)
            devices = [Device.model_validate(d).model_dump() for d in snapshot["devices"]]
            for device in devices:
                device["location"] = validator.validate_device_location(device)
                device["last_seen"], device["last_seen_source"] = None, None
            for resident in self.store.list("users"):
                if resident["role"] == "resident":
                    validator.validate_resident_location(resident["location"])
            rules = [self.engine.validate_rule(r, {d["id"]: d for d in devices}) for r in snapshot["rules"]]
            kinds = {"devices": devices, "rules": rules, "buildings": scratch.list("buildings"),
                     "organizations": scratch.list("organizations"), "complexes": scratch.list("complexes")}
        finally:
            scratch.close()
        with self.engine.lock, self.store.lock, self.store.db:
            previous = self.store.list("devices")
            for rule in self.store.list("rules"):
                self.engine.cancel_rule_jobs(rule["id"])
            for kind, items in kinds.items():
                self.store.db.execute("DELETE FROM records WHERE kind=?", (kind,))
                self.store.db.executemany("INSERT INTO records(kind,id,payload) VALUES(?,?,?)", [(kind, d["id"], json.dumps(d, ensure_ascii=False, allow_nan=False)) for d in items])
        self.integration.on_configuration_change(previous, devices)
        self.audit(user, "backup.restored", backup_id)
        return {"ok": True, "counts": backup["counts"]}

    def _routes(self):
        router = self.router
        @router.get("/incidents")
        def incidents(request: Request):
            user = require_user(request)
            return [item for item in self.incidents(user) if self._object_filter(user, item, request.query_params)]
        @router.patch("/incidents/{incident_id}")
        def patch_incident(incident_id: str, payload: dict, request: Request):
            user = require_admin(request)
            self.refresh_incidents()
            item = self.store.get("incidents", incident_id)
            if not item:
                raise HTTPException(404, "Инцидент не найден")
            if set(payload) - {"status", "assignee_id", "priority", "comment"}:
                raise ValueError("Недопустимое изменение инцидента")
            if "status" in payload:
                if payload["status"] not in ("open", "acknowledged", "in_progress", "resolved"):
                    raise ValueError("Неизвестный статус")
                if payload["status"] == "resolved" and item.get("active"):
                    raise ValueError("Сначала устраните активную причину неисправности")
                item["status"] = payload["status"]
            if "assignee_id" in payload:
                item["assignee_id"] = self._assignee(payload["assignee_id"])
            if "priority" in payload:
                if payload["priority"] not in ("critical", "high", "normal", "low"):
                    raise ValueError("Неизвестный приоритет")
                item["priority"] = payload["priority"]
            self._history(item, user, "updated", text(payload.get("comment", "")))
            self.store.save("incidents", incident_id, item)
            self.audit(user, "incident.updated", incident_id, fields=sorted(payload))
            return item
        @router.post("/incidents/{incident_id}/ack")
        def acknowledge(incident_id: str, request: Request, payload: dict | None = None):
            return patch_incident(incident_id, {"status": "acknowledged", "comment": (payload or {}).get("comment", "Принято в работу")}, request)
        @router.get("/tickets")
        def tickets(request: Request):
            user = require_user(request)
            return [self._ticket_view(user, item) for item in self.store.list("tickets") if (user["role"] == "admin"
                    or (item["owner_id"] == user["id"] and self._scope(user, item["location"], common=True)))
                    and self._object_filter(user, item, request.query_params)]
        @router.post("/tickets", status_code=201)
        def create_ticket(payload: dict, request: Request):
            return self.create_ticket(require_user(request), payload)
        @router.get("/tickets/{ticket_id}")
        def ticket(ticket_id: str, request: Request):
            user = require_user(request)
            return self._ticket_view(user, self._ticket(user, ticket_id), True)
        @router.patch("/tickets/{ticket_id}")
        def patch_ticket(ticket_id: str, payload: dict, request: Request):
            return self.patch_ticket(require_user(request), ticket_id, payload)
        @router.post("/tickets/{ticket_id}/comments", status_code=201)
        def comment(ticket_id: str, payload: dict, request: Request):
            user = require_user(request)
            self._ticket(user, ticket_id)
            if set(payload) - {"text", "internal"}:
                raise ValueError("Недопустимое поле комментария")
            if payload.get("internal") and user["role"] != "admin":
                raise HTTPException(403, "Служебные комментарии доступны сотруднику")
            item = self.store.append("ticket_comments", {"ticket_id": ticket_id, "author_id": user["id"],
                "author_name": user["display_name"], "internal": bool(payload.get("internal")), "text": text(payload.get("text", ""), 5000, True)})
            self.audit(user, "ticket.comment_added", ticket_id)
            ticket = self._ticket(user, ticket_id)
            if user["id"] != ticket["owner_id"] and not item["internal"]:
                self.notify(ticket["owner_id"], "tickets", "Новый ответ по заявке «" + ticket["title"] + "»", ticket["location"], ticket_id=ticket_id)
            return item
        @router.post("/tickets/{ticket_id}/rating")
        def rating(ticket_id: str, payload: dict, request: Request):
            user = require_user(request)
            ticket = self._ticket(user, ticket_id)
            if ticket["owner_id"] != user["id"] or ticket["status"] != "completed":
                raise HTTPException(403, "Оценить можно только свою выполненную заявку")
            value = payload.get("rating")
            if type(value) is not int or value not in range(1, 6):
                raise ValueError("Оценка — целое число от 1 до 5")
            ticket["rating"] = {"value": value, "comment": text(payload.get("comment", ""), 2000), "at": now()}
            self.store.save("tickets", ticket_id, ticket)
            self.audit(user, "ticket.rated", ticket_id, rating=value)
            return self._ticket_view(user, ticket, True)
        @router.post("/tickets/{ticket_id}/attachments", status_code=201)
        def attach(ticket_id: str, payload: dict, request: Request):
            user = require_user(request)
            self._ticket(user, ticket_id)
            filename, content_type, data = self.decode_file(payload)
            if len([a for a in self.store.list("attachments") if a["ticket_id"] == ticket_id]) >= 10:
                raise ValueError("К заявке можно приложить не более 10 файлов")
            item = self.store.append("attachments", {"ticket_id": ticket_id, "owner_id": user["id"], "filename": filename,
                "content_type": content_type, "size": len(data), "data_base64": base64.b64encode(data).decode("ascii")})
            self.audit(user, "ticket.file_added", ticket_id, size=len(data))
            return self._file_meta(item)
        @router.get("/attachments/{attachment_id}")
        def download(attachment_id: str, request: Request):
            user = require_user(request)
            item = self.store.get("attachments", attachment_id)
            if not item:
                raise HTTPException(404, "Файл не найден")
            self._ticket(user, item["ticket_id"])
            return Response(base64.b64decode(item["data_base64"]), media_type=item["content_type"],
                headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(item["filename"]), "X-Content-Type-Options": "nosniff"})
        @router.get("/specialists")
        def specialists(request: Request):
            require_admin(request)
            return [{"id": u["id"], "display_name": u["display_name"]} for u in self.store.list("users") if u["role"] == "admin" and u.get("active", True)]
        @router.get("/audit")
        def audit(request: Request):
            require_admin(request)
            return list(reversed(self.store.list("audit")[-1000:]))
        @router.get("/backups")
        def backups(request: Request):
            require_admin(request)
            return [{key: value for key, value in b.items() if key != "snapshot"} for b in self.store.list("backups")]
        @router.post("/backups", status_code=201)
        def backup(request: Request, payload: dict | None = None):
            return self.backup(require_admin(request), (payload or {}).get("name", ""))
        @router.get("/backups/{backup_id}")
        def get_backup(backup_id: str, request: Request):
            require_admin(request)
            item = self.store.get("backups", backup_id)
            if not item:
                raise HTTPException(404, "Резервная копия не найдена")
            return item
        @router.post("/backups/{backup_id}/restore")
        def restore(backup_id: str, request: Request):
            return self.restore(require_admin(request), backup_id)
        @router.get("/profile")
        def profile(request: Request):
            return self.profile(require_user(request))
        @router.put("/profile")
        def save_profile(payload: dict, request: Request):
            user = require_user(request)
            if set(payload) - {"display_name", "notification_preferences"}:
                raise ValueError("Через профиль нельзя изменить роль, логин или расположение")
            current = self.profile(user)
            prefs = payload.get("notification_preferences", {})
            if not isinstance(prefs, dict) or set(prefs) - set(current["notification_preferences"]) or any(type(v) is not bool for v in prefs.values()):
                raise ValueError("Некорректные настройки уведомлений")
            current["notification_preferences"].update(prefs)
            if "display_name" in payload:
                user["display_name"] = text(payload["display_name"], 120, True)
            self.store.save("profiles", user["id"], {"notification_preferences": current["notification_preferences"]})
            if "display_name" in payload:
                self.store.save("users", user["id"], user)
            self.audit(user, "profile.updated", user["id"], fields=sorted(payload))
            return self.profile(user)
