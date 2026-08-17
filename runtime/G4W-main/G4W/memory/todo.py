# -*- coding: utf-8 -*-
"""TodoStore — 确定性任务存储(按微信账号隔离,时间属性化)。

todo 是"内容仓库":无时间的任务由 checkin 持续注入督办,有时间的任务
到点触发 system.scheduled(kind=todo) 事件精确提醒;循环任务到点自动重排。
完成/取消的语义由 LLM 判断(用户确认),程序只做确定性状态存储。

存储: <state_dir>/todo-state.json
结构: {"tasks": {"<sender_id>": [ {id,text,status,dueAt,recurrenceSeconds,
       createdAt,completedAt,confirmText,fireIndex,lastFiredAt,source} ]}}
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from ..core.storage import JsonStore
from ..core.scheduler import parse_due_at

SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"
MAX_TEXT_LEN = 200
MAX_TASKS_PER_SENDER = 50


def _fmt_local(ts: float | None) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(float(ts), SHANGHAI).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return ""


def _fmt_time(ts: float | None) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(float(ts), SHANGHAI).strftime("%H:%M")
    except (TypeError, ValueError, OSError):
        return ""


def _fmt_day(ts: float | None) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(float(ts), SHANGHAI).strftime("%m-%d")
    except (TypeError, ValueError, OSError):
        return ""


def _today_start() -> float:
    now = datetime.now(SHANGHAI)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class TodoStore:
    def __init__(self, path: Path):
        self.store = JsonStore(path, {"tasks": {}})

    # ---------- 基础 CRUD ----------

    def _tasks(self, sender_id: str) -> list[dict]:
        return list(self.store.read().get("tasks", {}).get(sender_id, []))

    def _save_tasks(self, sender_id: str, tasks: list[dict]) -> None:
        def update(state):
            state.setdefault("tasks", {}).setdefault(sender_id, [])
            state["tasks"][sender_id] = tasks
            return state

        self.store.update(update)

    def _next_id(self, sender_id: str) -> str:
        used = {t.get("id", "") for t in self._tasks(sender_id)}
        index = 1
        while f"t{index}" in used:
            index += 1
        return f"t{index}"

    def add(self, sender_id: str, text: str, due_at=None, recurrence_seconds: int = 0,
            source: str = "user") -> dict:
        text = str(text or "").strip()
        if not text:
            raise ValueError("任务内容不能为空")
        if len(text) > MAX_TEXT_LEN:
            raise ValueError(f"任务内容过长(最多 {MAX_TEXT_LEN} 字)")
        sender = str(sender_id or "").strip()
        tasks = self._tasks(sender)
        if len(tasks) >= MAX_TASKS_PER_SENDER:
            raise ValueError(f"任务数量已达上限({MAX_TASKS_PER_SENDER}),请先完成或删除一些")
        due = parse_due_at(due_at) if due_at not in (None, "") else None
        task = {
            "id": self._next_id(sender),
            "text": text,
            "status": STATUS_PENDING,
            "dueAt": due,
            "recurrenceSeconds": max(0, int(recurrence_seconds or 0)),
            "createdAt": time.time(),
            "completedAt": None,
            "confirmText": "",
            "fireIndex": 0,
            "lastFiredAt": None,
            "source": str(source or "user"),
        }
        tasks.append(task)
        self._save_tasks(sender, tasks)
        return dict(task)

    def list(self, sender_id: str) -> list[dict]:
        return [dict(t) for t in self._tasks(sender_id)]

    def _find(self, sender_id: str, todo_id: str) -> tuple[list[dict], int]:
        tasks = self._tasks(sender_id)
        for index, task in enumerate(tasks):
            if task.get("id") == todo_id:
                return tasks, index
        raise KeyError(f"todo not found: {todo_id}")

    def done(self, sender_id: str, todo_id: str, confirm_text: str = "") -> dict:
        tasks, index = self._find(sender_id, todo_id)
        task = tasks[index]
        if task.get("status") == STATUS_PENDING:
            task["status"] = STATUS_DONE
            task["completedAt"] = time.time()
            task["confirmText"] = str(confirm_text or "").strip()
            self._save_tasks(sender_id, tasks)
        return dict(task)

    def cancel(self, sender_id: str, todo_id: str) -> dict:
        tasks, index = self._find(sender_id, todo_id)
        task = tasks[index]
        if task.get("status") == STATUS_PENDING:
            task["status"] = STATUS_CANCELLED
            task["completedAt"] = time.time()
            self._save_tasks(sender_id, tasks)
        return dict(task)

    def delete(self, sender_id: str, todo_id: str) -> bool:
        tasks, index = self._find(sender_id, todo_id)
        del tasks[index]
        self._save_tasks(sender_id, tasks)
        return True

    # ---------- 到点触发 ----------

    def emit_due(self, event_store, sender_to_binding: dict, limit: int = 20) -> int:
        """扫描所有账号的到期任务,发 system.scheduled(kind=todo) 事件。

        一次性任务到点后保持 pending(继续督办,由用户确认完成);
        循环任务到点后按 recurrenceSeconds 自动重排。
        """
        now = time.time()
        state = self.store.read()
        emitted = 0
        changed_senders: dict[str, list[dict]] = {}
        for sender_id, tasks in state.get("tasks", {}).items():
            if not tasks:
                continue
            binding_key = sender_to_binding.get(sender_id)
            if not binding_key:
                continue
            pending_system = any(
                item.get("bindingKey") == binding_key and item.get("status") == "pending"
                for item in event_store.store.read().get("events", [])
            )
            if pending_system:
                continue  # 绑定已有挂起事件,稍后重试
            for task in tasks:
                if emitted >= limit:
                    break
                if task.get("status") != STATUS_PENDING:
                    continue
                is_recurring = int(task.get("recurrenceSeconds") or 0) > 0
                if not is_recurring and int(task.get("fireIndex", 0)) > 0:
                    continue  # 一次性任务只到点触发一次,之后靠 checkin 菜单持续督办
                due = float(task.get("dueAt") or 0)
                if not due or due > now:
                    continue
                fire_index = int(task.get("fireIndex", 0)) + 1
                event = event_store.enqueue(
                    "system.scheduled",
                    binding_key,
                    {
                        "jobId": f"todo-{task['id']}",
                        "kind": "todo",
                        "todoId": task["id"],
                        "content": task.get("text", ""),
                        "fireIndex": fire_index,
                    },
                    dedupe_key=f"system.todo:{sender_id}:{task['id']}:{fire_index}",
                )
                if event.get("status") != "pending":
                    continue
                task["fireIndex"] = fire_index
                task["lastFiredAt"] = now
                if int(task.get("recurrenceSeconds") or 0) > 0:
                    task["dueAt"] = now + int(task["recurrenceSeconds"])
                changed_senders.setdefault(sender_id, tasks)
                emitted += 1
        for sender_id, tasks in changed_senders.items():
            self._save_tasks(sender_id, tasks)
        return emitted

    # ---------- 渲染 ----------

    @staticmethod
    def _classify(task: dict) -> str:
        """overdue(有时间且已到点) / scheduled(有时间未到点) / ongoing(无时间)"""
        if task.get("status") != STATUS_PENDING:
            return "done"
        due = float(task.get("dueAt") or 0)
        if not due:
            return "ongoing"
        return "overdue" if due <= time.time() else "scheduled"

    @staticmethod
    def _recur_label(task: dict) -> str:
        seconds = int(task.get("recurrenceSeconds") or 0)
        if seconds <= 0:
            return ""
        if seconds % 86400 == 0:
            return f"每天 {_fmt_time(task.get('dueAt'))}" if task.get("dueAt") else "每天"
        if seconds % 3600 == 0:
            return f"每 {seconds // 3600} 小时"
        return f"每 {seconds // 60} 分钟"

    def render_menu(self, sender_id: str) -> str:
        """微信 /todo 完整菜单:未办全部 + 当天已办。"""
        tasks = self.list(sender_id)
        pending = [t for t in tasks if t.get("status") == STATUS_PENDING]
        overdue = sorted([t for t in pending if self._classify(t) == "overdue"],
                         key=lambda t: float(t.get("dueAt") or 0))
        scheduled = sorted([t for t in pending if self._classify(t) == "scheduled"],
                           key=lambda t: float(t.get("dueAt") or 0))
        ongoing = [t for t in pending if self._classify(t) == "ongoing"]
        done_today = [t for t in tasks if t.get("status") == STATUS_DONE
                      and float(t.get("completedAt") or 0) >= _today_start()]
        done_today.sort(key=lambda t: float(t.get("completedAt") or 0), reverse=True)

        lines = [f"📋 任务 · {len(pending)} 未办"]
        if overdue:
            lines.append("⚠️ 已到期")
            for t in overdue:
                recur = self._recur_label(t)
                recur = f" · {recur}" if recur else ""
                fire_note = ""
                if int(t.get("fireIndex", 0)) > 0:
                    fire_note = f" · 已提醒 {t.get('fireIndex', 0)} 次"
                lines.append(f"  [{t['id']}] {t['text']} · {_fmt_local(t.get('dueAt'))}{recur}{fire_note}")
        if scheduled:
            lines.append("⏰ 有时间")
            for t in scheduled:
                recur = self._recur_label(t)
                lines.append(f"  [{t['id']}] {t['text']} · {_fmt_local(t.get('dueAt'))}"
                             + (f" · {recur}" if recur else ""))
        if ongoing:
            lines.append("📌 持续")
            for t in ongoing:
                lines.append(f"  [{t['id']}] {t['text']}")
        if not pending:
            lines.append("  没有未办任务 🎉")
        lines.append("")
        lines.append(f"✅ 今天已办 {len(done_today)}")
        if done_today:
            for t in done_today:
                suffix = f" · {t['confirmText']}" if t.get("confirmText") else ""
                lines.append(f"  [{t['id']}] {t['text']} · {_fmt_time(t.get('completedAt'))}{suffix}")
        else:
            lines.append("  今天还没有完成的任务")
        lines.append("")
        lines.append("操作: /todo add <内容> [--due 时间] [--repeat 分钟] · /todo done <id> · /todo del <id> · /todo cancel <id>")
        return "\n".join(lines)

    def render_checkin(self, sender_id: str) -> str:
        """checkin 注入的紧凑菜单:所有未完成,分组 overdue → scheduled → ongoing。"""
        tasks = self.list(sender_id)
        pending = [t for t in tasks if t.get("status") == STATUS_PENDING]
        if not pending:
            return ""
        overdue = sorted([t for t in pending if self._classify(t) == "overdue"],
                         key=lambda t: float(t.get("dueAt") or 0))
        scheduled = sorted([t for t in pending if self._classify(t) == "scheduled"],
                           key=lambda t: float(t.get("dueAt") or 0))
        ongoing = [t for t in pending if self._classify(t) == "ongoing"]

        def line(t: dict) -> str:
            parts = [t["text"]]
            recur = self._recur_label(t)
            if t.get("dueAt"):
                parts.append(f"due {_fmt_local(t.get('dueAt'))}")
            if recur:
                parts.append(recur)
            if int(t.get("fireIndex", 0)) > 0:
                parts.append(f"reminded {t.get('fireIndex')}")
            return f"  [{t['id']}] " + " · ".join(parts)

        groups = []
        if overdue:
            groups.append("⚠️ overdue:\n" + "\n".join(line(t) for t in overdue))
        if scheduled:
            groups.append("⏰ scheduled:\n" + "\n".join(line(t) for t in scheduled))
        if ongoing:
            groups.append("📌 ongoing:\n" + "\n".join(line(t) for t in ongoing))
        return "OPEN_TODOS\n" + "\n\n".join(groups) + \
            "\n\n规则: 只能通过用户明确确认标记完成(todo_done);拿不准就 ask_user 询问状态。"

    # ---------- 迁移(旧 reminders 并入) ----------

    @staticmethod
    def migrate_from_schedules(schedules_store, todo_store_path: Path, sender_to_binding: dict) -> int:
        """把 schedules.json 中 kind=reminder 的未触发 job 转成 todo(pending)。

        转换后原 job 标 status=migrated(不再被 emit_due 触发);幂等。
        """
        from ..core.scheduler import ScheduledStore  # 延迟导入避免环

        source = schedules_store if isinstance(schedules_store, ScheduledStore) else ScheduledStore(schedules_store)
        state = source.store.read()
        jobs = state.get("jobs", {})
        migrated = 0
        todo = TodoStore(todo_store_path)
        binding_to_sender = {v: k for k, v in sender_to_binding.items()}
        for job_id, job in list(jobs.items()):
            if job.get("kind") != "reminder" or job.get("status") != "scheduled":
                continue
            if job.get("status") == "migrated":
                continue
            sender_id = binding_to_sender.get(job.get("bindingKey"), job.get("senderId", ""))
            if not sender_id:
                continue
            try:
                todo.add(
                    sender_id,
                    str(job.get("content") or ""),
                    due_at=float(job.get("dueAt") or 0) or None,
                    recurrence_seconds=int(job.get("recurrenceSeconds") or 0),
                    source="migrated",
                )
                job["status"] = "migrated"
                job["updatedAt"] = time.time()
                migrated += 1
            except (ValueError, KeyError):
                continue
        if migrated:
            source.store.write(state)
        return migrated


def short_uuid() -> str:
    return uuid.uuid4().hex[:8]
