"""Account and apartment isolation through the HTTP boundary, not UI hiding."""
from copy import deepcopy
import hashlib
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.auth import COOKIE, PASSWORD_ITERATIONS
from app.main import create_app


ADMIN_PASSWORD = "Test-admin-password-2026"
RESIDENT_PASSWORD = "Test-resident-password-2026"


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path / "auth.sqlite", start_workers=False)


@pytest.fixture
def admin(app):
    with TestClient(app) as client:
        response = client.post("/api/auth/setup", json={"username": "administrator", "password": ADMIN_PASSWORD})
        assert response.status_code == 201, response.text
        yield client


@pytest.fixture
def household(admin):
    building = admin.get("/api/properties").json()["buildings"][0]
    entrance = building["entrances"][0]
    own_apartment = entrance["apartments"][0]["number"]
    other_apartment = "3008" if own_apartment != "3008" else "3009"
    entrance["apartments"].append({"number": other_apartment, "floor": None})
    assert admin.put(f"/api/properties/buildings/{building['id']}", json=building).status_code == 200
    profiles = []
    for username, apartment in (("resident-a", own_apartment), ("resident-b", other_apartment)):
        profile = {"username": username, "display_name": username, "password": RESIDENT_PASSWORD, "role": "resident",
                   "location": {"building_id": building["id"], "entrance": entrance["number"], "apartment": apartment}}
        response = admin.post("/api/users", json=profile)
        assert response.status_code == 201, response.text
        profiles.append(response.json())
    foreign = deepcopy(admin.get("/api/devices/light-1").json())
    foreign.update(id="foreign-light", name="Чужая квартира: личный свет")
    foreign["location"]["apartment"] = other_apartment
    assert admin.post("/api/devices", json=foreign).status_code == 201
    clients = []
    for profile in profiles:
        client = TestClient(admin.app)
        assert client.post("/api/auth/login", json={"username": profile["username"], "password": RESIDENT_PASSWORD}).status_code == 200
        clients.append(client)
    yield {"admin": admin, "resident": clients[0], "other": clients[1], "profiles": profiles, "building": building}
    for client in clients:
        client.close()


def own_rule(rule_id="resident-rule"):
    return {"id": rule_id, "name": "Мой свет", "trigger": {"device_id": "light-1", "signal": "power", "operator": "eq", "value": True},
            "actions": [{"type": "notification", "target": "resident", "message": "Личный сценарий жителя"}],
            "cooldown_seconds": 0}


@pytest.mark.parametrize("method,path,payload", [
    ("GET", "/api/bootstrap", None), ("GET", "/api/devices", None),
    ("GET", "/api/devices/light-1", None), ("GET", "/api/events", None),
    ("GET", "/api/runs", None), ("GET", "/api/notifications", None),
    ("GET", "/api/users", None), ("GET", "/api/export", None),
    ("GET", "/api/properties", None), ("GET", "/api/integration", None),
    ("GET", "/api/openapi.json", None), ("GET", "/api/stream", None),
    ("POST", "/api/demo/leak", {}),
])
def test_unauthenticated_clients_cannot_read_or_change_data(app, method, path, payload):
    with TestClient(app) as client:
        response = client.request(method, path, json=payload)
        assert response.status_code == 401
        assert client.get("/api/health").status_code == 200


def test_first_setup_validates_password_and_can_only_run_once(app):
    with TestClient(app) as client:
        assert client.get("/api/auth/status").json() == {"setup_required": True, "authenticated": False, "user": None}
        assert client.post("/api/auth/setup", json={"username": "admin", "password": "short"}).status_code == 422
        assert app.state.store.list("users") == []
        response = client.post("/api/auth/setup", json={"username": "Admin", "password": ADMIN_PASSWORD})
        assert response.status_code == 201
        assert response.json()["user"]["username"] == "admin"
        assert client.post("/api/auth/setup", json={"username": "other", "password": ADMIN_PASSWORD}).status_code == 409
        assert client.get("/api/auth/status").json()["authenticated"] is True


