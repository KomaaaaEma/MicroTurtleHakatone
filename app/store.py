"""Small durable repository. SQLite transactions protect configuration imports."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL, sequence INTEGER PRIMARY KEY AUTOINCREMENT, UNIQUE(kind,id))")
        self.db.execute("CREATE INDEX IF NOT EXISTS records_kind ON records(kind,sequence)")
        self.db.commit()

    def get(self, kind, id):
        with self.lock:
            row = self.db.execute("SELECT payload FROM records WHERE kind=? AND id=?", (kind, str(id))).fetchone()
            return json.loads(row[0]) if row else None

    def list(self, kind, limit=None):
        with self.lock:
            order = "DESC" if kind in ("events", "runs", "notifications") else "ASC"
            sql = f"SELECT payload FROM records WHERE kind=? ORDER BY sequence {order}"
            params = [kind]
            if limit is not None:
                sql += " LIMIT ?"
                params.append(int(limit))
            return [json.loads(row[0]) for row in self.db.execute(sql, params)]

    def save(self, kind, id, payload):
        with self.lock:
            self.db.execute("INSERT INTO records(kind,id,payload) VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload", (kind, str(id), json.dumps(payload, ensure_ascii=False, allow_nan=False)))
            self.db.commit()
        return payload

    def append(self, kind, payload):
        item = {"id": str(uuid4()), "timestamp": now(), **payload}
        self.save(kind, item["id"], item)
        if kind in ("events", "runs", "notifications"):
            with self.lock:
                self.db.execute("DELETE FROM records WHERE kind=? AND sequence NOT IN (SELECT sequence FROM records WHERE kind=? ORDER BY sequence DESC LIMIT 1000)", (kind, kind))
                self.db.commit()
        return item

    def delete(self, kind, id):
        with self.lock:
            self.db.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, str(id)))
            self.db.commit()

    def prune(self, kind, keep):
        with self.lock:
            self.db.execute("DELETE FROM records WHERE kind=? AND sequence NOT IN (SELECT sequence FROM records WHERE kind=? ORDER BY sequence DESC LIMIT ?)", (kind, kind, int(keep)))
            self.db.commit()

    def replace_config(self, devices, rules):
        with self.lock, self.db:
            self.db.execute("DELETE FROM records WHERE kind IN ('devices','rules')")
            for kind, items in (("devices", devices), ("rules", rules)):
                self.db.executemany("INSERT INTO records(kind,id,payload) VALUES(?,?,?)", [(kind, x["id"], json.dumps(x, ensure_ascii=False, allow_nan=False)) for x in items])

    def list_devices(self):
        return self.list("devices")

    def get_device(self, id):
        return self.get("devices", id)

    def update_device(self, id, payload):
        device = self.get_device(id)
        if device is None:
            raise ValueError("Устройство не найдено")
        device.update(payload)
        return self.save("devices", id, device)

    def log(self, kind, message, **extra):
        return self.append("events", {"kind": kind, "message": message, **extra})

    def close(self):
        with self.lock:
            self.db.close()
