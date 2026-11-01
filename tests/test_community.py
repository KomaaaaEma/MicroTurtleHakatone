"""News privacy, catalogue validation and purchase-request workflow."""
import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.community import OrderWrite, ProductWrite
from app.main import create_app

PASSWORD = "Community-test-password-2026"


@pytest.fixture
def world(tmp_path):
    app = create_app(tmp_path / "community.sqlite", start_workers=False)
    with TestClient(app) as admin:
        assert admin.post("/api/auth/setup", json={"username": "admin", "password": PASSWORD}).status_code == 201
        building = admin.get("/api/properties").json()["buildings"][0]
        another = deepcopy(building)
        another.update(id="another-house", name="Другой дом", address="Учебный адрес другого дома")
        another["entrances"] = [{"number": "1", "name": "", "apartments": [{"number": "3008", "floor": None}]}]
        assert admin.post("/api/properties/buildings", json=another).status_code == 201
        profiles, clients = [], []
        for username, location in [("reader-a", {"building_id": building["id"], "entrance": building["entrances"][0]["number"], "apartment": building["entrances"][0]["apartments"][0]["number"]}),
                                   ("reader-b", {"building_id": another["id"], "entrance": "1", "apartment": "3008"})]:
            response = admin.post("/api/users", json={"username": username, "display_name": username, "password": PASSWORD, "role": "resident", "location": location})
            assert response.status_code == 201, response.text
            profiles.append(response.json())
            client = TestClient(app)
            assert client.post("/api/auth/login", json={"username": username, "password": PASSWORD}).status_code == 200
            clients.append(client)
        yield {"app": app, "admin": admin, "resident": clients[0], "other": clients[1], "profiles": profiles, "building": building}
        for client in clients:
            client.close()


def product_body(**updates):
    return {"title": "Учебный комплект", "description": "Демонстрационная позиция для проверки заявки", "category": "test",
            "price": 1234.56, "stock": 3, "installation_available": True, "installation_price": 100,
            "ujin_compatibility": "requires_verification", "scenario_types": ["leak"], "is_demo": True, **updates}


def add_product(world, **updates):
    response = world["admin"].post("/api/products", json=product_body(**updates))
    assert response.status_code == 201, response.text
    return response.json()


def news_body(**updates):
    return {"title": "Объявление дома", "body": "Учебный текст объявления", "status": "published", "building_ids": [], "priority": "normal", **updates}


def test_news_hides_drafts_future_and_other_buildings_and_counts_reads_once(world):
    admin, resident, other = world["admin"], world["resident"], world["other"]
    target = admin.post("/api/news", json=news_body(building_ids=[world["building"]["id"]])).json()
    draft = admin.post("/api/news", json=news_body(status="draft", title="Черновик")).json()
    future = admin.post("/api/news", json=news_body(status="scheduled", title="Будущая новость", publish_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat())).json()
    ids = {item["id"] for item in resident.get("/api/news").json()}
    assert target["id"] in ids and draft["id"] not in ids and future["id"] not in ids
    assert target["id"] not in {item["id"] for item in other.get("/api/news").json()}
    assert other.post(f"/api/news/{target['id']}/read").status_code == 404
    assert resident.post(f"/api/news/{draft['id']}/read").status_code == 404
    for _ in range(2):
        view = resident.post(f"/api/news/{target['id']}/read").json()
        assert view["read_by_me"] is True
        assert "read_count" not in view and "recipient_count" not in view
    admin_view = next(item for item in admin.get("/api/news").json() if item["id"] == target["id"])
    assert admin_view["read_count"] == 1 and admin_view["recipient_count"] == 1
    assert all("user_id" not in item for item in resident.get("/api/news").json())