def test_passwords_are_salted_and_sessions_are_opaque_http_only(admin):
    cookie = admin.cookies.get(COOKIE)
    stored = admin.app.state.store.list("users")[0]
    assert ADMIN_PASSWORD not in json.dumps(stored)
    assert stored["password_hash"].startswith(f"pbkdf2_sha256${PASSWORD_ITERATIONS}$")
    hashed_token = hashlib.sha256(cookie.encode()).hexdigest()
    session = admin.app.state.store.get("sessions", hashed_token)
    assert session["user_id"] == stored["id"]
    assert cookie not in json.dumps(admin.app.state.store.list("sessions"))
    response = admin.post("/api/auth/login", json={"username": "ADMINISTRATOR", "password": ADMIN_PASSWORD})
    header = response.headers["set-cookie"].lower()
    assert "httponly" in header and "samesite=strict" in header
    assert "max-age=43200" in header
    assert "password_hash" not in admin.get("/api/users").text
    assert "password" not in admin.get("/api/auth/me").json()


def test_logout_revokes_replayed_cookie(admin):
    cookie = admin.cookies.get(COOKIE)
    assert admin.post("/api/auth/logout").status_code == 200
    admin.cookies.set(COOKIE, cookie)
    assert admin.get("/api/bootstrap").status_code == 401
    assert admin.app.state.store.get("sessions", hashlib.sha256(cookie.encode()).hexdigest()) is None


def test_expired_session_is_deleted_and_denied(admin):
    cookie = admin.cookies.get(COOKIE)
    key = hashlib.sha256(cookie.encode()).hexdigest()
    session = admin.app.state.store.get("sessions", key)
    session["expires_at"] = time.time() - 1
    admin.app.state.store.save("sessions", key, session)
    assert admin.get("/api/bootstrap").status_code == 401
    assert admin.app.state.store.get("sessions", key) is None


def test_failed_logins_are_rate_limited(admin):
    for _ in range(8):
        assert admin.post("/api/auth/login", json={"username": "administrator", "password": "wrong-password"}).status_code == 401
    assert admin.post("/api/auth/login", json={"username": "administrator", "password": ADMIN_PASSWORD}).status_code == 429


def test_last_administrator_cannot_be_disabled_or_demoted(admin):
    profile = admin.get("/api/auth/me").json()
    payload = {k: profile[k] for k in ("username", "display_name", "role", "location", "active")}
    payload["active"] = False
    assert admin.put(f"/api/users/{profile['id']}", json=payload).status_code == 422
    payload["active"] = True
    payload["role"] = "resident"
    building = admin.get("/api/properties").json()["buildings"][0]
    payload["location"] = {"building_id": building["id"], "entrance": building["entrances"][0]["number"],
                           "apartment": building["entrances"][0]["apartments"][0]["number"]}
    assert admin.put(f"/api/users/{profile['id']}", json=payload).status_code == 422


def test_resident_private_scope_and_explicit_common_permissions(household):
    resident, other = household["resident"], household["other"]
    data = resident.get("/api/bootstrap").json()
    assert "foreign-light" not in {d["id"] for d in data["devices"]}
    assert all(d["location"]["apartment"] == household["profiles"][0]["location"]["apartment"]
               for d in data["devices"] if d["location"]["scope"] == "apartment")
    assert resident.get("/api/devices/foreign-light").status_code == 404
    assert other.get("/api/devices/light-1").status_code == 404
    assert resident.post("/api/devices/foreign-light/command", json={"signal": "power", "value": False}).status_code == 404
    assert resident.post("/api/devices/elevator-1/command", json={"signal": "evacuation", "value": True}).status_code in (403, 404)
    assert resident.get("/api/bootstrap?apartment=" + household["profiles"][1]["location"]["apartment"]).json()["devices"] == []
    catalog = resident.get("/api/properties").json()["buildings"][0]
    assert [a["number"] for e in catalog["entrances"] for a in e["apartments"]] == [household["profiles"][0]["location"]["apartment"]]
    assert data["templates"] == [] and data["integration"]["mode"] == "restricted"


