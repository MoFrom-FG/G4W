import json
import os
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..core.storage import JsonStore


INBOX_PROJECT_ID = "inbox1028650430"


class DidaCli:
    def __init__(self, command: str = "", timeout_seconds: int = 30):
        self.command = str(command or "").strip()
        self.timeout_seconds = max(1, int(timeout_seconds or 30))

    def available(self) -> bool:
        return bool(self.command and (shutil.which(self.command) or Path(self.command).is_file()))

    def run(self, args: list[str], expect_json: bool = True):
        if not self.command:
            raise RuntimeError("DIDA CLI is not configured; set G4W_DIDA_COMMAND first")
        executable = shutil.which(self.command) or self.command
        command = [executable, *args]
        if os.name == "nt" and str(executable).lower().endswith((".cmd", ".bat")):
            command = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", executable, *args]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=self.timeout_seconds, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode != 0:
            raise RuntimeError(f"dida command failed: {(result.stderr or result.stdout).strip()[:500]}")
        if not expect_json:
            return result.stdout
        return _parse_json_output(result.stdout)

    def list_open_tasks(self, limit: int = 8) -> list[dict]:
        projects = _as_array(self.run(["project", "list", "--json"]))
        project_ids = [INBOX_PROJECT_ID]
        for project in projects:
            value = str(project.get("id") or project.get("projectId") or "").strip()
            if value and value not in project_ids:
                project_ids.append(value)
        tasks = _as_array(self.run(["task", "filter", "--projects", ",".join(project_ids), "--status", "0", "--json"]))
        normalized = [_normalize_task(item) for item in tasks]
        normalized = [item for item in normalized if item and item.get("status") != 2]
        normalized.sort(key=lambda item: (-item["priority"], item.get("dueDate") or "9999", item["title"]))
        return normalized[:max(1, min(int(limit or 8), 50))]

    def complete_task(self, task: dict) -> None:
        self.run(["task", "complete", str(task.get("projectId") or INBOX_PROJECT_ID), str(task["id"])], expect_json=False)

    def create_focus(self, task: dict, started_at: float, ended_at: float, minutes: int) -> None:
        args = ["focus", "create", "--type", "0", "--start-time", _iso(started_at), "--end-time", _iso(ended_at), "--duration", str(max(60, int(minutes) * 60)), "--note", "G4W CTDP supervision"]
        if task.get("id"):
            args.extend(["--task-id", str(task["id"])])
        self.run(args, expect_json=False)


