"""MicroTurtles HTTP API: account, apartment and administrative boundaries."""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import threading
from urllib.parse import urlsplit
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from .auth import AuthService, Login, Setup, UserWrite, public_user, require_admin, require_user
from .engine import DeviceOffline, Engine, NotFound
from .models import Command, Configuration, Device, EmulationUpdate, OnlineUpdate, Rule, StateUpdate
from .properties import Building, Complex, Organization, PropertyService
from .operations import Operations
from .meters import MeterService
from .community import Community
from .seed import seed_devices, seed_rules
from .store import Store

ROOT = Path(__file__).resolve().parent.parent

def create_app(db_path=None, start_workers=True):
    store = Store(db_path or os.environ.get("STUDIO_DB", str(ROOT / "data" / "studio.db")))
    engine = Engine(store)
    if not store.get("meta", "initialized"):
        for device in seed_devices():
            item = Device.model_validate(device).model_dump()
            store.save("devices", item["id"], item)
        for rule in seed_rules():
            item = engine.validate_rule(rule)
            store.save("rules", item["id"], item)
        store.save("meta", "initialized", {"version": 1})
        engine.log("system", "MicroTurtles готов. Доступны три демонстрационных сценария.")
    properties = PropertyService(store)
    properties.migrate()
    auth = AuthService(store, properties)
    from .ujin import UjinAdapter
    integration = UjinAdapter(store, engine)
    engine.publish_hook = lambda device, values: integration.on_state(device["id"], values)
    engine.integration_summary = integration.summary
    operations = Operations(store, engine, properties, integration)
    meters = MeterService(store, properties, engine)
    community = Community(store, engine, properties, operations)
    stop = threading.Event()

    def emulator_loop():
        while not stop.wait(2):
            try:
                engine.process_due_jobs()
            except Exception as exc:
                engine.log("scenario_error", "Отложенное действие завершилось ошибкой", error=type(exc).__name__)
            if (store.get("meta", "emulation") or {}).get("enabled"):
                try:
                    engine.tick(2)
                except Exception as exc:
                    engine.log("emulator_error", "Такт эмуляции завершился ошибкой", error=type(exc).__name__)

    @asynccontextmanager
    async def lifespan(app):
        worker = None
        if start_workers:
            integration.start()
            worker = threading.Thread(target=emulator_loop, daemon=True, name="microturtles-emulators")
            worker.start()
        yield
        stop.set()
        integration_stopped = integration.stop() if start_workers else True
        if worker:
            worker.join(timeout=3)
        if integration_stopped is not False:
            store.close()

    app = FastAPI(title="MicroTurtles API", version="2.0.0", description="Устройства, ECA-сценарии, аккаунты и помещения.",
                  docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json", lifespan=lifespan)
    app.state.store, app.state.engine = store, engine
    app.state.integration, app.state.auth, app.state.properties = integration, auth, properties
    app.state.operations, app.state.meters = operations, meters
    app.state.community = community
    app.include_router(integration.router, dependencies=[Depends(require_admin)])
    app.include_router(operations.router)
    app.include_router(community.router)

    @app.exception_handler(NotFound)
    async def not_found(request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})
    @app.exception_handler(DeviceOffline)
    async def offline(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})
    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.middleware("http")
    async def account_boundary(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and request.method not in ("GET", "HEAD", "OPTIONS"):
            parsed = urlsplit(origin)
            if parsed.scheme != request.url.scheme or parsed.netloc.lower() != request.url.netloc.lower():
                return JSONResponse(status_code=403, content={"detail": "Изменения разрешены только из интерфейса этого сервера"})
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > 2_000_000:
            return JSONResponse(status_code=413, content={"detail": "Максимальный размер запроса — 2 МБ"})
        request.state.user = auth.resolve(request)
        public = {"/api/health", "/api/auth/status", "/api/auth/setup", "/api/auth/login", "/api/auth/logout"}
        if request.url.path.startswith("/api") and request.url.path not in public:
            if not request.state.user:
                return JSONResponse(status_code=401, content={"detail": "Войдите в MicroTurtles"})
            if request.url.path in ("/api/docs", "/api/openapi.json") and request.state.user["role"] != "admin":
                return JSONResponse(status_code=403, content={"detail": "Доступно только администратору"})
        if request.method in ("POST", "PUT", "PATCH") and len(await request.body()) > 2_000_000:
            return JSONResponse(status_code=413, content={"detail": "Максимальный размер запроса — 2 МБ"})
        response = await call_next(request)
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and response.status_code < 300 and request.state.user:
            # Route and actor metadata only; never bodies, query strings or cookies.
            operations.audit(request.state.user, "http." + request.method.lower(), request.url.path)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def device_for(request, device_id):
        user = require_user(request)
        device = engine.device(device_id)
        if not properties.can_read(user, device):
            raise NotFound("Устройство не найдено")
        return device

    def references(rule):
        return {part["device_id"] for part in [rule["trigger"], *rule["conditions"], *rule["actions"]] if part.get("device_id")}

    def rule_readable(user, rule, allowed):
        return user["role"] == "admin" or (references(rule).issubset(allowed) and rule.get("owner_id") in (None, user["id"]))

    def rule_for(request, rule_id, editable=False):
        user = require_user(request)
        item = store.get("rules", rule_id)
        allowed = {d["id"] for d in store.list("devices") if properties.can_access(user, d)}
        if item is None or not rule_readable(user, item, allowed):
            raise NotFound("Сценарий не найден")
        if editable and user["role"] != "admin" and item.get("owner_id") != user["id"]:
            raise HTTPException(403, "Этот сценарий управляется администратором")
        return item

    def validate_rule_access(request, payload):
        user = require_user(request)
        if user["role"] != "admin":
            if any(a["type"] == "delay" for a in payload["actions"]) or payload["retries"] > 1 or payload["on_error"] != "continue":
                raise HTTPException(403, "Задержки и расширенная обработка ошибок доступны администратору")
            for device_id in references(payload):
                device = device_for(request, device_id)
                if not properties.can_access(user, device):
                    raise HTTPException(403, "Общедомовые устройства недоступны для пользовательских правил")
            if any(a["type"] == "notification" and a["target"] != "resident" for a in payload["actions"]):
                raise HTTPException(403, "Жителю доступны уведомления только в своей ленте")
            payload["owner_id"] = user["id"]
        return payload

    def view(request, history_limit=150):
        user = require_user(request)
        all_devices = store.list("devices")
        accessible = {d["id"] for d in all_devices if properties.can_access(user, d)}
        filters = {key: request.query_params.get(key, "") for key in ("organization_id", "complex_id", "building_id", "entrance", "apartment", "scope")}
        if user["role"] != "admin":
            filters["scope"] = "apartment"
        visible_devices = properties.filter_devices(all_devices, user, filters)
        visible_ids = {d["id"] for d in visible_devices}
        filtering = any(filters.get(k) for k in ("organization_id", "complex_id", "building_id", "entrance", "apartment")) or filters["scope"] not in ("", "all")
        rules = [r for r in store.list("rules") if rule_readable(user, r, accessible)
                 and (not filtering or references(r).intersection(visible_ids))]
        rule_ids = {r["id"] for r in rules}
        def history_allowed(item):
            snapshot = item.get("location")
            if user["role"] == "admin":
                return not filtering or not snapshot or bool(properties.filter_devices([{"location": snapshot}], user, filters))
            if item.get("owner_id") not in (None, user["id"]):
                return False
            if snapshot and item.get("owner_id") == user["id"] and item.get("channel") in ("incidents", "tickets", "news"):
                return properties.can_read(user, {"location": snapshot, "common_access": {"resident_read": True}})
            return properties.can_access(user, {"location": snapshot}) if snapshot else True
        runs = [r for r in store.list("runs", 1000) if r["rule_id"] in rule_ids and history_allowed(r)][:history_limit]
        events = store.list("events", 1000)
        notifications = store.list("notifications", 1000)
        if user["role"] != "admin" or filtering:
            events = [e for e in events if (e.get("device_id") in visible_ids or e.get("rule_id") in rule_ids)
                      and history_allowed(e)]
            notifications = [n for n in notifications if history_allowed(n)
                             and (bool(n.get("location")) or n.get("rule_id") in rule_ids)
                             and (user["role"] == "admin" or n.get("target") == "resident")]
        events, notifications = events[:history_limit], notifications[:history_limit]
        if user["role"] != "admin" and not operations.profile(user)["notification_preferences"].get("scenarios", True):
            notifications = [n for n in notifications if n.get("channel") in ("incidents", "tickets", "news")]
        admin = user["role"] == "admin"
        base = engine.bootstrap()
        base.update(devices=visible_devices, rules=rules, events=events, runs=runs, notifications=notifications,
                    user=public_user(user), properties=properties.catalog(user),
                    health={d["id"]: properties.health(d) for d in visible_devices},
                    integration=base["integration"] if admin else {"mode": "restricted", "connected": False},
                    templates=base["templates"] if admin else [],
                    emulation=base["emulation"] if admin else {"enabled": False})
        base["stats"] = {"devices": len(visible_devices), "online": sum(d["online"] for d in visible_devices),
                         "rules": len(rules), "active_rules": sum(r["enabled"] for r in rules), "runs": len(runs),
                         "successful_runs": sum(r["status"] == "success" for r in runs),
                         "errors": sum(r["status"] == "error" for r in runs), "notifications": len(notifications),
                         "unread": sum(not n["read"] for n in notifications)}
        return base

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": "2.0.0", "storage": "sqlite", "mode": integration.summary().get("mode", "dry_run")}
    @app.get("/api/auth/status")
    def auth_status(request: Request):
        user = request.state.user
        return {"setup_required": auth.setup_required(), "authenticated": bool(user), "user": public_user(user) if user else None}
    @app.post("/api/auth/setup", status_code=201)
    def setup(body: Setup, request: Request, response: Response):
        return auth.setup(body, request, response)
    @app.post("/api/auth/login")
    def login(body: Login, request: Request, response: Response):
        return auth.login(body, request, response)
    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response):
        return auth.logout(request, response)
    @app.get("/api/auth/me")
    def me(request: Request):
        return public_user(require_user(request))
    @app.get("/api/users")
    def users(request: Request):
        require_admin(request)
        return auth.list_users()
    @app.post("/api/users", status_code=201)
    def add_user(body: UserWrite, request: Request):
        require_admin(request)
        with engine.lock:
            return auth.save_user(body)
    @app.put("/api/users/{user_id}")
    def edit_user(user_id: str, body: UserWrite, request: Request):
        require_admin(request)
        with engine.lock:
            return auth.save_user(body, user_id)

    @app.get("/api/properties")
    def directory(request: Request):
        return properties.catalog(require_user(request))
    @app.post("/api/properties/organizations", status_code=201)
    def add_organization(body: Organization, request: Request):
        require_admin(request)
        return properties.save_organization(body.model_dump())
    @app.put("/api/properties/organizations/{organization_id}")
    def edit_organization(organization_id: str, body: Organization, request: Request):
        require_admin(request)
        return properties.save_organization(body.model_dump(), organization_id)
    @app.delete("/api/properties/organizations/{organization_id}")
    def delete_organization(organization_id: str, request: Request):
        require_admin(request)
        return properties.delete_organization(organization_id)
    @app.post("/api/properties/complexes", status_code=201)
    def add_complex(body: Complex, request: Request):
        require_admin(request)
        return properties.save_complex(body.model_dump())
    @app.put("/api/properties/complexes/{complex_id}")
    def edit_complex(complex_id: str, body: Complex, request: Request):
        require_admin(request)
        return properties.save_complex(body.model_dump(), complex_id)
    @app.delete("/api/properties/complexes/{complex_id}")
    def delete_complex(complex_id: str, request: Request):
        require_admin(request)
        return properties.delete_complex(complex_id)
    @app.post("/api/properties/buildings", status_code=201)
    def add_building(body: Building, request: Request):
        require_admin(request)
        return properties.save_building(body.model_dump())
    @app.put("/api/properties/buildings/{building_id}")
    def edit_building(building_id: str, body: Building, request: Request):
        require_admin(request)
        return properties.save_building(body.model_dump(), building_id)
    @app.delete("/api/properties/buildings/{building_id}")
    def remove_building(building_id: str, request: Request):
        require_admin(request)
        return properties.delete_building(building_id)

    @app.get("/api/bootstrap")
    def bootstrap(request: Request):
        return view(request)
    @app.get("/api/devices")
    def devices(request: Request):
        return view(request)["devices"]
    @app.get("/api/common-devices")
    def common_devices(request: Request):
        return properties.common_devices(require_user(request), dict(request.query_params))
    @app.get("/api/meters")
    def meter_list(request: Request):
        return meters.list(require_user(request), dict(request.query_params))
    @app.post("/api/meters/{device_id}/readings", status_code=201)
    def meter_reading(device_id: str, payload: dict, request: Request):
        return meters.add_reading(device_id, payload, require_user(request))
    @app.post("/api/devices", status_code=201)
    def add_device(device: Device, request: Request):
        require_admin(request)
        with engine.lock:
            if store.get("devices", device.id):
                raise HTTPException(409, "Устройство с таким ID уже существует")
            payload = device.model_dump()
            payload["location"] = properties.validate_device_location(payload)
            return engine.save_device(payload)
    @app.get("/api/devices/{device_id}")
    def get_device(device_id: str, request: Request):
        return device_for(request, device_id)
    @app.put("/api/devices/{device_id}")
    def edit_device(device_id: str, device: Device, request: Request):
        require_admin(request)
        with engine.lock:
            previous = engine.device(device_id)
            payload = device.model_dump()
            payload["location"] = properties.validate_device_location(payload)
            result = engine.save_device(payload, device_id)
            if any(previous.get(key) != result.get(key) for key in ("location", "ujin", "capabilities")):
                affected = {job["rule_id"] for job in engine.scenarios.pending() if device_id in references(job["rule"])}
                for rule_id in affected:
                    engine.cancel_rule_jobs(rule_id)
            integration.on_configuration_change([previous], [result])
            return result
    @app.delete("/api/devices/{device_id}")
    def delete_device(device_id: str, request: Request):
        require_admin(request)
        with engine.lock:
            previous = engine.device(device_id)
            engine.delete_device(device_id)
            integration.on_configuration_change([previous], [])
            return {"ok": True}
    @app.post("/api/devices/{device_id}/state")
    def update_state(device_id: str, update: StateUpdate, request: Request):
        require_admin(request)
        return engine.update_state(device_id, update.values, event_id=update.event_id)
    @app.post("/api/devices/{device_id}/command")
    def command_device(device_id: str, command: Command, request: Request):
        with engine.lock:
            device = device_for(request, device_id)
            if not properties.can_command(request.state.user, device, command.signal):
                raise HTTPException(403, "Управление этим общедомовым сигналом не разрешено")
            result = engine.command(device_id, command.signal, command.value)
            if request.state.user["role"] != "admin":
                visible_rules = {r["id"] for r in view(request)["rules"]}
                result["runs"] = [r for r in result.get("runs", []) if r["rule_id"] in visible_rules]
            acknowledgement = store.append("commands", {"device_id": device_id, "signal": command.signal,
                "value": command.value, "user_id": request.state.user["id"], "location": device["location"],
                "status": "local_confirmed", "local_confirmed": True, "platform_confirmed": False,
                "mode": integration.summary().get("mode", "dry_run")})
            store.prune("commands", 2000)
            result["command"] = acknowledgement
            return result
    @app.post("/api/devices/{device_id}/online")
    def set_online(device_id: str, body: OnlineUpdate, request: Request):
        require_admin(request)
        with engine.lock:
            device = engine.device(device_id)
            device["online"] = body.online
            store.save("devices", device_id, device)
            engine.log("device", f"{device['name']}: " + ("в сети" if body.online else "недоступен"), device_id=device_id)
            return device

    @app.get("/api/rules")
    def rules(request: Request):
        return view(request)["rules"]
    @app.get("/api/rules/{rule_id}")
    def get_rule(rule_id: str, request: Request):
        return rule_for(request, rule_id)
    @app.post("/api/rules", status_code=201)
    def add_rule(rule: Rule, request: Request):
        with engine.lock:
            if store.get("rules", rule.id):
                raise HTTPException(409, "Сценарий с таким ID уже существует")
            return engine.save_rule(validate_rule_access(request, rule.model_dump()))
    @app.put("/api/rules/{rule_id}")
    def edit_rule(rule_id: str, rule: Rule, request: Request):
        with engine.lock:
            rule_for(request, rule_id, editable=True)
            return engine.save_rule(validate_rule_access(request, rule.model_dump()), rule_id)
    @app.delete("/api/rules/{rule_id}")
    def delete_rule(rule_id: str, request: Request):
        with engine.lock:
            rule_for(request, rule_id, editable=True)
            engine.cancel_rule_jobs(rule_id)
            store.delete("rules", rule_id)
            engine.log("rule", "Сценарий удалён", rule_id=rule_id)
            return {"ok": True}
    @app.post("/api/rules/{rule_id}/run")
    def run_rule(rule_id: str, request: Request):
        with engine.lock:
            rule_for(request, rule_id, editable=True)
            return engine.run_rule(rule_id)
    @app.get("/api/rules/{rule_id}/versions")
    def rule_versions(rule_id: str, request: Request):
        require_admin(request)
        rule_for(request, rule_id)
        return engine.rule_versions(rule_id)
    @app.post("/api/rules/{rule_id}/rollback")
    def rollback_rule(rule_id: str, payload: dict, request: Request):
        require_admin(request)
        rule_for(request, rule_id, editable=True)
        version_id = payload.get("version_id")
        if not isinstance(version_id, str):
            raise ValueError("Укажите версию сценария")
        return engine.rollback_rule(rule_id, version_id)
    @app.get("/api/events")
    def events(request: Request, limit: int = Query(default=150, ge=1, le=1000)):
        return view(request, limit)["events"]
    @app.get("/api/runs")
    def runs(request: Request, limit: int = Query(default=100, ge=1, le=1000)):
        return view(request, limit)["runs"]
    @app.get("/api/notifications")
    def notifications(request: Request, limit: int = Query(default=100, ge=1, le=1000)):
        return view(request, limit)["notifications"]
    @app.post("/api/notifications/read")
    def read_notifications(request: Request):
        for item in view(request, 1000)["notifications"]:
            item["read"] = True
            store.save("notifications", item["id"], item)
        return {"ok": True}
    @app.post("/api/demo/{name}")
    def demo(name: str, request: Request):
        require_admin(request)
        return engine.demo(name)
    @app.post("/api/emulation")
    def emulation(body: EmulationUpdate, request: Request):
        require_admin(request)
        store.save("meta", "emulation", {"enabled": body.enabled})
        engine.log("system", "Физическая эмуляция " + ("запущена" if body.enabled else "приостановлена"))
        return {"enabled": body.enabled}
    @app.get("/api/export")
    def export_configuration(request: Request):
        require_admin(request)
        with engine.lock:
            return {"schema_version": 1, "devices": store.list("devices"), "rules": store.list("rules"),
                    "organizations": store.list("organizations"), "complexes": store.list("complexes"), "buildings": store.list("buildings")}
    @app.post("/api/import")
    def import_configuration(body: Configuration, request: Request):
        require_admin(request)
        devices = [d.model_dump() for d in body.devices]
        rules = [r.model_dump() for r in body.rules]
        if len({d["id"] for d in devices}) != len(devices) or len({r["id"] for r in rules}) != len(rules):
            raise ValueError("ID устройств и сценариев должны быть уникальны")
        with engine.lock:
            scratch = Store(":memory:")
            try:
                validator = PropertyService(scratch)
                catalog = {}
                for kind, cls, saver in (("organizations", Organization, validator.save_organization),
                                         ("complexes", Complex, validator.save_complex), ("buildings", Building, validator.save_building)):
                    proposed = getattr(body, kind) if getattr(body, kind) is not None else store.list(kind)
                    items = [cls.model_validate(item).model_dump() for item in proposed]
                    if len({i["id"] for i in items}) != len(items):
                        raise ValueError("ID справочника должны быть уникальны")
                    for item in items:
                        saver(item)
                    catalog[kind] = items
                for device in devices:
                    device["location"] = validator.validate_device_location(device)
                    # Imported timestamps are historical data, not a new device receipt.
                    device["last_seen"], device["last_seen_source"] = None, None
                for user in store.list("users"):
                    if user["role"] == "resident":
                        validator.validate_resident_location(user["location"])
                validated_rules = [engine.validate_rule(r, {d["id"]: d for d in devices}) for r in rules]
            finally:
                scratch.close()
            previous = store.list("devices")
            for previous_rule in store.list("rules"):
                engine.cancel_rule_jobs(previous_rule["id"])
            with store.lock, store.db:
                for kind, items in {**catalog, "devices": devices, "rules": validated_rules}.items():
                    store.db.execute("DELETE FROM records WHERE kind=?", (kind,))
                    store.db.executemany("INSERT INTO records(kind,id,payload) VALUES(?,?,?)", [(kind, item["id"], json.dumps(item, ensure_ascii=False, allow_nan=False)) for item in items])
            integration.on_configuration_change(previous, devices)
            engine.log("system", "Конфигурация импортирована", devices=len(devices), rules=len(rules))
        return {"ok": True, "devices": len(devices), "rules": len(rules)}
    @app.get("/api/stream")
    async def stream(request: Request):
        async def generate():
            last = None
            while not await request.is_disconnected():
                user = auth.resolve(request)
                if not user:
                    break
                request.state.user = user
                latest = view(request)["events"]
                marker = latest[0]["id"] if latest else "empty"
                if marker != last:
                    yield "event: change\ndata: " + json.dumps({"id": marker}) + "\n\n"
                    last = marker
                else:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(2)
        return StreamingResponse(generate(), media_type="text/event-stream")
    @app.get("/")
    def index():
        return FileResponse(ROOT / "static" / "index.html")
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    return app

app = create_app()
