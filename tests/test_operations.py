import base64
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from app.main import create_app

PASSWORD = "Operations-test-password-2026"

@pytest.fixture
def clients(tmp_path):
    app = create_app(tmp_path / "operations.sqlite", start_workers=False)
    with TestClient(app) as admin:
        setup = admin.post("/api/auth/setup", json={"username": "admin", "password": PASSWORD}).json()
        building = admin.get("/api/properties").json()["buildings"][0]
        location = {"building_id": building["id"], "entrance": building["entrances"][0]["number"],
                    "apartment": building["entrances"][0]["apartments"][0]["number"]}
        other_location = {**location, "apartment": "other-test-apartment"}
        building["entrances"][0]["apartments"].append({"number": other_location["apartment"], "floor": None})
        assert admin.put("/api/properties/buildings/" + building["id"], json=building).status_code == 200
        ids = {}
        for username, assigned in (("resident", location), ("neighbour", other_location)):
            response = admin.post("/api/users", json={"username": username, "display_name": username,
                "role": "resident", "location": assigned, "password": PASSWORD})
            assert response.status_code == 201, response.text
            ids[username] = response.json()["id"]
        resident, neighbour = TestClient(app), TestClient(app)
        assert resident.post("/api/auth/login", json={"username": "resident", "password": PASSWORD}).status_code == 200
        assert neighbour.post("/api/auth/login", json={"username": "neighbour", "password": PASSWORD}).status_code == 200
        yield admin, resident, neighbour, {**ids, "admin": setup["user"]["id"], "location": location, "other_location": other_location}

def ticket(resident, **extra):
    response = resident.post("/api/tickets", json={"title": "Течёт кран", "description": "Нужна проверка сантехника", "category": "plumbing", **extra})
    assert response.status_code == 201, response.text
    return response.json()

def test_ticket_location_is_server_derived_and_other_resident_cannot_access(clients):
    admin, resident, neighbour, ids = clients
    item = ticket(resident, location=ids["other_location"])
    assert item["owner_id"] == ids["resident"]
    assert item["location"]["apartment"] == ids["location"]["apartment"]
    assert neighbour.get("/api/tickets").json() == []
    for path in ("/api/tickets/" + item["id"],):
        assert neighbour.get(path).status_code == 404
        assert neighbour.patch(path, json={"status": "cancelled"}).status_code == 404
    assert admin.get("/api/tickets?apartment=" + ids["other_location"]["apartment"]).json() == []

def test_ticket_workflow_assignment_sla_and_rating(clients):
    admin, resident, _, ids = clients
    item = ticket(resident, priority="urgent")
    due = datetime.fromisoformat(item["due_at"])
    assert 0 < (due - datetime.now(timezone.utc)).total_seconds() <= 3600
    path = "/api/tickets/" + item["id"]
    assert resident.post(path + "/rating", json={"rating": 5}).status_code == 403
    assert admin.patch(path, json={"status": "completed"}).status_code == 422
    assert admin.patch(path, json={"assignee_id": ids["resident"]}).status_code == 422
    assert admin.patch(path, json={"status": "in_progress", "assignee_id": ids["admin"]}).status_code == 200
    assert resident.patch(path, json={"priority": "high"}).status_code == 422
    assert admin.patch(path, json={"status": "completed"}).status_code == 200
    rated = resident.post(path + "/rating", json={"rating": 5, "comment": "Всё исправлено"})
    assert rated.status_code == 200
    assert rated.json()["rating"]["value"] == 5
    assert len(rated.json()["history"]) == 3
    assert resident.post(path + "/rating", json={"rating": True}).status_code == 422

def test_comments_internal_notes_and_authenticated_attachment(clients):
    admin, resident, neighbour, _ = clients
    item = ticket(resident)
    path = "/api/tickets/" + item["id"]
    assert admin.post(path + "/comments", json={"text": "Нужны запчасти", "internal": True}).status_code == 201
    assert resident.get(path).json()["comments"] == []
    assert resident.post(path + "/comments", json={"text": "Скрытая заметка", "internal": True}).status_code == 403
    assert admin.post(path + "/comments", json={"text": "Мастер придёт сегодня"}).status_code == 201
    assert len(resident.get(path).json()["comments"]) == 1
    uploaded = resident.post(path + "/attachments", json={"filename": "../../problem.txt", "content_type": "text/plain", "data_base64": base64.b64encode(b"fixture content").decode()})
    assert uploaded.status_code == 201
    metadata = uploaded.json()
    assert metadata["filename"] == "problem.txt" and "data_base64" not in metadata
    assert resident.get(metadata["url"]).content == b"fixture content"
    assert "attachment" in resident.get(metadata["url"]).headers["content-disposition"]
    assert neighbour.get(metadata["url"]).status_code == 404
    assert TestClient(admin.app).get(metadata["url"]).status_code == 401
    assert resident.post(path + "/attachments", json={"filename": "a.txt", "data_base64": "%%%"}).status_code == 422