def test_scheduled_news_becomes_visible_at_due_time_without_manual_publish(world):
    body = news_body(status="scheduled", publish_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    response = world["admin"].post("/api/news", json=body)
    assert response.status_code == 201
    identifier = response.json()["id"]
    view = next(item for item in world["resident"].get("/api/news").json() if item["id"] == identifier)
    assert view["effective_status"] == "published" and view["status"] == "published"
    assert world["admin"].post("/api/news", json=news_body(status="scheduled", publish_at="2030-01-01T12:00:00")).status_code == 422
    assert world["admin"].post("/api/news", json=news_body(building_ids=["unknown-house"])).status_code == 422
    assert world["resident"].post("/api/news", json=news_body()).status_code == 403


def test_news_notifications_respect_preferences_and_do_not_duplicate(world):
    resident = world["resident"]
    assert resident.put("/api/profile", json={"notification_preferences": {"news": False}}).status_code == 200
    item = world["admin"].post("/api/news", json=news_body(title="Проверка настройки уведомлений")).json()
    resident.get("/api/news")
    assert not any(n.get("news_id") == item["id"] for n in resident.get("/api/notifications").json())
    resident.put("/api/profile", json={"notification_preferences": {"news": True}})
    resident.get("/api/news")
    resident.get("/api/news")
    notifications = [n for n in resident.get("/api/notifications").json() if n.get("news_id") == item["id"]]
    assert len(notifications) == 1 and notifications[0]["delivery"] == "local"


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:image/svg+xml,<svg/>", "//external.example/x.png", "/static/../private.txt", "/static/%2e%2e/private.txt", "https://user:password@example.com/x.png", "http://example.com/x.png"])
def test_catalog_rejects_executable_or_unsafe_image_addresses(url):
    with pytest.raises(ValidationError):
        ProductWrite.model_validate(product_body(image_url=url))


def test_catalogue_crud_inactive_visibility_and_personal_recommendations(world):
    admin, resident, other = world["admin"], world["resident"], world["other"]
    item = add_product(world)
    assert resident.post("/api/products", json=product_body()).status_code == 403
    assert resident.delete(f"/api/products/{item['id']}").status_code == 403
    inactive = product_body(id=item["id"], active=False)
    assert admin.put(f"/api/products/{item['id']}", json=inactive).status_code == 200
    assert resident.get(f"/api/products/{item['id']}").status_code == 404
    assert item["id"] not in {p["id"] for p in resident.get("/api/products").json()}
    assert admin.get(f"/api/products/{item['id']}").status_code == 200
    own = resident.get("/api/products/demo-leak-sensor").json()
    empty_apartment = other.get("/api/products/demo-leak-sensor").json()
    assert empty_apartment["recommendation"]["score"] > own["recommendation"]["score"]
    assert "датчика протечки" in " ".join(empty_apartment["recommendation"]["reasons"])
    assert admin.delete(f"/api/products/{item['id']}").status_code == 200
    assert admin.get(f"/api/products/{item['id']}").status_code == 404


def test_product_images_require_authorization_and_match_image_signature(world):
    admin, resident, app = world["admin"], world["resident"], world["app"]
    product = add_product(world)
    # A real 1x1 PNG, rather than an executable file renamed to .png.
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=")
    payload = {"filename": "photo.png", "content_type": "image/png", "data_base64": base64.b64encode(png).decode()}
    assert resident.post(f"/api/products/{product['id']}/image", json=payload).status_code == 403
    bad = {**payload, "data_base64": base64.b64encode(b"<script>bad</script>").decode()}
    assert admin.post(f"/api/products/{product['id']}/image", json=bad).status_code == 422
    response = admin.post(f"/api/products/{product['id']}/image", json=payload)
    assert response.status_code == 200, response.text
    url = response.json()["image_url"]
    image = resident.get(url)
    assert image.status_code == 200 and image.content == png
    assert image.headers["content-type"] == "image/png"
    assert image.headers["x-content-type-options"] == "nosniff"
    unauthenticated = TestClient(app)
    assert unauthenticated.get(url).status_code == 401
    unauthenticated.close()
    assert admin.put(f"/api/products/{product['id']}", json=product_body(id=product["id"], active=False, image_url=url)).status_code == 200
    assert resident.get(url).status_code == 404


def test_order_creates_owned_installation_ticket_reserves_stock_and_does_not_charge(world):
    product = add_product(world, stock=2)
    resident, other, admin = world["resident"], world["other"], world["admin"]
    response = resident.post("/api/orders", json={"product_id": product["id"], "quantity": 2, "installation": True, "comment": "Личное примечание не для аудита"})
    assert response.status_code == 201, response.text
    order = response.json()
    assert order["total"] == 2669.12 and order["installation_total"] == 200
    assert order["payment_status"] == "not_charged" and order["status"] == "requested"
    assert order["stock_status"] == "reserved_locally"
    assert admin.get(f"/api/products/{product['id']}").json()["stock"] == 0
    ticket = resident.get("/api/tickets/" + order["ticket_id"])
    assert ticket.status_code == 200 and ticket.json()["order_id"] == order["id"]
    assert ticket.json()["category"] == "installation"
    assert other.get("/api/tickets/" + order["ticket_id"]).status_code == 404
    assert other.get("/api/orders").json() == []
    assert len(resident.get("/api/orders").json()) == 1
    assert len(admin.get("/api/orders").json()) == 1
    assert "Личное примечание не для аудита" not in admin.get("/api/audit").text


def test_failed_order_does_not_create_ticket_or_consume_inventory(world):
    product = add_product(world, stock=1, installation_available=False)
    resident = world["resident"]
    before = len(resident.get("/api/tickets").json())
    assert resident.post("/api/orders", json={"product_id": product["id"], "quantity": 2}).status_code == 422
    assert resident.post("/api/orders", json={"product_id": product["id"], "installation": True}).status_code == 422
    assert resident.post("/api/orders", json={"product_id": product["id"], "quantity": True}).status_code == 422
    assert len(resident.get("/api/tickets").json()) == before
    assert world["admin"].get(f"/api/products/{product['id']}").json()["stock"] == 1


def test_simultaneous_requests_cannot_reserve_the_same_last_item_twice(world):
    product = add_product(world, stock=1)
    def request(client):
        return client.post("/api/orders", json={"product_id": product["id"], "quantity": 1}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(request, [world["resident"], world["other"]]))
    assert sorted(statuses) == [201, 422]
    assert world["admin"].get(f"/api/products/{product['id']}").json()["stock"] == 0
    assert len(world["admin"].get("/api/orders").json()) == 1


def test_order_storage_failure_rolls_back_ticket_stock_and_order_together(world):
    product = add_product(world, stock=1)
    app, store = world["app"], world["app"].state.store
    user = store.get("users", world["profiles"][0]["id"])
    tickets_before = len(store.list("tickets"))
    notifications_before = len(store.list("notifications"))
    store.db.execute("CREATE TRIGGER fail_fixture_order BEFORE INSERT ON records WHEN NEW.kind='orders' BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")
    store.db.commit()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            app.state.community.create_order(user, OrderWrite(product_id=product["id"]))
    finally:
        store.db.execute("DROP TRIGGER fail_fixture_order")
        store.db.commit()
    assert store.get("products", product["id"])["stock"] == 1
    assert len(store.list("tickets")) == tickets_before
    assert store.list("orders") == []
    assert len(store.list("notifications")) == notifications_before


def test_admin_news_filter_keeps_global_and_selected_house_audience(world):
    admin = world["admin"]
    global_news = admin.post("/api/news", json=news_body(title="Всем домам")).json()
    own_news = admin.post("/api/news", json=news_body(title="Первому дому", building_ids=[world["building"]["id"]])).json()
    other_news = admin.post("/api/news", json=news_body(title="Другому дому", building_ids=["another-house"])).json()
    ids = {n["id"] for n in admin.get("/api/news", params={"building_id": world["building"]["id"]}).json()}
    assert global_news["id"] in ids and own_news["id"] in ids and other_news["id"] not in ids
