from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from ..agents.input_capture import InputCaptureStore
from ..agents.turn_progress import TurnProgressStore
from ..agents.worker_turn import WorkerTurnStore
from ..core.config import Config
from ..core.dashboard_control import DashboardControlMailbox
from ..core.storage import JsonStore, safe_segment
from ..knowledge.ingest import extract_text, validate_extracted_text
from ..memory.checkin import CheckinService
from ..memory.instructions import render_instruction_template, update_env_file
from ..memory.vector.vector_config import load_config as load_vector_config
from ..memory.vector.vector_config import set_vector_enabled


STATIC_DIR = Path(__file__).with_name("static")
PROMPT_HEADINGS = (
    "# 工具所有权",
    "# 微信可见对话历史格式",
    "# 当前G4W配置",
    "# G4W能力与任务路由注册表",
    "[Memory] (G4W Shared Memory)",
    "# 用户长期记忆",
)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except Exception:
        return default


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        handle = ctypes.WinDLL("kernel32", use_last_error=True).OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _short_time(value) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(value)))
    except (TypeError, ValueError, OverflowError):
        return "--:--:--"


def _short_date(value) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(value)))
    except (TypeError, ValueError, OverflowError):
        return "--"


def _timestamp(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _human_event(event: dict) -> str:
    names = {
        "worker.spawned": "Worker 已启动",
        "worker.completed": "Worker 已完成",
        "worker.failed": "Worker 执行失败",
        "worker.progress": "Worker 进度更新",
        "worker.progress_milestone": "Worker 到达进度节点",
        "worker.model_switched": "Worker 已切换模型",
        "memory.l4_started": "记忆整理开始",
        "memory.l4_completed": "记忆整理完成",
        "checkin.due": "主动联系待处理",
        "system.checkin": "主动联系检查",
        "wechat.user_message": "收到微信消息",
        "wechat.command": "执行微信命令",
        "timeline.updated": "时间线已更新",
        "diary.updated": "日记已更新",
    }
    return names.get(str(event.get("type") or ""), str(event.get("type") or "系统事件"))


def _event_category(event_type: str) -> str:
    if event_type.startswith("worker."):
        return "worker"
    if event_type.startswith("wechat."):
        return "conversation"
    if event_type.startswith("memory.") or event_type.startswith("timeline.") or event_type.startswith("diary."):
        return "memory"
    if event_type.startswith("checkin.") or event_type == "system.checkin":
        return "automation"
    return "system"


def _event_detail(event: dict) -> str:
    event_type = str(event.get("type") or "")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    if event_type == "wechat.user_message":
        return "消息已进入 G4W 对话队列" + ("，到达时系统正忙" if payload.get("arrivedWhileBusy") else "")
    if event_type == "wechat.command":
        command = str(payload.get("userText") or payload.get("text") or "微信命令").strip()
        return command[:120]
    if event_type == "system.checkin":
        minimum = payload.get("minimumMinutes")
        maximum = payload.get("maximumMinutes")
        return f"随机窗口 {minimum}–{maximum} 分钟" if minimum and maximum else "已完成主动联系状态检查"
    for key in ("label", "topic", "summary", "progress", "reason", "task"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value[:220]
    return str(event.get("error") or "")[:220]


def _mask_sensitive(text: str) -> str:
    value = str(text or "")
    value = re.sub(
        r"(?i)((?:api[_-]?key|access[_-]?token|context[_-]?token|secret|password)\s*[:=]\s*)([^\s,\"']+)",
        r"\1***",
        value,
    )
    value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}", r"\1***", value)
    return value


def _prompt_node(title: str, source: str, content: str, *, kind: str = "file", in_model: bool = True) -> dict:
    raw = _mask_sensitive(content)
    path = Path(source) if source and kind == "file" else None
    return {
        "title": title,
        "kind": kind,
        "source": source,
        "exists": bool(path and path.is_file()) if path else True,
        "chars": len(raw),
        "tokens": max(0, round(len(raw) / 3.2)),
        "sha": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12] if raw else "",
        "updatedAt": _short_date(_timestamp(path)) if path else "运行时生成",
        "inModel": bool(in_model),
        "content": raw,
    }


def _extract_prompt_section(text: str, marker: str) -> str:
    start = text.find(marker)
    if start < 0:
        return ""
    end = len(text)
    for candidate in PROMPT_HEADINGS:
        if candidate == marker:
            continue
        index = text.find("\n\n" + candidate, start + len(marker))
        if index >= 0:
            end = min(end, index)
    return text[start:end].strip()


