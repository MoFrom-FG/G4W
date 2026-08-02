import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .storage import JsonStore


SHANGHAI = timezone(timedelta(hours=8))


def parse_due_at(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        raise ValueError("due_at is required")
    normalized = text.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.timestamp()


class ScheduledStore:
    def __init__(self, path: Path):
        self.store = JsonStore(path, {"jobs": {}})

    def create(self, binding_key: str, sender_id: str, content: str, due_at, kind: str = "reminder", recurrence_seconds: int = 0) -> dict:
        text = str(content or "").strip()
        if not text:
            raise ValueError("scheduled content is empty")
        due = parse_due_at(due_at)
        job_id = f"schedule-{uuid.uuid4().hex[:10]}"
        job = {
            "id": job_id,
            "bindingKey": binding_key,
            "senderId": sender_id,
            "kind": kind if kind in ("reminder", "deferred_reply", "checkin") else "reminder",
            "content": text,
            "dueAt": due,
            "recurrenceSeconds": max(0, int(recurrence_seconds or 0)),
            "status": "scheduled",
            "createdAt": time.time(),
            "updatedAt": time.time(),
            "fireIndex": 0,
        }
        self.store.update(lambda state: state.setdefault("jobs", {}).update({job_id: job}))
        return self.public(job)

    def list_for(self, binding_key: str) -> list[dict]:
        jobs = [self.public(job) for job in self.store.read().get("jobs", {}).values() if job.get("bindingKey") == binding_key]
        return sorted(jobs, key=lambda item: item.get("dueAt", 0))

    def cancel(self, binding_key: str, job_id: str) -> dict:
        def update(state):
            job = state.get("jobs", {}).get(job_id)
            if not job or job.get("bindingKey") != binding_key:
                raise KeyError(f"scheduled job not found: {job_id}")
            job.update({"status": "cancelled", "updatedAt": time.time()})
            return self.public(job)
        return self.store.update(update)

    def emit_due(self, event_store, limit: int = 20) -> int:
        now = time.time()
        due = [
            job for job in self.store.read().get("jobs", {}).values()
            if job.get("status") == "scheduled" and float(job.get("dueAt", 0)) <= now
        ]
        emitted = 0
        for job in sorted(due, key=lambda item: item.get("dueAt", 0))[:limit]:
            fire_index = int(job.get("fireIndex", 0)) + 1
            event_store.enqueue(
                "system.scheduled",
                job["bindingKey"],
                {"jobId": job["id"], "kind": job["kind"], "content": job["content"], "fireIndex": fire_index},
                dedupe_key=f"system.scheduled:{job['id']}:{fire_index}",
            )
            def advance(state, job_id=job["id"], index=fire_index):
                current = state["jobs"][job_id]
                recurrence = int(current.get("recurrenceSeconds", 0))
                current["fireIndex"] = index
                current["lastFiredAt"] = now
                current["updatedAt"] = now
                if recurrence > 0:
                    current["dueAt"] = max(float(current.get("dueAt", now)) + recurrence, now + 1)
                else:
                    current["status"] = "fired"
            self.store.update(advance)
            emitted += 1
        return emitted

    @staticmethod
    def public(job: dict) -> dict:
        return {key: job.get(key) for key in (
            "id", "kind", "content", "dueAt", "recurrenceSeconds", "status", "fireIndex", "createdAt", "updatedAt"
        )}