def test_incidents_follow_health_ack_and_resolve_and_object_filters(clients):
    admin, resident, neighbour, ids = clients
    assert admin.get("/api/incidents").json() == []
    admin.post("/api/devices/leak-1/state", json={"values": {"leak": True}})
    incident = next(i for i in admin.get("/api/incidents").json() if i["device_id"] == "leak-1")
    assert incident["priority"] == "critical" and incident["status"] == "open"
    assert any(i["id"] == incident["id"] for i in resident.get("/api/incidents").json())
    assert neighbour.get("/api/incidents").json() == []
    assert admin.get("/api/incidents?apartment=" + ids["other_location"]["apartment"]).json() == []
    assert resident.post("/api/incidents/" + incident["id"] + "/ack").status_code == 403
    assert admin.post("/api/incidents/" + incident["id"] + "/ack").json()["status"] == "acknowledged"
    assert admin.patch("/api/incidents/" + incident["id"], json={"status": "resolved"}).status_code == 422
    linked = ticket(resident, incident_id=incident["id"])
    assert linked["device_id"] == "leak-1" and linked["incident_id"] == incident["id"]
    admin.post("/api/devices/leak-1/state", json={"values": {"leak": False}})
    resolved = next(i for i in admin.get("/api/incidents").json() if i["id"] == incident["id"])
    assert resolved["status"] == "resolved" and resolved["active"] is False
    assert len(resolved["history"]) >= 3

def test_common_device_ticket_preserves_common_placement(clients):
    admin, resident, neighbour, _ = clients
    item = ticket(resident, device_id="barrier-1")
    assert item["location"]["scope"] == "common" and item["location"]["apartment"] == ""
    assert resident.get("/api/tickets/" + item["id"]).status_code == 200
    assert neighbour.get("/api/tickets/" + item["id"]).status_code == 404
    assert any(t["id"] == item["id"] for t in admin.get("/api/tickets?scope=common").json())

def test_notification_preferences_work_and_cannot_escalate_role(clients):
    admin, resident, _, _ = clients
    response = resident.put("/api/profile", json={"display_name": "Житель", "notification_preferences": {"tickets": False}})
    assert response.status_code == 200 and response.json()["notification_preferences"]["tickets"] is False
    ticket(resident)
    assert not any(n.get("channel") == "tickets" for n in resident.get("/api/notifications").json())
    assert resident.put("/api/profile", json={"role": "admin"}).status_code == 422
    assert resident.put("/api/profile", json={"notification_preferences": {"tickets": "false"}}).status_code == 422
    assert resident.get("/api/auth/me").json()["role"] == "resident"

def test_audit_and_backup_are_admin_only_and_secret_free(clients):
    admin, resident, _, _ = clients
    item = ticket(resident)
    backup = admin.post("/api/backups", json={"name": "До изменения"})
    assert backup.status_code == 201
    path = "/api/backups/" + backup.json()["id"]
    snapshot = admin.get(path).json()["snapshot"]
    assert set(snapshot) == {"schema_version", "devices", "rules", "buildings", "organizations", "complexes"}
    serialized = str(snapshot)
    assert PASSWORD not in serialized and "password_hash" not in serialized and "sessions" not in serialized
    device = admin.get("/api/devices/light-1").json()
    original = device["name"]
    device["name"] = "Изменённое имя"
    assert admin.put("/api/devices/light-1", json=device).status_code == 200
    assert admin.post(path + "/restore").status_code == 200
    assert admin.get("/api/devices/light-1").json()["name"] == original
    assert resident.get("/api/audit").status_code == 403
    assert resident.get(path).status_code == 403
    assert resident.post(path + "/restore").status_code == 403
    audit = str(admin.get("/api/audit").json())
    assert "backup.restored" in audit and "ticket.created" in audit
    assert PASSWORD not in audit and "data_base64" not in audit and "password_hash" not in audit