class SelfControlService:
    def __init__(self, path: Path, event_store, dida: DidaCli, default_delay_minutes: int = 15, default_focus_minutes: int = 25):
        self.store = JsonStore(path, {"version": 1, "sessions": {}, "chains": {}, "events": []})
        self.events = event_store
        self.dida = dida
        self.default_delay_minutes = max(1, int(default_delay_minutes or 15))
        self.default_focus_minutes = max(1, int(default_focus_minutes or 25))

    def act(self, binding_key: str, sender_id: str, action: str, arguments: dict) -> dict:
        action = str(action or "status")
        state = self.store.read()
        session = state.setdefault("sessions", {}).get(sender_id)
        now = time.time()
        if action == "open":
            if session and session.get("state") not in ("completed", "cancelled", "interrupted"):
                return self._result(session, state)
            candidates = arguments.get("tasks") if isinstance(arguments.get("tasks"), list) else None
            if candidates is None:
                candidates = self.dida.list_open_tasks(arguments.get("limit", 8))
            candidates = [_normalize_task(item) for item in candidates]
            candidates = [item for item in candidates if item]
            session = {
                "id": str(uuid.uuid4()), "bindingKey": binding_key, "senderId": sender_id,
                "state": "awaiting_selection", "candidates": candidates, "task": None,
                "focusMinutes": int(arguments.get("focus_minutes") or self.default_focus_minutes),
                "extensionCount": 0, "createdAt": now, "updatedAt": now,
            }
            state["sessions"][sender_id] = session
            self._event(state, sender_id, "session_opened", {"candidateCount": len(candidates)})
        elif action == "select":
            session = self._require(session)
            task_id = str(arguments.get("task_id") or "")
            task = next((item for item in session.get("candidates", []) if item["id"] == task_id), None)
            if not task and arguments.get("task_title"):
                task = {"id": task_id or f"manual-{uuid.uuid4().hex[:8]}", "title": str(arguments["task_title"]).strip(), "projectId": str(arguments.get("project_id") or ""), "priority": 0, "status": 0, "dueDate": ""}
            if not task:
                raise KeyError("selected task was not found")
            session.update({"task": task, "candidates": [], "state": "awaiting_schedule", "updatedAt": now})
            self._event(state, sender_id, "task_selected", {"task": task})
        elif action == "schedule":
            session = self._require(session)
            delay = max(0, int(arguments.get("delay_minutes", self.default_delay_minutes)))
            if delay == 0:
                self._start(state, session, now, arguments.get("focus_minutes"))
            else:
                session.update({"state": "scheduled", "scheduledStart": now + delay * 60, "updatedAt": now})
                self._event(state, sender_id, "start_scheduled", {"delayMinutes": delay})
        elif action == "start":
            session = self._require(session)
            self._start(state, session, now, arguments.get("focus_minutes"))
        elif action == "extend":
            session = self._require(session)
            minutes = max(1, int(arguments.get("minutes") or self.default_focus_minutes))
            session.update({"state": "active", "focusMinutes": minutes, "focusStartedAt": now, "focusEndsAt": now + minutes * 60, "extensionCount": int(session.get("extensionCount", 0)) + 1, "updatedAt": now})
            self._event(state, sender_id, "focus_extended", {"minutes": minutes})
        elif action == "complete":
            session = self._require(session)
            sync_error = ""
            try:
                if session.get("task", {}).get("id") and not str(session["task"]["id"]).startswith("manual-"):
                    self.dida.complete_task(session["task"])
                    self.dida.create_focus(session["task"], float(session.get("focusStartedAt") or now), now, int(session.get("focusMinutes") or self.default_focus_minutes))
                session["didaSynced"] = True
            except Exception as error:
                session["didaSynced"] = False; sync_error = str(error)
            self._chain(state, sender_id, "execution", True)
            session.update({"state": "completed", "completedAt": now, "syncError": sync_error, "updatedAt": now})
            self._event(state, sender_id, "focus_completed", {"syncError": sync_error})
        elif action in ("interrupt", "cancel", "skip"):
            session = self._require(session)
            if session.get("state") in ("active", "awaiting_completion"):
                self._chain(state, sender_id, "execution", False)
                next_state = "interrupted"
            else:
                if session.get("task"):
                    self._chain(state, sender_id, "reservation", False)
                next_state = "cancelled"
            session.update({"state": next_state, "updatedAt": now, "reason": str(arguments.get("reason") or "")})
            self._event(state, sender_id, next_state, {"reason": session["reason"]})
        elif action == "exception":
            session = self._require(session)
            session.update({"state": "cancelled", "updatedAt": now, "exception": str(arguments.get("reason") or "unspecified")})
            self._event(state, sender_id, "exception_recorded", {"reason": session["exception"]})
        elif action != "status":
            raise ValueError(f"unknown supervision action: {action}")
        self.store.write(state)
        return self._result(session, state)

    def process_due(self) -> int:
        now = time.time()
        state = self.store.read()
        emitted = 0
        for sender_id, session in state.get("sessions", {}).items():
            event_type = ""
            if session.get("state") == "scheduled" and float(session.get("scheduledStart", 0)) <= now:
                session["state"] = "awaiting_start"; event_type = "start_due"
            elif session.get("state") == "active" and float(session.get("focusEndsAt", 0)) <= now:
                session["state"] = "awaiting_completion"; event_type = "focus_due"
            if event_type:
                session["updatedAt"] = now
                self._event(state, sender_id, event_type, {})
                self.events.enqueue("supervision.due", session["bindingKey"], {"kind": event_type, "session": self.public(session), "chains": state.get("chains", {}).get(sender_id, {})}, dedupe_key=f"supervision.due:{session['id']}:{event_type}:{int(now)}")
                emitted += 1
        if emitted:
            self.store.write(state)
        return emitted

    def set_worker(self, sender_id: str, worker_id: str) -> None:
        def update(state):
            session = state.get("sessions", {}).get(sender_id)
            if session:
                session["workerId"] = worker_id; session["updatedAt"] = time.time()
        self.store.update(update)

    def status(self, sender_id: str) -> dict:
        state = self.store.read()
        return self._result(state.get("sessions", {}).get(sender_id), state)

    def _start(self, state: dict, session: dict, now: float, focus_minutes=None):
        if not session.get("task"):
            raise RuntimeError("select a task before starting focus")
        minutes = max(1, int(focus_minutes or session.get("focusMinutes") or self.default_focus_minutes))
        self._chain(state, session["senderId"], "reservation", True)
        session.update({"state": "active", "focusMinutes": minutes, "scheduledStart": session.get("scheduledStart") or now, "focusStartedAt": now, "focusEndsAt": now + minutes * 60, "updatedAt": now})
        self._event(state, session["senderId"], "focus_started", {"minutes": minutes})

    @staticmethod
    def _require(session):
        if not session:
            raise RuntimeError("no active supervision session")
        return session

    @staticmethod
    def _chain(state: dict, sender_id: str, name: str, success: bool):
        chains = state.setdefault("chains", {}).setdefault(sender_id, {})
        chain = chains.setdefault(name, {"count": 0, "best": 0, "successes": 0, "failures": 0})
        if success:
            chain["count"] += 1; chain["best"] = max(chain["best"], chain["count"]); chain["successes"] += 1
        else:
            chain["count"] = 0; chain["failures"] += 1
        chain["updatedAt"] = time.time()

    @staticmethod
    def _event(state: dict, sender_id: str, event_type: str, payload: dict):
        state.setdefault("events", []).append({"id": uuid.uuid4().hex, "senderId": sender_id, "type": event_type, "payload": payload, "createdAt": time.time()})
        state["events"] = state["events"][-2000:]

    def _result(self, session: dict | None, state: dict) -> dict:
        return {"session": self.public(session) if session else None, "chains": state.get("chains", {}).get((session or {}).get("senderId", ""), {"reservation": {"count": 0, "best": 0, "successes": 0, "failures": 0}, "execution": {"count": 0, "best": 0, "successes": 0, "failures": 0}}), "didaAvailable": self.dida.available()}

    @staticmethod
    def public(session: dict) -> dict:
        return {key: session.get(key) for key in ("id", "state", "candidates", "task", "focusMinutes", "scheduledStart", "focusStartedAt", "focusEndsAt", "extensionCount", "workerId", "didaSynced", "syncError", "reason", "createdAt", "updatedAt", "completedAt")}


def _parse_json_output(text: str):
    value = str(text or "").strip()
    if not value:
        return []
    try:
        return json.loads(value)
    except Exception:
        starts = sorted(index for index in (value.find("["), value.find("{")) if index >= 0)
        for index in starts:
            try:
                return json.loads(value[index:])
            except Exception:
                pass
    raise ValueError(f"unable to parse dida JSON: {value[:200]}")


def _as_array(value):
    if isinstance(value, list): return value
    if isinstance(value, dict):
        for key in ("tasks", "projects", "data", "items", "list"):
            if isinstance(value.get(key), list): return value[key]
    return []


def _normalize_task(value):
    if not isinstance(value, dict): return None
    task_id = str(value.get("id") or value.get("taskId") or "").strip()
    title = str(value.get("title") or "").strip()
    if not task_id or not title: return None
    return {"id": task_id, "title": title, "projectId": str(value.get("projectId") or value.get("project") or value.get("project_id") or INBOX_PROJECT_ID), "dueDate": str(value.get("dueDate") or value.get("due_date") or ""), "priority": int(value.get("priority") or 0), "status": int(value.get("status") or 0)}


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")
