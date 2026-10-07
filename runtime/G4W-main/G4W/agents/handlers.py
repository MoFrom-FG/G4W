import json
import hashlib
import os
import re
import threading
import time
from contextlib import nullcontext
from pathlib import Path

from agent_loop import StepOutcome
from ga import GenericAgentHandler

from .reply_gates import ensure_ledger, gate_final_reply, sanitize_outbound_reply


_HISTORY_ARCHIVE_REPLY = re.compile(
    r"^\s*长回复已归档[：:]\s*(?P<path>[^\r\n]+)"
    r"(?:\r?\n摘要[：:]\s*(?P<summary>[\s\S]*?))?\s*$",
    flags=re.I,
)

# On-demand embed server start: cooldown so a dead server is not retried on
# every search (each attempt can take ~30s of model loading).
_EMBED_START_COOLDOWN_S = 600.0
_last_embed_start_ts: float = 0.0
_embed_start_lock = threading.Lock()

# Historical sender segments of the same human user (old WeChat OpenIDs bound
# to the same GA account before re-login). Ownership is judged by userid +
# these aliases, never by exact path matching alone — the user's history spans
# multiple sender ids and must stay retrievable after account migration.
#
# 注意：源码/发布物里**不写死任何账号**（公开仓库不能出现个人微信 ID）。
# 需要兼容旧账号时，在本机 runtime\G4W-main\.env 里填（逗号分隔）：
#   G4W_LEGACY_SENDER_SEGMENTS=<旧 sender 段>[,<更多>]
def _legacy_sender_segments() -> tuple[str, ...]:
    raw = str(os.environ.get("G4W_LEGACY_SENDER_SEGMENTS", "") or "")
    if not raw:
        env_file = Path(__file__).resolve().parents[2] / ".env"
        try:
            for line in env_file.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
                if line.strip().startswith("G4W_LEGACY_SENDER_SEGMENTS="):
                    raw = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        except OSError:
            raw = ""
    return tuple(part.strip().lower() for part in raw.split(",") if part.strip())


def _sender_segment(sender_id: str) -> str:
    return str(sender_id or "").replace("@", "_").lower()


def _belongs_to_user(path_or_id: str, sender_id: str) -> bool:
    """Ownership by userid + legacy aliases (path-independent of account era)."""
    pl = str(path_or_id or "").replace("\\", "/").lower()
    seg = _sender_segment(sender_id)
    if seg and seg in pl:
        return True
    return any(alias in pl for alias in _legacy_sender_segments())


def _maybe_start_embed() -> bool:
    """Start the embed server at most once per cooldown window.

    Returns True when the server is healthy (inference-verified) afterwards.
    Only meaningful when /vector is on — callers gate on that already.
    """
    global _last_embed_start_ts
    now = time.time()
    with _embed_start_lock:
        if now - _last_embed_start_ts < _EMBED_START_COOLDOWN_S:
            return False
        _last_embed_start_ts = now
    try:
        from G4W.memory.vector.embed_lifecycle import ensure_embed_running

        out = ensure_embed_running(timeout_s=30.0)
        return bool(out.get("ok"))
    except Exception:
        return False


def _restore_history_archive_reply(text: str) -> str:
    """Keep clean-history archive references out of user-visible delivery."""
    value = str(text or "").strip()
    match = _HISTORY_ARCHIVE_REPLY.fullmatch(value)
    if not match:
        return value
    raw_path = str(match.group("path") or "").strip().strip("`\"'")
    summary = str(match.group("summary") or "").strip()
    try:
        path = Path(raw_path).resolve()
        parts = {part.lower() for part in path.parts}
        if path.suffix.lower() == ".md" and "assistant-replies" in parts and path.is_file():
            archived = path.read_text(encoding="utf-8-sig", errors="replace")
            marker = "\n## Reply\n"
            if marker in archived:
                restored = archived.split(marker, 1)[1].strip()
                if restored:
                    return restored
    except Exception:
        pass
    # A model can imitate the archive envelope and invent a nonexistent path.
    # Never expose that internal-looking path to WeChat; retain only its preview.
    return summary


def clean_visible_reply(text: str) -> str:
    value = str(text or "").replace("\r\n", "\n")
    value = re.sub(r"<thinking>[\s\S]*?</thinking>", "", value, flags=re.I)
    value = re.sub(r"<summary>[\s\S]*?</summary>", "", value, flags=re.I)
    value = re.sub(r"<silent\s*/?>", "", value, flags=re.I)
    value = re.sub(r"(?im)^\s*(?:\*\*)?(?:LLM Running \(Turn \d+\)|Turn \d+) \.\.\.(?:\*\*)?\s*$", "", value)
    value = re.sub(r"(?im)^\s*\[ROUND END\]\s*$", "", value)
    value = re.sub(r"(?im)^\s*\[G4W\][^\n]*$", "", value)
    # GA appends tool protocol and JSON arguments to the same turn output.
    # Only the natural-language prefix before that protocol is user-visible.
    value = re.split(r"(?im)^\s*🛠️(?:\s+Tool:)?", value, maxsplit=1)[0]
    value = re.sub(r"\n{3,}", "\n\n", value)
    value = value.strip()
    value = _restore_history_archive_reply(value)
    if value.lower() in ("silent", "none", "null"):
        return ""
    return value


