"""Scoped house news and a local catalogue/request workflow.

Example prices and compatibility labels are demonstration data. Orders create
maintenance requests and local stock reservations; no payment is collected and
no external supplier receives a purchase or installation instruction.
"""
from __future__ import annotations

import base64
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import quote, unquote, urlsplit
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing import Literal

from .auth import require_admin, require_user
from .store import now


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Укажите дату ISO 8601 с часовым поясом")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed
    except ValueError:
        raise ValueError("Укажите дату ISO 8601 с часовым поясом") from None


def money(value):
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class NewsWrite(Model):
    id: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=180)
    body: str = Field(min_length=1, max_length=15000)
    status: Literal["draft", "published", "scheduled"] = "draft"
    publish_at: str | None = None
    building_ids: list[str] = Field(default_factory=list, max_length=200)
    priority: Literal["normal", "important", "urgent"] = "normal"
    is_demo: bool = False

    @field_validator("title", "body")
    @classmethod
    def clean_text(cls, value):
        if not value.strip():
            raise ValueError("Заполните название и текст новости")
        return value.strip()

    @model_validator(mode="after")
    def valid_schedule(self):
        if self.status == "scheduled" and not self.publish_at:
            raise ValueError("Для публикации по расписанию укажите дату")
        if self.publish_at:
            parsed = timestamp(self.publish_at)
            self.publish_at = parsed.isoformat()
            if self.status == "published" and parsed > datetime.now(timezone.utc):
                raise ValueError("Для будущей даты выберите публикацию по расписанию")
        if len(set(self.building_ids)) != len(self.building_ids):
            raise ValueError("Не повторяйте дома в аудитории новости")
        return self


class ProductWrite(Model):
    id: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=180)
    description: str = Field(min_length=1, max_length=12000)
    category: str = Field(min_length=1, max_length=80)
    price: float = Field(ge=0, le=100_000_000)
    stock: int = Field(ge=0, le=1_000_000, strict=True)
    image_url: str = Field(default="", max_length=2000)
    specifications: dict[str, str] = Field(default_factory=dict, max_length=40)
    ujin_compatibility: Literal["emulator_only", "verified", "requires_verification"] = "requires_verification"
    scenario_types: list[str] = Field(default_factory=list, max_length=20)
    installation_available: bool = False
    installation_price: float = Field(default=0, ge=0, le=100_000_000)
    active: bool = True
    is_demo: bool = False

    @field_validator("title", "description", "category")
    @classmethod
    def clean_text(cls, value):
        if not value.strip():
            raise ValueError("Заполните название, описание и категорию товара")
        return value.strip()

    @field_validator("image_url")
    @classmethod
    def safe_image(cls, value):
        if not value:
            return ""
        if any(ord(c) < 32 for c in value) or "\\" in value:
            raise ValueError("Некорректный адрес изображения")
        parsed = urlsplit(value)
        if value.startswith(("/static/", "/api/product-images/")):
            if parsed.netloc or parsed.query or parsed.fragment or ".." in unquote(parsed.path).split("/"):
                raise ValueError("Некорректный локальный адрес изображения")
            return value
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("Изображение: HTTPS-адрес или локальный файл каталога")
        return value

    @model_validator(mode="after")
    def bounded_details(self):
        if any(not key.strip() or len(key) > 80 or len(value) > 500 for key, value in self.specifications.items()):
            raise ValueError("Характеристики: название до 80 символов, значение до 500")
        if any(not value.strip() or len(value) > 80 for value in self.scenario_types) or len(set(self.scenario_types)) != len(self.scenario_types):
            raise ValueError("Укажите уникальные типы сценариев до 80 символов")
        self.price, self.installation_price = money(self.price), money(self.installation_price)
        return self


class OrderWrite(Model):
    product_id: str = Field(min_length=1, max_length=100)
    quantity: int = Field(default=1, ge=1, le=20, strict=True)
    installation: bool = False
    comment: str = Field(default="", max_length=3000)


