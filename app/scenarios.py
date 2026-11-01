"""Durable rule versions and delayed continuations; no sleeps or timer threads."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import time
from uuid import uuid4


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds")


class ScenarioStorage:
    def __init__(self, store, *, clock=time.time):
        self.store = store
        self.clock = clock

    def versions(self, rule_id):
        return sorted((item for item in self.store.list("rule_versions") if item["rule_id"] == rule_id),
                      key=lambda item: item["version"], reverse=True)

    def record_version(self, rule, *, restored_from=None):
        previous = self.versions(rule["id"])
        item = {"id": str(uuid4()), "rule_id": rule["id"], "version": previous[0]["version"] + 1 if previous else 1,
                "created_at": timestamp(self.clock()), "owner_id": rule.get("owner_id"),
                "name": rule["name"], "rule": deepcopy(rule), "restored_from": restored_from}
        self.store.save("rule_versions", item["id"], item)
        return item

    def version(self, rule_id, version_id):
        item = self.store.get("rule_versions", version_id)
        if item is None or item["rule_id"] != rule_id:
            raise ValueError("Версия сценария не найдена")
        return item

    def schedule(self, run, rule, event, index, seconds):
        job = {"id": f"{run['id']}:{index}", "run_id": run["id"], "rule_id": rule["id"],
               "rule": deepcopy(rule), "event": deepcopy(event), "trace_id": run["trace_id"],
               "next_index": index + 1, "delay_index": index, "due_at": self.clock() + seconds,
               "state": "pending", "created_at": timestamp(self.clock())}
        # A visible waiting run and its continuation must be committed together.
        with self.store.lock, self.store.db:
            for kind, item in (("runs", run), ("scenario_jobs", job)):
                self.store.db.execute("INSERT INTO records(kind,id,payload) VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload",
                                      (kind, item["id"], json.dumps(item, ensure_ascii=False, allow_nan=False)))
        return job

    def due(self, limit=20):
        return sorted((job for job in self.store.list("scenario_jobs")
                       if job["state"] in ("pending", "running") and job["due_at"] <= self.clock()),
                      key=lambda job: job["due_at"])[:limit]

    def finish(self, job, state="done"):
        job["state"] = state
        job["finished_at"] = timestamp(self.clock())
        self.store.save("scenario_jobs", job["id"], job)

    def pending(self, rule_id=None):
        return [job for job in self.store.list("scenario_jobs") if job["state"] in ("pending", "running")
                and (rule_id is None or job["rule_id"] == rule_id)]
