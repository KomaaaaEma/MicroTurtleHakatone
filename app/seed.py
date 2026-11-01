"""Declarative templates, reused by constructor, emulator and scenario engine."""
from copy import deepcopy


def cap(key, label, type="number", role="measurement", default=0, unit="", min=None, max=None):
    return dict(key=key, label=label, type=type, role=role, default=default, unit=unit, min=min, max=max)


TEMPLATES = [
    dict(kind="leak", name="Датчик протечки", room="Ванная", capabilities=[cap("leak", "Протечка", "boolean", "event", False), cap("battery", "Батарея", default=98, unit="%", min=0, max=100)]),
    dict(kind="valve", name="Клапан воды", room="Ванная", capabilities=[cap("closed", "Вода перекрыта", "boolean", "command", False)]),
    dict(kind="thermostat", name="Термостат", room="Гостиная", capabilities=[cap("temperature", "Температура", default=23.4, unit="°C", min=-40, max=80), cap("humidity", "Влажность", default=45, unit="%", min=0, max=100), cap("target_temp", "Целевая температура", role="property", default=22, unit="°C", min=5, max=35), cap("mode", "Режим", "string", "command", "auto")]),
    dict(kind="light", name="Умный свет", room="Прихожая", capabilities=[cap("power", "Питание", "boolean", "command", True), cap("brightness", "Яркость", role="property", default=75, unit="%", min=0, max=100)]),
    dict(kind="barrier", name="Шлагбаум", room="Паркинг", capabilities=[cap("open", "Открыт", "boolean", "command", False), cap("obstacle", "Препятствие", "boolean", "event", False)]),
    dict(kind="meter", name="Счётчик воды", room="Ванная", capabilities=[cap("total", "Расход", default=124.32, unit="м³", min=0), cap("flow", "Текущий поток", default=0.12, unit="м³/ч", min=0, max=50)]),
    dict(kind="fire", name="Датчик дыма", room="Кухня", capabilities=[cap("smoke", "Дым обнаружен", "boolean", "event", False), cap("battery", "Батарея", default=100, unit="%", min=0, max=100)]),
    dict(kind="ventilation", name="Вентиляция", room="Гостиная", capabilities=[cap("power", "Питание", "boolean", "command", True), cap("speed", "Скорость", role="property", default=1, min=0, max=3), cap("co2", "CO₂", default=580, unit="ppm", min=0, max=10000)]),
    dict(kind="elevator", name="Лифт", room="Подъезд", capabilities=[cap("floor", "Этаж назначения", role="command", default=1, min=1, max=25), cap("evacuation", "Пожарный режим", "boolean", "command", False), cap("door_open", "Двери открыты", "boolean", "event", False)]),
    dict(kind="lock", name="Умный замок", room="Прихожая", capabilities=[cap("locked", "Закрыт", "boolean", "command", True), cap("access", "Доступ разрешён", "boolean", "event", False)]),
    dict(kind="camera", name="Камера входа", room="Прихожая", capabilities=[cap("motion", "Движение", "boolean", "event", False), cap("recording", "Запись", "boolean", "command", False)]),
    dict(kind="intercom", name="Домофон подъезда · эмулятор", room="Входная группа", capabilities=[cap("door_open", "Открыть дверь", "boolean", "command", False), cap("call", "Вызов домофона", "boolean", "event", False)]),
    dict(id="common-camera-1", kind="camera", name="Камера подъезда · эмулятор", room="Входная группа", capabilities=[cap("motion", "Движение", "boolean", "event", False), cap("recording", "Запись", "boolean", "command", False)]),
    dict(kind="custom", name="Новое устройство", room="Квартира", capabilities=[cap("value", "Значение", default=0)])
]


def seed_devices():
    result = []
    for template in TEMPLATES[:-1]:
        device = deepcopy(template)
        location = dict(building_id="building-1", entrance="1", apartment="3007", floor=None, scope="apartment")
        identifier = device.get("id") or device["kind"] + "-1"
        if device["kind"] in ("barrier", "elevator", "intercom") or identifier == "common-camera-1":
            location.update(scope="common", apartment="", entrance="" if device["kind"] == "barrier" else "1", floor=None)
        device.update(id=identifier, online=True, location=location, state={c["key"]: c["default"] for c in device["capabilities"]}, ujin={"serialnumber": "", "ext_guid": "", "model": "parkcloud_device", "device_class": "integrations_devices", "signal_map": {}})
        device["common_access"] = {"resident_read": True, "resident_commands": {"barrier": ["open"], "elevator": ["floor"], "intercom": ["door_open"]}.get(device["kind"], [])}
        if device["kind"] == "meter":
            device["meter"] = {"type": "cold_water", "unit": "м³", "manual_allowed": True, "stale_after_hours": 48, "max_daily_usage": 5}
        result.append(device)
    result.append(corridor_meter())
    result.extend(additional_meters())
    return result


