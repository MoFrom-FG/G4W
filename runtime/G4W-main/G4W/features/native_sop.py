"""Deterministic G4W native actions invoked through GA code_run.

This module is deliberately not an LLM tool.  L3 SOPs import ``execute`` from
ordinary GA Python execution, while the operations below retain deterministic
stores, validation and durable WeChat delivery.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ..core.config import Config
from ..core.records import DiaryStore, TimelineStore
from ..core.scheduler import ScheduledStore
from ..core.storage import EventStore, OutboxStore
from ..memory.checkin import CheckinService
from ..memory.instructions import update_env_file
from ..memory.wechat_maintenance import WechatMaintenanceService
from .supervision import DidaCli, SelfControlService
from .timeline_analytics import default_taxonomy
from .timeline_publish import TimelinePublisher


def _context(path: str | Path = "G4W-context.json") -> dict:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"G4W control context not found: {source}")
    value = json.loads(source.read_text(encoding="utf-8"))
    required = ("stateDir", "workspaceRoot", "senderId", "bindingKey")
    missing = [key for key in required if not str(value.get(key) or "").strip()]
    if missing:
        raise ValueError("G4W control context missing: " + ", ".join(missing))
    return value


def _file_delivery(context: dict, file_path: str, dedupe_key: str = "") -> dict:
    state_dir = Path(context["stateDir"]).resolve()
    workspace = Path(context["workspaceRoot"]).resolve()
    path = Path(str(file_path or "")).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    if not any(path == root or root in path.parents for root in (state_dir, workspace)):
        raise PermissionError("file is outside G4W state/workspace roots")
    dedupe = str(dedupe_key or f"sop-file:{context['senderId']}:{path}:{path.stat().st_mtime_ns}")
    held = bool(context.get("roundId"))
    queued = OutboxStore(state_dir / "outbox.json").prepare_file(
        context["bindingKey"], context["senderId"], str(context.get("contextToken") or ""),
        str(path), dedupe,
        round_id=str(context.get("roundId") or dedupe),
        turn=max(1, int(context.get("turn", 1) or 1)),
        round_final=not held,
        source="sop-script",
        held=held,
    )
    return {
        "ok": True,
        "queued": True,
        "fileName": path.name,
        "deliveryId": queued["id"],
        "deliveryHeldUntilTurnComplete": queued.get("status") == "held",
    }


def _timeline(context: dict, action: str, arguments: dict) -> dict:
    config = Config.load()
    state_dir = Path(context["stateDir"]).resolve()
    timeline_dir = state_dir / "timeline"
    store = TimelineStore(
        timeline_dir / "timeline-facts.json",
        state_dir / "legacy-import" / "timeline" / "timeline-facts.json",
    )
    publisher = TimelinePublisher(store, timeline_dir, locale=config.timeline_locale, theme=config.timeline_theme)
    if action == "read":
        return store.read(arguments.get("date", ""))
    if action == "list":
        return {"items": store.list_dates(arguments.get("limit", 30))}
    if action == "taxonomy":
        return {"ok": True, "taxonomy": default_taxonomy()}
    if action == "repair_categories":
        return store.repair_categories()
    if action == "write":
        result = store.write(
            arguments.get("date", ""), arguments.get("events") or [],
            mode=arguments.get("mode", "append"), finalize=bool(arguments.get("finalize", False)),
        )
        WechatMaintenanceService(state_dir / "wechat-maintenance-state.json").mark_timeline_written(context["senderId"])
        return result
    if action == "delete":
        return store.delete(arguments.get("date", ""), arguments.get("event_id", ""))
    if action == "build":
        return publisher.build()
    if action == "serve":
        return publisher.serve(arguments.get("host", "127.0.0.1"), arguments.get("port", 0))
    if action == "screenshot":
        result = publisher.screenshot(
            arguments.get("output_file", ""),
            arguments.get("width", 1680),
            arguments.get("height", 1400),
            range_name=arguments.get("range", arguments.get("view", "week")),
            date_value=arguments.get("date", ""),
            month_value=arguments.get("month", ""),
        )
        if arguments.get("send"):
            result.update(_file_delivery(context, result["outputFile"], f"timeline-screenshot:{context['senderId']}:{result['outputFile']}"))
        return result
    raise ValueError(f"unknown timeline action: {action}")


def _diary(context: dict, action: str, arguments: dict) -> dict:
    state_dir = Path(context["stateDir"]).resolve()
    store = DiaryStore(
        state_dir / "diary",
        state_dir / "legacy-import" / "diary",
        conversation_root=state_dir / "memory" / "conversations",
    )
    if action == "append":
        result = store.append(
            arguments.get("content", ""), title=arguments.get("title", ""),
            date=arguments.get("date", ""), at_time=arguments.get("time", ""),
            sender_id=context["senderId"],
        )
        WechatMaintenanceService(state_dir / "wechat-maintenance-state.json").mark_diary_written(context["senderId"])
        return result
    if action == "read":
        return store.read(arguments.get("date", ""), sender_id=context["senderId"])
    if action == "list":
        return {"items": store.list_dates(arguments.get("limit", 30), sender_id=context["senderId"])}
    raise ValueError(f"unknown diary action: {action}")


def _supervision(context: dict, action: str, arguments: dict) -> dict:
    config = Config.load()
    state_dir = Path(context["stateDir"]).resolve()
    service = SelfControlService(
        state_dir / "self-control.json", EventStore(state_dir / "events.json"), DidaCli(config.dida_command),
        config.supervision_default_delay_minutes, config.supervision_default_focus_minutes,
    )
    return service.act(context["bindingKey"], context["senderId"], action, arguments)


def _dida(action: str, arguments: dict) -> dict:
    cli = DidaCli(Config.load().dida_command)
    if action in ("status", "available"):
        return {"ok": True, "available": cli.available()}
    if action == "list":
        return {"ok": True, "items": cli.list_open_tasks(arguments.get("limit", 8))}
    if action == "complete":
        task = arguments.get("task") or {}
        cli.complete_task(task)
        return {"ok": True, "completed": str(task.get("id") or "")}
    if action == "focus":
        task = arguments.get("task") or {}
        started = float(arguments.get("started_at") or time.time())
        ended = float(arguments.get("ended_at") or time.time())
        minutes = max(1, int(arguments.get("minutes") or max(1, round((ended - started) / 60))))
        cli.create_focus(task, started, ended, minutes)
        return {"ok": True, "focusMinutes": minutes}
    raise ValueError(f"unknown dida action: {action}")


def _scheduling(context: dict, action: str, arguments: dict) -> dict:
    state_dir = Path(context["stateDir"]).resolve()
    if action.startswith("checkin_") or action in ("configure", "disable", "status") and arguments.get("kind") == "checkin":
        service = CheckinService(state_dir / "checkin-config.json")
        operation = action.removeprefix("checkin_")
        if operation == "status":
            return service.status(context["bindingKey"])
        if operation == "disable":
            update_env_file(Config.load().env_file, {"G4W_CHECKIN_ENABLED": "0"})
            return service.disable(context["bindingKey"])
        if operation == "configure":
            minimum = max(1, int(arguments.get("minimum_minutes", 10) or 10))
            maximum = max(minimum, int(arguments.get("maximum_minutes", 90) or 90))
            update_env_file(Config.load().env_file, {
                "G4W_CHECKIN_ENABLED": "1",
                "G4W_CHECKIN_MIN_INTERVAL_MS": str(minimum * 60000),
                "G4W_CHECKIN_MAX_INTERVAL_MS": str(maximum * 60000),
            })
            return service.configure(context["bindingKey"], context["senderId"], minimum, maximum, True)
        raise ValueError(f"unknown check-in action: {action}")
    store = ScheduledStore(state_dir / "schedules.json")
    operation = action.removeprefix("schedule_")
    if operation == "create":
        return store.create(
            context["bindingKey"], context["senderId"], arguments.get("content", ""), arguments.get("due_at"),
            kind=arguments.get("kind", "reminder"), recurrence_seconds=int(arguments.get("recurrence_seconds", 0) or 0),
        )
    if operation == "list":
        return {"items": store.list_for(context["bindingKey"])}
    if operation == "cancel":
        return store.cancel(context["bindingKey"], arguments.get("job_id", ""))
    raise ValueError(f"unknown scheduling action: {action}")


def _todo(context: dict, action: str, arguments: dict) -> dict:
    """确定性任务清单(todo)。创建前必须先 ask_user 确认;完成/取消只能由用户确认。"""
    from ..memory.todo import TodoStore

    store = TodoStore(Path(context["stateDir"]).resolve() / "todo-state.json")
    sender_id = context.get("senderId", "")
    operation = str(action or "").removeprefix("todo_")
    if operation == "add":
        return store.add(sender_id, str(arguments.get("text") or ""),
                         due_at=arguments.get("due_at"),
                         recurrence_seconds=int(arguments.get("recurrence_seconds", 0) or 0),
                         source="llm")
    if operation == "list":
        return {"tasks": store.list(sender_id)}
    if operation == "done":
        return store.done(sender_id, str(arguments.get("todo_id") or ""),
                          confirm_text=str(arguments.get("confirm_text") or ""))
    if operation == "cancel":
        return store.cancel(sender_id, str(arguments.get("todo_id") or ""))
    if operation == "del":
        return {"deleted": store.delete(sender_id, str(arguments.get("todo_id") or ""))}
    if operation in ("menu", "render"):
        return {"menu": store.render_menu(sender_id)}
    raise ValueError(f"unknown todo action: {action}")


def execute(domain: str, action: str, arguments: dict | None = None, context_path: str | Path = "G4W-context.json") -> dict:
    name = str(domain or "").strip().lower().replace("_", "-")
    action = str(action or "status").strip().lower()
    args = dict(arguments or {})
    context = _context(context_path) if name not in ("dida",) else None
    if name == "file-send":
        return _file_delivery(context, args.get("path", ""), args.get("dedupe_key", ""))
    if name == "timeline":
        return _timeline(context, action, args)
    if name == "diary":
        return _diary(context, action, args)
    if name == "supervision":
        return _supervision(context, action, args)
    if name == "dida":
        return _dida(action, args)
    if name == "scheduling":
        return _scheduling(context, action, args)
    if name == "todo":
        return _todo(context, action, args)
    raise ValueError(f"unknown G4W native SOP domain: {domain}")


def print_result(domain: str, action: str, arguments: dict | None = None, context_path: str | Path = "G4W-context.json") -> None:
    print(json.dumps(execute(domain, action, arguments, context_path), ensure_ascii=False, indent=2, default=str))
