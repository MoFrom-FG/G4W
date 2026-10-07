from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import subprocess
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
from ..memory.persona_store import BACKUP_KEEP, MAX_CHARS, PERSONA_VARIABLES, PersonaError, PersonaStore
from ..core.model_config import (ANTHROPIC_HINT, LITE_VAR, MAIN_VAR, PROVIDER_PRESETS, ModelConfigError,
                                 add_model, add_provider, delete_model, delete_provider,
                                 ensure_template as ensure_mykey_template, fetch_models,
                                 load_providers, mask_key, probe as probe_model_config,
                                 raw_key as mykey_raw_key, read_config as read_mykey_config,
                                 save_config as save_mykey_config, test_model)
from ..memory.vector.vector_config import load_config as load_vector_config
from ..memory.vector.vector_config import set_vector_enabled
from ..core.platform_adapt import detached_kwargs, kill_tree, pid_alive, pids_listening_on, portable_python, service_launch_kwargs, service_python, venv_python, venv_site_packages
from . import setup_wizard


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


# 自定义主题可编辑的 CSS 变量白名单(无 "--" 前缀,存储时用)
_THEME_VARS = frozenset({
    # 强调/通用(shared)
    "accent-rgb", "wechat", "wechat-strong", "wechat-soft", "line-strong",
    "danger", "warning", "blue", "purple",
    "cat-life", "cat-work", "cat-study", "cat-exercise", "cat-entertainment",
    "cat-health", "cat-social", "cat-care", "cat-travel", "cat-rest",
    # 界面色(分 dark / light 两套)
    "bg", "sidebar", "panel", "panel-strong", "field", "line",
    "text", "soft", "muted", "faint", "overlay", "shadow",
    # 形状
    "radius",
})


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _pid_alive(pid: int) -> bool:
    return pid_alive(pid)


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