def additional_common_devices():
    """Signals-only virtual hardware; no camera feed or real door is connected."""
    return [item for item in seed_devices() if item["id"] in ("intercom-1", "common-camera-1")]


def corridor_meter():
    """A separate communal meter, rather than relabeling a resident's meter."""
    return dict(id="corridor-meter-1", kind="meter", name="Счётчик освещения этажа", room="Коридор", online=True,
                location=dict(building_id="building-1", entrance="1", apartment="", floor=None, scope="common"),
                capabilities=[cap("total", "Потребление", default=530.4, unit="кВт·ч", min=0),
                              cap("power", "Мощность", default=0.15, unit="кВт", min=0, max=100)],
                state={"total": 530.4, "power": 0.15},
                common_access={"resident_read": True, "resident_commands": []},
                meter={"type": "electricity", "unit": "кВт·ч", "manual_allowed": False, "stale_after_hours": 48, "max_daily_usage": 100},
                ujin={"serialnumber": "", "ext_guid": "", "model": "parkcloud_device", "device_class": "integrations_devices", "signal_map": {}})


def additional_meters():
    result = []
    for identifier, name, kind, unit, value, maximum in [
        ("hot-water-meter-1", "Счётчик горячей воды", "hot_water", "м³", 86.15, 5),
        ("electricity-meter-1", "Счётчик электроэнергии", "electricity", "кВт·ч", 2840.2, 100),
        ("heating-meter-1", "Счётчик отопления", "heating", "Гкал", 18.42, 1),
    ]:
        result.append(dict(id=identifier, kind="meter", name=name, room="Щит учёта", online=True,
                           location=dict(building_id="building-1", entrance="1", apartment="3007", floor=None, scope="apartment"),
                           capabilities=[cap("total", "Показание", default=value, unit=unit, min=0)], state={"total": value},
                           meter=dict(type=kind, unit=unit, manual_allowed=True, stale_after_hours=48, max_daily_usage=maximum),
                           common_access=dict(resident_read=True, resident_commands=[]), ujin={}))
    return result


def predicate(device, signal, value, operator="eq"):
    return dict(device_id=device, signal=signal, value=value, operator=operator)


def command(device, signal, value):
    return dict(type="command", device_id=device, signal=signal, value=value)


def notification(target, message):
    return dict(type="notification", target=target, message=message)


def seed_rules():
    common = dict(enabled=True, conditions=[], condition_mode="all", cooldown_seconds=3, on_error="continue", retries=1)
    return [
        dict(**common, id="rule-leak", name="Защита от протечки", description="Перекрыть воду и оповестить консьержа и жителя", trigger=predicate("leak-1", "leak", True), actions=[command("valve-1", "closed", True), notification("concierge", "Обнаружена протечка в квартире. Проверьте расположение датчика и свяжитесь с жителем."), notification("resident", "Обнаружена протечка в ванной. Проверьте состояние клапана в приложении.")]),
        dict(**common, id="rule-climate", name="Комфортный климат", description="При температуре выше 26 °C включить охлаждение и вентиляцию", trigger=predicate("thermostat-1", "temperature", 26, "gt"), actions=[command("thermostat-1", "mode", "cool"), command("thermostat-1", "target_temp", 22), command("ventilation-1", "speed", 3), notification("resident", "В комнате жарко. Включено охлаждение до 22 °C.")]),
        dict(**common, id="rule-fire", name="Пожарная безопасность · демонстрация", description="Модель учебного сценария: отключение вентиляции, лифт на первый этаж, оповещения", trigger=predicate("fire-1", "smoke", True), actions=[command("ventilation-1", "power", False), command("elevator-1", "evacuation", True), command("elevator-1", "floor", 1), notification("concierge", "Учебная тревога: датчик дыма на кухне. Требуется проверка."), notification("resident", "Учебная тревога: обнаружен дым. Проверьте обстановку.")])
    ]