@pytest.mark.parametrize("method,path,payload", [
    ("GET", "/api/users", None), ("GET", "/api/integration", None), ("GET", "/api/export", None),
    ("POST", "/api/integration/sync", {}), ("POST", "/api/integration/check", {}),
    ("POST", "/api/demo/leak", {}), ("POST", "/api/emulation", {"enabled": True}),
    ("POST", "/api/devices/light-1/state", {"values": {"power": False}}),
    ("POST", "/api/devices/light-1/online", {"online": False}),
    ("DELETE", "/api/devices/light-1", None),
    ("GET", "/api/docs", None), ("GET", "/api/openapi.json", None),
])
def test_resident_is_denied_administrative_operations(household, method, path, payload):
    assert household["resident"].request(method, path, json=payload).status_code == 403


def test_resident_rule_ownership_is_assigned_by_server_and_checked_on_every_mutation(household):
    admin, resident, other = household["admin"], household["resident"], household["other"]
    payload = own_rule()
    payload["owner_id"] = household["profiles"][1]["id"]
    response = resident.post("/api/rules", json=payload)
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["owner_id"] == household["profiles"][0]["id"]
    assert other.put("/api/rules/resident-rule", json=created).status_code == 404
    assert other.delete("/api/rules/resident-rule").status_code == 404
    assert other.post("/api/rules/resident-rule/run").status_code == 404
    assert resident.post("/api/rules/resident-rule/run").status_code == 200
    assert resident.delete("/api/rules/resident-rule").status_code == 200
    seeded = next(r for r in admin.get("/api/rules").json() if r["id"] == "rule-leak")
    assert resident.put("/api/rules/rule-leak", json=seeded).status_code == 403
    assert resident.delete("/api/rules/rule-leak").status_code == 403
    assert resident.post("/api/rules/rule-leak/run").status_code == 403


def test_resident_cannot_reference_foreign_or_ungranted_common_command_in_automation(household):
    resident = household["resident"]
    foreign = own_rule()
    foreign["trigger"]["device_id"] = "foreign-light"
    assert resident.post("/api/rules", json=foreign).status_code == 404
    common = own_rule()
    common["actions"] = [{"type": "command", "device_id": "elevator-1", "signal": "evacuation", "value": True}]
    assert resident.post("/api/rules", json=common).status_code in (403, 404)
    concierge = own_rule()
    concierge["actions"][0]["target"] = "concierge"
    assert resident.post("/api/rules", json=concierge).status_code == 403


def test_resident_events_runs_notifications_do_not_leak_foreign_activity(household):
    admin, resident, other = household["admin"], household["resident"], household["other"]
    assert admin.post("/api/devices/foreign-light/command", json={"signal": "power", "value": False}).status_code == 200
    assert admin.post("/api/demo/leak").status_code == 200
    data = resident.get("/api/bootstrap").json()
    assert all(e.get("device_id") != "foreign-light" for e in data["events"])
    assert all(n["target"] == "resident" for n in data["notifications"])
    assert len(data["notifications"]) == 1
    assert other.get("/api/notifications").json() == []
    assert other.get("/api/runs").json() == []
    assert all(e.get("device_id") not in {"leak-1", "valve-1"} for e in other.get("/api/events").json())
    assert resident.post("/api/notifications/read").status_code == 200
    assert len([n for n in admin.get("/api/notifications").json() if n["read"]]) == 1