def _backup_time(path: Path) -> float:
    """快照时间优先从备份目录名解析(l4compress-YYYYMMDD-HHMMSS = 压缩运行时刻),
    回退文件 mtime。备份内文件复制时保留原 mtime,不可作为快照时间。"""
    try:
        name = path.parents[1].name
        stamp = name[len("l4compress-"): len("l4compress-") + 15]
        return time.mktime(time.strptime(stamp, "%Y%m%d-%H%M%S"))
    except (ValueError, IndexError, OSError):
        return _timestamp(path)


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
    _SESSIONS_FILE = "dashboard-sessions.json"

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / self._AUTH_FILE
        self._lock = threading.Lock()
        self._sessions: dict[str, float] = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 会话持久化:重启 dashboard 后已登录的浏览器 cookie 仍然有效
        self._sessions = self._load_sessions()

    def _sessions_path(self) -> Path:
        return self.path.parent / self._SESSIONS_FILE

    def _load_sessions(self) -> dict:
        try:
            raw = json.loads(self._sessions_path().read_text(encoding="utf-8"))
        except Exception:
            return {}
        now = time.time()
        return {str(k): float(v) for k, v in raw.items() if float(v) > now}

    def _save_sessions(self) -> None:
        try:
            self._sessions_path().write_text(json.dumps(self._sessions, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

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
            self._save_sessions()
        return token

    def check(self, token: str) -> bool:
        if not token:
            return False
        now = time.time()
        with self._lock:
            expiry = self._sessions.get(token)
            if expiry is None:
                # 磁盘上有但内存没有(极端情况:文件被外部改动后本进程未重启)
                saved = self._load_sessions()
                if saved:
                    self._sessions.update(saved)
                    expiry = self._sessions.get(token)
            if expiry is None:
                return False
            if expiry < now:
                self._sessions.pop(token, None)
                self._save_sessions()
                return False
            return True

    def revoke(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)
            self._save_sessions()

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


_EMBED_MIRRORS_MODULE = None


def _embed_mirrors_module():
    """按文件加载 embed_mirrors.py（不能 import G4W.memory.vector 包：会拉 numpy/hnsw）。"""
    global _EMBED_MIRRORS_MODULE
    if _EMBED_MIRRORS_MODULE is None:
        import importlib.util

        path = Path(__file__).resolve().parents[1] / "memory" / "vector" / "embed_mirrors.py"
        spec = importlib.util.spec_from_file_location("_dash_embed_mirrors", path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _EMBED_MIRRORS_MODULE = mod
    return _EMBED_MIRRORS_MODULE


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
            # v3.0.4: 不再要求该对象出现在 profiles.json 里。
            # 旧逻辑是「profiles.json 命中 ∩ 有 history_insight 产物」，而 profiles.json
            # 只在看板里手动命名、或在微信里发身份更新命令时才会写入 —— 于是用户跑完
            # /l4compress（active_knowledge.json / memory_brief.md 都已生成），记忆页
            # 依旧显示「暂无记忆账号」。现在只要「有 L4 产物」就列出；没有产物的非会话
            # 目录（env-audit-local 之类）仍会被下面的判断滤掉。
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
            # 显示名兜底：profiles.json 没记录时用 .env 里的身份（单人使用语义正确），
            # 再退到会话目录名，避免页面只显示一串 sender id。
            display_name = str(profile.get("userName") or config.user_name or conversation.name)
            display_identity = str(profile.get("userIdentity") or config.user_identity or "")
            items.append({
                "id": conversation.name,
                "name": display_name,
                "identity": display_identity,
                "updatedAt": _short_date(max(_timestamp(active), _timestamp(brief))),
                "updatedTimestamp": max(_timestamp(active), _timestamp(brief)),
                "brief": re.sub(r"\s+", " ", brief_text)[:220],
                "counts": counts,
                "backupCount": len(backups),
                "activeBytes": active.stat().st_size if active.is_file() else 0,
            })
        items.sort(key=lambda item: item["updatedTimestamp"], reverse=True)
        return items

    # ---------- G4W 程序更新（环境配置页 →「G4W 程序更新」卡片）----------
    # 更新源与 ga-admin 同构：GitHub Releases（api.github.com/repos/<owner>/<repo>/releases/latest），
    # 直连失败时回落到 gh-proxy 镜像；只做「检查 + 展示」，落盘/替换仍走经过验证的
    # apply-update 流程（停服 → 备份 → 原子替换 → journal → 重启）。
    UPDATE_REPO = "MoFrom-FG/G4W"

    def update_status(self) -> dict:
        config = self._fresh_config()
        root = Path(config.workspace_root)
        info = {
            "current": "",
            "appliedAt": "",
            "sourceFile": "",
            "root": str(root),
            "channel": f"https://github.com/{self.UPDATE_REPO}/releases",
            "latestReleasePage": f"https://github.com/{self.UPDATE_REPO}/releases/latest",
        }
        for candidate in (root / "runtime" / "G4W-version.json", root / "G4W_RELEASE_MANIFEST.json"):
            data = _read_json(candidate, {})
            if not isinstance(data, dict):
                continue
            version = str(data.get("version") or data.get("release") or "").strip()
            if not version:
                continue
            info["current"] = version
            info["appliedAt"] = str(data.get("applied_at") or data.get("built_at") or "")
            try:
                info["sourceFile"] = str(candidate.relative_to(root))
            except ValueError:
                info["sourceFile"] = str(candidate)
            break
        return info

    # ---- 一键更新：下载补丁 → 校验 → 解压 → 交给经过验证的补丁脚本接管 ----
    _UPDATE_STATE: dict = {}

    @staticmethod
    def _set_stage(stage: str, progress: int, message: str, **extra) -> None:
        st = DashboardState._UPDATE_STATE
        st.update({"stage": stage, "progress": int(progress), "message": message, **extra})
        st["log"] = (list(st.get("log") or []) + [f"[{time.strftime('%H:%M:%S')}] {stage} {progress}% {message}"])[-40:]

    @staticmethod
    def _download_asset(url: str, dst: Path, mirror: bool, lo: int, hi: int) -> bool:
        import urllib.request
        st = DashboardState._UPDATE_STATE
        urls = [("https://gh-proxy.com/" + url) if mirror else url]
        if mirror:
            urls.append(url)
        for u in urls:
            try:
                req = urllib.request.Request(u, headers={"User-Agent": "G4W-dashboard"})
                with urllib.request.urlopen(req, timeout=30) as resp, open(dst, "wb") as fh:
                    total = int(resp.headers.get("Content-Length") or 0)
                    got = 0
                    while True:
                        chunk = resp.read(1 << 18)
                        if not chunk:
                            break
                        fh.write(chunk)
                        got += len(chunk)
                        if total:
                            pct = lo + int((hi - lo) * got / total)
                            st.update({"progress": max(lo, min(hi, pct)),
                                       "message": f"下载中 {got // 1024} KB / {total // 1024} KB"})
                return True
            except Exception as exc:  # noqa: BLE001
                st["log"] = (list(st.get("log") or []) + [f"download failed {u}: {type(exc).__name__}: {exc}"])[-40:]
        return False

    def _update_worker(self, root: Path, asset: dict, sha_asset, mirror: bool, pid: int) -> None:
        import hashlib
        import os as _os
        import shutil as _sh
        import subprocess
        import zipfile

        try:
            name = str(asset.get("name") or "patch.zip")
            work = root / ".g4w-update"
            dl = work / "downloads"
            dl.mkdir(parents=True, exist_ok=True)
            dst = dl / name
            if not self._download_asset(str(asset.get("url") or ""), dst, mirror, 3, 55):
                self._set_stage("failed", 0, "下载失败（检查网络或镜像设置）")
                return
            self._set_stage("verify", 60, "校验 sha256…")
            if sha_asset and sha_asset.get("url"):
                sha_path = dl / (name + ".sha256")
                if self._download_asset(str(sha_asset["url"]), sha_path, mirror, 60, 63):
                    want = sha_path.read_text(encoding="utf-8", errors="replace").split()[0].strip().upper()
                    h = hashlib.sha256()
                    with open(dst, "rb") as fh:
                        for c in iter(lambda: fh.read(1 << 20), b""):
                            h.update(c)
                    got = h.hexdigest().upper()
                    if got != want:
                        self._set_stage("failed", 0, "sha256 校验不通过，已放弃更新")
                        return
                    DashboardState._UPDATE_STATE["sha256"] = got
            self._set_stage("extract", 68, "解压补丁…")
            staged = work / "staged" / str(DashboardState._UPDATE_STATE.get("version") or "new")
            _sh.rmtree(staged, ignore_errors=True)
            staged.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(dst) as zf:
                zf.extractall(staged)
            ps1 = None
            for base, _dirs, names in _os.walk(staged):
                if "apply-update.ps1" in names and "update-manifest.json" in names:
                    ps1 = Path(base) / "apply-update.ps1"
                    break
            if ps1 is None:
                self._set_stage("failed", 0, "补丁包结构异常（缺少 apply-update.ps1 / update-manifest.json）")
                return
            self._set_stage("handoff", 80, "停服并应用更新…")
            cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1),
                   "-Root", str(root), "-AutoStart", "-ForceStop",
                   "-WaitPid", str(pid), "-WaitSeconds", "60"]
            # 注意：这里**不用** DETACHED_PROCESS。实测（本机 + 部分沙箱/安全软件）
            # DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP 的子进程根本不会执行，
            # 而 CREATE_NO_WINDOW 正常；后者同样没有控制台窗口，且不会被安全软件拦。
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            helper_log = work / "helper-console.log"
            handle = open(helper_log, "w", encoding="utf-8", errors="replace")
            proc = subprocess.Popen(cmd, cwd=str(staged), creationflags=flags,
                                    stdout=handle, stderr=handle, stdin=subprocess.DEVNULL)
            DashboardState._UPDATE_STATE["_helper_log_handle"] = handle   # 持有句柄，避免被 GC 关闭
            DashboardState._UPDATE_STATE["log"] = (
                list(DashboardState._UPDATE_STATE.get("log") or [])
                + [f"helper: {ps1}", f"helper pid={proc.pid}", f"helper args: -AutoStart -ForceStop -WaitPid {pid}"]
            )[-40:]
            # 这里**故意不退出**：helper 会以 -ForceStop 结束本进程（以及主服务/监控）。
            # 这样就不依赖“子进程在父进程退出后仍然存活”——某些环境（带 Job Object 的
            # 沙箱/调度器）会在父进程退出时清掉整棵进程树，detached 也没用。
            self._set_stage("handoff", 88, "已交给更新脚本，本窗口即将被重启…")
            return
        except Exception as exc:  # noqa: BLE001
            self._set_stage("failed", 0, f"{type(exc).__name__}: {exc}")

    def update_progress(self) -> dict:
        return dict(DashboardState._UPDATE_STATE)

    def update_apply(self, mirror: bool = True) -> dict:
        import os as _os
        st = DashboardState._UPDATE_STATE
        if st.get("stage") in ("download", "verify", "extract", "handoff"):
            return {"ok": False, "error": "更新已在进行中", "state": dict(st)}
        config = self._fresh_config()
        root = Path(config.workspace_root)
        info = self.update_check(mirror=mirror)
        if not info.get("ok"):
            return {"ok": False, "error": info.get("error") or "无法检查最新版本", "hint": info.get("hint")}
        if not info.get("hasUpdate"):
            return {"ok": False, "error": f"当前已是最新版本（{info.get('current') or info.get('latest')}）"}
        current = str(info.get("current") or "").lstrip("vV") or "unknown"
        latest = str(info.get("latest") or "").lstrip("vV")
        want = f"g4w-patch-{current}-to-{latest}.zip".lower()
        asset = None
        sha_asset = None
        for item in info.get("assets") or []:
            nm = str(item.get("name") or "").lower()
            if nm == want:
                asset = item
            elif nm == want + ".sha256":
                sha_asset = item
        if asset is None:
            names = [str(i.get("name") or "") for i in (info.get("assets") or [])]
            return {
                "ok": False,
                "error": f"最新版本没有提供 {want} 增量补丁，无法一键更新",
                "hint": "可点「打开发布页」下载整包手动覆盖；发布页现有附件：" + ("、".join(names[:6]) or "（无）"),
            }
        st.clear()
        st.update({
            "stage": "download", "progress": 3, "message": "准备下载补丁…",
            "version": str(info.get("latest") or ""), "current": str(info.get("current") or ""),
            "asset": str(asset.get("name") or ""), "error": "",
            "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "log": [f"root={root}", f"asset={asset.get('name')} ({asset.get('size')} bytes)"],
        })
        threading.Thread(target=self._update_worker,
                         args=(root, asset, sha_asset, mirror, _os.getpid()), daemon=True).start()
        return {"ok": True, "started": True, "version": info.get("latest"), "asset": asset.get("name")}

    @staticmethod
    def _version_key(value: str) -> tuple:
        """把 'v3.0.10' 这类版本号转成可比较的元组（只取数字段）。"""
        parts = []
        for chunk in str(value or "").lstrip("vV").split("."):
            digits = "".join(ch for ch in chunk if ch.isdigit())
            parts.append(int(digits) if digits else 0)
        return tuple(parts) if parts else (0,)

    def update_check(self, mirror: bool = True) -> dict:
        """查询 GitHub Releases 最新版本。

        mirror=True（环境配置页「国内镜像加速」打开，默认）先走 gh-proxy 镜像再直连；
        mirror=False 只直连（适合有代理或境外网络）。
        """
        import json as _json
        import platform
        import urllib.request

        api = f"https://api.github.com/repos/{self.UPDATE_REPO}/releases/latest"
        mirrored = "https://gh-proxy.com/" + api
        urls = [mirrored, api] if mirror else [api]
        payload = None
        last_error = ""
        used_url = ""
        for url in urls:
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "G4W-dashboard",
                    "Accept": "application/vnd.github+json",
                })
                with urllib.request.urlopen(req, timeout=12) as resp:
                    payload = _json.loads(resp.read().decode("utf-8", "replace"))
                used_url = url
                last_error = ""
                break
            except Exception as exc:  # noqa: BLE001 - 网络错误种类多，统一回报
                last_error = f"{type(exc).__name__}: {exc}"
        if payload is None:
            return {
                "ok": False,
                "error": last_error or "无法连接 GitHub Releases",
                "tried": urls,
                "mirror": mirror,
                "hint": "国内网络请打开「国内镜像加速」，或为 G4W 进程设置 HTTPS_PROXY（例如 http://127.0.0.1:7897）。",
                "current": self.update_status().get("current", ""),
            }
        tag = str(payload.get("tag_name") or payload.get("name") or "").strip()
        assets = []
        for item in payload.get("assets") or []:
            if isinstance(item, dict):
                assets.append({
                    "name": str(item.get("name") or ""),
                    "size": int(item.get("size") or 0),
                    "url": str(item.get("browser_download_url") or ""),
                })
        current = str(self.update_status().get("current") or "")
        latest_key = self._version_key(tag)
        current_key = self._version_key(current)
        return {
            "ok": True,
            "latest": tag,
            "current": current,
            "hasUpdate": bool(tag) and latest_key > current_key,
            "ahead": bool(current) and current_key > latest_key,
            "publishedAt": str(payload.get("published_at") or ""),
            "notesUrl": str(payload.get("html_url") or f"https://github.com/{self.UPDATE_REPO}/releases"),
            "body": str(payload.get("body") or "")[:1500],
            "assets": assets,
            "platform": f"{platform.system()} {platform.release()}",
            "mirror": mirror,
            "tried": urls,
            "usedUrl": used_url,
        }

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
            "dir": str(item.get("dir") or ""),
            "archivePath": str(item.get("archivePath") or ""),
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
        # 下次主动联系（checkin）：所有 enabled binding 中最近的 nextAt
        next_checkin_at = 0
        try:
            checkin_state = _read_json(state_dir / "checkin-config.json", {"bindings": {}})
            now = time.time()
            for binding in (checkin_state.get("bindings") or {}).values():
                if not isinstance(binding, dict) or not binding.get("enabled"):
                    continue
                try:
                    at = float(binding.get("nextAt") or 0)
                except (TypeError, ValueError):
                    continue
                if at > now and (next_checkin_at == 0 or at < next_checkin_at):
                    next_checkin_at = at
        except Exception:
            pass

        return {
            "updatedAt": time.time(),
            "nextCheckinAt": next_checkin_at,
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
                "updatedAt": _short_date(_backup_time(path)),
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
        # 上一版画像：优先 user_profile.prev.md（conductor 覆盖写时留档），否则最近备份里的画像
        profile_path = insight / "user_profile.md"
        prev_path = insight / "user_profile.prev.md"
        user_profile = _read_text(profile_path).strip()
        user_profile_prev = _read_text(prev_path).strip()
        profile_prev_source = "prev" if user_profile_prev else ""
        if not user_profile_prev and backups_root.is_dir():
            for bak in sorted(
                backups_root.glob("*/history_insight/user_profile.md"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            ):
                candidate = _read_text(bak).strip()
                if candidate and candidate != user_profile:
                    user_profile_prev = candidate
                    profile_prev_source = bak.parents[1].name
                    break
        return {
            "id": target.name,
            "name": str(profile.get("userName") or target.name),
            "identity": str(profile.get("userIdentity") or ""),
            "botName": str(profile.get("botName") or ""),
            "gender": str(profile.get("userGender") or ""),
            "userProfile": user_profile,
            "userProfilePrev": user_profile_prev,
            "userProfilePrevSource": profile_prev_source,
            "brief": _read_text(brief_path).strip(),
            "sections": sections,
            "backups": backups,
            "source": str(active_path),
            "activeBytes": active_path.stat().st_size if active_path.is_file() else 0,
            "updatedAt": _short_date(_timestamp(active_path)),
        }

    def memory_timeline(self, memory_id: str) -> dict:
        """画像时间轴:遍历所有历史快照,返回每份的元信息(键计数等,轻量)。"""
        config = self._fresh_config()
        target = config.conversations_dir / safe_segment(memory_id)
        if target.parent != config.conversations_dir or not target.is_dir():
            raise FileNotFoundError(memory_id)
        backups_root = target / "summaries" / ".backups"
        points = []
        if backups_root.is_dir():
            for path in backups_root.glob("l4compress-*/history_insight/active_knowledge.json"):
                active = _read_json(path, {})
                points.append({
                    "name": path.parents[1].name,
                    "updatedAt": _short_date(_backup_time(path)),
                    "bytes": path.stat().st_size,
                    "counts": {
                        key: (len(value) if isinstance(value, (list, dict)) else 0)
                        for key, value in active.items()
                    },
                    "hasBrief": (path.parent / "memory_brief.md").is_file(),
                })
        points.sort(key=lambda item: item["name"], reverse=True)  # 新在前
        return {"points": points}

    def memory_backup(self, memory_id: str, backup_name: str) -> dict:
        """单份历史快照详情:当时的完整画像 + 当时简报。"""
        config = self._fresh_config()
        target = config.conversations_dir / safe_segment(memory_id)
        if target.parent != config.conversations_dir or not target.is_dir():
            raise FileNotFoundError(memory_id)
        name = safe_segment(backup_name)
        active_path = target / "summaries" / ".backups" / name / "history_insight" / "active_knowledge.json"
        if not active_path.is_file():
            raise FileNotFoundError(backup_name)
        return {
            "name": name,
            "updatedAt": _short_date(_backup_time(active_path)),
            "active": _read_json(active_path, {}),
            "brief": _read_text(active_path.parent / "memory_brief.md").strip(),
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
            "theme": str(getattr(config, "timeline_theme", "default") or "default").lower(),
            "metrics": {
                "days": len(dates),
                "events": event_count,
                "updatedAt": _short_date(_timestamp(path)),
            },
        }

    def timeline_theme_set(self, theme: str) -> dict:
        """切换独立时间线站点的主题（default / neko）：写 .env 并立即重建站点。

        站点是静态产物：主题决定用哪份资产包（timeline-dashboard-assets-<theme>.zip），
        build() 时会按资产包哈希重新释放 assets/dashboard.{css,js}。看板/主服务都不需要重启
        （publisher 在 build 时读 .env），已经跑着的时间线服务也会直接提供新文件。
        """
        value = str(theme or "").strip().lower()
        if value not in ("default", "neko"):
            return {"ok": False, "error": "主题只能是 default 或 neko"}
        config = self._fresh_config()
        update_env_file(config.env_file, {"G4W_TIMELINE_UI_THEME": value})
        from ..core.records import TimelineStore
        from ..features.timeline_publish import TimelinePublisher

        timeline_dir = config.timeline_dir
        store = TimelineStore(
            timeline_dir / "timeline-facts.json",
            config.state_dir / "legacy-import" / "timeline" / "timeline-facts.json",
        )
        publisher = TimelinePublisher(store, timeline_dir, locale=config.timeline_locale, theme=value)
        info = publisher.build()
        return {
            "ok": True,
            "theme": value,
            "siteDir": info.get("siteDir"),
            "indexFile": info.get("indexFile"),
            "url": "http://127.0.0.1:18181/",
            "assets": info.get("assets"),
            "envFile": str(config.env_file),
        }

    # ---------- 人设预设（presets） ----------
    def _persona_store(self, config: Config | None = None) -> PersonaStore:
        store = PersonaStore(config or self._fresh_config())
        store.seed_builtins()
        return store

    def persona(self) -> dict:
        config = self._fresh_config()
        store = self._persona_store(config)
        presets = store.list_presets()
        active_id = store.active_id()
        return {
            "presets": presets,
            "activeId": active_id,
            "activeRegistered": any(item["id"] == active_id for item in presets),
            "variables": PERSONA_VARIABLES,
            "runtimeFile": str(store.runtime_file),
            "runtimeBytes": len(store.runtime_markdown().encode("utf-8")),
            "presetsDir": str(store.presets_dir),
            "backups": store.backups()[:20],
            "maxChars": MAX_CHARS,
            "backupKeep": BACKUP_KEEP,
        }

    def persona_save(self, payload: dict) -> dict:
        store = self._persona_store()
        preset_id = str(payload.get("id") or "").strip()
        if not preset_id:
            raise PersonaError("缺少预设 id")
        sections = payload.get("sections")
        if not isinstance(sections, list):
            raise PersonaError("sections 必须是数组")
        doc = store.save(preset_id, sections, name=str(payload.get("name") or ""))
        return {
            "ok": True,
            "preset": {key: doc.get(key) for key in ("id", "name", "chars", "warnings", "runtimeUpdated")},
            **self.persona(),
        }

    def persona_create(self, payload: dict) -> dict:
        store = self._persona_store()
        doc = store.create(str(payload.get("name") or "新人设"), source_id=str(payload.get("sourceId") or ""))
        return {"ok": True, "createdId": doc.get("id"), **self.persona()}

    def persona_rename(self, payload: dict) -> dict:
        store = self._persona_store()
        doc = store.rename(str(payload.get("id") or ""), str(payload.get("name") or ""))
        return {"ok": True, "id": doc.get("id"), "name": doc.get("name"), **self.persona()}

    def persona_delete(self, payload: dict) -> dict:
        store = self._persona_store()
        info = store.delete(str(payload.get("id") or ""))
        return {"ok": True, **info, **self.persona()}

    def persona_restore(self, payload: dict) -> dict:
        store = self._persona_store()
        doc = store.restore_builtin(str(payload.get("id") or ""))
        return {"ok": True, "id": doc.get("id"), "name": doc.get("name"), **self.persona()}

    def persona_import(self, payload: dict) -> dict:
        store = self._persona_store()
        doc = store.import_markdown(
            str(payload.get("name") or "导入人设"),
            str(payload.get("markdown") or ""),
            preset_id=str(payload.get("id") or ""),
        )
        return {"ok": True, "id": doc.get("id"), "name": doc.get("name"),
                "parseWarnings": doc.get("parseWarnings") or [], **self.persona()}

    def persona_import_runtime(self, payload: dict) -> dict:
        store = self._persona_store()
        doc = store.import_runtime_as_preset(str(payload.get("name") or "当前人设"))
        return {"ok": True, "id": doc.get("id"), "name": doc.get("name"), **self.persona()}

    def persona_export(self, preset_id: str) -> dict:
        return {"ok": True, **self._persona_store().export_markdown(preset_id)}

    def _control_submit(self, action: str, payload: dict) -> str:
        """只投递不等待（用于 reread 这类会跑模型轮次的慢动作）。"""
        config = self._fresh_config()
        pid = self._core_pid(config)
        if not _pid_alive(pid):
            raise RuntimeError("G4W 主进程未运行")
        return DashboardControlMailbox(config.state_dir).submit(action, payload)

    def persona_activate(self, preset_id: str) -> dict:
        """注入（第一步）：激活预设（写运行时文件）并投递一次 reread，立刻返回。

        第二步由前端轮询 /api/persona/inject-status —— reread 会跑一次真实模型轮次（可能几十秒），
        不能让 HTTP 请求一直等（前端 fetchJson 有 10s 超时，等下去会变成
        “signal is aborted without reason”）。
        """
        config = self._fresh_config()
        store = self._persona_store(config)
        info = store.activate(preset_id)
        _, sender_id, _binding_entry = self._binding(config)
        inject_id, note = "", ""
        if not sender_id:
            note = "已激活（当前没有微信会话，下一条消息生效）。"
        else:
            try:
                inject_id = self._control_submit("reread", {"senderId": sender_id})
                note = "已激活，正在让当前会话重读（其他会话下一条消息自动生效）…"
            except Exception as error:  # 主服务没运行时降级：文件已写，下一条消息生效
                note = f"已激活；注入未开始（{error}）。下一条消息即生效。"
        return {"ok": True, **info, "injected": bool(inject_id), "injectId": inject_id,
                "injectedDone": False, "senderId": sender_id, "reply": "", "note": note}

    # ---------- 模型配置（供应商 / 可用模型 / 角色） ----------
    def _masked_providers(self, config) -> list:
        rows = []
        for row in load_providers(config):
            rows.append({
                "id": row["id"],
                "name": row["name"],
                "apibase": row["apibase"],
                "hasKey": bool(str(row.get("apikey") or "").strip()),
                "keyMask": mask_key(row.get("apikey")),
                "addedAt": row.get("addedAt") or "",
            })
        return rows

    def model_config(self) -> dict:
        """供应商列表 + 可用模型 + conductor/worker 当前选择（密钥只给掩码）。"""
        config = self._fresh_config()
        state = read_mykey_config(config)
        models = []
        for entry in state.get("entries") or []:
            name = entry["name"] or entry["model"]
            models.append({
                "var": entry["var"], "name": name, "model": entry["model"], "apibase": entry["apibase"],
                "hasKey": entry["hasKey"], "keyMask": entry["keyMask"],
                "managed": entry["var"] in {MAIN_VAR, LITE_VAR},
                "inUse": name in {str(config.conductor_model or ""), str(config.worker_model or "")},
            })
        return {
            "ok": True,
            "file": state["file"],
            "exists": state["exists"],
            "updatedAt": state["updatedAt"],
            "parseError": state["parseError"],
            "providers": self._masked_providers(config),
            "models": models,
            "presets": PROVIDER_PRESETS,
            "anthropicHint": ANTHROPIC_HINT,
            "skipped": (Path(config.state_dir) / ".model-key-skipped").is_file(),
        }

    def model_config_save(self, payload: dict) -> dict:
        """兼容旧接口：保存 main / lite 两个变量（密钥留空 = 沿用已保存的）。"""
        config = self._fresh_config()
        current = read_mykey_config(config)

        def merge(variable: str, incoming) -> dict:
            row = dict(incoming) if isinstance(incoming, dict) else {}
            existing = dict((current.get("managed") or {}).get(variable) or {})
            model = str(row.get("model") or "").strip()
            row["model"] = model
            row["name"] = str(row.get("name") or model or existing.get("name") or variable)
            if not str(row.get("apikey") or "").strip():
                row["apikey"] = mykey_raw_key(config, variable)
            row.setdefault("api_mode", existing.get("apiMode") or "chat_completions")
            row.setdefault("reasoning_effort", existing.get("reasoningEffort") or "xhigh")
            row.setdefault("stream", True)
            return row

        result = save_mykey_config(config, merge(MAIN_VAR, payload.get("main")), merge(LITE_VAR, payload.get("lite")))
        state = self.model_config()
        state["saved"] = {"file": str(result.get("file") or ""), "backup": str(result.get("backup") or "")}
        return state

    def model_config_probe(self, payload: dict) -> dict:
        """探测接口连通性（key 留空则用已保存的密钥，明文只在进程内使用）。"""
        config = self._fresh_config()
        apibase = str(payload.get("apibase") or "").strip()
        apikey = str(payload.get("apikey") or "").strip() or mykey_raw_key(config)
        model = str(payload.get("model") or "").strip()
        return dict(probe_model_config(apibase, apikey, model))

    def model_config_template(self, payload: dict) -> dict:
        """按包内模板创建 mykey.py（已存在则不动，绝不覆盖）。"""
        config = self._fresh_config()
        info = ensure_mykey_template(config)
        state = self.model_config()
        state["created"] = bool(info.get("created"))
        return state

    def model_config_provider_add(self, payload: dict) -> dict:
        config = self._fresh_config()
        result = add_provider(config, payload.get("name"), payload.get("apibase"), payload.get("apikey"))
        state = self.model_config()
        state["saved"] = {"updated": str(result.get("updated") or ""), "added": str(result.get("added") or "")}
        return state

    def model_config_provider_delete(self, payload: dict) -> dict:
        config = self._fresh_config()
        delete_provider(config, payload.get("id"))
        return self.model_config()

    def model_config_models_fetch(self, payload: dict) -> dict:
        """用某个已添加供应商的 base + key 拉取模型列表。"""
        config = self._fresh_config()
        provider_id = str(payload.get("providerId") or payload.get("id") or "").strip()
        if not provider_id:
            raise ModelConfigError("请先选择一个供应商")
        result = fetch_models(config, provider_id)
        result["existing"] = [entry["model"] for entry in read_mykey_config(config).get("entries") or []]
        return result

    def model_config_model_add(self, payload: dict) -> dict:
        """把模型加入可用模型（写进 mykey.py）。"""
        config = self._fresh_config()
        provider_id = str(payload.get("providerId") or "").strip()
        model = str(payload.get("model") or "").strip()
        if not provider_id:
            raise ModelConfigError("请先选择一个供应商")
        result = add_model(config, provider_id, model)
        state = self.model_config()
        state["added"] = str(result.get("added") or result.get("already") or "")
        return state

    def model_config_model_delete(self, payload: dict) -> dict:
        config = self._fresh_config()
        variable = str(payload.get("var") or "").strip()
        delete_model(config, variable)
        return self.model_config()

    def model_config_model_test(self, payload: dict) -> dict:
        """测试某个模型连通性：真发一次最小 chat，返回耗时与回复片段。"""
        config = self._fresh_config()
        provider_id = str(payload.get("providerId") or "").strip()
        model = str(payload.get("model") or "").strip()
        if not provider_id:
            raise ModelConfigError("请先选择该模型所属的供应商")
        return test_model(config, provider_id, model)

    def persona_inject_status(self, inject_id: str) -> dict:
        """注入（第二步）：轮询 reread 的结果。"""
        request_id = str(inject_id or "").strip()
        if not request_id:
            raise PersonaError("缺少 injectId")
        config = self._fresh_config()
        result = DashboardControlMailbox(config.state_dir).wait(request_id, timeout=0.2)
        if result is None:
            return {"ok": True, "done": False, "injectId": request_id}
        reply = str(result.get("reply") or "")
        if result.get("ok"):
            note = "已对当前会话注入；其他会话下一条消息自动生效。"
        else:
            note = f"注入未完成（{result.get('error') or '未知错误'}）。下一条消息仍会生效。"
        return {"ok": True, "done": True, "injectId": request_id, "injectOk": bool(result.get("ok")),
                "reply": reply, "note": note}

    def _diary_root(self, config: Config) -> tuple[Path, str]:
        _, sender_id, _ = self._binding(config)
        if sender_id:
            return config.conversations_dir / safe_segment(sender_id) / "summaries" / "diary", sender_id
        candidates = sorted(config.conversations_dir.glob("*/summaries/diary"), key=_timestamp, reverse=True)
        return (candidates[0], candidates[0].parents[1].name) if candidates else (config.diary_dir, "")

    # ---------- 待办（与微信 /todo 同源） ----------

    def _todo_store(self, config: Config):
        from ..memory.todo import TodoStore

        return TodoStore(config.state_dir / "todo-state.json")

    def todo_list(self) -> dict:
        config = self._fresh_config()
        try:
            from ..memory.todo import _fmt_local

            store = self._todo_store(config)
            raw = store.store.read().get("tasks", {})
            tasks = {}
            for sender_id, items in (raw or {}).items():
                tasks[sender_id] = [
                    {
                        "id": t.get("id"),
                        "text": t.get("text"),
                        "status": t.get("status"),
                        "dueAt": t.get("dueAt"),
                        "dueLabel": _fmt_local(t.get("dueAt")),
                        "recurrenceSeconds": t.get("recurrenceSeconds") or 0,
                        "recurLabel": store._recur_label(t),
                        "classify": store._classify(t),
                        "createdAt": t.get("createdAt"),
                        "createdLabel": _fmt_local(t.get("createdAt")),
                        "completedAt": t.get("completedAt"),
                        "fireIndex": t.get("fireIndex") or 0,
                        "source": t.get("source") or "",
                    }
                    for t in items
                ]
            return {"ok": True, "tasks": tasks, "total": sum(len(v) for v in tasks.values())}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def todo_action(self, sender_id: str, todo_id: str, action: str) -> dict:
        config = self._fresh_config()
        try:
            store = self._todo_store(config)
            if action == "done":
                task = store.done(sender_id, todo_id)
                return {"ok": True, "action": "done", "task": task}
            if action == "delete":
                store.delete(sender_id, todo_id)
                return {"ok": True, "action": "delete", "id": todo_id}
            return {"ok": False, "error": f"unknown action: {action}"}
        except KeyError as exc:
            return {"ok": False, "error": f"todo not found: {exc}"}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

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

    # ---------- SOP 浏览（共享 sop / 用户 sop-user） ----------

    _SOP_EXTS = {".md", ".json", ".txt", ".py"}

    def _sop_roots(self, config: Config) -> dict[str, Path]:
        """scope → 绝对根目录。sop 默认 shared_memory_root；sop-user 与之并列。"""
        from ..core.config import PACKAGE_DIR as _PKG
        sop_root = Path(config.sop_dir).resolve()
        # shared_memory_root 可能直接指向 .../memory/sop
        parent = sop_root.parent
        user_root = (parent / "sop-user").resolve()
        # 兼容：若用户目录不在并列位置，再试包内 memory/sop-user
        if not user_root.is_dir():
            alt = (_PKG / "memory" / "sop-user").resolve()
            if alt.is_dir():
                user_root = alt
        return {"sop": sop_root, "sop-user": user_root}

    def _sop_resolve(self, config: Config, scope: str, rel: str = "") -> tuple[Path, Path]:
        roots = self._sop_roots(config)
        key = str(scope or "sop-user").strip().lower()
        if key not in roots:
            raise FileNotFoundError(scope)
        root = roots[key]
        if not root.is_dir():
            raise FileNotFoundError(key)
        rel_norm = str(rel or "").replace("\\", "/").lstrip("/")
        if rel_norm in ("", "."):
            return root, root
        # 禁止越界
        if ".." in Path(rel_norm).parts:
            raise FileNotFoundError(rel)
        target = (root / rel_norm).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise FileNotFoundError(rel) from exc
        return root, target

    def sop_index(self, scope: str = "sop-user") -> dict:
        config = self._fresh_config()
        key = str(scope or "sop-user").strip().lower() or "sop-user"
        if key not in ("sop", "sop-user"):
            key = "sop-user"
        roots = self._sop_roots(config)
        root = roots.get(key)
        files = []
        if root is not None and root.is_dir():
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                if path.suffix.lower() not in self._SOP_EXTS:
                    continue
                # 跳过常见噪音目录
                parts = set(path.relative_to(root).parts)
                if parts & {"__pycache__", ".git", "node_modules", ".venv"}:
                    continue
                rel = path.relative_to(root).as_posix()
                content_head = ""
                try:
                    raw = path.read_text(encoding="utf-8-sig", errors="replace")
                    content_head = raw[:400]
                except Exception:
                    raw = ""
                title = path.name
                if path.suffix.lower() == ".md":
                    for line in (raw or "").splitlines():
                        stripped = line.strip()
                        if stripped.startswith("#"):
                            title = stripped.lstrip("#").strip() or title
                            break
                plain = re.sub(r"\s+", " ", re.sub(r"^#{1,6}\s+", "", content_head, flags=re.M)).strip()
                files.append({
                    "path": rel,
                    "name": path.name,
                    "title": title,
                    "ext": path.suffix.lower().lstrip("."),
                    "folder": path.parent.relative_to(root).as_posix() if path.parent != root else "",
                    "excerpt": plain[:140],
                    "bytes": path.stat().st_size,
                    "updatedAt": _short_date(_timestamp(path)),
                })
        files.sort(key=lambda item: (item.get("folder") or "", item.get("path") or ""))
        available = []
        for name, path in roots.items():
            available.append({
                "id": name,
                "label": "用户 SOP" if name == "sop-user" else "共享 SOP",
                "exists": path.is_dir(),
                "root": str(path),
                "count": sum(
                    1 for p in path.rglob("*")
                    if p.is_file() and p.suffix.lower() in self._SOP_EXTS
                ) if path.is_dir() else 0,
            })
        return {
            "scope": key,
            "root": str(root) if root else "",
            "files": files,
            "count": len(files),
            "scopes": available,
        }

    def sop_detail(self, scope: str, rel_path: str) -> dict:
        config = self._fresh_config()
        key = str(scope or "sop-user").strip().lower() or "sop-user"
        root, target = self._sop_resolve(config, key, rel_path)
        if not target.is_file() or target.suffix.lower() not in self._SOP_EXTS:
            raise FileNotFoundError(rel_path)
        raw = _read_text(target)
        ext = target.suffix.lower().lstrip(".")
        # 前端统一用 markdown 渲染：非 md 包进代码围栏
        if ext == "md":
            content = raw
        else:
            fence = "json" if ext == "json" else ("python" if ext == "py" else "text")
            body = raw if raw.endswith("\n") else raw + "\n"
            content = f"```{fence}\n{body}```"
        title = target.name
        if ext == "md":
            for line in raw.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    title = stripped.lstrip("#").strip() or title
                    break
        rel = target.relative_to(root).as_posix()
        return {
            "scope": key,
            "path": rel,
            "name": target.name,
            "title": title,
            "ext": ext,
            "content": content,
            "raw": raw,
            "source": str(target),
            "bytes": target.stat().st_size,
            "updatedAt": _short_date(_timestamp(target)),
            "sections": sum(1 for line in raw.splitlines() if line.startswith("## ")) if ext == "md" else 0,
        }

    # ---------- 服务注册表（可视化启停 + 日志；agent 可动态注册/移除） ----------

    def _services_store(self, config: Config) -> dict:
        path = config.state_dir / "dashboard-services.json"
        state = _read_json(path, {"services": {}})
        if not isinstance(state, dict):
            state = {"services": {}}
        return {"path": path, "state": state}

    def _ensure_builtin_services(self, config: Config) -> dict:
        """内置服务定义；内置条目以代码为准整体同步（名称/命令/健康更新生效），agent 注册条目不覆盖。"""
        store = self._services_store(config)
        state = store["state"]
        services = state.setdefault("services", {})
        root = Path(config.workspace_root)
        base_py = service_python(root)
        venv_py = venv_python(root / "runtime" / "app" / ".venv")
        # 统一用 base python 直启：venv python 是 redirector（子进程新开控制台窗口），
        # CREATE_NO_WINDOW 加在 redirector 上不生效；依赖经 _env_for_service 的 PYTHONPATH 提供
        if not base_py.is_file():
            base_py = venv_py
        ga_home = root / "runtime" / "G4W-main"
        emb_root = root / "runtime" / "G4W-embedding"
        emb_py = venv_python(emb_root / ".venv")
        builtin = {
            "main": {
                "name": "G4W 主服务",
                "desc": "Conductor 主服务（-m G4W start）",
                "command": [str(base_py), "-u", "-m", "G4W", "start"],
                "cwd": str(ga_home),
                "logs": [str(root / "runtime" / "g4w.service.log")],
                "health": {"kind": "pidfile", "value": str(config.pid_file)},
                "managed": True,
                "builtin": True,
            },
            "monitor": {
                "name": "模型输出监视器",
                "desc": "Model monitor（-m G4W monitor，随主服务自动退出）",
                "command": [str(base_py), "-u", "-m", "G4W", "monitor"],
                "cwd": str(ga_home),
                "logs": [str(root / "runtime" / "g4w.monitor.log")],
                "health": {"kind": "cmdline", "value": "g4w monitor"},
                "managed": True,
                "builtin": True,
            },
            "embedding": {
                "name": "向量 Embedding",
                "desc": "ST 推理服务（127.0.0.1:8081，GPU 强制策略）",
                "command": [str(base_py), "server.py"],
                "cwd": str(emb_root),
                "logs": [str(root / "runtime" / "g4w-embedding.log")],
                "health": {"kind": "port", "value": "127.0.0.1:8081"},
                "managed": True,
                "builtin": True,
            },
            "timeline": {
                "name": "时间线服务",
                "desc": "独立时间线服务（127.0.0.1:18181，站点同端口）",
                # 入口必须是随包发布的模块：以前指向 runtime\G4W-data\timeline\
                # start_timeline_serve.py —— 那是某个安装里手工放的脚本（硬编码本机路径、
                # 写死某个会话的 context、绑 0.0.0.0），而 G4W-data 不进发布包，用户点启动
                # 必然失败。现在改为包内 G4W/features/timeline_serve.py（-m 方式启动）。
                "command": [str(base_py), "-u", "-m", "G4W.features.timeline_serve"],
                "cwd": str(ga_home),
                "logs": [str(root / "runtime" / "G4W-data" / "timeline" / "serve-startup.log")],
                "health": {"kind": "port", "value": "127.0.0.1:18181"},
                "managed": True,
                "builtin": True,
            },
            "dashboard": {
                "name": "看板",
                "desc": "当前控制中心（18180）",
                "command": [],
                "cwd": "",
                "logs": [str(root / "runtime" / "dashboard.stdout.log")],
                "health": {"kind": "port", "value": "127.0.0.1:18180"},
                "managed": False,
                "builtin": True,
            },
        }
        changed = False
        for sid, spec in builtin.items():
            existing = services.get(sid)
            if existing is None or existing.get("builtin"):
                if existing != spec:
                    services[sid] = spec
                    changed = True
        # 清理：注册表中 builtin 但代码已移除的条目（如旧版内置服务下线）
        for sid in [k for k, v in services.items() if v.get("builtin") and k not in builtin]:
            del services[sid]
            changed = True
        if changed:
            store["path"].write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        return state

    @staticmethod
    def _port_open(host: str, port: int, timeout: float = 0.3) -> bool:
        try:
            with socket.create_connection((host, int(port)), timeout=timeout):
                return True
        except OSError:
            return False

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if not pid or pid <= 0:
            return False
        try:
            import psutil

            return psutil.pid_exists(int(pid))
        except Exception:
            try:
                os.kill(int(pid), 0)
                return True
            except OSError:
                return False

    def _service_status(self, config: Config, sid: str, spec: dict) -> dict:
        health = spec.get("health") or {}
        kind = str(health.get("kind") or "")
        status = "stopped"
        detail = ""
        if kind == "pidfile":
            pid_path = Path(str(health.get("value") or ""))
            if pid_path.is_file():
                try:
                    pid = int(pid_path.read_text(encoding="utf-8").strip())
                    if self._pid_alive(pid):
                        status, detail = "running", f"pid={pid}"
                    else:
                        detail = f"stale pid {pid}"
                except Exception:
                    pass
        elif kind == "port":
            value = str(health.get("value") or "")
            host, _, port = value.partition(":")
            if self._port_open(host or "127.0.0.1", int(port or 0)):
                status, detail = "running", f"port {port}"
            else:
                detail = f"port {port} 未监听"
        elif kind == "cmdline":
            needle = str(health.get("value") or "").lower()
            pids = self._cmdline_pids(needle)
            if pids:
                status, detail = "running", f"pid={pids[0]}"
            else:
                detail = f"进程特征 '{needle}' 未匹配"
        return {"id": sid, "name": spec.get("name") or sid, "desc": spec.get("desc") or "",
                "managed": bool(spec.get("managed")), "builtin": bool(spec.get("builtin")),
                "logs": spec.get("logs") or [], "status": status, "detail": detail}

    def services_list(self) -> dict:
        config = self._fresh_config()
        state = self._ensure_builtin_services(config)
        services = state.get("services", {})
        # 端口探测并发执行：关闭端口的 connect 可能要等满超时（防火墙策略），
        # 串行会叠加延迟（实测 3 个关闭端口串行 ~1.8s）
        items = []
        try:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=min(8, max(1, len(services)))) as pool:
                items = list(pool.map(
                    lambda sid_spec: self._service_status(config, sid_spec[0], sid_spec[1]),
                    list(services.items()),
                ))
        except Exception:
            items = [self._service_status(config, sid, spec) for sid, spec in services.items()]
        return {
            "ok": True,
            "services": items,
            "source": str(self._services_store(config)["path"]),
        }

    def _env_for_service(self, config: Config, spec: dict) -> dict:
        env = os.environ.copy()
        root = Path(config.workspace_root)
        env.setdefault("G4W_WORKSPACE_ROOT", str(root))
        env.setdefault("G4W_STATE_DIR", str(config.state_dir))
        env.setdefault("G4W_HOME", str(root / "runtime" / "G4W-main"))
        env.setdefault("GA_APP_DIR", str(root / "runtime" / "app"))
        site_packages = []
        for venv in (root / "runtime" / "app" / ".venv", root / "runtime" / "G4W-embedding" / ".venv"):
            site_packages.extend(str(path) for path in venv_site_packages(venv))
        # base python 直启时依赖来自各 venv 的 site-packages。
        # 注意：不能用 setdefault——bat 已设置 PYTHONPATH 时不会生效（实证 bug），
        # 必须把 venv site-packages 追加进现有值并去重。
        parts: list[str] = []
        for entry in str(env.get("PYTHONPATH") or "").split(os.pathsep):
            entry = entry.strip()
            if entry and entry not in parts:
                parts.append(entry)
        for entry in (str(root / "runtime" / "G4W-main"), str(root / "runtime" / "app")):
            if entry not in parts:
                parts.append(entry)
        for sp in site_packages:
            if sp not in parts:
                parts.append(sp)
        env["PYTHONPATH"] = os.pathsep.join(parts)
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        # 从 G4W-main/.env 补充 EMBEDDING_* / G4W_VECTOR_*
        env_path = root / "runtime" / "G4W-main" / ".env"
        try:
            if env_path.is_file():
                for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
                    s = raw.strip()
                    if not s or s.startswith("#") or "=" not in s:
                        continue
                    k, v = s.split("=", 1)
                    k = k.strip()
                    if k.startswith("EMBEDDING_") or k.startswith("G4W_VECTOR_"):
                        env.setdefault(k, v.strip().strip("'\""))
        except Exception:
            pass
        return env

    def service_start(self, service_id: str) -> dict:
        config = self._fresh_config()
        state = self._ensure_builtin_services(config)
        spec = state.get("services", {}).get(service_id)
        if not spec:
            return {"ok": False, "error": f"unknown service: {service_id}"}
        if not spec.get("command"):
            return {"ok": False, "error": f"service {service_id} 不支持启动"}
        current = self._service_status(config, service_id, spec)
        if current["status"] == "running":
            return {"ok": True, "message": "已在运行", "status": "running", "id": service_id}
        cmd = list(spec.get("command") or [])
        cwd = str(spec.get("cwd") or str(Path(config.workspace_root)))
        log_path = None
        for p in spec.get("logs") or []:
            candidate = Path(p)
            try:
                candidate.parent.mkdir(parents=True, exist_ok=True)
                log_path = candidate.open("ab")
                break
            except Exception:
                continue
        popen_kwargs = {
            "cwd": cwd,
            "env": self._env_for_service(config, spec),
            "stdout": log_path or subprocess.DEVNULL,
            "stderr": subprocess.STDOUT,
            "shell": False,
        }
        popen_kwargs.update(service_launch_kwargs())
        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
        except Exception as exc:
            return {"ok": False, "error": f"启动失败: {type(exc).__name__}: {exc}", "id": service_id}
        return {"ok": True, "started": True, "pid": proc.pid, "id": service_id,
                "log": str(log_path.name) if log_path else None}

    def service_stop(self, service_id: str) -> dict:
        config = self._fresh_config()
        state = self._ensure_builtin_services(config)
        spec = state.get("services", {}).get(service_id)
        if not spec:
            return {"ok": False, "error": f"unknown service: {service_id}"}
        if not spec.get("managed"):
            return {"ok": False, "error": f"service {service_id} 不允许通过看板停止"}
        health = spec.get("health") or {}
        kind = str(health.get("kind") or "")
        stopped = False
        actions = []
        try:
            import psutil
        except Exception:
            psutil = None
        # 1) pidfile
        if kind == "pidfile":
            pid_path = Path(str(health.get("value") or ""))
            if pid_path.is_file():
                try:
                    pid = int(pid_path.read_text(encoding="utf-8").strip())
                    if pid and self._pid_alive(pid):
                        kill_tree(pid)
                        stopped = True
                        actions.append(f"killed pid {pid}（进程树）")
                        try:
                            pid_path.unlink(missing_ok=True)
                        except OSError:
                            pass
                except Exception:
                    pass
        # 2) port 监听者（校验命令行特征，防误杀）
        if kind == "port" and not stopped:
            value = str(health.get("value") or "")
            host, _, port = value.partition(":")
            port = int(port or 0)
            if port and psutil is not None:
                for conn in psutil.net_connections():
                    try:
                        if conn.status == "LISTEN" and conn.laddr.port == port and conn.pid:
                            proc = psutil.Process(conn.pid)
                            blob = " ".join(proc.cmdline() or []).lower()
                            sid = service_id.lower()
                            if sid in blob or sid in str(proc.name() or "").lower():
                                kill_tree(conn.pid)
                                stopped = True
                                actions.append(f"killed pid {conn.pid}（端口 {port} 监听者）")
                    except Exception:
                        continue
        # 3) cmdline 特征（monitor 等服务，无端口/pidfile）
        if kind == "cmdline" and not stopped and psutil is not None:
            needle = str(health.get("value") or "").lower()
            for proc_id in self._cmdline_pids(needle):
                try:
                    kill_tree(proc_id)
                    stopped = True
                    actions.append(f"killed pid {proc_id}（cmdline 特征 {needle}）")
                except Exception:
                    continue
        return {"ok": True, "stopped": stopped, "id": service_id, "actions": actions}

    def _cmdline_pids(self, needle: str) -> list[int]:
        """按 cmdline 特征找 pid；结果缓存 3 秒。

        psutil 对每个进程取 cmdline 在 Windows 上很慢（全进程约 1.2s），
        服务列表每 3 秒轮询一次，不能每次都全扫。只对 python 进程取
        cmdline（服务都是 python 起的），并缓存整表。
        """
        now = time.time()
        cache = self.__dict__.setdefault("_cmdline_cache", {"at": 0.0, "hits": {}})
        if now - cache["at"] > 3.0:
            hits: dict[str, list[int]] = {}
            try:
                import psutil

                for proc in psutil.process_iter(["pid", "name"]):
                    try:
                        name = str(proc.info.get("name") or "").lower()
                        if "python" not in name:
                            continue
                        blob = " ".join(proc.cmdline() or []).lower()
                        for word in blob.split():
                            hits.setdefault(word, []).append(proc.info["pid"])
                            # 同时索引路径最后一段：install_embedding.py 需匹配 ...\vector\install_embedding.py
                            base = word.replace("\\", "/").rsplit("/", 1)[-1]
                            if base and base != word:
                                hits.setdefault(base, []).append(proc.info["pid"])
                    except Exception:
                        continue
            except Exception:
                pass
            cache["at"] = now
            cache["hits"] = hits
        words = [word for word in str(needle or "").lower().split() if word]
        if not words:
            return []
        common = set(cache["hits"].get(words[0], []))
        for word in words[1:]:
            common &= set(cache["hits"].get(word, []))
        return sorted(common)

    def service_logs(self, service_id: str, cursor: int = 0) -> dict:
        config = self._fresh_config()
        state = self._ensure_builtin_services(config)
        spec = state.get("services", {}).get(service_id)
        if not spec:
            return {"ok": False, "error": f"unknown service: {service_id}"}
        logs = spec.get("logs") or []
        if not logs:
            return {"ok": True, "lines": [], "cursor": cursor, "service": service_id}
        path = Path(str(logs[0]))
        try:
            size = path.stat().st_size if path.is_file() else 0
            start = max(0, int(cursor or 0))
            if size <= start:
                return {"ok": True, "lines": [], "cursor": size, "service": service_id}
            with path.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(start)
                chunk = f.read(max(size - start, 0))
            lines = chunk.splitlines()
            # 只保留尾部 500 行,避免超大日志撑爆响应
            if len(lines) > 500:
                lines = lines[-500:]
            return {"ok": True, "lines": lines, "cursor": size, "service": service_id}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "service": service_id}

    def worker_monitor(self, cursor: int = 0, file_id: str = "", worker_dir: str = "", worker_id: str = "") -> dict:
        """Worker 页监视器数据源：tail worker 模型输出流 + 历史信息回退。

        live：tail 最新 model_responses_*.txt（worker_runner 实时模型输出）+
        progress.json 进度摘要（字符串 key 去重，避免大整数经 JSON/JS 丢精度）。
        没有实时输出时回退历史：优先该 worker 最近 run 的 report.md，
        archived 则读本地 zip 内 report.md，zip 不存在/读不了则用注册表
        里的 task/result/summary。history 模式返回 fileId="hist:<key>"，
        前端带去重（再次轮询不再重复返回）。
        """
        config = self._fresh_config()
        root = Path(config.state_dir) / "memory" / "conversations"
        filter_dir: Path | None = None
        label = ""
        if str(worker_dir or "").strip():
            try:
                candidate = Path(str(worker_dir).strip()).resolve()
                if candidate == root or root in candidate.parents:
                    filter_dir = candidate
                    label = filter_dir.name
            except Exception:
                filter_dir = None
        archive_path = ""
        if filter_dir is None and str(worker_id or "").strip():
            try:
                registry = _read_json(config.worker_registry_file, {"workers": {}})
                item = (registry.get("workers") or {}).get(str(worker_id).strip())
                if isinstance(item, dict):
                    d = str(item.get("dir") or "")
                    if d and Path(d).is_dir():
                        filter_dir = Path(d).resolve()
                        label = Path(d).name
                    else:
                        archive_path = str(item.get("archivePath") or "")
                        if not label:
                            label = str(item.get("topic") or worker_id)
            except Exception:
                pass

        def iter_workers_root():
            try:
                for sender in root.iterdir():
                    workers_dir = sender / "workers"
                    if workers_dir.is_dir():
                        yield workers_dir
            except Exception:
                return

        # --- 实时输出（仅扫 workers 树，避免全量 conversations rglob 卡 2s+；
        #    archived worker（有 archivePath 无活目录）跳过 live 直接走历史） ---
        responses: list[Path] = []
        if not (archive_path and filter_dir is None):
            try:
                if filter_dir is not None:
                    if filter_dir.is_dir():
                        responses = list(filter_dir.rglob("model_responses_*.txt"))
                else:
                    for workers_root in iter_workers_root():
                        responses.extend(workers_root.rglob("model_responses_*.txt"))
            except Exception:
                pass
        responses.sort(key=lambda p: p.stat().st_mtime_ns, reverse=True)
        active = responses[0] if responses else None
        if active is not None and not label:
            try:
                # .../workers/<date>/<topic>-<id>/runtime/model_responses/<file>
                label = active.parents[2].name
            except Exception:
                label = active.parent.name
        progress = None
        try:
            best_path: Path | None = None
            best_mtime = 0
            if filter_dir is not None:
                candidates = list(filter_dir.rglob("progress.json")) if filter_dir.is_dir() else []
            elif archive_path:
                candidates = []
            else:
                candidates = []
                for workers_root in iter_workers_root():
                    candidates.extend(workers_root.rglob("progress.json"))
            for path in candidates:
                try:
                    mtime_ns = path.stat().st_mtime_ns
                except Exception:
                    continue
                if mtime_ns > best_mtime:
                    best_path, best_mtime = path, mtime_ns
            if best_path is not None:
                try:
                    data = json.loads(best_path.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
                if isinstance(data, dict):
                    try:
                        plabel = best_path.parents[4].name
                    except Exception:
                        plabel = ""
                    progress = {
                        "turn": int(data.get("turn") or 0),
                        "summary": str(data.get("summary") or ""),
                        "label": plabel,
                        "key": f"{best_path}|{best_mtime}",
                    }
        except Exception:
            pass
        lines: list[str] = []
        new_cursor = int(cursor or 0)
        if active is not None:
            try:
                size = active.stat().st_size
                if str(active) != str(file_id or ""):
                    # 新文件：只显示尾部（防超大 run 撑爆），游标跳到文件末尾
                    start = max(0, size - 20000)
                    with active.open("r", encoding="utf-8", errors="replace") as f:
                        f.seek(start)
                        lines = f.read().splitlines()
                    if len(lines) > 300:
                        lines = lines[-300:]
                    new_cursor = size
                elif size > new_cursor:
                    with active.open("r", encoding="utf-8", errors="replace") as f:
                        f.seek(new_cursor)
                        lines = f.read().splitlines()
                    new_cursor = size
            except Exception as exc:
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "active": bool(active)}
            return {"ok": True, "active": True, "history": False,
                    "fileId": str(active), "label": label, "cursor": new_cursor,
                    "lines": lines, "progress": progress,
                    "filtered": filter_dir is not None, "filterLabel": label}

        # --- 历史回退：worker 当前没有实时输出 ---
        history_lines: list[str] = []
        history_key = f"hist:{filter_dir or archive_path or worker_id}"
        try:
            report_sources: list[tuple[str, Path]] = []
            if filter_dir is not None and filter_dir.is_dir():
                for path in filter_dir.rglob("report.md"):
                    report_sources.append((str(path), path))
            elif archive_path:
                local_zip = Path(archive_path)
                if local_zip.is_file():
                    import zipfile
                    with zipfile.ZipFile(local_zip) as zf:
                        reports = sorted(
                            (name for name in zf.namelist() if name.endswith("report.md")),
                            key=lambda name: [int(part[4:]) if part.startswith("run-") else 0 for part in name.replace("\\", "/").split("/")],
                        )
                        if reports:
                            with zf.open(reports[-1]) as f:
                                report_sources.append((reports[-1], None))
                                history_lines = f.read().decode("utf-8", errors="replace").splitlines()
            if report_sources and not history_lines and filter_dir is not None:
                report_sources.sort(key=lambda pair: pair[0], reverse=True)
                path = report_sources[0][1]
                if path is not None:
                    try:
                        history_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                    except Exception:
                        history_lines = []
            if history_lines:
                history_lines = [f"[历史] 该 worker 当前未运行，显示最近一次运行报告：", ""] + history_lines
                if len(history_lines) > 250:
                    history_lines = history_lines[:250] + ["…（已截断）"]
        except Exception:
            history_lines = []
        if not history_lines:
            # 注册表兜底：task / result / summary
            try:
                registry = _read_json(config.worker_registry_file, {"workers": {}})
                item = (registry.get("workers") or {}).get(str(worker_id or "").strip()) or {}
                if not isinstance(item, dict):
                    item = {}
                topic = str(item.get("topic") or label or worker_id or "worker")
                task = str(item.get("task") or "")
                summary = str(item.get("summary") or "")
                result = item.get("result") if isinstance(item.get("result"), dict) else {}
                history_lines.append(f"[历史] {topic} 暂无本地输出（可能已归档到其他设备）")
                if task:
                    history_lines += ["", "任务：", task]
                if summary:
                    history_lines += ["", "摘要：", summary]
                if result:
                    history_lines += ["", "结果：" + json.dumps(result, ensure_ascii=False)[:600]]
            except Exception:
                history_lines = [f"[历史] {label or worker_id} 暂无输出"]
        if str(file_id) == history_key:
            history_lines = []
        return {"ok": True, "active": False, "history": True, "fileId": history_key,
                "label": label, "cursor": 0, "lines": history_lines, "progress": None,
                "filtered": filter_dir is not None, "filterLabel": label}

    def service_register(self, service_id: str, name: str, command: list, cwd: str = "",
                         logs: list = None, health: dict = None, desc: str = "") -> dict:
        """agent 动态注册服务（看板可显示/启停）。"""
        config = self._fresh_config()
        store = self._services_store(config)
        state = store["state"]
        services = state.setdefault("services", {})
        if not str(service_id or "").strip():
            return {"ok": False, "error": "service_id 必填"}
        services[service_id] = {
            "name": str(name or service_id),
            "desc": str(desc or ""),
            "command": list(command or []),
            "cwd": str(cwd or ""),
            "logs": list(logs or []),
            "health": dict(health or {}),
            "managed": True,
            "builtin": False,
        }
        store["path"].write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "registered": service_id}

    def service_remove(self, service_id: str) -> dict:
        config = self._fresh_config()
        store = self._services_store(config)
        state = store["state"]
        services = state.get("services", {})
        spec = services.get(service_id)
        if not spec:
            return {"ok": False, "error": f"unknown service: {service_id}"}
        if spec.get("builtin"):
            return {"ok": False, "error": f"内置服务 {service_id} 不允许移除"}
        services.pop(service_id, None)
        store["path"].write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "removed": service_id}

    # ---------- Embedding 配置 / 测速 / 安装 ----------
    def _embedding_paths(self, config: Config) -> tuple:
        g4w_root = Path(config.state_dir).parent.parent  # state_dir = runtime\G4W-data
        return (
            g4w_root,
            g4w_root / "runtime" / "G4W-embedding",
            g4w_root / "runtime" / "G4W-main" / "G4W" / "memory" / "vector" / "install_embedding.py",
        )

    def embedding_config(self) -> dict:
        config = self._fresh_config()
        em = _embed_mirrors_module()
        _, emb_root, _install_py = self._embedding_paths(config)
        return {
            "ok": True,
            "config": em.load_config(config.state_dir),
            "status": em.installed_status(emb_root),
        }

    def embedding_save_config(self, network: str) -> dict:
        config = self._fresh_config()
        em = _embed_mirrors_module()
        if network not in ("domestic", "abroad"):
            return {"ok": False, "error": "network 必须是 domestic（国内）或 abroad（国外）"}
        cfg = em.load_config(config.state_dir)
        cfg["network"] = network
        em.save_config(config.state_dir, cfg)
        return {"ok": True, "config": cfg}

    def embedding_speedtest(self) -> dict:
        config = self._fresh_config()
        em = _embed_mirrors_module()
        return em.speedtest(config.state_dir, force=True)

    def embedding_install(self) -> dict:
        """启动 Embedding 安装任务：DETACHED 进程 + 日志落盘 + 注册为服务（服务页可看进度/停止）。"""
        config = self._fresh_config()
        em = _embed_mirrors_module()
        g4w_root, emb_root, install_py = self._embedding_paths(config)
        log_path = g4w_root / "runtime" / "embedding-install.log"
        python = service_python(g4w_root)
        if not install_py.is_file() or not python.is_file():
            return {"ok": False, "error": "环境不完整：缺少 runtime\\python 或 install_embedding.py"}
        if self._cmdline_pids("install_embedding.py"):
            return {"ok": False, "error": "安装任务已在运行（服务与终端页可查看进度）"}
        env = self._env_for_service(config, {"command": [], "cwd": "", "logs": [], "health": {}})
        env.update(em.install_env(config.state_dir))
        detach = detached_kwargs()
        try:
            out = open(log_path, "a", encoding="utf-8", errors="replace")
        except OSError:
            out = subprocess.DEVNULL
        proc = subprocess.Popen(
            [str(python), "-u", str(install_py), "--yes", "--root", str(emb_root)],
            cwd=str(g4w_root / "runtime" / "G4W-main"),
            env=env, **detach, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
        )
        spec = {
            "name": "Embedding 安装任务",
            "desc": "向量检索环境安装（torch + sentence-transformers + 模型下载，约 4-8GB）。完成后自动变为已停止。",
            "command": [str(python), "-u", str(install_py), "--yes", "--root", str(emb_root)],
            "cwd": str(g4w_root / "runtime" / "G4W-main"),
            "logs": [str(log_path)],
            "health": {"kind": "cmdline", "value": "install_embedding.py"},
            "managed": True,
            "builtin": False,
        }
        try:
            store = self._services_store(config)
            state = store["state"]
            state.setdefault("services", {})["embedding-install"] = spec
            store["path"].write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        return {"ok": True, "pid": proc.pid, "log": str(log_path)}

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
        elif stored_path.suffix.lower() in (".pdf", ".docx"):
            try:
                extracted, _ = extract_text(stored_path)
                quality = validate_extracted_text(extracted)
                if not quality.get("ok"):
                    raise ValueError(str(quality.get("reason") or "文档没有可读文本"))
                text_path = root / "documents" / f"{document_id}.extracted.txt"
                text_path.write_text(extracted, encoding="utf-8")
                document["text_path"] = str(text_path)
                document["updated_at"] = int(time.time())
                _write_json(root / "manifest.json", manifest)
                content = extracted
                content_source = "extracted"
            except Exception as error:
                extraction_warning = f"文本提取失败：{error}"
        elif stored_path.suffix.lower() == ".doc":
            extraction_warning = "旧版 .doc（二进制）不支持提取，建议另存为 .docx 或 PDF 后重新添加"
            content = ""
            content_source = "unsupported"
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
            "workerTurnEnabled": worker_turn.get(binding_key) if binding_key else False,
            "inputCaptureEnabled": input_capture.get(binding_key) if binding_key else False,
            "chunkChars": max(1, min(int(chunk.get("minChunkChars") or 10), 3800)),
            "checkinEnabled": bool(checkin.get("enabled", config.checkin_enabled)),
            "checkinMin": int(checkin.get("minimumMinutes") or config.checkin_minimum_minutes),
            "checkinMax": int(checkin.get("maximumMinutes") or config.checkin_maximum_minutes),
            "checkinWindow": f"{int(checkin.get('minimumMinutes') or config.checkin_minimum_minutes)}–{int(checkin.get('maximumMinutes') or config.checkin_maximum_minutes)} 分钟",
            "locationEnabled": config.location_enabled,
            "webSearchEnabled": config.web_search_enabled,
            "vectorEnabled": bool(load_vector_config().get("enabled")),
            "theme": self.read_custom_theme(),
        }

    def read_custom_theme(self) -> dict:
        """Custom dashboard theme: {"dark": {...}, "light": {...}, "shared": {...}}."""
        return _read_json(self.config.state_dir / "dashboard-theme.json", {"dark": {}, "light": {}, "shared": {}})

    def update_custom_theme(self, theme: dict) -> dict:
        """Validate + persist custom theme (whitelist vars, reject CSS injection)."""
        if not isinstance(theme, dict):
            raise ValueError("theme 必须是对象")
        cleaned: dict = {}
        for mode in ("dark", "light", "shared"):
            raw = theme.get(mode)
            if raw is None:
                continue
            if not isinstance(raw, dict):
                raise ValueError(f"theme.{mode} 必须是对象")
            cleaned[mode] = {}
            for key, value in raw.items():
                if key not in _THEME_VARS:
                    continue
                value = str(value).strip()
                if not value:
                    continue
                if any(ch in value for ch in ("\r", "\n", ";", "{", "}", "<", ">")):
                    raise ValueError(f"theme 值不合法: {key}")
                cleaned[mode][key] = value
        target = self.config.state_dir / "dashboard-theme.json"
        target.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
        return cleaned

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

        if "theme" in updates:
            self.update_custom_theme(updates["theme"])
            # 主题即时生效,无需重启

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
    # HTTP/1.1 + Content-Length → 连接复用（keep-alive）。原 1.0 每次响应都关连接，
    # 前端 1s/2s/3s 级轮询导致连接风暴（数百 TIME_WAIT），代理连接池被占满后页面冻结。
    protocol_version = "HTTP/1.1"

    def handle(self) -> None:
        """吞掉客户端断连类异常,避免框架打印噪音 traceback。

        浏览器刷新/关闭页面时,可能在读取请求行(rfile.readline)或写出
        响应时中止连接(ConnectionAbortedError/BrokenPipeError/
        ConnectionResetError)——发生在 http.server 框架层,do_GET 捕获不到。
        """
        try:
            super().handle()
        except (ConnectionAbortedError, BrokenPipeError, ConnectionResetError):
            pass

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
            if setup_wizard.handle_get(self, path):
                return
            skip_setup = "skip_setup" in query and self.client_address[0] in ("127.0.0.1", "::1")
            if (not skip_setup) and setup_wizard.gate_active() and not path.startswith("/api/auth/"):
                if path.startswith("/api/"):
                    self._send_json({"ok": False, "error": "setup_required", "setup": setup_wizard.status_payload()}, 428)
                else:
                    self._send_bytes(setup_wizard.render_page(), "text/html; charset=utf-8")
                return
            if path.startswith("/api/") and not self._require_auth():
                return
            if path == "/api/dashboard":
                self._send_json(self.dashboard.snapshot())
                return
            if path == "/api/update/status":
                self._send_json(self.dashboard.update_status())
                return
            if path == "/api/update/check":
                flag = str((query.get("mirror") or ["1"])[0]).strip().lower()
                self._send_json(self.dashboard.update_check(mirror=flag not in ("0", "false", "off", "no")))
                return
            if path == "/api/update/apply":
                flag = str((query.get("mirror") or ["1"])[0]).strip().lower()
                self._send_json(self.dashboard.update_apply(mirror=flag not in ("0", "false", "off", "no")))
                return
            if path == "/api/update/progress":
                self._send_json(self.dashboard.update_progress())
                return
            if path == "/api/memory":
                self._send_json(self.dashboard.memory_detail(str((query.get("id") or [""])[0])))
                return
            if path == "/api/memory/timeline":
                self._send_json(self.dashboard.memory_timeline(str((query.get("id") or [""])[0])))
                return
            if path == "/api/memory/backup":
                self._send_json(self.dashboard.memory_backup(
                    str((query.get("id") or [""])[0]),
                    str((query.get("name") or [""])[0]),
                ))
                return
            if path == "/api/todo":
                action = str((query.get("action") or [""])[0])
                if action in ("done", "delete"):
                    self._send_json(self.dashboard.todo_action(
                        str((query.get("sender") or [""])[0]),
                        str((query.get("id") or [""])[0]),
                        action,
                    ))
                else:
                    self._send_json(self.dashboard.todo_list())
                return
            if path in ("/api/embedding/config", "/api/embedding/status"):
                self._send_json(self.dashboard.embedding_config())
                return
            if path == "/api/services":
                action = str((query.get("action") or [""])[0])
                sid = str((query.get("id") or [""])[0])
                if action == "start" and sid:
                    self._send_json(self.dashboard.service_start(sid))
                elif action == "stop" and sid:
                    self._send_json(self.dashboard.service_stop(sid))
                elif action == "register":
                    self._send_json(self.dashboard.service_register(
                        sid,
                        str((query.get("name") or [""])[0]),
                        json.loads(str((query.get("command") or "[]")[0]) or "[]"),
                        cwd=str((query.get("cwd") or [""])[0]),
                        logs=json.loads(str((query.get("logs") or "[]")[0]) or "[]"),
                        health=json.loads(str((query.get("health") or "{}")[0]) or "{}"),
                        desc=str((query.get("desc") or [""])[0]),
                    ))
                elif action == "remove" and sid:
                    self._send_json(self.dashboard.service_remove(sid))
                else:
                    self._send_json(self.dashboard.services_list())
                return
            if path == "/api/services/logs":
                sid = str((query.get("id") or [""])[0])
                try:
                    cursor = int(str((query.get("cursor") or ["0"])[0]) or "0")
                except (TypeError, ValueError):
                    cursor = 0
                self._send_json(self.dashboard.service_logs(sid, cursor))
                return
            if path == "/api/timeline":
                self._send_json(self.dashboard.timeline_data())
                return
            if path == "/api/diary":
                date = str((query.get("date") or [""])[0])
                self._send_json(self.dashboard.diary_detail(date) if date else self.dashboard.diary_index())
                return
            if path == "/api/sop":
                scope = str((query.get("scope") or ["sop-user"])[0]) or "sop-user"
                rel = str((query.get("path") or [""])[0])
                if rel:
                    self._send_json(self.dashboard.sop_detail(scope, rel))
                else:
                    self._send_json(self.dashboard.sop_index(scope))
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
            if path == "/api/workers/monitor":
                try:
                    cursor = int(str((query.get("cursor") or ["0"])[0]) or "0")
                except (TypeError, ValueError):
                    cursor = 0
                self._send_json(self.dashboard.worker_monitor(
                    cursor,
                    str((query.get("file") or [""])[0]),
                    str((query.get("dir") or [""])[0]),
                    str((query.get("id") or [""])[0]),
                ))
                return
            if path == "/api/prompt":
                self._send_json(self.dashboard.prompt_detail())
                return
            if path == "/api/persona":
                self._send_json(self.dashboard.persona())
                return
            if path == "/api/persona/export":
                self._send_json(self.dashboard.persona_export(str((query.get("id") or [""])[0])))
                return
            if path == "/api/persona/inject-status":
                self._send_json(self.dashboard.persona_inject_status(str((query.get("id") or [""])[0])))
                return
            if path == "/api/model-config":
                self._send_json(self.dashboard.model_config())
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
        except (ConnectionAbortedError, BrokenPipeError, ConnectionResetError):
            # 客户端主动断开(刷新/关闭页面):静默结束,不尝试回写响应
            return
        except Exception as error:
            self._send_json({"ok": False, "error": str(error)}, 500)
            return
        self._serve_static(path)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if setup_wizard.handle_post(self, path):
                return
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
            if path == "/api/embedding/config":
                if not self._require_auth():
                    return
                payload = self._read_json_body()
                self._send_json(self.dashboard.embedding_save_config(str(payload.get("network") or "")))
                return
            if path == "/api/embedding/speedtest":
                if not self._require_auth():
                    return
                self._send_json(self.dashboard.embedding_speedtest())
                return
            if path == "/api/embedding/install":
                if not self._require_auth():
                    return
                self._send_json(self.dashboard.embedding_install())
                return
            if path == "/api/debug-log":
                # 前端 JS 错误上报（诊断用，写 G4W-data/debug-js.log）
                payload = self._read_json_body()
                try:
                    dbg = Path(self.dashboard.config.state_dir) / "debug-js.log"
                    dbg.parent.mkdir(parents=True, exist_ok=True)
                    with dbg.open("a", encoding="utf-8", errors="replace") as f:
                        f.write(f"{time.strftime('%H:%M:%S')} {json.dumps(payload, ensure_ascii=False)[:600]}\n")
                except Exception:
                    pass
                self._send_json({"ok": True})
                return
            if path.startswith("/api/model-config/"):
                if not self._require_auth():
                    return
                payload = self._read_json_body()
                action = path[len("/api/model-config/"):]
                handlers = {
                    "save": lambda: self.dashboard.model_config_save(payload),
                    "probe": lambda: self.dashboard.model_config_probe(payload),
                    "template": lambda: self.dashboard.model_config_template(payload),
                    "provider/add": lambda: self.dashboard.model_config_provider_add(payload),
                    "provider/delete": lambda: self.dashboard.model_config_provider_delete(payload),
                    "models/fetch": lambda: self.dashboard.model_config_models_fetch(payload),
                    "model/add": lambda: self.dashboard.model_config_model_add(payload),
                    "model/delete": lambda: self.dashboard.model_config_model_delete(payload),
                    "model/test": lambda: self.dashboard.model_config_model_test(payload),
                }
                handler = handlers.get(action)
                if handler is None:
                    self._send_json({"ok": False, "error": "Not found"}, 404)
                    return
                self._send_json(handler())
                return
            if path.startswith("/api/persona/"):
                if not self._require_auth():
                    return
                payload = self._read_json_body()
                action = path[len("/api/persona/"):]
                handlers = {
                    "save": lambda: self.dashboard.persona_save(payload),
                    "create": lambda: self.dashboard.persona_create(payload),
                    "rename": lambda: self.dashboard.persona_rename(payload),
                    "delete": lambda: self.dashboard.persona_delete(payload),
                    "restore": lambda: self.dashboard.persona_restore(payload),
                    "import": lambda: self.dashboard.persona_import(payload),
                    "import-runtime": lambda: self.dashboard.persona_import_runtime(payload),
                    "activate": lambda: self.dashboard.persona_activate(str(payload.get("id") or "")),
                }
                handler = handlers.get(action)
                if handler is None:
                    self._send_json({"ok": False, "error": "Not found"}, 404)
                    return
                self._send_json(handler())
                return
            if path not in {"/api/settings", "/api/model", "/api/timeline/theme"}:
                self._send_json({"ok": False, "error": "Not found"}, 404)
                return
            if not self._require_auth():
                return
            payload = self._read_json_body()
            if path == "/api/timeline/theme":
                self._send_json(self.dashboard.timeline_theme_set(payload.get("theme")))
            elif path == "/api/model":
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
        # 流式响应无 Content-Length：HTTP/1.1 下必须显式关闭连接，
        # 否则 keep-alive 挂起让客户端一直等响应结束
        self.close_connection = True
        previous = ""
        try:
            for _ in range(300):
                snapshot = self.dashboard.snapshot()
                serialized = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
                if serialized != previous:
                    self.wfile.write(f"event: dashboard\ndata: {serialized}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    previous = serialized
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
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


def _warmup(server) -> None:
    """启动预热：先跑一遍重 IO 的接口，把 Windows 文件缓存热起来。

    实测冷缓存下首个 snapshot / worker_monitor 可能慢几百 ms，用户
    第一次点 Worker 页会明显卡顿；预热后稳定在 ~75ms / ~26ms。
    """
    try:
        time.sleep(0.3)
        state = server.dashboard  # type: ignore[attr-defined]
        state.snapshot()
        state.services_list()
        state.worker_monitor(0, "")
    except Exception:
        pass


def run_dashboard(config: Config | None = None, host: str = "127.0.0.1", port: int = 18180) -> int:
    config = config or Config.load()
    # 禁止端口复用：Windows 上 SO_REUSEADDR 允许重复绑定同一端口，
    # 会同时跑两个看板实例、请求被随机分发（实证：用户页面时好时坏）。
    # 关闭后重复启动会直接报 "address already in use"，行为更清晰。
    ThreadingHTTPServer.allow_reuse_address = False
    server = ThreadingHTTPServer((host, int(port)), DashboardHandler)
    server.dashboard = DashboardState(config)  # type: ignore[attr-defined]
    server.auth = DashboardAuth(config.state_dir)  # type: ignore[attr-defined]
    threading.Thread(target=_warmup, args=(server,), daemon=True).start()
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
