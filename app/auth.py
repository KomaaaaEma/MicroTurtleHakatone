"""Local accounts and revocable server-side sessions; no shared demo passwords."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

COOKIE = "microturtles_session"
SESSION_SECONDS = 12 * 60 * 60
PASSWORD_ITERATIONS = 600_000
USERNAME = re.compile(r"^[a-zA-Z0-9_.@+\-]{3,80}$")


class AuthModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(AuthModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class Setup(Login):
    display_name: str = Field(default="Администратор", min_length=1, max_length=120)


class UserWrite(AuthModel):
    username: str = Field(min_length=3, max_length=80)
    display_name: str = Field(min_length=1, max_length=120)
    role: Literal["admin", "resident"] = "resident"
    location: dict[str, str] = Field(default_factory=dict)
    password: str | None = Field(default=None, max_length=256)
    active: bool = True

    @field_validator("username")
    @classmethod
    def username_format(cls, value):
        if not USERNAME.fullmatch(value):
            raise ValueError("Логин: 3–80 символов латиницы, цифры, . _ @ + -")
        return value.lower()


def password_hash(password: str) -> str:
    if len(password) < 10:
        raise ValueError("Пароль должен содержать не меньше 10 символов")
    salt = secrets.token_bytes(32)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def password_matches(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt, expected = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


def public_user(user: dict) -> dict:
    return {key: user.get(key) for key in ("id", "username", "display_name", "role", "location", "active", "created_at")}


class AuthService:
    def __init__(self, store, properties):
        self.store = store
        self.properties = properties
        self.lock = threading.RLock()
        self.dummy_hash = password_hash(secrets.token_urlsafe(32))

    def setup_required(self):
        return not self.store.list("users")

    def _username(self, value):
        if not USERNAME.fullmatch(value):
            raise ValueError("Логин: 3–80 символов латиницы, цифры, . _ @ + -")
        return value.lower()

    def _find(self, username):
        normalized = username.lower()
        return next((u for u in self.store.list("users") if u["username"] == normalized), None)

    def resolve(self, request: Request) -> dict | None:
        token = request.cookies.get(COOKIE)
        if not token or len(token) > 200:
            return None
        session_id = hashlib.sha256(token.encode()).hexdigest()
        session = self.store.get("sessions", session_id)
        if not session:
            return None
        if session["expires_at"] <= time.time():
            self.store.delete("sessions", session_id)
            return None
        user = self.store.get("users", session["user_id"])
        return user if user and user.get("active", True) else None

    def session(self, user, request: Request, response: Response):
        token = secrets.token_urlsafe(40)
        key = hashlib.sha256(token.encode()).hexdigest()
        self.store.save("sessions", key, {"id": key, "user_id": user["id"], "expires_at": time.time() + SESSION_SECONDS})
        for existing in self.store.list("sessions"):
            # Session IDs are deliberately not included in public account data.
            if existing["expires_at"] <= time.time() and existing.get("id"):
                self.store.delete("sessions", existing["id"])
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
                            secure=request.url.scheme == "https", samesite="strict", path="/")
        return public_user(user)

    def setup(self, body: Setup, request: Request, response: Response):
        with self.lock, self.store.lock:
            if not self.setup_required():
                raise HTTPException(409, "Первый администратор уже создан. Войдите в систему.")
            username = self._username(body.username)
            hashed = password_hash(body.password)
            user = {"id": str(uuid4()), "username": username, "display_name": body.display_name.strip(),
                    "role": "admin", "location": {}, "active": True,
                    "password_hash": hashed, "created_at": datetime.now(timezone.utc).isoformat()}
            if not user["display_name"]:
                raise ValueError("Укажите имя пользователя")
            self.store.save("users", user["id"], user)
            self.store.log("account", "Создан первый администратор", user_id=user["id"])
            return {"user": self.session(user, request, response)}

    def login(self, body: Login, request: Request, response: Response):
        username = body.username.lower()
        ip = request.client.host if request.client else "local"
        bucket_id = hashlib.sha256((ip + "|" + username).encode()).hexdigest()
        now = time.time()
        with self.lock:
            bucket = self.store.get("auth_failures", bucket_id) or {"attempts": 0, "until": now + 900}
            if bucket["until"] <= now:
                bucket = {"attempts": 0, "until": now + 900}
            if bucket["attempts"] >= 8:
                raise HTTPException(429, "Слишком много попыток входа. Повторите через 15 минут.")
            user = self._find(username)
            valid = password_matches(body.password, user["password_hash"] if user else self.dummy_hash)
            if not valid or not user or not user.get("active", True):
                bucket["attempts"] += 1
                self.store.save("auth_failures", bucket_id, bucket)
                raise HTTPException(401, "Неверный логин или пароль")
            self.store.delete("auth_failures", bucket_id)
            return {"user": self.session(user, request, response)}

    def logout(self, request: Request, response: Response):
        token = request.cookies.get(COOKIE)
        if token:
            self.store.delete("sessions", hashlib.sha256(token.encode()).hexdigest())
        response.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict")
        return {"ok": True}

    def list_users(self):
        return [public_user(u) for u in self.store.list("users")]

    def save_user(self, payload: UserWrite, expected_id=None):
        with self.lock, self.store.lock:
            previous = self.store.get("users", expected_id) if expected_id else None
            if expected_id and not previous:
                raise HTTPException(404, "Пользователь не найден")
            duplicate = self._find(payload.username)
            if duplicate and duplicate["id"] != expected_id:
                raise HTTPException(409, "Этот логин уже занят")
            location = self.properties.validate_resident_location(payload.location) if payload.role == "resident" else {}
            if previous and previous["role"] == "admin" and (payload.role != "admin" or not payload.active):
                admins = [u for u in self.store.list("users") if u["role"] == "admin" and u.get("active", True)]
                if len(admins) <= 1:
                    raise ValueError("Нельзя отключить или понизить последнего администратора")
            if not previous and not payload.password:
                raise ValueError("Для нового пользователя требуется пароль")
            display_name = payload.display_name.strip()
            if not display_name:
                raise ValueError("Укажите имя пользователя")
            user = {"id": expected_id or str(uuid4()), "username": payload.username,
                    "display_name": display_name, "role": payload.role, "location": location,
                    "active": payload.active, "created_at": previous["created_at"] if previous else datetime.now(timezone.utc).isoformat(),
                    "password_hash": password_hash(payload.password) if payload.password else previous["password_hash"]}
            self.store.save("users", user["id"], user)
            if previous and any(user.get(k) != previous.get(k) for k in ("password_hash", "role", "location", "active")):
                # Revocation is persistent, including existing tabs of the edited account.
                with self.store.lock:
                    self.store.db.execute("DELETE FROM records WHERE kind='sessions' AND json_extract(payload,'$.user_id')=?", (user["id"],))
                    self.store.db.commit()
            self.store.log("account", "Сохранён пользователь", user_id=user["id"])
            return public_user(user)


def require_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, "Войдите в MicroTurtles")
    return user


def require_admin(request: Request) -> dict:
    user = require_user(request)
    if user["role"] != "admin":
        raise HTTPException(403, "Доступно только администратору")
    return user