def test_changing_account_password_revokes_old_sessions_and_never_exports_auth_records(household):
    admin, resident = household["admin"], household["resident"]
    profile = household["profiles"][0]
    payload = {k: profile[k] for k in ("username", "display_name", "role", "location", "active")}
    payload["password"] = "New-resident-password-2026"
    assert admin.put(f"/api/users/{profile['id']}", json=payload).status_code == 200
    assert resident.get("/api/bootstrap").status_code == 401
    assert resident.post("/api/auth/login", json={"username": profile["username"], "password": RESIDENT_PASSWORD}).status_code == 401
    assert resident.post("/api/auth/login", json={"username": profile["username"], "password": payload["password"]}).status_code == 200
    exported = admin.get("/api/export").json()
    assert set(exported) == {"schema_version", "devices", "rules", "organizations", "complexes", "buildings"}
    assert "password_hash" not in json.dumps(exported)
    assert "pbkdf2_sha256" not in json.dumps(exported)


def test_foreign_origin_cannot_login_or_initialize_an_account(app):
    with TestClient(app) as client:
        response = client.post("/api/auth/setup", json={"username": "attacker", "password": ADMIN_PASSWORD}, headers={"Origin": "https://foreign.example"})
        assert response.status_code == 403
        assert app.state.store.list("users") == []


def test_device_relocation_does_not_reveal_previous_apartment_event_history(household):
    admin, resident = household["admin"], household["resident"]
    admin.post("/api/devices/foreign-light/command", json={"signal": "power", "value": False})
    before = {e["id"] for e in admin.get("/api/events").json() if e.get("device_id") == "foreign-light"}
    device = admin.get("/api/devices/foreign-light").json()
    device["location"]["apartment"] = household["profiles"][0]["location"]["apartment"]
    assert admin.put("/api/devices/foreign-light", json=device).status_code == 200
    events = resident.get("/api/events").json()
    assert not before.intersection(e["id"] for e in events), "Historical apartment B events must not become visible when its device moves to A"


def test_rule_relocation_does_not_reveal_previous_apartment_runs_or_notifications(household):
    admin, resident = household["admin"], household["resident"]
    rule = own_rule("foreign-rule")
    rule["trigger"]["device_id"] = "foreign-light"
    rule["actions"][0]["message"] = "История чужой квартиры до переноса"
    assert admin.post("/api/rules", json=rule).status_code == 201
    old_run = admin.post("/api/rules/foreign-rule/run").json()
    device = admin.get("/api/devices/foreign-light").json()
    device["location"]["apartment"] = household["profiles"][0]["location"]["apartment"]
    assert admin.put("/api/devices/foreign-light", json=device).status_code == 200
    assert old_run["id"] not in {r["id"] for r in resident.get("/api/runs").json()}
    assert all(n["message"] != rule["actions"][0]["message"] for n in resident.get("/api/notifications").json())


def test_administrator_location_filter_includes_resident_owned_notifications(household):
    admin, resident = household["admin"], household["resident"]
    assert resident.post("/api/rules", json=own_rule()).status_code == 201
    assert resident.post("/api/rules/resident-rule/run").status_code == 200
    profile = household["profiles"][0]
    notifications = admin.get("/api/notifications", params=profile["location"]).json()
    assert any(n["message"] == "Личный сценарий жителя" for n in notifications)


def test_common_inventory_is_separate_and_only_explicit_commands_are_permitted(household):
    resident = household["resident"]
    private = resident.get("/api/bootstrap").json()["devices"]
    assert all(d["location"]["scope"] == "apartment" for d in private)
    response = resident.get("/api/common-devices")
    assert response.status_code == 200
    common = response.json()
    if isinstance(common, dict):
        common = common["devices"]
    assert {"barrier-1", "elevator-1", "corridor-meter-1"}.issubset(d["id"] for d in common)
    assert all(d["location"]["scope"] == "common" for d in common)
    assert resident.post("/api/devices/barrier-1/command", json={"signal": "open", "value": True}).status_code == 200
    assert resident.post("/api/devices/elevator-1/command", json={"signal": "evacuation", "value": True}).status_code in (403, 404)