class Community:
    def __init__(self, store, engine, properties, operations):
        self.store, self.engine, self.properties, self.operations = store, engine, properties, operations
        self.router = APIRouter(prefix="/api", tags=["News and catalogue"])
        self._seed()
        self._routes()

    def _seed(self):
        if self.store.get("meta", "community-v1"):
            return
        example_products = [
            {"id": "demo-leak-sensor", "title": "Датчик протечки · демомодель", "category": "leak_sensor", "price": 1490, "stock": 12,
             "description": "Пример позиции каталога MicroTurtles. Цена и остаток учебные; поставка и совместимость реального оборудования не подтверждены.",
             "image_url": "/static/assets/catalog-leak.svg", "specifications": {"Назначение": "Обнаружение воды", "Модель": "Учебный пример"},
             "ujin_compatibility": "emulator_only", "scenario_types": ["leak"], "installation_available": True, "installation_price": 500},
            {"id": "demo-water-valve", "title": "Клапан перекрытия воды · демомодель", "category": "valve", "price": 3990, "stock": 8,
             "description": "Учебный пример исполнительного устройства для цепочки защиты от протечки. Не является предложением поставщика.",
             "image_url": "/static/assets/catalog-valve.svg", "specifications": {"Назначение": "Перекрытие воды", "Установка": "По заявке инженеру"},
             "ujin_compatibility": "emulator_only", "scenario_types": ["leak"], "installation_available": True, "installation_price": 1500},
            {"id": "demo-thermostat", "title": "Термостат · демомодель", "category": "thermostat", "price": 5490, "stock": 5,
             "description": "Пример карточки климатического устройства. Цена, наличие и установка показаны для проверки интерфейса заявок.",
             "image_url": "/static/assets/catalog-climate.svg", "specifications": {"Назначение": "Климат-контроль", "Диапазон настройки": "5–35 °C в эмуляторе"},
             "ujin_compatibility": "emulator_only", "scenario_types": ["climate"], "installation_available": True, "installation_price": 1000},
        ]
        for item in example_products:
            product = ProductWrite.model_validate({**item, "is_demo": True}).model_dump()
            product.update(created_at=now(), updated_at=now(), deleted=False)
            self.store.save("products", product["id"], product)
        for item in [
            {"id": "demo-welcome", "title": "Добро пожаловать в MicroTurtles", "body": "Демонстрационная новость. Здесь УК публикует объявления для жителей выбранных домов.", "status": "published", "publish_at": now()},
            {"id": "demo-draft", "title": "Пример черновика УК", "body": "Этот учебный черновик виден только администратору.", "status": "draft"},
            {"id": "demo-scheduled", "title": "Пример отложенного объявления", "body": "Учебное объявление станет доступно после заданной даты публикации.", "status": "scheduled", "publish_at": (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()},
        ]:
            news = NewsWrite.model_validate({**item, "is_demo": True}).model_dump()
            news.update(created_at=now(), updated_at=now())
            self.store.save("news", news["id"], news)
        self.store.save("meta", "community-v1", {"version": 1})

    @staticmethod
    def _published(item):
        return item["status"] in ("published", "scheduled") and (not item.get("publish_at") or timestamp(item["publish_at"]) <= datetime.now(timezone.utc))

    def _news(self, user, identifier):
        item = self.store.get("news", identifier)
        if not item or (user["role"] != "admin" and (not self._published(item) or
                (item["building_ids"] and (user.get("location") or {}).get("building_id") not in item["building_ids"]))):
            raise HTTPException(404, "Новость не найдена")
        return item

    def _news_view(self, user, item):
        result = deepcopy(item)
        result["effective_status"] = "published" if self._published(item) else item["status"]
        result["read_by_me"] = bool(self.store.get("news_reads", item["id"] + ":" + user["id"]))
        if user["role"] == "admin":
            recipients = {u["id"] for u in self.store.list("users") if u["role"] == "resident" and u.get("active", True)
                          and (not item["building_ids"] or (u.get("location") or {}).get("building_id") in item["building_ids"])}
            result["recipient_count"] = len(recipients)
            result["read_count"] = len({r["user_id"] for r in self.store.list("news_reads") if r["news_id"] == item["id"] and r["user_id"] in recipients})
        else:
            result["status"] = result["effective_status"]
        return result

    def _deliver_news(self, user, items):
        """Deliver an eligible item once when this resident next loads the feed."""
        if user["role"] != "resident":
            return
        with self.engine.lock:
            for item in items:
                key = item["id"] + ":" + user["id"]
                if not self.store.get("news_deliveries", key):
                    delivery = self.operations.notify(user["id"], "news", item["title"], user["location"], news_id=item["id"])
                    if delivery:
                        self.store.save("news_deliveries", key, {"id": key, "news_id": item["id"], "user_id": user["id"]})

    def save_news(self, user, payload, identifier=None):
        with self.engine.lock:
            previous = self.store.get("news", identifier) if identifier else None
            if identifier and not previous:
                raise HTTPException(404, "Новость не найдена")
            item = payload.model_dump()
            if identifier and item.get("id") and item["id"] != identifier:
                raise ValueError("Нельзя изменить ID новости")
            item["id"] = identifier or item.get("id") or str(uuid4())
            if not identifier and self.store.get("news", item["id"]):
                raise HTTPException(409, "Новость с таким ID уже существует")
            for building_id in item["building_ids"]:
                self.properties.get_building(building_id)
            if item["status"] == "published" and not item["publish_at"]:
                item["publish_at"] = now()
            item.update(created_at=previous["created_at"] if previous else now(), updated_at=now())
            self.store.save("news", item["id"], item)
            self.operations.audit(user, "news.updated" if previous else "news.created", item["id"], status=item["status"])
            return self._news_view(user, item)

    def _product(self, user, identifier):
        item = self.store.get("products", identifier)
        if not item or item.get("deleted") or (user["role"] != "admin" and not item["active"]):
            raise HTTPException(404, "Товар не найден")
        return item

    def _recommendation(self, user, item):
        if user["role"] == "admin":
            return {"score": 0, "reasons": []}
        devices = [d for d in self.store.list("devices") if self.properties.can_access(user, d)]
        kinds = {d["kind"] for d in devices}
        score, reasons = 5, []
        if "leak" in item["scenario_types"]:
            if item["category"] == "leak_sensor" and "leak" not in kinds:
                score += 50
                reasons.append("В квартире ещё нет датчика протечки")
            if item["category"] == "valve" and "valve" not in kinds:
                score += 50
                reasons.append("Клапан добавит действие перекрытия воды к сценарию протечки")
        if "climate" in item["scenario_types"] and "thermostat" not in kinds:
            score += 40
            reasons.append("В квартире ещё нет термостата для климатического сценария")
        if item["ujin_compatibility"] == "verified":
            score += 10
            reasons.append("Совместимость с Ujin отмечена администратором каталога")
        elif item["ujin_compatibility"] == "emulator_only":
            reasons.append("Совместимость показана только на эмуляторе; реальное оборудование требует проверки")
        else:
            reasons.append("Совместимость с Ujin нужно подтвердить до установки")
        if not reasons:
            reasons.append("Позиция доступна для заявки; подбор оборудования подтверждает инженер")
        return {"score": score, "reasons": reasons}

    def _product_view(self, user, item):
        return {**deepcopy(item), "recommendation": self._recommendation(user, item), "currency": "RUB"}

    def save_product(self, user, payload, identifier=None):
        with self.engine.lock:
            previous = self._product(user, identifier) if identifier else None
            item = payload.model_dump()
            if identifier and item.get("id") and item["id"] != identifier:
                raise ValueError("Нельзя изменить ID товара")
            item["id"] = identifier or item.get("id") or str(uuid4())
            if not identifier and self.store.get("products", item["id"]):
                raise HTTPException(409, "Товар с таким ID уже существует")
            item.update(created_at=previous["created_at"] if previous else now(), updated_at=now(), deleted=False)
            self.store.save("products", item["id"], item)
            self.operations.audit(user, "product.updated" if previous else "product.created", item["id"])
            return self._product_view(user, item)

    def create_order(self, user, payload):
        with self.engine.lock:
            product = self._product(user, payload.product_id)
            if not product["active"]:
                raise ValueError("Товар снят с продажи")
            if product["stock"] < payload.quantity:
                raise ValueError("Недостаточно товара в локальном остатке")
            if payload.installation and not product["installation_available"]:
                raise ValueError("Для этого товара установка недоступна")
            identifier = str(uuid4())
            goods = Decimal(str(product["price"])) * payload.quantity
            installation = Decimal(str(product["installation_price"])) * payload.quantity if payload.installation else Decimal(0)
            order = {"id": identifier, "owner_id": user["id"], "owner_name": user["display_name"],
                     "product_id": product["id"], "product_title": product["title"], "quantity": payload.quantity,
                     "unit_price": product["price"], "installation": payload.installation,
                     "installation_total": money(installation), "total": money(goods + installation), "currency": "RUB",
                     "comment": payload.comment.strip(), "status": "requested", "payment_status": "not_charged",
                     "stock_status": "reserved_locally", "location": deepcopy(user.get("location") or {}),
                     "is_demo": product["is_demo"], "created_at": now()}
            request = {"title": (("Установка: " if payload.installation else "Заявка на приобретение: ") + product["title"])[:160],
                       "description": f"Заявка из каталога: {product['title']}; количество {payload.quantity}. "
                       f"Стоимость по локальному каталогу: {order['total']:.2f} ₽. Оплата не списывалась. "
                       + ("Демонстрационная позиция. " if product["is_demo"] else "") + payload.comment.strip(),
                       "category": "installation" if payload.installation else "purchase", "priority": "normal"}
            ticket = self.operations.create_ticket(user, request, order_id=identifier, persist=False)
            order["ticket_id"] = ticket["id"]
            product["stock"] -= payload.quantity
            product["updated_at"] = now()
            with self.store.lock, self.store.db:
                for kind, item in (("tickets", ticket), ("products", product), ("orders", order)):
                    self.store.db.execute("INSERT INTO records(kind,id,payload) VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload",
                        (kind, item["id"], json.dumps(item, ensure_ascii=False, allow_nan=False)))
            self.operations.audit(user, "ticket.created", ticket["id"])
            self.operations.notify(user["id"], "tickets", "Заявка принята: " + ticket["title"], ticket["location"], ticket_id=ticket["id"])
            self.operations.audit(user, "order.requested", identifier, product_id=product["id"], quantity=payload.quantity,
                                  installation=payload.installation, ticket_id=ticket["id"])
            return order

    def _routes(self):
        @self.router.get("/news")
        def news(request: Request):
            user = require_user(request)
            result = []
            context_keys = ("organization_id", "complex_id", "building_id")
            contextual = any(request.query_params.get(key) for key in context_keys)
            allowed_buildings = {b["id"] for b in self.properties.catalog(user)["buildings"]
                                 if all(not request.query_params.get(key) or (b["id"] if key == "building_id" else b.get(key)) == request.query_params[key] for key in context_keys)}
            for item in self.store.list("news"):
                if contextual and (not allowed_buildings or (item["building_ids"] and not allowed_buildings.intersection(item["building_ids"]))):
                    continue
                try:
                    result.append(self._news_view(user, self._news(user, item["id"])))
                except HTTPException:
                    continue
            self._deliver_news(user, result)
            return sorted(result, key=lambda item: ({"urgent": 0, "important": 1, "normal": 2}[item["priority"]], item["created_at"]), reverse=False)

        @self.router.post("/news", status_code=201)
        def add_news(body: NewsWrite, request: Request):
            return self.save_news(require_admin(request), body)

        @self.router.put("/news/{identifier}")
        def edit_news(identifier: str, body: NewsWrite, request: Request):
            return self.save_news(require_admin(request), body, identifier)

        @self.router.delete("/news/{identifier}")
        def delete_news(identifier: str, request: Request):
            user = require_admin(request)
            with self.engine.lock:
                self._news(user, identifier)
                self.store.delete("news", identifier)
                for kind in ("news_reads", "news_deliveries"):
                    for receipt in self.store.list(kind):
                        if receipt["news_id"] == identifier:
                            self.store.delete(kind, receipt["id"])
                self.operations.audit(user, "news.deleted", identifier)
            return {"ok": True}

        @self.router.post("/news/{identifier}/read")
        def read_news(identifier: str, request: Request):
            user = require_user(request)
            with self.engine.lock:
                item = self._news(user, identifier)
                key = identifier + ":" + user["id"]
                if not self.store.get("news_reads", key):
                    self.store.save("news_reads", key, {"id": key, "news_id": identifier, "user_id": user["id"], "read_at": now()})
                return self._news_view(user, item)

        @self.router.get("/products")
        def products(request: Request, category: str = "", recommended: bool = False):
            user = require_user(request)
            result = [self._product_view(user, item) for item in self.store.list("products") if not item.get("deleted")
                      and (user["role"] == "admin" or item["active"]) and (not category or item["category"] == category)]
            return sorted(result, key=lambda item: item["recommendation"]["score"], reverse=True) if recommended else result

        @self.router.get("/products/{identifier}")
        def product(identifier: str, request: Request):
            user = require_user(request)
            return self._product_view(user, self._product(user, identifier))

        @self.router.post("/products", status_code=201)
        def add_product(body: ProductWrite, request: Request):
            return self.save_product(require_admin(request), body)

        @self.router.put("/products/{identifier}")
        def edit_product(identifier: str, body: ProductWrite, request: Request):
            return self.save_product(require_admin(request), body, identifier)

        @self.router.delete("/products/{identifier}")
        def delete_product(identifier: str, request: Request):
            user = require_admin(request)
            with self.engine.lock:
                item = self._product(user, identifier)
                item.update(active=False, deleted=True, updated_at=now())
                self.store.save("products", identifier, item)
                self.operations.audit(user, "product.deleted", identifier)
            return {"ok": True}

        @self.router.post("/products/{identifier}/image")
        def upload_image(identifier: str, body: dict, request: Request):
            user = require_admin(request)
            with self.engine.lock:
                product = self._product(user, identifier)
                filename, content_type, content = self.operations.decode_file(body, images_only=True)
                file_id = str(uuid4())
                image = {"id": file_id, "product_id": identifier, "filename": filename, "content_type": content_type,
                         "size": len(content), "data_base64": base64.b64encode(content).decode(), "created_at": now()}
                self.store.save("product_images", file_id, image)
                product.update(image_url="/api/product-images/" + file_id, updated_at=now())
                self.store.save("products", identifier, product)
                for previous in self.store.list("product_images"):
                    if previous["product_id"] == identifier and previous["id"] != file_id:
                        self.store.delete("product_images", previous["id"])
                self.operations.audit(user, "product.image_uploaded", identifier, content_type=content_type, size=len(content))
                return {"image_url": product["image_url"], "id": file_id, "size": len(content), "content_type": content_type}

        @self.router.get("/product-images/{identifier}")
        def image(identifier: str, request: Request):
            user = require_user(request)
            item = self.store.get("product_images", identifier)
            if not item:
                raise HTTPException(404, "Изображение не найдено")
            self._product(user, item["product_id"])
            return Response(base64.b64decode(item["data_base64"]), media_type=item["content_type"],
                            headers={"Content-Disposition": "inline; filename*=UTF-8''" + quote(item["filename"]),
                                     "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})

        @self.router.get("/orders")
        def orders(request: Request):
            user = require_user(request)
            return [item for item in self.store.list("orders") if user["role"] == "admin" or item["owner_id"] == user["id"]]

        @self.router.post("/orders", status_code=201)
        def order(body: OrderWrite, request: Request):
            return self.create_order(require_user(request), body)