class ConductorHandler(GenericAgentHandler):
    def __init__(self, parent, last_history=None, cwd="./temp"):
        super().__init__(parent, last_history, getattr(parent, "G4W_runtime_dir", cwd))
        self.controller = parent.G4W_controller
        self.sender_id = parent.G4W_sender_id
        self._file_read_round = ""
        self._file_read_results = {}
        self._sop_state_round = ""
        self._sop_before = {}
        self._sop_management_ready = False
        self._fallback_round_id = f"round-{time.time_ns()}"

    def turn_end_callback(self, response, tool_calls, tool_results, turn, next_prompt, exit_reason):
        """Keep GA's `_intervene` consume and G4W bookkeeping atomic.

        Conductor intentionally leaves ``agent.task_dir`` unset so upstream GA
        keeps its normal ``LLM Running (Turn N)`` log form.  The G4W-owned
        control file is promoted to GA's in-memory ``intervene`` slot while the
        shared lock is held, then the unmodified upstream callback handles it.
        """
        lock = getattr(self.parent, "G4W_intervention_lock", None)
        with lock if lock is not None else nullcontext():
            path_value = getattr(self.parent, "G4W_intervene_path", "")
            path = Path(path_value) if path_value else None
            if path and path.exists():
                injected = path.read_text(encoding="utf-8", errors="replace")
                path.unlink(missing_ok=True)
                if injected.strip():
                    current = str(getattr(self.parent, "intervene", "") or "").strip()
                    self.parent.intervene = "\n\n".join(part for part in (current, injected.strip()) if part)
            return super().turn_end_callback(
                response, tool_calls, tool_results, turn, next_prompt, exit_reason
            )

    def _current_round_id(self) -> str:
        context = getattr(getattr(self, "parent", None), "G4W_input_context", {}) or {}
        return str(context.get("roundId") or getattr(self, "_fallback_round_id", "round-unbound"))

    def _sop_snapshot(self) -> dict[str, tuple[int, int]]:
        catalog = getattr(self.controller, "sop_catalog", None)
        if catalog is None:
            return {}
        snapshot = {}
        for item in catalog.entries(include_references=True):
            path = Path(item["path"])
            try:
                stat = path.stat()
                snapshot[item["relativePath"]] = (int(stat.st_mtime_ns), int(stat.st_size))
            except OSError:
                continue
        return snapshot

    def _ensure_sop_round_state(self) -> None:
        round_id = self._current_round_id()
        if round_id == getattr(self, "_sop_state_round", ""):
            return
        self._sop_state_round = round_id
        self._sop_before = self._sop_snapshot()
        self._sop_management_ready = False

    def _sop_target(self, value: str) -> tuple[Path | None, str]:
        catalog = getattr(self.controller, "sop_catalog", None)
        if catalog is None or not str(value or "").strip():
            return None, ""
        path = Path(self._get_abs_path(str(value))).resolve()
        try:
            relative = path.relative_to(catalog.root).as_posix()
        except ValueError:
            return None, ""
        return path, relative

    @staticmethod
    def _is_sop_management_target(path: Path | None) -> bool:
        if path is None:
            return False
        name = path.name.lower()
        return name in ("global_mem_insight.txt", "global_mem.txt", "memory_management_sop.md") or bool(
            name == "sop.md" or re.search(r"(?:^|[_-])sop\.md$", name)
        )

    def _require_sop_management(self, args: dict) -> StepOutcome | None:
        self._ensure_sop_round_state()
        path, relative = self._sop_target(args.get("path", ""))
        if not self._is_sop_management_target(path) or getattr(self, "_sop_management_ready", False):
            return None
        return StepOutcome(
            {"status": "blocked", "reason": "G4W L0 SOP management flow is required", "path": relative},
            next_prompt=(
                "你正在创建或修改G4W共享SOP/L1/L2，但尚未进入L0管理流程。"
                "先调用start_long_term_update；随后按返回的memory_management_sop读取L1/L2、查重并分类落盘。"
                "测试或演示SOP必须放到demo/<主题>/，且不要写入L1/L2。"
            ),
        )

    def _sop_completion_issue(self) -> str:
        self._ensure_sop_round_state()
        catalog = getattr(self.controller, "sop_catalog", None)
        if catalog is None:
            return ""
        current = self._sop_snapshot()
        before = getattr(self, "_sop_before", {})
        changed = sorted(path for path, signature in current.items() if before.get(path) != signature)
        if not changed:
            return ""
        if not getattr(self, "_sop_management_ready", False):
            return (
                "本Round已经通过code_run或其他方式修改共享SOP，但没有调用start_long_term_update。"
                "立即进入L0管理流程，复核改动、查重、分类并完成索引闭环后再回复用户。"
            )
        new_paths = [path for path in changed if path not in before]
        misplaced_demo = []
        for relative in new_paths:
            path = catalog.root / relative
            try:
                content = path.read_text(encoding="utf-8-sig", errors="replace")[:2000].lower()
            except OSError:
                content = ""
            demo_like = any(token in relative.lower() for token in ("test", "demo", "示例", "测试", "演示")) or any(
                token in content for token in ("测试sop", "演示sop", "测试演示", "demo sop")
            )
            if demo_like and "demo" not in {part.lower() for part in Path(relative).parts}:
                misplaced_demo.append(relative)
        if misplaced_demo:
            return (
                "检测到测试/演示SOP未放入demo/<主题>/：" + ", ".join(misplaced_demo) +
                "。请移动到demo子目录；demo内容不得写入L1/L2。"
            )
        return ""

    @staticmethod
    def _tool_text(data) -> str:
        if isinstance(data, (dict, list)):
            return json.dumps(data, ensure_ascii=False, indent=2, default=str)
        return str(data or "")

    def _bound_tool_outcome(self, tool_name: str, args: dict, outcome: StepOutcome) -> StepOutcome:
        text = self._tool_text(outcome.data)
        limit = 8000
        path_value = str(args.get("path") or "")
        nonempty_lines = [line for line in text.splitlines() if line.strip()]
        looks_minified = bool(nonempty_lines) and (len(text) / len(nonempty_lines)) > 320
        if tool_name == "file_read" and path_value.lower().endswith((".js", ".min.js")) and looks_minified:
            limit = 5000
        tool_count = max(1, int(args.get("_tool_num", 1) or 1))
        limit = max(2000, limit // tool_count)
        if len(text) <= limit:
            return outcome

        round_id = re.sub(r"[^A-Za-z0-9._-]+", "_", self._current_round_id())[:100]
        stamp = time.localtime()
        root = (
            self.controller.conversations.conversation_dir(self.sender_id)
            / "runtime" / "tool-results"
            / time.strftime("%Y/%m/%d", stamp)
            / round_id
        )
        digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
        filename = f"turn{max(1, int(getattr(self, 'current_turn', 1) or 1)):02d}-{tool_name}-{digest}.txt"
        target = root / filename
        root.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text(text + ("\n" if text and not text.endswith("\n") else ""), encoding="utf-8")
        preview = text[:limit].rstrip()
        bounded = "\n".join([
            preview,
            "",
            f"[G4W：工具结果过长，后续内容已归档到 {target}]",
            "如确需细节，请用keyword或行号定向读取该文件；不要无目的重复读取全文。",
        ])
        return StepOutcome(bounded, next_prompt=outcome.next_prompt, should_exit=outcome.should_exit)

    def do_file_read(self, args, response):
        self._ensure_sop_round_state()
        round_id = self._current_round_id()
        if round_id != self._file_read_round:
            self._file_read_round = round_id
            self._file_read_results = {}
        dedupe_args = {key: value for key, value in args.items() if key not in ("_index", "_tool_num")}
        dedupe_key = json.dumps(dedupe_args, ensure_ascii=False, sort_keys=True, default=str)
        if dedupe_key in self._file_read_results:
            previous = self._file_read_results[dedupe_key]
            return StepOutcome(
                "本Round已执行过完全相同的file_read，未重复读取。"
                + (f" 上次归档：{previous}" if previous else ""),
                next_prompt=self._get_anchor_prompt(skip=args.get("_index", 0) > 0),
            )
        outcome = yield from super().do_file_read(args, response)
        bounded = self._bound_tool_outcome("file_read", args, outcome)
        match = re.search(r"已归档到 (.+?)\]", self._tool_text(bounded.data))
        self._file_read_results[dedupe_key] = match.group(1) if match else ""
        try:
            ledger = ensure_ledger(self)
            body = self._tool_text(bounded.data)
            archived = bool(match) or ("已归档" in body[:200])
            ledger.add_file_read(str(args.get("path") or ""), body, archived=archived)
        except Exception:
            pass
        return bounded

    def do_code_run(self, args, response):
        self._ensure_sop_round_state()
        # Arbitrary code may change files, so earlier file_read results are no
        # longer safe to reuse within this Round.
        self._file_read_results = {}
        outcome = yield from super().do_code_run(args, response)
        return self._bound_tool_outcome("code_run", args, outcome)

    def do_file_write(self, args, response):
        blocked = self._require_sop_management(args)
        if blocked is not None:
            yield "[G4W] Shared SOP write requires the L0 management flow.\n"
            return blocked
        self._file_read_results = {}
        # 用户画像覆盖写之前先留一份 prev，看板用主题色 diff 标新增/删除。
        # content 可能在 response 标签里，这里只负责备份旧文件（有内容就留）。
        try:
            from pathlib import Path as _Path
            write_path = str(args.get("path") or "")
            norm = write_path.replace("\\", "/")
            if norm.endswith("history_insight/user_profile.md") or norm.endswith("/user_profile.md"):
                get_abs = getattr(self, "_get_abs_path", None)
                target = _Path(get_abs(write_path) if callable(get_abs) else write_path)
                if not target.is_absolute():
                    target = (_Path.cwd() / target).resolve()
                if target.is_file() and target.stat().st_size > 0:
                    prev = target.with_name("user_profile.prev.md")
                    old = target.read_text(encoding="utf-8-sig", errors="replace")
                    if old.strip():
                        prev.write_text(old if old.endswith("\n") else old + "\n", encoding="utf-8")
        except Exception:
            pass
        outcome = yield from super().do_file_write(args, response)
        try:
            ok = True
            data = getattr(outcome, "data", None)
            if isinstance(data, dict) and data.get("ok") is False:
                ok = False
            ensure_ledger(self).note_write(
                "file_write",
                str(args.get("content") or args.get("text") or "")[:500],
                ok=ok,
                path=str(args.get("path") or ""),
            )
        except Exception:
            pass
        return outcome

    def do_file_patch(self, args, response):
        blocked = self._require_sop_management(args)
        if blocked is not None:
            yield "[G4W] Shared SOP patch requires the L0 management flow.\n"
            return blocked
        self._file_read_results = {}
        # 画像 patch 前同样留 prev，供看板主题色 diff
        try:
            from pathlib import Path as _Path
            write_path = str(args.get("path") or "")
            norm = write_path.replace("\\", "/")
            if norm.endswith("history_insight/user_profile.md") or norm.endswith("/user_profile.md"):
                get_abs = getattr(self, "_get_abs_path", None)
                target = _Path(get_abs(write_path) if callable(get_abs) else write_path)
                if not target.is_absolute():
                    target = (_Path.cwd() / target).resolve()
                if target.is_file() and target.stat().st_size > 0:
                    prev = target.with_name("user_profile.prev.md")
                    old = target.read_text(encoding="utf-8-sig", errors="replace")
                    if old.strip():
                        prev.write_text(old if old.endswith("\n") else old + "\n", encoding="utf-8")
        except Exception:
            pass
        outcome = yield from super().do_file_patch(args, response)
        try:
            ok = True
            data = getattr(outcome, "data", None)
            if isinstance(data, dict) and data.get("ok") is False:
                ok = False
            ensure_ledger(self).note_write(
                "file_patch",
                str(args.get("content") or args.get("new_content") or args.get("old_content") or "")[:500],
                ok=ok,
                path=str(args.get("path") or ""),
            )
        except Exception:
            pass
        return outcome

    def do_web_scan(self, args, response):
        outcome = yield from super().do_web_scan(args, response)
        return self._bound_tool_outcome("web_scan", args, outcome)

    def do_G4W_worker_spawn(self, args, response):
        result = self.controller.spawn_worker(
            self.sender_id,
            args.get("capability_id", ""),
            args.get("task", ""),
            args.get("lifecycle", ""),
            args.get("model_tier", ""),
        )
        return StepOutcome(result, next_prompt="后台任务已登记。向用户做一句简短自然确认，不展示内部ID。")

    def do_G4W_worker_send(self, args, response):
        result = self.controller.send_worker(
            self.sender_id,
            args.get("worker_id", ""),
            args.get("message", ""),
        )
        if result.get("routedFrom") == "send_worker:worker.l4":
            return StepOutcome(
                result,
                next_prompt=(
                    "已按 worker.l4 的完整 L4 流程处理（prepare 新窗口→语义挖掘→finalize 三任务链）。"
                    "向用户简短确认已开始，完成后索引会自动更新。"
                ),
            )
        return StepOutcome(result, next_prompt="根据结果自然回复用户。")

    def do_G4W_worker_list(self, args, response):
        return StepOutcome(self.controller.list_workers(self.sender_id), next_prompt="结合列表回答用户，不暴露无关内部细节。")

    def do_G4W_worker_get(self, args, response):
        result = self.controller.get_worker(self.sender_id, args.get("worker_id", ""))
        return StepOutcome(result, next_prompt="审查任务目标、当前run、进度和结果，再决定验收、返工或补问。")

    def do_G4W_worker_review(self, args, response):
        worker_id = args.get("worker_id", "")
        result = self.controller.review_worker(
            self.sender_id,
            worker_id,
            int(args.get("run_index", 0) or 0),
            args.get("decision", ""),
            args.get("note", ""),
        )
        self.parent.G4W_reviewed_workers.add(worker_id)
        return StepOutcome(result, next_prompt="按验收决定执行下一步；accept后可用G4W自己的语言交付。")

    def do_G4W_worker_stop(self, args, response):
        result = self.controller.stop_worker(self.sender_id, args.get("worker_id", ""))
        return StepOutcome(result, next_prompt="根据停止结果自然回复用户。")

    def _services_store_path(self):
        config = getattr(self.controller, "config", None)
        if config is not None and getattr(config, "state_dir", None) is not None:
            return Path(config.state_dir) / "dashboard-services.json"
        return None

    def do_G4W_service_register(self, args, response):
        """把自定义服务登记进看板服务管理（可显示/启动/停止/日志）。"""
        path = self._services_store_path()
        if path is None:
            return StepOutcome({"ok": False, "error": "state_dir 不可用"}, next_prompt="如实告知用户注册失败。")
        service_id = str(args.get("service_id") or "").strip()
        command = args.get("command") or []
        if not service_id or not isinstance(command, list) or not command:
            return StepOutcome(
                {"ok": False, "error": "service_id 与 command(list) 必填"},
                next_prompt="补充 service_id 和启动命令后重试。",
            )
        try:
            state = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"services": {}}
            state.setdefault("services", {})[service_id] = {
                "name": str(args.get("name") or service_id),
                "desc": str(args.get("desc") or ""),
                "command": [str(x) for x in command],
                "cwd": str(args.get("cwd") or ""),
                "logs": [str(x) for x in (args.get("logs") or [])],
                "health": dict(args.get("health") or {}),
                "managed": True,
                "builtin": False,
            }
            path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            return StepOutcome(
                {"ok": True, "registered": service_id, "path": str(path)},
                next_prompt="已注册进看板服务管理，向用户简短确认。",
            )
        except Exception as exc:
            return StepOutcome({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, next_prompt="如实告知失败。")

    def do_G4W_service_remove(self, args, response):
        """从看板服务管理移除自定义服务（内置服务不可移除）。"""
        path = self._services_store_path()
        service_id = str(args.get("service_id") or "").strip()
        if path is None:
            return StepOutcome({"ok": False, "error": "state_dir 不可用"}, next_prompt="如实告知用户。")
        try:
            if not path.is_file():
                return StepOutcome({"ok": False, "error": "服务注册表不存在"}, next_prompt="如实告知。")
            state = json.loads(path.read_text(encoding="utf-8"))
            services = state.get("services", {})
            spec = services.get(service_id)
            if not spec:
                return StepOutcome({"ok": False, "error": f"unknown service: {service_id}"}, next_prompt="如实告知。")
            if spec.get("builtin"):
                return StepOutcome({"ok": False, "error": f"内置服务 {service_id} 不允许移除"}, next_prompt="如实告知。")
            services.pop(service_id, None)
            path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            return StepOutcome({"ok": True, "removed": service_id}, next_prompt="已移除，向用户简短确认。")
        except Exception as exc:
            return StepOutcome({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, next_prompt="如实告知失败。")

    def do_G4W_memory_search(self, args, response):
        """Explicit Hybrid/HNSW memory search for Conductor (read-only)."""
        query = str(args.get("query") or "").strip()
        if not query:
            return StepOutcome(
                {"ok": False, "error": "query is required"},
                next_prompt="补充检索 query 后重试 G4W_memory_search。",
            )
        try:
            # Agent may raise k for broad recall; hard cap keeps payload bounded.
            k = max(1, min(80, int(args.get("k") or 5)))
        except (TypeError, ValueError):
            k = 5
        # Default stays pure-text/hybrid. Vector is an optional addon and must be
        # requested explicitly via scope=vector/both, then still passes /vector gate.
        scope = str(args.get("scope") or "hybrid").strip().lower()
        if scope not in ("both", "hybrid", "vector"):
            scope = "hybrid"

        conversations = getattr(self.controller, "conversations", None)
        memory_root = getattr(conversations, "memory_root", None) if conversations is not None else None
        sender_id = str(self.sender_id or "")
        payload = {
            "ok": True,
            "query": query,
            "k": k,
            "scope": scope,
            "queries_used": [],
            "hybrid": {"enabled": False, "hits": []},
            "vector": {"enabled": False, "hits": []},
        }

        def _prefer_memory_hits(raw_hits, *, id_key="item_id", score_key="score", path_key=None, overfetch=None):
            """Prefer transcripts / current sender; drop ultra-weak scores; note if empty after filter."""
            if not raw_hits:
                return [], None
            items = list(raw_hits)
            # overfetch then filter so k still fills with personal memory when possible
            pool = items[: max(int(overfetch or (k * 4)), k)]

            def path_of(h):
                if path_key and isinstance(h, dict):
                    return str(h.get(path_key) or h.get(id_key) or "")
                if isinstance(h, dict):
                    return str(h.get(id_key) or "")
                return str(getattr(h, id_key, "") or getattr(h, "item_id", "") or "")

            def score_of(h):
                if isinstance(h, dict):
                    return float(h.get(score_key) or 0.0)
                return float(getattr(h, score_key, 0.0) or 0.0)

            def is_transcript(p: str) -> bool:
                pl = p.replace("\\", "/").lower()
                return "/transcripts/" in pl or pl.startswith("transcripts/")

            def sender_match(p: str) -> bool:
                if not sender_id:
                    return True
                return _belongs_to_user(p, sender_id)

            # Soft score floor: pure-vector cosine often ~0.3–0.6; keep low but drop near-zero noise.
            # CAS hybrid often returns ~0.0 filler hits — do NOT fall back to them.
            WEAK = 0.12
            strong = [h for h in pool if score_of(h) >= WEAK]
            if not strong:
                # No ultra-weak fallback: empty hybrid lets vector path own the answer.
                strong = []

            preferred = [h for h in strong if is_transcript(path_of(h)) and sender_match(path_of(h))]
            if len(preferred) < k:
                # fill with any transcript
                seen = {id(h) for h in preferred}
                for h in strong:
                    if id(h) in seen:
                        continue
                    if is_transcript(path_of(h)):
                        preferred.append(h)
                        seen.add(id(h))
                    if len(preferred) >= k:
                        break
            if len(preferred) < k:
                seen = {id(h) for h in preferred}
                for h in strong:
                    if id(h) in seen:
                        continue
                    preferred.append(h)
                    seen.add(id(h))
                    if len(preferred) >= k:
                        break

            out = preferred[:k]
            note = None
            if not out:
                note = "no hits after transcript/sender prefer filter"
            elif all(score_of(h) < 0.25 for h in out):
                note = "weak scores only; verify with file_read before asserting facts"
            return out, note

        if scope in ("both", "hybrid") and memory_root is not None:
            try:
                from G4W.memory.hybrid_reader import get_reader, hybrid_main_read_enabled

                if hybrid_main_read_enabled():
                    hybrid_root = Path(memory_root).resolve().parent / "hybrid"
                    reader = get_reader(hybrid_root)
                    payload["hybrid"]["enabled"] = True
                    payload["hybrid"]["available"] = bool(reader.available())
                    raw_h = reader.search(query, top_k=max(k * 4, 12)) if reader.available() else []
                    docs_h = getattr(reader, "_docs", None) or {}
                    cas = getattr(reader, "cas", None)

                    def _hybrid_full_text(h):
                        cid = str(getattr(h, "chunk_id", "") or "")
                        body = str(docs_h.get(cid) or "").strip()
                        if not body and cas is not None:
                            ch = str(getattr(h, "cas_hash", "") or "")
                            if ch:
                                try:
                                    rawb = cas.get(ch)
                                    if rawb:
                                        body = rawb.decode("utf-8", errors="replace").strip()
                                except Exception:
                                    body = ""
                        if not body:
                            body = str(getattr(h, "text_preview", None) or "").strip()
                        return body

                    def _anchor_of(item_id: str, source_path=None) -> str:
                        s = str(item_id or "")
                        if "#c" in s:
                            return "#" + s.rsplit("#", 1)[-1]
                        return ""

                    mapped_h = []
                    for h in raw_h:
                        full = _hybrid_full_text(h)
                        src = (h.provenance or {}).get("source_path")
                        iid = h.chunk_id
                        mapped_h.append(
                            {
                                "item_id": iid,
                                "score": float(h.score),
                                "cas_hash": h.cas_hash,
                                "source_path": src,
                                "anchor": _anchor_of(str(iid), src),
                                "text_preview": full[:240],
                                "text": full[:8000],
                            }
                        )
                    # Incremental window: vector-index watermark → latest chat.
                    # The hybrid side covers this gap with direct keyword search
                    # over recent transcript files, so very recent conversations
                    # are retrievable without waiting for the next index run.
                    try:
                        from G4W.memory.hybrid_reader import transcript_window_hits
                        from G4W.memory.l4_safe import safe_segment
                        from G4W.memory.vector.transcript_chunk_upsert import (
                            read_transcript_watermark,
                        )

                        wm = read_transcript_watermark()
                        tx_root = (
                            Path(memory_root)
                            / "conversations"
                            / safe_segment(str(sender_id))
                            / "transcripts"
                        )
                        inc = transcript_window_hits(
                            tx_root, wm, query, k=max(k, 5)
                        )
                        if inc:
                            payload["hybrid"]["incremental_watermark"] = wm
                            mapped_h.extend(inc)
                    except Exception:
                        pass
                    filtered_h, h_note = _prefer_memory_hits(
                        mapped_h, id_key="item_id", path_key="source_path"
                    )
                    payload["hybrid"]["hits"] = filtered_h
                    if h_note:
                        payload["hybrid"]["note"] = h_note
                else:
                    payload["hybrid"]["note"] = "G4W_HYBRID_MAIN_READ off"
            except Exception as e:
                payload["hybrid"]["error"] = str(e)

        if scope in ("both", "vector"):
            try:
                from G4W.memory.vector.flags import vector_retrieval_enabled
                from G4W.memory.vector.hnsw_index import HnswIndex
                from G4W.memory.vector.hybrid_query import (
                    HybridQueryEngine,
                    expand_memory_queries,
                )
                from G4W.memory.vector.prod_inject import (
                    _ensure_docs_for_labels,
                    _index_ready,
                    _labels_from_index,
                    _load_docs,
                    _load_tier_records,
                )
                from G4W.memory.vector.sandbox_paths import resolve_vector_index_dir
                from G4W.memory.vector.tier_policy import default_policy

                # Product addon gate (installed∧enabled) before legacy env flag.
                # Total switch: vector_enabled() False → skip path entirely.
                addon_on = True
                try:
                    from G4W.memory.vector.vector_config import (
                        vector_enabled as _addon_vector_enabled,
                    )

                    if not _addon_vector_enabled():
                        payload["vector"]["note"] = "vector_addon disabled"
                        addon_on = False
                except Exception:
                    # import failure → fall through to legacy flag only
                    pass

                if not addon_on:
                    pass
                elif not vector_retrieval_enabled():
                    payload["vector"]["note"] = "vector retrieval flag off"
                else:
                    index_dir = resolve_vector_index_dir()
                    payload["vector"]["index_dir"] = str(index_dir)
                    if _index_ready(index_dir):
                        idx = HnswIndex.load(index_dir)
                        if getattr(idx, "count", 0) > 0:
                            labels = _labels_from_index(index_dir, idx)
                            docs = _ensure_docs_for_labels(
                                _load_docs(index_dir / "docs.json"), labels
                            )
                            records = _load_tier_records(
                                index_dir / "tier_records.jsonl", labels
                            )
                            dim = int(getattr(idx, "dim", 0) or 384)
                            engine = HybridQueryEngine(
                                index=idx,
                                policy=default_policy(),
                                records=records,
                                docs=docs,
                                dim=dim,
                            )
                            # Query-side expand + neighbor chunks (no re-index).
                            # Rank/life-event questions need more alts (口语≠原文).
                            _alt_n = 6 if re.search(
                                r"段位|上分|巅峰|突破|王者|冲分|商场|婚宴|针清|痘", query
                            ) else 5
                            variants = expand_memory_queries(query, max_alts=_alt_n)
                            payload["vector"]["queries_used"] = list(variants)
                            payload["queries_used"] = list(variants)
                            raw_v = engine.search_memory(
                                query,
                                k=max(k * 4, 12),
                                expand=True,
                                neighbors=True,
                                max_alts=_alt_n,
                                day_neighbors=True,
                            )
                            # Embedding service down/half-dead (BM25-only or empty)?
                            # Try starting the server once (cooldown-bounded) and
                            # retry for semantic recall; surface the outcome to
                            # the agent instead of silently returning nothing.
                            # Note: BM25-only results still count as "vector side
                            # degraded" — start+retry whenever embed failed, so
                            # the agent is never misled into thinking vectors ran.
                            if (
                                getattr(engine, "last_embed_error", None)
                                and _maybe_start_embed()
                            ):
                                raw_v = engine.search_memory(
                                    query,
                                    k=max(k * 4, 12),
                                    expand=True,
                                    neighbors=True,
                                    max_alts=_alt_n,
                                    day_neighbors=True,
                                )
                            if not raw_v and getattr(engine, "last_embed_error", None):
                                payload["vector"]["note"] = (
                                    "embed 服务不可用（已尝试拉起失败或处于冷却期）→ "
                                    "向量侧无命中；如需语义检索请告知用户检查向量服务"
                                )
                            elif raw_v and getattr(engine, "last_embed_error", None):
                                payload["vector"]["note"] = (
                                    "embed 服务不可用，本次为词法(BM25)降级结果；"
                                    "已尝试拉起服务，重试后仍不可用"
                                )
                            payload["vector"]["enabled"] = True
                            payload["vector"]["backend"] = str(
                                getattr(idx, "backend", "?") or "?"
                            )
                            payload["vector"]["count"] = int(
                                getattr(idx, "count", 0) or 0
                            )
                            def _anchor_v(item_id: str) -> str:
                                s = str(item_id or "")
                                if "#c" in s:
                                    return "#" + s.rsplit("#", 1)[-1]
                                return ""

                            def _vector_full_text(item_id: str, preview: str) -> str:
                                # Prefer live docs table (full chunk); preview is often truncated.
                                iid = str(item_id or "")
                                body = ""
                                if iid and isinstance(docs, dict):
                                    body = str(docs.get(iid) or "").strip()
                                if not body:
                                    body = str(preview or "").strip()
                                return body

                            mapped_v = []
                            for h in raw_v:
                                iid = str(getattr(h, "item_id", "") or "")
                                full = _vector_full_text(
                                    iid, getattr(h, "text_preview", None) or ""
                                )
                                mapped_v.append(
                                    {
                                        "item_id": iid,
                                        "score": float(getattr(h, "score", 0.0) or 0.0),
                                        "tier": getattr(h, "tier", None),
                                        "source_path": iid.split("#", 1)[0] if iid else "",
                                        "anchor": _anchor_v(iid),
                                        "text_preview": full[:240],
                                        "text": full[:8000],
                                        "stages": dict(getattr(h, "stages", None) or {}),
                                    }
                                )
                            filtered_v, v_note = _prefer_memory_hits(
                                mapped_v, id_key="item_id", path_key="item_id"
                            )
                            payload["vector"]["hits"] = filtered_v
                            if v_note:
                                payload["vector"]["note"] = v_note
                        else:
                            payload["vector"]["note"] = "index empty"
                    else:
                        payload["vector"]["note"] = "index not ready"
            except Exception as e:
                payload["vector"]["error"] = str(e)

        n_h = len(payload["hybrid"].get("hits") or [])
        n_v = len(payload["vector"].get("hits") or [])
        payload["hit_count"] = n_h + n_v
        notes = []
        for side in ("hybrid", "vector"):
            n = (payload.get(side) or {}).get("note")
            if n:
                notes.append(f"{side}: {n}")
        if payload["hit_count"]:
            next_prompt = (
                "已有 hybrid/vector hits：禁止仅凭 text_preview 断言用户原话。回答记忆题前必须 file_read 对应 source_path（或 item_id 路径部分）核实原文与日期；可引用 tool 返回中 text 字段的逐字内容，但日期/归属仍以 file_read 为准。答记忆题模板：日期 + 路径/锚点 + 原文引用 + 一句话解释；缺任一不得声称「找到了」。禁止 code_run/es/glob 全盘扫库当主路径；hits 不对题时最多换 1～2 次更具体/同义 query 再 search。"
                + ((" 注意：" + "；".join(notes)) if notes else "")
            )
        else:
            next_prompt = (
                "无检索命中：先换 1～2 次更具体/同义 query 再 G4W_memory_search （用用户原话实体、时间线索、同义表述；勿编造路径）。禁止 code_run/es 全盘扫库当主路径；仅连续无命中且用户明确要求时才允许单文件 file_read 核实。"
            )
        try:
            ensure_ledger(self).add_memory_search(payload)
        except Exception:
            pass
        return StepOutcome(payload, next_prompt=next_prompt)

    def do_G4W_knowledge_search(self, args, response):
        """Explicit keyword search over the independent Knowledge Base."""
        query = str(args.get("query") or "").strip()
        if not query:
            return StepOutcome(
                {"ok": False, "error": "query is required"},
                next_prompt="补充检索 query 后重试 G4W_knowledge_search。",
            )
        try:
            k = max(1, min(20, int(args.get("k") or 5)))
        except (TypeError, ValueError):
            k = 5

        try:
            from G4W.knowledge.search import search_knowledge
            from G4W.knowledge.store import KnowledgeStore

            result = search_knowledge(query, k=k, store=KnowledgeStore())
            hits = result.get("hits") or []
            payload = {
                "ok": True,
                "query": query,
                "k": k,
                "mode": result.get("mode") or "keyword",
                "hit_count": len(hits),
                "hits": [
                    {
                        "doc_id": hit.get("doc_id"),
                        "chunk_id": hit.get("chunk_id"),
                        "score": hit.get("score"),
                        "title": hit.get("title") or hit.get("source"),
                        "source": hit.get("source"),
                        "page": hit.get("page"),
                        "section": hit.get("section") or "",
                        "quote": hit.get("quote") or "",
                        "quote_truncated": bool(hit.get("quote_truncated")),
                        "text_length": hit.get("text_length"),
                        "tags": hit.get("tags") or [],
                    }
                    for hit in hits
                ],
            }
        except Exception as e:
            payload = {"ok": False, "query": query, "k": k, "error": str(e)}
            return StepOutcome(
                payload,
                next_prompt="知识库检索失败；不要改用 G4W_memory_search 回答知识库问题。必须调用 ask_user() 告知失败原因，并询问用户是否启动/修复 embedding 后重试、换 query 重试，或停止本次知识库检索。",
            )

        if payload["hit_count"]:
            payload["recommended_action"] = "read_relevant_knowledge_chunk_then_answer"
            next_prompt = "知识库检索已定位候选；若 quote 不完整、题干/选项/答案可能跨段、或用户要求精确原文，下一步调用 G4W_knowledge_read 读取对应 chunk/page 小窗口后再答。quote 已足够时可直接回答，但仍必须带文档名、页码/章节和原文引用。不得反复 search 或改用 G4W_memory_search 补证据。"
        else:
            payload["recommended_action"] = "retry_knowledge_search_with_more_specific_query_or_report_no_kb_evidence"
            next_prompt = "知识库检索没有命中；不要改用 G4W_memory_search 冒充知识库证据。最多换 1～2 次更具体 query 重试，仍无命中就明确告知未找到 KB 证据。"
        try:
            ensure_ledger(self).add_knowledge_search(payload)
        except Exception:
            pass
        return StepOutcome(payload, next_prompt=next_prompt)

    def do_G4W_knowledge_read(self, args, response):
        """Read a small original-text window from the independent Knowledge Base."""
        chunk_id = str(args.get("chunk_id") or "").strip()
        doc_id = str(args.get("doc_id") or "").strip()
        page = args.get("page")
        try:
            window = max(0, min(3, int(args.get("window") or 0)))
        except (TypeError, ValueError):
            window = 0
        try:
            max_chars = max(500, min(6000, int(args.get("max_chars") or 3000)))
        except (TypeError, ValueError):
            max_chars = 3000
        if not chunk_id and not (doc_id and page is not None):
            return StepOutcome(
                {"ok": False, "error": "chunk_id or doc_id+page is required"},
                next_prompt="缺少 chunk_id 或 doc_id+page；先用 G4W_knowledge_search 定位后再调用 G4W_knowledge_read。",
            )

        try:
            from G4W.knowledge.store import KnowledgeStore

            store = KnowledgeStore()
            docs = {str(doc.get("doc_id") or ""): doc for doc in store.list_documents()}
            chunks = store.read_chunk_window(chunk_id, window=window) if chunk_id else store.read_page_chunks(doc_id, page, window=window)
            text_used = 0
            out_chunks = []
            truncated = False
            for chunk in chunks:
                text = str(chunk.get("text") or "")
                remaining = max_chars - text_used
                if remaining <= 0:
                    truncated = True
                    break
                if len(text) > remaining:
                    text = text[:remaining]
                    truncated = True
                text_used += len(text)
                doc = docs.get(str(chunk.get("doc_id") or ""), {})
                out_chunks.append({
                    "doc_id": chunk.get("doc_id"),
                    "chunk_id": chunk.get("chunk_id"),
                    "title": doc.get("title") or chunk.get("title") or chunk.get("doc_id"),
                    "source": doc.get("source_path") or doc.get("stored_path"),
                    "page": chunk.get("page"),
                    "section": chunk.get("section") or "",
                    "text": text,
                    "text_length": len(str(chunk.get("text") or "")),
                })
            payload = {
                "ok": bool(out_chunks),
                "chunk_id": chunk_id or None,
                "doc_id": doc_id or None,
                "page": page,
                "window": window,
                "max_chars": max_chars,
                "chunk_count": len(out_chunks),
                "truncated": truncated,
                "chunks": out_chunks,
            }
        except Exception as e:
            payload = {"ok": False, "chunk_id": chunk_id or None, "doc_id": doc_id or None, "page": page, "error": str(e)}
            return StepOutcome(payload, next_prompt="知识库原文读取失败；不要用长期记忆或猜测补证据，说明失败原因并询问是否换 chunk/page 重试。")

        if not payload["ok"]:
            return StepOutcome(payload, next_prompt="知识库未找到对应 chunk/page；可回到 G4W_knowledge_search 换更具体 query 定位，或明确告知未找到原文证据。")
        return StepOutcome(payload, next_prompt="知识库原文已读取；必须依据 G4W_knowledge_read 的 chunks.text 回答，并带文档名、页码/章节和原文引用。若 truncated=true 且还缺关键上下文，可再次 read 更小窗口或相邻 chunk；不得反复 search。")

    def do_G4W_knowledge_ingest(self, args, response):
        """Import a local document into the independent Knowledge Base."""
        path = str(args.get("path") or "").strip()
        if not path:
            return StepOutcome(
                {"ok": False, "error": "path is required"},
                next_prompt="缺少要导入的文件 path；不要声称已收进知识库，补充 path 后重试 G4W_knowledge_ingest。",
            )
        title = str(args.get("title") or "").strip() or None
        raw_tags = args.get("tags") or []
        if isinstance(raw_tags, str):
            raw_tags = [raw_tags]
        tags = [str(tag).strip() for tag in raw_tags if str(tag).strip()]

        try:
            from G4W.knowledge.ingest import ingest_document
            from G4W.knowledge.store import KnowledgeStore

            meta = ingest_document(path, tags=tags, title=title, store=KnowledgeStore())
            payload = {
                "ok": True,
                "doc_id": meta.get("doc_id"),
                "title": meta.get("title"),
                "chunk_count": meta.get("chunk_count"),
                "source_path": meta.get("source_path"),
                "stored_path": meta.get("stored_path"),
                "tags": meta.get("tags") or [],
            }
        except Exception as e:
            payload = {"ok": False, "path": path, "title": title, "tags": tags, "error": str(e)}
            return StepOutcome(
                payload,
                next_prompt="知识库导入失败；不得回复已收进知识库。说明失败原因，必要时请求正确文件路径或可解析文档。",
            )

        return StepOutcome(
            payload,
            next_prompt="知识库导入成功；只有现在可以回复已收进知识库，并带 doc_id、title、chunk_count。不要把文档正文写入长期记忆或L4。",
        )

    def do_G4W_knowledge_remove(self, args, response):
        """Remove a document from the independent Knowledge Base by doc_id or /kb list number."""
        doc_id = str(args.get("doc_id") or "").strip()
        number = str(args.get("number") or "").strip()
        if not doc_id and not number:
            return StepOutcome(
                {"ok": False, "error": "doc_id or number is required"},
                next_prompt="缺少 doc_id 或 /kb list 编号；不得回复已移除，先补充标识后重试 G4W_knowledge_remove。",
            )

        try:
            from G4W.knowledge.store import KnowledgeStore

            store = KnowledgeStore()
            if not doc_id:
                doc_id = store.resolve_number(number) or ""
            docs = {str(doc.get("doc_id") or ""): doc for doc in store.list_documents()}
            doc = docs.get(doc_id)
            if not doc_id or not doc:
                payload = {"ok": False, "doc_id": doc_id or None, "number": number or None, "error": "document not found"}
                return StepOutcome(payload, next_prompt="知识库未找到该文档；不得回复已移除。可先 /kb list 或请用户确认 doc_id。")
            removed = store.remove_by_doc_id(doc_id)
            payload = {"ok": bool(removed), "doc_id": doc_id, "title": doc.get("title")}
        except Exception as e:
            payload = {"ok": False, "doc_id": doc_id or None, "number": number or None, "error": str(e)}
            return StepOutcome(payload, next_prompt="知识库删除失败；不得回复已移除，说明失败原因。")

        if not payload["ok"]:
            return StepOutcome(payload, next_prompt="知识库删除未成功；不得回复已移除。")
        return StepOutcome(payload, next_prompt="知识库删除成功；只有现在可以回复已移除，并带 doc_id 和 title。")

    def do_G4W_web_search(self, args, response):
        """网络信息检索：DeepSeek 模型优先走官方 Responses API web_search，否则/失败回退 web_scan。"""
        query = str(args.get("query") or "").strip()
        if not query:
            return StepOutcome(
                {"ok": False, "error": "query is required"},
                next_prompt="补充检索 query 后重试 G4W_web_search。",
            )
        enabled = bool(getattr(getattr(self.controller, "config", None), "web_search_enabled", True))
        if not enabled:
            return StepOutcome(
                {"ok": False, "error": "G4W_web_search disabled by config"},
                next_prompt="G4W_web_search 已被配置禁用；改用 web_scan 或说明无法联网检索。",
            )

        # 读 mykey.py（GA 同源配置）：model 决定是否走 deepseek websearch；
        # apibase/apikey 决定请求端点（便携包=官方直连，个人环境=CPA 中转，均可被看板统计）。
        # 注意：G4W 进程的 sys.path 不含 runtime/app，需用 GA_APP_DIR 显式加载 mykey.py。
        model = ""
        api_key = ""
        base_url = ""
        try:
            import importlib.util

            from G4W.core.config import GA_APP_DIR

            mykey_path = GA_APP_DIR / "mykey.py"
            if mykey_path.is_file():
                spec = importlib.util.spec_from_file_location("g4w_mykey", mykey_path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                lite = getattr(module, "native_oai_config_lite", None) or {}
                model = str(lite.get("model") or "").strip()
                api_key = str(lite.get("apikey") or "").strip()
                base_url = str(lite.get("apibase") or "").strip().rstrip("/")
        except Exception:
            pass

        is_deepseek = "deepseek" in model.lower()
        if not is_deepseek or not api_key or not base_url:
            return (yield from self._web_search_fallback(response))

        # DeepSeek Responses API + web_search（服务端执行搜索）。
        # 端点跟随 mykey 的 apibase：便携包直连官方，个人环境经 CPA 中转（看板可统计）。
        # 固定 stream=False：CPA 对 responses 请求按 stream 字段分流，非流式才路由到 codex 段。
        import json as _json
        import urllib.request as _urllib

        payload = {
            "model": model,
            "tools": [{"type": "web_search"}],
            "input": query,
            "stream": False,
        }
        request = _urllib.Request(
            f"{base_url}/v1/responses",
            data=_json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with _urllib.urlopen(request, timeout=90) as response_http:
                data = _json.loads(response_http.read().decode("utf-8"))
        except Exception:
            return (yield from self._web_search_fallback(response))

        outputs = data.get("output") or []
        search_calls = [item for item in outputs if item.get("type") == "web_search_call"]
        texts = []
        for item in outputs:
            if item.get("type") != "message":
                continue
            for block in item.get("content") or []:
                if block.get("type") == "output_text" and block.get("text"):
                    texts.append(block["text"])
        answer = "\n".join(texts).strip()
        if not answer or not search_calls:
            # 无搜索结果或模型未实际执行搜索 → 视为失效，回退 web_scan。
            return (yield from self._web_search_fallback(response))

        result = {
            "ok": True,
            "query": query,
            "engine": "deepseek_web_search",
            "model": model,
            "search_count": len(search_calls),
            "searches": [str(item.get("search_query") or item.get("query") or "") for item in search_calls],
            "answer": answer,
        }
        outcome = StepOutcome(result, next_prompt="基于检索结果回答用户；回答应带来源或日期，不得编造未出现的内容。")
        return self._bound_tool_outcome("G4W_web_search", args, outcome)

    def _web_search_fallback(self, response):
        """G4W_web_search 失效时的回退：走 GA 浏览器扫描（text_only 拿正文）。"""
        outcome = yield from super().do_web_scan(
            {"tabs_only": False, "text_only": True}, response
        )
        return self._bound_tool_outcome("web_scan", {"text_only": True}, outcome)

    def do_ask_user(self, args, response):
        question = str(args.get("question") or "需要你补充一点信息").strip()
        candidates = [str(item).strip() for item in (args.get("candidates") or []) if str(item).strip()]
        if candidates:
            question += "\n" + "\n".join(f"- {item}" for item in candidates)
        self.parent.G4W_final_reply = question
        return StepOutcome({"status": "needs_input", "question": question}, should_exit=True)

    def do_start_long_term_update(self, args, response):
        self._ensure_sop_round_state()
        catalog = self.controller.sop_catalog
        management_path = catalog.management_path
        if not management_path.is_file():
            return StepOutcome(
                {"ok": False, "status": "not_found", "path": str(management_path)},
                next_prompt="SOP管理元SOP不可用；不要修改SOP，直接完成当前任务。",
            )
        management_text = management_path.read_text(encoding="utf-8-sig", errors="replace")
        self._sop_management_ready = True
        prompt = (
            "你已请求沉淀长期可复用的执行经验。严格按照下面的G4W L0管理SOP判断和操作。\n"
            "用户事实、关系、聊天内容和情绪记忆不写SOP，它们由G4W L4独立维护。\n"
            "先用GA file_read读取L1/L2和候选L3；只用file_patch做最小修改，新文件才允许file_write。\n"
            f"共享MemoryRoot：{catalog.root}\nL1索引：{catalog.index_path}\nL2事实：{catalog.root / 'global_mem.txt'}\n\n"
            + management_text
        )
        return StepOutcome(
            {"status": "ready", "memoryLayer": "G4W-sop", "index": str(catalog.index_path), "root": str(catalog.root)},
            next_prompt=prompt,
        )

    def do_no_tool(self, args, response):
        sop_issue = self._sop_completion_issue()
        if sop_issue:
            yield "[G4W] Shared SOP maintenance is incomplete.\n"
            return StepOutcome({}, next_prompt=sop_issue)
        pending = set(getattr(self.parent, "G4W_pending_review_workers", set()))
        reviewed = set(getattr(self.parent, "G4W_reviewed_workers", set()))
        missing = sorted(pending - reviewed)
        if missing:
            yield "[G4W] Worker result review required before user-visible delivery.\n"
            return StepOutcome(
                {},
                next_prompt=(
                    "你尚未验收这些Worker结果：" + ", ".join(missing) +
                    "。必须先调用G4W_worker_get（若信息不足）和G4W_worker_review，"
                    "选择accept/revise/needs_input/reject；禁止直接向用户报告。"
                ),
            )
        raw = clean_visible_reply(getattr(response, "content", ""))
        try:
            ledger = ensure_ledger(self)
            user_message = str(
                getattr(self.parent, "G4W_user_message", "")
                or getattr(self.parent, "last_user_message", "")
                or ""
            )
            gated = gate_final_reply(raw, ledger, user_message=user_message, allow_remediate=False)
            final_reply = gated.text if gated.text is not None else raw
            if not gated.ok:
                final_reply = sanitize_outbound_reply(final_reply, ledger, user_message=user_message)
            self.parent.G4W_final_reply = final_reply
            self.parent.G4W_gate_reasons = list(gated.reasons or [])
        except Exception:
            self.parent.G4W_final_reply = raw
        return (yield from super().do_no_tool(args, response))


class WorkerHandler(GenericAgentHandler):
    def __init__(self, parent, last_history=None, cwd="./temp"):
        super().__init__(parent, last_history, getattr(parent, "G4W_runtime_dir", cwd))

    def do_ask_user(self, args, response):
        question = str(args.get("question") or "需要补充信息").strip()
        self.parent.worker_input_request = question
        return StepOutcome({"status": "needs_input", "question": question}, should_exit=True)

    def do_G4W_worker_switch_model(self, args, response):
        from .ga_adapter import select_model_name

        requested = str(args.get("model_tier") or "pro").strip().lower()
        if requested != "pro":
            return StepOutcome({"ok": False, "reason": "automatic Worker switching only supports Flash to Pro"}, next_prompt="继续使用当前模型。")
        switches = int(getattr(self.parent, "G4W_model_switches", 0) or 0)
        current = str(getattr(getattr(self.parent.llmclient, "backend", None), "model", "") or "")
        if "pro" in current.lower():
            return StepOutcome({"ok": True, "already": True, "model": current}, next_prompt="已经是Pro，继续当前任务。")
        if switches >= 1:
            return StepOutcome({"ok": False, "reason": "this run already used its automatic model upgrade"}, next_prompt="不要重复切换，继续完成任务。")
        target = str(getattr(self.parent, "G4W_pro_model", "deepseek-v4-pro") or "deepseek-v4-pro")
        selected = select_model_name(self.parent, target, 0)
        self.parent.G4W_model_switches = switches + 1
        event = {
            "at": time.time(),
            "from": current,
            "to": selected.get("model", target),
            "reason": str(args.get("reason") or "Worker judged the task complex")[:1000],
            "historyPreserved": True,
        }
        event_file_value = str(getattr(self.parent, "G4W_model_events_file", "") or "").strip()
        if event_file_value:
            event_file = Path(event_file_value)
            event_file.parent.mkdir(parents=True, exist_ok=True)
            with event_file.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        return StepOutcome(event, next_prompt="模型已在同一Worker会话中热切换为Pro，保留全部history。继续原任务，不要重新开始。")

    def do_start_long_term_update(self, args, response):
        root_value = str(getattr(self.parent, "G4W_ga_memory_root", "") or "").strip()
        if not root_value:
            return StepOutcome({"status": "skipped", "reason": "external GA memory root unavailable"}, next_prompt="直接完成当前任务。")
        root = Path(root_value)
        root.mkdir(parents=True, exist_ok=True)
        insight = root / "global_mem_insight.txt"
        facts = root / "global_mem.txt"
        sop_dir = root / "sop"
        sop_dir.mkdir(parents=True, exist_ok=True)
        prompt = (
            "提炼本次任务中经过工具验证、长期可复用的GA执行经验。\n"
            f"外置GA记忆根目录：{root}\n"
            f"L1索引：{insight}\nL2事实：{facts}\nL3 SOP目录：{sop_dir}\n"
            "先读取现有文件，再做最小patch。禁止写runtime/app/memory；禁止写G4W用户事实、人格、关系或微信聊天内容。"
        )
        return StepOutcome({"status": "ready", "memoryRoot": str(root)}, next_prompt=prompt)