class DashboardAuth:
    """Password auth for the dashboard (ga-admin style PBKDF2), sessions in memory.

    First visit shows a "set password" screen (auth file absent).  Credentials
    live in ``<state_dir>/dashboard-auth.json`` (username, salt, hash,
    iterations).  Change the password later via POST /api/auth/password.
    """

    _ITERATIONS = 210_000
    _SESSION_TTL = 12 * 3600
    _AUTH_FILE = "dashboard-auth.json"

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / self._AUTH_FILE
        self._lock = threading.Lock()
        self._sessions: dict[str, float] = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def is_initialized(self) -> bool:
        return self.path.is_file()

    def setup(self, username: str, password: str) -> str:
        """First-run: create credentials (only while uninitialized). Returns a session token."""
        with self._lock:
            if self.path.is_file():
                raise ValueError("已初始化，请直接登录")
            username = str(username or "").strip()
            if not username:
                raise ValueError("用户名不能为空")
            password = str(password or "")
            if len(password) < 8:
                raise ValueError("密码至少 8 位")
            salt = secrets.token_bytes(16)
            self.path.write_text(
                json.dumps(
                    {
                        "username": username,
                        "salt": base64.b64encode(salt).decode(),
                        "hash": base64.b64encode(self._derive(password, salt)).decode(),
                        "iterations": self._ITERATIONS,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        # create_session 也要拿 _lock;放锁外避免死锁
        return self.create_session()

    def _derive(self, password: str, salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt, self._ITERATIONS)

    def verify(self, username: str, password: str) -> bool:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return False
        if str(username or "").strip() != str(data.get("username") or ""):
            return False
        try:
            salt = base64.b64decode(str(data.get("salt") or ""))
            expected = base64.b64decode(str(data.get("hash") or ""))
            iterations = int(data.get("iterations") or self._ITERATIONS)
            actual = hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"), salt, iterations)
        except Exception:
            return False
        return hmac.compare_digest(actual, expected)

    def create_session(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = time.time() + self._SESSION_TTL
        return token

    def check(self, token: str) -> bool:
        if not token:
            return False
        now = time.time()
        with self._lock:
            expiry = self._sessions.get(token)
            if expiry is None:
                return False
            if expiry < now:
                self._sessions.pop(token, None)
                return False
            return True

    def revoke(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    def change_password(self, username: str, old: str, new: str) -> dict:
        if not self.verify(username, old):
            raise ValueError("旧密码不正确")
        new = str(new or "")
        if len(new) < 8:
            raise ValueError("新密码至少 8 位")
        salt = secrets.token_bytes(16)
        data = {
            "username": str(username or "").strip(),
            "salt": base64.b64encode(salt).decode(),
            "hash": base64.b64encode(self._derive(new, salt)).decode(),
            "iterations": self._ITERATIONS,
        }
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "message": "密码已更新"}


class DashboardState:
    def __init__(self, config: Config):
        self.config = config

    def _fresh_config(self) -> Config:
        self.config = Config.load()
        return self.config

    @staticmethod
    def _core_pid(config: Config) -> int:
        try:
            return int(config.pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return 0

    def _control(self, action: str, payload: dict, timeout: float = 4.0) -> dict:
        config = self._fresh_config()
        pid = self._core_pid(config)
        if not _pid_alive(pid):
            raise RuntimeError("G4W 主进程未运行，模型控制暂不可用")
        mailbox = DashboardControlMailbox(config.state_dir)
        request_id = mailbox.submit(action, payload)
        result = mailbox.wait(request_id, timeout=timeout)
        if result is None:
            raise RuntimeError("G4W 主进程未确认操作，请确认主程序已更新并正在运行")
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or "模型操作失败"))
        return result

    @staticmethod
    def _binding(config: Config) -> tuple[str, str, dict]:
        state = _read_json(config.conversations_dir / "bindings.json", {"bindings": {}})
        bindings = state.get("bindings", {}) if isinstance(state, dict) else {}
        if not isinstance(bindings, dict) or not bindings:
            return "", "", {}
        key, entry = max(
            ((str(key), value) for key, value in bindings.items() if isinstance(value, dict)),
            key=lambda pair: float(pair[1].get("updatedAt") or 0),
            default=("", {}),
        )
        return key, str(entry.get("senderId") or ""), dict(entry)

    @staticmethod
    def _profile_names(config: Config) -> dict[str, dict]:
        state = _read_json(config.state_dir / "profiles.json", {"senders": {}})
        senders = state.get("senders", {}) if isinstance(state, dict) else {}
        return {safe_segment(str(key)): value for key, value in senders.items() if isinstance(value, dict)}

    def _memory_items(self, config: Config) -> list[dict]:
        names = self._profile_names(config)
        items = []
        if not config.conversations_dir.is_dir():
            return items
        for conversation in config.conversations_dir.iterdir():
            if not conversation.is_dir():
                continue
            if conversation.name not in names:
                continue
            insight = conversation / "summaries" / "history_insight"
            active = insight / "active_knowledge.json"
            brief = insight / "memory_brief.md"
            if not active.is_file() and not brief.is_file():
                continue
            data = _read_json(active, {}) if active.is_file() else {}
            counts = {
                "profile": len(data.get("user_profile", {})) if isinstance(data.get("user_profile"), dict) else 0,
                "projects": len(data.get("ongoing_projects", [])) if isinstance(data.get("ongoing_projects"), list) else 0,
                "capabilities": len(data.get("agent_capabilities_learned", [])) if isinstance(data.get("agent_capabilities_learned"), list) else 0,
                "lessons": len(data.get("memory_lessons", [])) if isinstance(data.get("memory_lessons"), list) else 0,
                "facts": len(data.get("user_facts", [])) if isinstance(data.get("user_facts"), list) else 0,
            }
            backups_root = conversation / "summaries" / ".backups"
            backups = list(backups_root.glob("*/history_insight/active_knowledge.json")) if backups_root.is_dir() else []
            profile = names.get(conversation.name, {})
            brief_text = _read_text(brief).strip()
            items.append({
                "id": conversation.name,
                "name": str(profile.get("userName") or profile.get("userIdentity") or conversation.name),
                "identity": str(profile.get("userIdentity") or ""),
                "updatedAt": _short_date(max(_timestamp(active), _timestamp(brief))),
                "updatedTimestamp": max(_timestamp(active), _timestamp(brief)),
                "brief": re.sub(r"\s+", " ", brief_text)[:220],
                "counts": counts,
                "backupCount": len(backups),
                "activeBytes": active.stat().st_size if active.is_file() else 0,
            })
        items.sort(key=lambda item: item["updatedTimestamp"], reverse=True)
        return items

    @staticmethod
    def _worker_summary(item: dict, config: Config) -> dict:
        progress = _read_json(Path(str(item.get("progressFile") or "")), {}) if item.get("progressFile") else {}
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        summary = str(progress.get("summary") or result.get("summary") or item.get("task") or "等待状态更新")
        return {
            "id": str(item.get("id") or "worker"),
            "topic": str(item.get("topic") or item.get("capabilityId") or "Worker"),
            "capability": str(item.get("capabilityId") or ""),
            "status": str(item.get("status") or "unknown"),
            "model": str(item.get("model") or item.get("modelName") or config.worker_model),
            "summary": summary[:240],
            "runIndex": int(item.get("runIndex") or 0),
            "updatedAt": item.get("updatedAt") or item.get("createdAt"),
            "hasDetail": bool(item.get("dir") or item.get("result") or item.get("archivePath")),
        }

    def _workers(self, config: Config) -> tuple[list[dict], dict]:
        workers_state = _read_json(config.worker_registry_file, {"workers": {}})
        workers_map = workers_state.get("workers", {}) if isinstance(workers_state, dict) else {}
        workers = [self._worker_summary(item, config) for item in workers_map.values() if isinstance(item, dict)] if isinstance(workers_map, dict) else []
        workers.sort(key=lambda item: float(item.get("updatedAt") or 0), reverse=True)
        return workers, workers_map if isinstance(workers_map, dict) else {}

    def snapshot(self) -> dict:
        config = self._fresh_config()
        state_dir = config.state_dir
        workers, _ = self._workers(config)

        event_state = _read_json(state_dir / "events.json", {"events": []})
        raw_events = event_state.get("events", []) if isinstance(event_state, dict) else []
        raw_events = [item for item in raw_events if isinstance(item, dict)]
        raw_events.sort(key=lambda item: float(item.get("createdAt") or 0), reverse=True)
        events = []
        checkin_count = 0
        latest_checkin = None
        for item in raw_events[:250]:
            event_type = str(item.get("type") or "")
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            if event_type == "system.checkin":
                checkin_count += 1
                latest_checkin = latest_checkin or item
                continue
            events.append({
                "id": str(item.get("id") or f"{event_type}:{item.get('createdAt') or len(events)}"),
                "type": _human_event(item),
                "rawType": event_type,
                "category": _event_category(event_type),
                "status": str(item.get("status") or ""),
                "time": _short_time(item.get("createdAt")),
                "createdAt": float(item.get("createdAt") or 0),
                "detail": _event_detail(item),
                "relatedId": str(payload.get("workerId") or payload.get("id") or ""),
            })
            if len(events) >= 79:
                break
        if latest_checkin:
            events.append({
                "id": "system-checkin-rollup",
                "type": "主动联系检查",
                "rawType": "system.checkin",
                "category": "automation",
                "status": str(latest_checkin.get("status") or "done"),
                "time": _short_time(latest_checkin.get("createdAt")),
                "createdAt": float(latest_checkin.get("createdAt") or 0),
                "detail": f"最近 250 条记录中合并了 {checkin_count} 次检查；{_event_detail(latest_checkin)}",
                "relatedId": "",
                "count": checkin_count,
            })
        events.sort(key=lambda item: item["createdAt"], reverse=True)

        knowledge_root = state_dir / "knowledge"
        manifest = _read_json(knowledge_root / "manifest.json", {"documents": {}})
        documents = manifest.get("documents", {}) if isinstance(manifest, dict) else {}
        knowledge_items = []
        for document in documents.values() if isinstance(documents, dict) else []:
            if not isinstance(document, dict):
                continue
            knowledge_items.append({
                "id": str(document.get("doc_id") or ""),
                "title": str(document.get("title") or document.get("doc_id") or "未命名文档"),
                "chunks": int(document.get("chunk_count") or 0),
                "tags": [str(tag) for tag in (document.get("tags") or [])[:5]],
                "updatedAt": _short_date(document.get("updated_at") or document.get("created_at")),
            })
        knowledge_items.sort(key=lambda item: item["updatedAt"], reverse=True)
        try:
            chunks = sum(1 for line in (knowledge_root / "chunks.jsonl").read_text(encoding="utf-8").splitlines() if line.strip())
        except OSError:
            chunks = 0

        try:
            pid = int(config.pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = 0
        running_workers = sum(item["status"] in {"running", "starting"} for item in workers)
        pending_events = sum(item.get("status") == "pending" for item in raw_events)
        memory_items = self._memory_items(config)
        vector_config = load_vector_config()
        vector_on = bool(vector_config.get("enabled") and vector_config.get("installed"))

        return {
            "updatedAt": time.time(),
            "system": {
                "name": config.bot_name or "G4W",
                "user": config.user_name or "本地用户",
                "status": "online" if _pid_alive(pid) else "standby",
                "corePid": pid,
                "model": config.conductor_model,
                "vector": vector_on,
                "vectorModel": str(vector_config.get("model") or ""),
                "envPath": str(config.env_file),
                "statePath": str(state_dir),
            },
            "metrics": {
                "workers": len(workers),
                "runningWorkers": running_workers,
                "memoryProfiles": len(memory_items),
                "memoryBackups": sum(item.get("backupCount", 0) for item in memory_items),
                "knowledgeDocuments": len(documents) if isinstance(documents, dict) else 0,
                "knowledgeChunks": chunks,
                "pendingEvents": pending_events,
            },
            "workers": workers[:80],
            "events": events[:40],
            "knowledge": {
                "documents": len(documents) if isinstance(documents, dict) else 0,
                "chunks": chunks,
                "vector": vector_on,
                "root": str(knowledge_root),
                "items": knowledge_items[:80],
            },
            "memory": {
                "profiles": len(memory_items),
                "backups": sum(item.get("backupCount", 0) for item in memory_items),
                "root": str(config.memory_dir),
                "active": bool(memory_items),
                "items": memory_items,
            },
            "configuration": self.settings(config),
        }

    def memory_detail(self, memory_id: str) -> dict:
        config = self._fresh_config()
        target = config.conversations_dir / safe_segment(memory_id)
        if target.parent != config.conversations_dir or not target.is_dir():
            raise FileNotFoundError(memory_id)
        insight = target / "summaries" / "history_insight"
        active_path = insight / "active_knowledge.json"
        brief_path = insight / "memory_brief.md"
        active = _read_json(active_path, {})
        profile = self._profile_names(config).get(target.name, {})
        backups_root = target / "summaries" / ".backups"
        backups = []
        for path in backups_root.glob("*/history_insight/active_knowledge.json") if backups_root.is_dir() else []:
            backups.append({
                "name": path.parents[1].name,
                "updatedAt": _short_date(_timestamp(path)),
                "bytes": path.stat().st_size,
            })
        backups.sort(key=lambda item: item["name"], reverse=True)
        sections = []
        labels = {
            "user_profile": "用户画像",
            "ongoing_projects": "进行中项目",
            "agent_capabilities_learned": "能力经验",
            "memory_lessons": "记忆经验",
            "user_facts": "用户事实",
        }
        for key, label in labels.items():
            value = active.get(key, [] if key != "user_profile" else {})
            count = len(value) if isinstance(value, (list, dict)) else 0
            sections.append({"key": key, "label": label, "count": count, "items": value})
        return {
            "id": target.name,
            "name": str(profile.get("userName") or target.name),
            "identity": str(profile.get("userIdentity") or ""),
            "botName": str(profile.get("botName") or ""),
            "gender": str(profile.get("userGender") or ""),
            "brief": _read_text(brief_path).strip(),
            "sections": sections,
            "backups": backups,
            "source": str(active_path),
            "activeBytes": active_path.stat().st_size if active_path.is_file() else 0,
            "updatedAt": _short_date(_timestamp(active_path)),
        }

    def timeline_data(self) -> dict:
        config = self._fresh_config()
        path = config.timeline_dir / "timeline-facts.json"
        state = _read_json(path, {"version": 1, "timezone": "Asia/Shanghai", "facts": {}})
        facts = state.get("facts", {}) if isinstance(state, dict) else {}
        facts = facts if isinstance(facts, dict) else {}
        dates = sorted(key for key, value in facts.items() if isinstance(value, dict))
        event_count = sum(len((facts.get(day) or {}).get("events") or []) for day in dates)
        return {
            "root": str(config.timeline_dir),
            "source": str(path),
            "timezone": str(state.get("timezone") or "Asia/Shanghai"),
            "dates": dates,
            "latestDate": dates[-1] if dates else "",
            "facts": facts,
            "metrics": {
                "days": len(dates),
                "events": event_count,
                "updatedAt": _short_date(_timestamp(path)),
            },
        }

    def _diary_root(self, config: Config) -> tuple[Path, str]:
        _, sender_id, _ = self._binding(config)
        if sender_id:
            return config.conversations_dir / safe_segment(sender_id) / "summaries" / "diary", sender_id
        candidates = sorted(config.conversations_dir.glob("*/summaries/diary"), key=_timestamp, reverse=True)
        return (candidates[0], candidates[0].parents[1].name) if candidates else (config.diary_dir, "")

    def diary_index(self) -> dict:
        config = self._fresh_config()
        root, sender_id = self._diary_root(config)
        entries = []
        for path in root.rglob("????-??-??.md") if root.is_dir() else []:
            content = _read_text(path).strip()
            plain = re.sub(r"^#{1,6}\s+", "", content, flags=re.M)
            plain = re.sub(r"\s+", " ", plain).strip()
            entries.append({
                "date": path.stem,
                "title": next((line[3:].strip() for line in content.splitlines() if line.startswith("## ")), "当日日记"),
                "excerpt": plain[:180],
                "sections": sum(1 for line in content.splitlines() if line.startswith("## ")),
                "bytes": path.stat().st_size,
                "updatedAt": _short_date(_timestamp(path)),
            })
        entries.sort(key=lambda item: item["date"], reverse=True)
        return {"root": str(root), "senderId": sender_id, "entries": entries, "count": len(entries)}

    def diary_detail(self, date: str) -> dict:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(date or "")):
            raise FileNotFoundError(date)
        config = self._fresh_config()
        root, sender_id = self._diary_root(config)
        path = root / date[:4] / date[5:7] / f"{date}.md"
        if not path.is_file():
            legacy = root / f"{date}.md"
            path = legacy if legacy.is_file() else path
        if not path.is_file():
            raise FileNotFoundError(date)
        content = _read_text(path).strip()
        return {
            "date": date,
            "senderId": sender_id,
            "content": content,
            "source": str(path),
            "sections": sum(1 for line in content.splitlines() if line.startswith("## ")),
            "updatedAt": _short_date(_timestamp(path)),
        }

    def knowledge_detail(self, document_id: str) -> dict:
        config = self._fresh_config()
        root = config.state_dir / "knowledge"
        manifest = _read_json(root / "manifest.json", {"documents": {}})
        documents = manifest.get("documents", {}) if isinstance(manifest, dict) else {}
        document = None
        if isinstance(documents, dict):
            document = documents.get(document_id)
            if not isinstance(document, dict):
                document = next((value for value in documents.values() if isinstance(value, dict) and str(value.get("doc_id") or "") == document_id), None)
        if not isinstance(document, dict):
            raise FileNotFoundError(document_id)
        stored_path = Path(str(document.get("stored_path") or "")).expanduser().resolve()
        allowed_root = root.resolve()
        if stored_path != allowed_root and allowed_root not in stored_path.parents:
            raise PermissionError("知识库文档路径超出允许目录")
        content = ""
        content_source = "stored"
        extraction_warning = ""
        text_path_value = str(document.get("text_path") or "").strip()
        text_path = Path(text_path_value).expanduser().resolve() if text_path_value else None
        if text_path and (text_path == allowed_root or allowed_root in text_path.parents) and text_path.is_file():
            content = _read_text(text_path)
            content_source = "extracted"
        elif stored_path.suffix.lower() == ".pdf":
            try:
                extracted, _ = extract_text(stored_path)
                quality = validate_extracted_text(extracted)
                if not quality.get("ok"):
                    raise ValueError(str(quality.get("reason") or "PDF 没有可读文本"))
                text_path = root / "documents" / f"{document_id}.extracted.txt"
                text_path.write_text(extracted, encoding="utf-8")
                document["text_path"] = str(text_path)
                document["updated_at"] = int(time.time())
                _write_json(root / "manifest.json", manifest)
                content = extracted
                content_source = "extracted"
            except Exception as error:
                extraction_warning = f"PDF 文本提取失败：{error}"
        else:
            content = _read_text(stored_path)
        content_limit = 1_200_000
        chunks = []
        matching_chunks = 0
        chunks_path = root / "chunks.jsonl"
        try:
            with chunks_path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if document_id not in line:
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if str(item.get("doc_id") or "") != document_id:
                        continue
                    matching_chunks += 1
                    if len(chunks) < 160:
                        text = str(item.get("text") or "")
                        chunks.append({
                            "id": str(item.get("chunk_id") or ""),
                            "page": item.get("page"),
                            "section": str(item.get("section") or ""),
                            "text": text[:1600],
                            "truncated": len(text) > 1600,
                        })
        except OSError:
            pass
        if not content and chunks:
            parts = []
            last_page = object()
            for chunk in chunks:
                page = chunk.get("page")
                if page != last_page and page is not None:
                    parts.append(f"[第 {page} 页]")
                parts.append(str(chunk.get("text") or ""))
                last_page = page
            content = "\n\n".join(part for part in parts if part).strip()
            content_source = "chunks"
            extraction_warning = extraction_warning or "已从现有知识分块重建正文。"
        if not content and stored_path.suffix.lower() == ".pdf":
            content = "该 PDF 没有可提取的文字，可能是扫描版文件，需要 OCR 后才能阅读正文。"
            content_source = "unavailable"
        return {
            "id": str(document.get("doc_id") or document_id),
            "title": str(document.get("title") or document_id),
            "tags": document.get("tags") if isinstance(document.get("tags"), list) else [],
            "chunkCount": int(document.get("chunk_count") or matching_chunks),
            "loadedChunks": len(chunks),
            "chunks": chunks,
            "sourcePath": str(document.get("source_path") or ""),
            "storedPath": str(stored_path),
            "createdAt": _short_date(document.get("created_at")),
            "updatedAt": _short_date(document.get("updated_at")),
            "content": content[:content_limit],
            "contentTruncated": len(content) > content_limit,
            "contentSource": content_source,
            "format": str(stored_path.suffix.lower().lstrip(".") or "text"),
            "extractionWarning": extraction_warning,
            "textPath": str(text_path or ""),
            "bytes": stored_path.stat().st_size if stored_path.is_file() else 0,
        }

    def worker_detail(self, worker_id: str) -> dict:
        config = self._fresh_config()
        _, workers_map = self._workers(config)
        item = workers_map.get(worker_id)
        if not isinstance(item, dict):
            raise FileNotFoundError(worker_id)
        worker_dir = Path(str(item.get("dir") or "")) if item.get("dir") else None
        run_dirs = []
        if worker_dir and worker_dir.is_dir():
            runs_root = worker_dir / "runs"
            if runs_root.is_dir():
                run_dirs = [path for path in runs_root.rglob("run-*") if path.is_dir()]
        run_dirs.sort(key=_timestamp, reverse=True)
        runs = []
        for run_dir in run_dirs[:30]:
            result = _read_json(run_dir / "result.json", {})
            progress = _read_json(run_dir / "progress.json", {})
            job = _read_json(run_dir / "job.json", {})
            report_path = run_dir / "report.md"
            artifacts = []
            files = (result.get("files") or result.get("files_written") or []) if isinstance(result, dict) else []
            output_dir = Path(str(result.get("output_dir") or result.get("outputDir") or "")) if isinstance(result, dict) else Path()
            if isinstance(files, list):
                for value in files[:80]:
                    path_value = value.get("path") if isinstance(value, dict) else value
                    if not path_value:
                        continue
                    candidate = Path(str(path_value))
                    if not candidate.is_absolute():
                        output_candidate = (output_dir / candidate).resolve() if str(output_dir) not in {"", "."} else None
                        candidate = output_candidate if output_candidate and output_candidate.is_file() else (run_dir / candidate).resolve()
                    artifacts.append({
                        "name": candidate.name,
                        "path": str(candidate),
                        "exists": candidate.is_file(),
                        "bytes": candidate.stat().st_size if candidate.is_file() else 0,
                    })
            runs.append({
                "name": run_dir.name,
                "path": str(run_dir),
                "status": str(result.get("status") or progress.get("status") or item.get("status") or "unknown"),
                "summary": str(result.get("summary") or progress.get("summary") or ""),
                "job": job,
                "progress": progress,
                "result": result,
                "report": _read_text(report_path).strip(),
                "artifacts": artifacts,
                "updatedAt": _short_date(_timestamp(run_dir)),
            })
        embedded_result = item.get("result") if isinstance(item.get("result"), dict) else {}
        latest = runs[0] if runs else {}
        return {
            "id": worker_id,
            "topic": str(item.get("topic") or item.get("capabilityId") or "Worker"),
            "task": str(item.get("task") or ""),
            "status": str(item.get("status") or "unknown"),
            "capability": str(item.get("capabilityId") or ""),
            "model": str(item.get("model") or item.get("modelName") or config.worker_model),
            "runIndex": int(item.get("runIndex") or 0),
            "summary": str(latest.get("summary") or embedded_result.get("summary") or item.get("task") or ""),
            "result": latest.get("result") or embedded_result,
            "report": str(latest.get("report") or ""),
            "runs": runs,
            "archivePath": str(item.get("archivePath") or ""),
            "updatedAt": _short_date(item.get("updatedAt") or item.get("createdAt")),
        }

    def prompt_detail(self) -> dict:
        config = self._fresh_config()
        binding_key, sender_id, binding = self._binding(config)
        sender_safe = safe_segment(sender_id) if sender_id else ""
        latest_path = None
        candidates = []
        if config.conversations_dir.is_dir():
            for path in config.conversations_dir.rglob("turn*.json"):
                if path.parent.name == "inputs":
                    candidates.append(path)
        if candidates:
            latest_path = max(candidates, key=_timestamp)
        snapshot = _read_json(latest_path, {}) if latest_path else {}
        submitted = snapshot.get("submittedRequest", {}) if isinstance(snapshot, dict) else {}
        final_system = str(submitted.get("system") or "")
        thinking_marker = "\n\n### 行动规范（持续有效）"
        if thinking_marker in final_system:
            controller_system, thinking = final_system.split(thinking_marker, 1)
            thinking = "### 行动规范（持续有效）" + thinking
        else:
            controller_system, thinking = final_system, ""

        policy_path = Path(__file__).resolve().parents[1] / "templates" / "agents" / "conductor-policy.md"
        raw_policy = _read_text(policy_path).strip()
        values = {
            "user_name": config.user_name,
            "user_identity": config.user_identity,
            "user_gender": config.user_gender,
            "bot_name": config.bot_name,
        }
        policy = render_instruction_template(raw_policy, **values).strip()
        persona_raw = _read_text(config.persona_file).strip()
        persona = render_instruction_template(persona_raw, **values).strip()
        operations_raw = _read_text(config.operations_file).strip()
        operations = render_instruction_template(operations_raw, **values).strip()
        memory_user = config.memory_dir / "users" / f"{sender_safe}.md"
        memory_operational = config.memory_dir / "operational" / f"{sender_safe}.md"
        memory_index = config.conversations_dir / sender_safe / "indexes" / "memory-index.md"
        sop_root = Path(config.shared_memory_root or config.sop_dir)
        sop_structure = sop_root / "insight_fixed_structure.txt"
        sop_index = sop_root / "global_mem_insight.txt"
        llmcore = Path(__file__).resolve().parents[3] / "app" / "llmcore.py"

        generated_nodes = []
        for marker, title in (
            ("# 工具所有权", "工具所有权"),
            ("# 微信可见对话历史格式", "微信历史格式"),
            ("# 当前G4W配置", "当前 G4W 配置"),
            ("# G4W能力与任务路由注册表", "能力与任务路由注册表"),
        ):
            generated_nodes.append(_prompt_node(title, "controller.py · 运行时生成", _extract_prompt_section(controller_system, marker), kind="generated"))

        memory_nodes = [
            _prompt_node("User Memory", str(memory_user), _read_text(memory_user).strip()),
            _prompt_node("Operational Memory", str(memory_operational), _read_text(memory_operational).strip()),
            _prompt_node("L1 Memory Index", str(memory_index), _read_text(memory_index).strip()),
        ]
        components = [
            _prompt_node("Conductor Policy", str(policy_path), policy),
            _prompt_node("微信人格 Persona", str(config.persona_file), persona),
            _prompt_node("微信操作规则", str(config.operations_file), operations),
            *generated_nodes,
            {
                **_prompt_node("G4W 共享 SOP 索引", str(sop_index), _extract_prompt_section(controller_system, "[Memory] (G4W Shared Memory)")),
                "children": [
                    _prompt_node("Prompt 结构模板", str(sop_structure), _read_text(sop_structure).strip()),
                    _prompt_node("共享 L1 索引", str(sop_index), _read_text(sop_index).strip()),
                ],
            },
            {
                **_prompt_node("用户长期记忆", "ConversationStore.read_memory()", _extract_prompt_section(controller_system, "# 用户长期记忆"), kind="generated"),
                "children": memory_nodes,
            },
        ]
        dynamic = ""
        current_messages = submitted.get("currentMessages", []) if isinstance(submitted, dict) else []
        for message in current_messages if isinstance(current_messages, list) else []:
            content = message.get("content", []) if isinstance(message, dict) else []
            for block in content if isinstance(content, list) else []:
                text = str(block.get("text") or "") if isinstance(block, dict) else ""
                match = re.search(r"<current_round_retrieval_context>\s*(.*?)\s*</current_round_retrieval_context>", text, re.S)
                if match:
                    dynamic = match.group(1).strip()
                    break
        return {
            "senderId": sender_id,
            "bindingKey": binding_key,
            "model": str((snapshot.get("metadata") or {}).get("model") or binding.get("conductorModel") or config.conductor_model),
            "generatedAt": str((snapshot.get("metadata") or {}).get("generatedAt") or ""),
            "snapshotPath": str(latest_path or ""),
            "final": _mask_sensitive(final_system),
            "controller": _mask_sensitive(controller_system),
            "thinking": _mask_sensitive(thinking),
            "dynamic": _mask_sensitive(dynamic),
            "components": components,
            "thinkingNode": _prompt_node("GA 行动规范", str(llmcore), thinking, kind="code"),
        }

    def settings(self, config: Config | None = None) -> dict:
        config = config or self._fresh_config()
        binding_key, sender_id, binding = self._binding(config)
        turn = TurnProgressStore(config.state_dir / "turn-progress-config.json")
        worker_turn = WorkerTurnStore(config.state_dir / "worker-turn-config.json")
        input_capture = InputCaptureStore(config.state_dir / "input-capture-config.json", config.conversations_dir)
        checkins = CheckinService(config.state_dir / "checkin-config.json")
        checkin = checkins.status(binding_key) if binding_key else {}
        chunk = _read_json(config.state_dir / "weixin-config.json", {"minChunkChars": 10})
        return {
            "envFile": str(config.env_file),
            "stateDir": str(config.state_dir),
            "workspaceRoot": str(binding.get("workspaceRoot") or config.workspace_root),
            "bindingKey": binding_key,
            "senderId": sender_id,
            "userName": config.user_name,
            "userIdentity": config.user_identity,
            "userGender": config.user_gender,
            "botName": config.bot_name,
            "conductorModel": str(binding.get("conductorModel") or config.conductor_model),
            "workerModel": config.worker_model,
            "proModel": config.pro_model,
            "turnEnabled": turn.get(binding_key) if binding_key else True,
            "workerTurnEnabled": worker_turn.get(binding_key) if binding_key else True,
            "inputCaptureEnabled": input_capture.get(binding_key) if binding_key else False,
            "chunkChars": max(1, min(int(chunk.get("minChunkChars") or 10), 3800)),
            "checkinEnabled": bool(checkin.get("enabled", config.checkin_enabled)),
            "checkinMin": int(checkin.get("minimumMinutes") or config.checkin_minimum_minutes),
            "checkinMax": int(checkin.get("maximumMinutes") or config.checkin_maximum_minutes),
            "checkinWindow": f"{int(checkin.get('minimumMinutes') or config.checkin_minimum_minutes)}–{int(checkin.get('maximumMinutes') or config.checkin_maximum_minutes)} 分钟",
            "locationEnabled": config.location_enabled,
            "webSearchEnabled": config.web_search_enabled,
            "vectorEnabled": bool(load_vector_config().get("enabled")),
        }

    def models(self) -> dict:
        config = self._fresh_config()
        _, sender_id, _ = self._binding(config)
        if not sender_id:
            raise RuntimeError("尚未找到微信会话，无法读取模型列表")
        result = self._control("list_models", {"senderId": sender_id})
        result.update({
            "workerModel": config.worker_model,
            "proModel": config.pro_model,
            "online": True,
        })
        return result

    def switch_model(self, target: str, value) -> dict:
        config = self._fresh_config()
        _, sender_id, _ = self._binding(config)
        target = str(target or "").strip().lower()
        if target == "conductor":
            if not sender_id:
                raise RuntimeError("尚未找到微信会话，无法切换当前模型")
            result = self._control("set_model", {"senderId": sender_id, "query": value})
            selected = result.get("selected") if isinstance(result.get("selected"), dict) else {}
            model = str(selected.get("model") or "").strip()
            if model:
                update_env_file(config.env_file, {"G4W_CONDUCTOR_MODEL": model})
            result["message"] = f"当前对话模型已切换为 {model or value}"
            return result
        if target not in {"worker", "pro"}:
            raise ValueError("target 必须是 conductor、worker 或 pro")
        model = str(value or "").strip()
        if not model:
            raise ValueError("模型不能为空")
        worker_model = model if target == "worker" else config.worker_model
        pro_model = model if target == "pro" else config.pro_model
        result = self._control("set_model_defaults", {"workerModel": worker_model, "proModel": pro_model})
        update_env_file(config.env_file, {
            "G4W_WORKER_MODEL": worker_model,
            "G4W_PRO_MODEL": pro_model,
        })
        result["message"] = f"{target.title()} 默认模型已切换为 {model}，新任务立即生效"
        return result

    def update_settings(self, updates: dict) -> dict:
        config = self._fresh_config()
        before = self.settings(config)
        binding_key, sender_id, binding = self._binding(config)
        env_updates = {}
        restart_fields = []

        identity_map = {
            "userName": ("G4W_USER_NAME", "userName"),
            "userIdentity": ("G4W_USER_IDENTITY", "userIdentity"),
            "userGender": ("G4W_USER_GENDER", "userGender"),
            "botName": ("G4W_BOT_NAME", "botName"),
        }
        profile_updates = {}
        for field, (env_key, profile_key) in identity_map.items():
            if field not in updates:
                continue
            value = str(updates[field] or "").strip()
            if not value:
                raise ValueError(f"{field} 不能为空")
            if field == "userGender" and value not in {"male", "female", "neutral"}:
                raise ValueError("userGender 必须是 male、female 或 neutral")
            env_updates[env_key] = value
            profile_updates[profile_key] = value
            restart_fields.append(field)

        model_map = {
            "conductorModel": "G4W_CONDUCTOR_MODEL",
            "workerModel": "G4W_WORKER_MODEL",
            "proModel": "G4W_PRO_MODEL",
        }
        for field, env_key in model_map.items():
            if field in updates:
                value = str(updates[field] or "").strip()
                if not value:
                    raise ValueError(f"{field} 不能为空")
                env_updates[env_key] = value
                restart_fields.append(field)

        if "webSearchEnabled" in updates:
            env_updates["G4W_WEB_SEARCH_ENABLED"] = "1" if bool(updates["webSearchEnabled"]) else "0"
            restart_fields.append("webSearchEnabled")

        if env_updates:
            # 防 .env 注入:任何值含换行即拒绝(update_env_file 不做转义)
            for key, value in env_updates.items():
                if "\r" in str(value) or "\n" in str(value):
                    raise ValueError(f"{key} 不能包含换行符")
            update_env_file(config.env_file, env_updates)

        if profile_updates and sender_id:
            profiles_path = config.state_dir / "profiles.json"
            profiles = _read_json(profiles_path, {"senders": {}})
            entry = profiles.setdefault("senders", {}).setdefault(sender_id, {})
            entry.update(profile_updates)
            entry["updatedAt"] = time.time()
            _write_json(profiles_path, profiles)

        if binding_key:
            if "turnEnabled" in updates:
                TurnProgressStore(config.state_dir / "turn-progress-config.json").set(binding_key, bool(updates["turnEnabled"]))
            if "workerTurnEnabled" in updates:
                WorkerTurnStore(config.state_dir / "worker-turn-config.json").set(binding_key, bool(updates["workerTurnEnabled"]))
            if "inputCaptureEnabled" in updates:
                InputCaptureStore(config.state_dir / "input-capture-config.json", config.conversations_dir).set(binding_key, bool(updates["inputCaptureEnabled"]))

        if "chunkChars" in updates:
            value = int(updates["chunkChars"])
            if not 1 <= value <= 3800:
                raise ValueError("微信分片阈值必须在 1–3800 之间")
            JsonStore(config.state_dir / "weixin-config.json", {"minChunkChars": 10}).write({"minChunkChars": value})

        checkin_fields = {"checkinEnabled", "checkinMin", "checkinMax"}
        if checkin_fields.intersection(updates):
            enabled = bool(updates.get("checkinEnabled", before["checkinEnabled"]))
            minimum = max(1, int(updates.get("checkinMin", before["checkinMin"])))
            maximum = max(minimum, int(updates.get("checkinMax", before["checkinMax"])))
            update_env_file(config.env_file, {
                "G4W_CHECKIN_ENABLED": "1" if enabled else "0",
                "G4W_CHECKIN_MIN_INTERVAL_MS": str(minimum * 60_000),
                "G4W_CHECKIN_MAX_INTERVAL_MS": str(maximum * 60_000),
            })
            if binding_key:
                checkins = CheckinService(config.state_dir / "checkin-config.json")
                if enabled:
                    checkins.configure(binding_key, sender_id, minimum, maximum, True)
                else:
                    checkins.disable(binding_key)

        if "vectorEnabled" in updates:
            set_vector_enabled(bool(updates["vectorEnabled"]))

        if binding_key and ({"workspaceRoot", "conductorModel"} & set(updates)):
            bindings_path = config.conversations_dir / "bindings.json"
            bindings_state = _read_json(bindings_path, {"bindings": {}})
            entry = bindings_state.setdefault("bindings", {}).setdefault(binding_key, binding)
            if "workspaceRoot" in updates:
                workspace = Path(str(updates["workspaceRoot"] or "")).expanduser().resolve()
                if not workspace.is_dir():
                    raise ValueError(f"工作目录不存在：{workspace}")
                entry["workspaceRoot"] = str(workspace)
            if "conductorModel" in updates:
                entry["conductorModel"] = str(updates["conductorModel"]).strip()
                entry["modelSource"] = "webui"
            entry["updatedAt"] = time.time()
            _write_json(bindings_path, bindings_state)

        after = self.settings(Config.load())
        return {
            "ok": True,
            "before": before,
            "after": after,
            "restartRequired": sorted(set(restart_fields)),
            "message": "设置已保存" + ("；部分全局设置需重启 G4W 后进入当前运行进程" if restart_fields else "并已实时生效"),
        }

    def resolve_download(self, raw_path: str) -> Path:
        config = self._fresh_config()
        candidate = Path(unquote(str(raw_path or ""))).expanduser().resolve()
        roots = [config.workspace_root.resolve(), config.state_dir.resolve()]
        if not candidate.is_file() or not any(candidate == root or root in candidate.parents for root in roots):
            raise FileNotFoundError(candidate)
        # 敏感文件拒绝下载:ENV(含 LLM API Key)/ GA 密钥 / 微信账号凭证(bot_token 可接管账号)
        name = candidate.name.lower()
        if name in {".env", "mykey.py", "mykey.json"}:
            raise FileNotFoundError(candidate)
        try:
            rel_state = candidate.relative_to(config.state_dir.resolve())
        except ValueError:
            rel_state = None
        if rel_state is not None and rel_state.parts and rel_state.parts[0] == "accounts":
            raise FileNotFoundError(candidate)
        return candidate


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "G4WDashboard/0.2"

    @property
    def dashboard(self) -> DashboardState:
        return self.server.dashboard  # type: ignore[attr-defined]

    @property
    def auth(self) -> DashboardAuth:
        return self.server.auth  # type: ignore[attr-defined]

    def _auth_token(self) -> str:
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            key, _, value = part.strip().partition("=")
            if key == "g4w_auth" and value:
                return value
        authorization = self.headers.get("Authorization") or ""
        if authorization.lower().startswith("bearer "):
            return authorization[7:].strip()
        return ""

    def _require_auth(self) -> bool:
        if self.auth.check(self._auth_token()):
            return True
        self._send_json({"ok": False, "error": "未登录"}, 401)
        return False

    def _read_json_body(self) -> dict:
        length = min(int(self.headers.get("Content-Length", "0") or 0), 1_000_000)
        payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return payload

    def log_message(self, format: str, *args) -> None:
        return

    def _send_bytes(self, body: bytes, content_type: str, status: int = 200, *, disposition: str = "", extra_headers: list = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if disposition:
            self.send_header("Content-Disposition", disposition)
        for key, value in (extra_headers or []):
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, value, status: int = 200, *, extra_headers: list = None) -> None:
        self._send_bytes(
            json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
            extra_headers=extra_headers,
        )

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/api/auth/status":
                self._send_json({
                    "authenticated": self.auth.check(self._auth_token()),
                    "initialized": self.auth.is_initialized(),
                })
                return
            if path.startswith("/api/") and not self._require_auth():
                return
            if path == "/api/dashboard":
                self._send_json(self.dashboard.snapshot())
                return
            if path == "/api/memory":
                self._send_json(self.dashboard.memory_detail(str((query.get("id") or [""])[0])))
                return
            if path == "/api/timeline":
                self._send_json(self.dashboard.timeline_data())
                return
            if path == "/api/diary":
                date = str((query.get("date") or [""])[0])
                self._send_json(self.dashboard.diary_detail(date) if date else self.dashboard.diary_index())
                return
            if path == "/api/knowledge":
                self._send_json(self.dashboard.knowledge_detail(str((query.get("id") or [""])[0])))
                return
            if path == "/api/knowledge/pdf":
                detail = self.dashboard.knowledge_detail(str((query.get("id") or [""])[0]))
                target = Path(str(detail.get("storedPath") or "")).resolve()
                if target.suffix.lower() != ".pdf" or not target.is_file():
                    self._send_json({"ok": False, "error": "仅支持 PDF 原文件"}, 400)
                    return
                name = target.name.encode("ascii", "ignore").decode() or "document.pdf"
                self._send_bytes(target.read_bytes(), "application/pdf", disposition=f'inline; filename="{name}"')
                return
            if path == "/api/worker":
                self._send_json(self.dashboard.worker_detail(str((query.get("id") or [""])[0])))
                return
            if path == "/api/prompt":
                self._send_json(self.dashboard.prompt_detail())
                return
            if path == "/api/settings":
                self._send_json(self.dashboard.settings())
                return
            if path == "/api/models":
                self._send_json(self.dashboard.models())
                return
            if path == "/api/file":
                target = self.dashboard.resolve_download(str((query.get("path") or [""])[0]))
                mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                self._send_bytes(target.read_bytes(), mime, disposition=f'attachment; filename="{target.name.encode("ascii", "ignore").decode() or "download"}"')
                return
            if path == "/api/events":
                self._stream_events()
                return
        except FileNotFoundError as error:
            self._send_json({"ok": False, "error": f"未找到：{error}"}, 404)
            return
        except Exception as error:
            self._send_json({"ok": False, "error": str(error)}, 500)
            return
        self._serve_static(path)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/auth/setup":
                payload = self._read_json_body()
                token = self.auth.setup(
                    str(payload.get("username") or "").strip(),
                    str(payload.get("password") or ""),
                )
                self._send_json(
                    {"ok": True, "username": str(payload.get("username") or "").strip()},
                    extra_headers=[("Set-Cookie", f"g4w_auth={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={DashboardAuth._SESSION_TTL}")],
                )
                return
            if path == "/api/auth/login":
                payload = self._read_json_body()
                username = str(payload.get("username") or "").strip()
                password = str(payload.get("password") or "")
                if not self.auth.verify(username, password):
                    self._send_json({"ok": False, "error": "用户名或密码错误"}, 401)
                    return
                token = self.auth.create_session()
                self._send_json(
                    {"ok": True, "username": username},
                    extra_headers=[("Set-Cookie", f"g4w_auth={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={DashboardAuth._SESSION_TTL}")],
                )
                return
            if path == "/api/auth/logout":
                self.auth.revoke(self._auth_token())
                self._send_json(
                    {"ok": True},
                    extra_headers=[("Set-Cookie", "g4w_auth=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0")],
                )
                return
            if path == "/api/auth/password":
                if not self._require_auth():
                    return
                payload = self._read_json_body()
                result = self.auth.change_password(
                    str(payload.get("username") or "").strip(),
                    str(payload.get("oldPassword") or ""),
                    str(payload.get("newPassword") or ""),
                )
                self._send_json(result)
                return
            if path not in {"/api/settings", "/api/model"}:
                self._send_json({"ok": False, "error": "Not found"}, 404)
                return
            if not self._require_auth():
                return
            payload = self._read_json_body()
            if path == "/api/model":
                self._send_json(self.dashboard.switch_model(payload.get("target"), payload.get("value")))
            else:
                self._send_json(self.dashboard.update_settings(payload))
        except ValueError as error:
            self._send_json({"ok": False, "error": str(error)}, 400)
        except Exception as error:
            self._send_json({"ok": False, "error": str(error)}, 500)

    def _stream_events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        previous = ""
        try:
            for _ in range(25):
                snapshot = self.dashboard.snapshot()
                serialized = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
                if serialized != previous:
                    self.wfile.write(f"event: dashboard\ndata: {serialized}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    previous = serialized
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        candidate = (STATIC_DIR / relative).resolve()
        if STATIC_DIR not in candidate.parents and candidate != STATIC_DIR:
            self._send_bytes(b"Not found", "text/plain; charset=utf-8", 404)
            return
        if not candidate.is_file():
            candidate = STATIC_DIR / "index.html"
        try:
            body = candidate.read_bytes()
        except OSError:
            self._send_bytes(b"Dashboard assets unavailable", "text/plain; charset=utf-8", 500)
            return
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self._send_bytes(body, f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type)


def run_dashboard(config: Config | None = None, host: str = "127.0.0.1", port: int = 18180) -> int:
    config = config or Config.load()
    server = ThreadingHTTPServer((host, int(port)), DashboardHandler)
    server.dashboard = DashboardState(config)  # type: ignore[attr-defined]
    server.auth = DashboardAuth(config.state_dir)  # type: ignore[attr-defined]
    print(f"[G4W] Dashboard listening at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(prog="python -m G4W.dashboard.server")
    parser.add_argument("--host", default=os.environ.get("G4W_DASHBOARD_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("G4W_DASHBOARD_PORT", "18180")))
    options = parser.parse_args()
    raise SystemExit(run_dashboard(Config.load(), options.host, options.port))
