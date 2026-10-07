import json
import re
import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .ga_adapter import create_agent, load_ga_tool_schema, mark_tool_ownership, merge_tool_schemas, select_model_name, start_agent_runner, update_process_tools
from .handlers import ConductorHandler, clean_visible_reply
from .reply_gates import ensure_ledger, sanitize_outbound_reply
from ..memory.instructions import InstructionManager, render_instruction_template, update_env_file
from .round_log import ConductorRoundLog


SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")


class G4WTurnPrompt(str):
    """Keep GA's unmodified 1500-char desktop prompt archiver out of WeChat turns.

    The value remains a normal JSON-serializable string; only GA's local len()
    guard sees a bounded length. G4W owns long-message archival under the
    conversation directory instead of writing wrapped prompts into app/temp.
    """

    def __len__(self):
        return min(super().__len__(), 1499)


class ConductorSession:
    def __init__(self, controller, sender_id: str):
        self.controller = controller
        self.sender_id = sender_id
        self.lock = threading.RLock()
        self.state_lock = threading.RLock()
        package = Path(__file__).resolve().parent
        G4W_tools = json.loads((package / "conductor_tools.json").read_text(encoding="utf-8"))
        tools = merge_tool_schemas(
            mark_tool_ownership(G4W_tools, "G4W原生/控制平面"),
            mark_tool_ownership(load_ga_tool_schema(controller.config.conductor_model), "GA借用/执行平面"),
        )
        runtime_dir = controller.config.conversations_dir / sender_id_safe(sender_id) / "runtime"
        self.runtime_dir = runtime_dir
        self.intervention_lock = threading.RLock()
        self.pending_interventions = []
        self.awaiting_intervention_acks = []
        self.intervention_closing = False
        self.active_output_path = None
        self.intervene_path = runtime_dir / "control" / "_intervene"
        self.agent = create_agent(
            handler_class=ConductorHandler,
            tools_schema=tools,
            runtime_dir=runtime_dir,
            system_prompt_provider=lambda agent: controller.build_system_prompt(sender_id, agent=agent),
            max_turns=controller.config.conductor_max_turns,
        )
        self.agent.peer_hint = False
        self.agent.verbose = False
        self.agent.inc_out = True
        self.agent.no_print = True
        self.agent.G4W_controller = controller
        self.agent.G4W_sender_id = sender_id
        self.agent.G4W_final_reply = ""
        self.agent.G4W_cache_sink = lambda value: controller.record_cache_metric(sender_id, value)
        self.agent.G4W_input_sink = lambda value: controller.record_input_snapshot(sender_id, value)
        self.agent.G4W_input_context = {}
        self.agent.G4W_input_turn = 0
        self.agent.G4W_intervention_lock = self.intervention_lock
        self.agent.G4W_intervene_path = str(self.intervene_path)
        self.agent.G4W_intervention_ack_sink = self._ack_interventions
        self.intervene_path.unlink(missing_ok=True)
        hooks = getattr(self.agent, "_turn_end_hooks", None)
        if hooks is None:
            hooks = self.agent._turn_end_hooks = {}
        hooks[f"G4W_intervene_{sender_id_safe(sender_id)}"] = self._on_intervention_turn_end
        binding = controller._binding_for_sender(sender_id)
        requested_model = binding.get("conductorModel") or controller.config.conductor_model
        self.selected_model = select_model_name(self.agent, requested_model, controller.config.model_no)
        self.refresh_tool_schema(self.selected_model.get("model", requested_model))
        if binding.get("conductorModel") != self.selected_model.get("model"):
            controller.conversations.update_binding(binding["bindingKey"], conductorModel=self.selected_model.get("model", requested_model), modelSource=binding.get("modelSource") or "default")
        self.history_file = controller.conversations.clean_history_path(sender_id)
        legacy_history = self.history_file.with_name("current.json")
        if legacy_history.exists():
            legacy_dir = self.history_file.parent / "legacy-cross-round"
            legacy_dir.mkdir(parents=True, exist_ok=True)
            legacy_history.replace(legacy_dir / f"current.{time.time_ns()}.json")
        self.last_turn = 1
        self.active_round_id = ""
        self.active_delivery_kind = "plain_reply"
        self.active_binding = {}
        self.round_user_intervened = False
        self.last_delivery_kind = "plain_reply"
        self.last_user_intervened = False
        self.cancelled_rounds = set()
        self._load_history()
        start_agent_runner(self.agent, f"G4W-{sender_id_safe(sender_id)}")

    @staticmethod
    def _intervention_sort_key(item: dict) -> tuple:
        return (
            str(item.get("receivedAt") or ""),
            float(item.get("createdAt", 0) or 0),
            str(item.get("eventId") or ""),
        )

    def _ensure_intervention_state(self) -> None:
        if not hasattr(self, "intervention_lock"):
            self.intervention_lock = threading.RLock()
        if not hasattr(self, "pending_interventions"):
            self.pending_interventions = []
        if not hasattr(self, "awaiting_intervention_acks"):
            self.awaiting_intervention_acks = []
        if not hasattr(self, "intervention_closing"):
            self.intervention_closing = False
        if not hasattr(self, "active_output_path"):
            self.active_output_path = None
        if not hasattr(self, "intervene_path"):
            fallback = Path(getattr(self, "runtime_dir", Path(self.history_file).parent / "runtime"))
            self.intervene_path = fallback / "control" / "_intervene"
        if hasattr(self, "agent"):
            self.agent.G4W_intervention_lock = self.intervention_lock
            self.agent.G4W_intervene_path = str(self.intervene_path)
            self.agent.G4W_intervention_ack_sink = self._ack_interventions

    @staticmethod
    def _intervention_prompt(event: dict) -> str:
        payload = event.get("payload") or {}
        text = str(payload.get("userText") if "userText" in payload else payload.get("text") or "").strip()
        received_at = str(payload.get("receivedAt") or "")
        lines = [
            "用户在你处理当前任务时发来了一条新的微信消息。",
            "请把它作为最新用户意图纳入当前执行；若与旧方向冲突，以新消息为准。",
            "",
            f"[{format_received_time(received_at)}][user/current-intervention]",
            text or "用户发送了新的附件。",
        ]
        attachments = list(payload.get("attachments") or [])
        if attachments:
            lines.extend(["", "当前消息附件："])
            for item in attachments:
                path = str(item.get("absolutePath") or item.get("relativePath") or item.get("fileName") or "").strip()
                kind = str(item.get("kind") or "file")
                if path:
                    lines.append(f"- {kind}: {path}")
        return "\n".join(lines).strip()

    def _rewrite_intervention_file(self) -> None:
        ordered = sorted(self.pending_interventions, key=self._intervention_sort_key)
        content = "\n\n".join(str(item.get("prompt") or "").strip() for item in ordered if str(item.get("prompt") or "").strip())
        if not content:
            self.intervene_path.unlink(missing_ok=True)
            return
        self.intervene_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.intervene_path.with_name(f"._intervene.{time.time_ns()}.tmp")
        temporary.write_text(content + "\n", encoding="utf-8")
        temporary.replace(self.intervene_path)

    def _audit_intervention(self, item: dict, outcome: str, **extra) -> None:
        output_path = Path(self.active_output_path) if self.active_output_path else None
        if output_path is None:
            return
        record = {
            "eventId": str(item.get("eventId") or ""),
            "messageId": str(item.get("messageId") or ""),
            "receivedAt": str(item.get("receivedAt") or ""),
            "parentRoundId": str(item.get("roundId") or ""),
            "targetTurn": max(1, int(item.get("targetTurn", 1) or 1)),
            "outcome": str(outcome or ""),
            "recordedAt": datetime.now(SHANGHAI).isoformat(),
            **extra,
        }
        path = output_path.parent / "interventions.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def try_intervene(self, event: dict, binding: dict | None = None) -> dict:
        self._ensure_intervention_state()
        if event.get("type") != "wechat.user_message":
            return {"accepted": False, "reason": "not a user message"}
        events = getattr(self.controller, "events", None)
        if events is None:
            return {"accepted": False, "reason": "event store unavailable"}
        with self.intervention_lock:
            with self.state_lock:
                round_id = str(self.active_round_id or "")
                closing = bool(self.intervention_closing)
            if not round_id or closing or not bool(getattr(self.agent, "is_running", False)):
                return {"accepted": False, "reason": "conductor is not accepting interventions"}
            handler_turn = int(getattr(getattr(self.agent, "handler", None), "current_turn", 0) or 0)
            api_turn = int(getattr(self.agent, "G4W_input_turn", 0) or 0)
            target_turn = max(1, handler_turn, api_turn) + 1
            claimed = events.claim_intervention(event.get("id", ""), round_id, target_turn)
            if not claimed:
                return {"accepted": False, "reason": "event was already claimed"}
            payload = claimed.get("payload") or {}
            item = {
                "eventId": claimed.get("id", ""),
                "messageId": payload.get("messageId", ""),
                "receivedAt": payload.get("receivedAt", ""),
                "createdAt": claimed.get("createdAt", 0),
                "roundId": round_id,
                "targetTurn": target_turn,
                "prompt": self._intervention_prompt(claimed),
            }
            self.pending_interventions.append(item)
            try:
                self._rewrite_intervention_file()
            except Exception as error:
                self.pending_interventions = [value for value in self.pending_interventions if value.get("eventId") != item["eventId"]]
                events.resolve_intervention([item["eventId"]], consumed=False, reason=f"intervention write failed: {error}")
                return {"accepted": False, "reason": str(error)}
            context = getattr(self.agent, "G4W_input_context", None)
            if isinstance(context, dict):
                context["deliveryKind"] = "plain_reply"
                context["source"] = "wechat.user_intervention"
                context["isUserMessage"] = True
                context.setdefault("interventions", []).append({
                    "eventId": item["eventId"],
                    "messageId": item["messageId"],
                    "receivedAt": item["receivedAt"],
                    "targetTurn": target_turn,
                })
            with self.state_lock:
                self.active_delivery_kind = "plain_reply"
                self.round_user_intervened = True
                if binding:
                    self.active_binding = dict(binding)
            self._audit_intervention(item, "queued")
            return {"accepted": True, "roundId": round_id, "targetTurn": target_turn}

    def _on_intervention_turn_end(self, context: dict) -> None:
        self._ensure_intervention_state()
        if not self.pending_interventions:
            return
        turn = max(1, int((context or {}).get("turn", 1) or 1))
        handler = (context or {}).get("self")
        max_turns = int(getattr(handler, "max_turns", 0) or 0)
        terminal = bool((context or {}).get("exit_reason")) or bool(max_turns and turn >= max_turns)
        items = list(self.pending_interventions)
        self.pending_interventions = []
        events = getattr(self.controller, "events", None)
        event_ids = [str(item.get("eventId") or "") for item in items]
        if terminal:
            self.intervention_closing = True
            if events is not None:
                events.resolve_intervention(event_ids, consumed=False, reason="active Round ended at intervention boundary")
            for item in items:
                self._audit_intervention(item, "requeued", boundaryTurn=turn)
        else:
            self.awaiting_intervention_acks.extend(items)
            for item in items:
                self._audit_intervention(item, "injected", boundaryTurn=turn, deliveredToTurn=turn + 1)

    def _ack_interventions(self, turn: int, messages: list | None = None) -> int:
        self._ensure_intervention_state()
        current_turn = max(1, int(turn or 1))
        with self.intervention_lock:
            acknowledged = [
                item for item in self.awaiting_intervention_acks
                if int(item.get("targetTurn", 1) or 1) <= current_turn
            ]
            if not acknowledged:
                return 0
            ids = {str(item.get("eventId") or "") for item in acknowledged}
            self.awaiting_intervention_acks = [
                item for item in self.awaiting_intervention_acks
                if str(item.get("eventId") or "") not in ids
            ]
            events = getattr(self.controller, "events", None)
            count = events.resolve_intervention(sorted(ids), consumed=True) if events is not None else 0
            for item in acknowledged:
                self._audit_intervention(item, "consumed", deliveredToTurn=current_turn)
            return int(count or 0)

    def _return_pending_interventions(self, reason: str) -> int:
        self._ensure_intervention_state()
        with self.intervention_lock:
            items = list(self.pending_interventions) + list(self.awaiting_intervention_acks)
            self.pending_interventions = []
            self.awaiting_intervention_acks = []
            self.intervene_path.unlink(missing_ok=True)
            if not items:
                return 0
            events = getattr(self.controller, "events", None)
            event_ids = [str(item.get("eventId") or "") for item in items]
            count = events.resolve_intervention(event_ids, consumed=False, reason=reason) if events is not None else 0
            for item in items:
                self._audit_intervention(item, "requeued", reason=reason)
            return int(count or 0)

    def write_control_context(self, binding: dict | None = None, round_id: str = "", turn: int = 1) -> Path:
        resolved = dict(binding or getattr(self, "active_binding", {}) or {})
        runtime_dir = Path(getattr(self, "runtime_dir", self.history_file.parent / "runtime"))
        config = self.controller.config
        state_dir = Path(getattr(config, "state_dir", runtime_dir.parent))
        workspace_root = Path(getattr(config, "workspace_root", runtime_dir.parent))
        shared_root = Path(getattr(config, "sop_dir", Path(__file__).resolve().parents[1] / "memory" / "sop"))
        payload = {
            "version": 1,
            "stateDir": str(state_dir),
            "workspaceRoot": str(workspace_root),
            "sharedMemoryRoot": str(shared_root),
            "senderId": self.sender_id,
            "bindingKey": str(resolved.get("bindingKey") or ""),
            "contextToken": str(resolved.get("contextToken") or ""),
            "roundId": str(round_id or getattr(self, "active_round_id", "") or ""),
            "turn": max(1, int(turn or 1)),
            "updatedAt": time.time(),
        }
        path = runtime_dir / "G4W-context.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
        return path

    def refresh_tool_schema(self, model_name: str) -> None:
        package = Path(__file__).resolve().parent
        G4W_tools = json.loads((package / "conductor_tools.json").read_text(encoding="utf-8"))
        update_process_tools(merge_tool_schemas(
            mark_tool_ownership(G4W_tools, "G4W原生/控制平面"),
            mark_tool_ownership(load_ga_tool_schema(model_name), "GA借用/执行平面"),
        ))

    def _load_history(self):
        sync = self.controller.conversations.sync_clean_history(self.sender_id)
        self.agent.llmclient.backend.history = deepcopy(sync.get("messages") or [])
        return sync

    def _save_history(self):
        # Cross-Round history is derived only from delivered transcript data.
        # Never persist GA's current tool loop into the clean history file.
        return self.controller.conversations.sync_clean_history(self.sender_id)

    def _history(self) -> list:
        value = getattr(self.agent.llmclient.backend, "history", [])
        return value if isinstance(value, list) else []

    def _restore_clean_history(self, messages: list | None = None) -> None:
        if messages is None:
            messages = self.controller.conversations.sync_clean_history(self.sender_id).get("messages") or []
        self.agent.llmclient.backend.history = deepcopy(messages)

    def _append_turn_context(self, text: str) -> None:
        value = str(text or "").strip()
        if not value:
            return
        self._history().append({"role": "user", "content": [{"type": "text", "text": value}]})

    @staticmethod
    def _knowledge_sop_needed(prompt: str, event_context: str = "") -> bool:
        text = f"{prompt or ''}\n{event_context or ''}".lower()
        if not text.strip():
            return False
        keywords = (
            "知识库", "资料库", "文档", "入库", "导入", "检索", "引用", "原文", "pdf", "markdown", ".md", ".pdf",
            "knowledge", "kb", "source", "quote", "citation",
        )
        return any(keyword in text for keyword in keywords)

    @staticmethod
    def _knowledge_sop_prompt() -> str:
        return (
            "# G4W 知识库SOP\n"
            "- Knowledge 与 Memory 严格分离；知识库数据只使用 runtime/G4W-data/knowledge，不写入 memory/L4/transcripts。\n"
            "- 用户询问知识库内容时，优先使用 G4W_knowledge_search；不要改用 G4W_memory_search 冒充知识库证据。\n"
            "- /vector off 时知识库检索必须保持纯关键词/BM25，不触发 embedding；/vector on 也只能使用独立 knowledge 索引。\n"
            "- 回答知识库内容必须基于 search 返回的 source/title/page/section/quote，并说明无页码/章节的情况。\n"
            "- KB 内容不得写入长期记忆；最多只记录必要的文档元信息。\n"
            "- 导入/删除必须以工具返回的 StepOutcome.data 为准；附件保存不等于已入库，未成功删除不得承诺已删除。"
        )

    def cancel(self) -> bool:
        with self.state_lock:
            if not self.active_round_id:
                return False
            self.cancelled_rounds.add(self.active_round_id)
            self.agent.abort()
            return True

    def run(self, prompt: str, event_context: str = "", pending_review_workers=None, round_id: str = "", user_message: bool = False, received_at: str = "", delivery_kind: str = "plain_reply", binding: dict | None = None) -> str:
        with self.lock:
            self._return_pending_interventions("new Round started before prior intervention resolved")
            clean_sync = self.controller.conversations.sync_clean_history(
                self.sender_id,
                exclude_open_user=user_message,
                minimum_user_rounds=20,
                maximum_user_rounds=40,
            )
            clean_snapshot = deepcopy(clean_sync.get("messages") or [])
            self._restore_clean_history(clean_snapshot)
            self.agent.G4W_final_reply = ""
            self.agent.G4W_pending_review_workers = set(pending_review_workers or [])
            self.agent.G4W_reviewed_workers = set()
            resolved_binding = dict(binding or {})
            if not resolved_binding:
                try:
                    resolved_binding = self.controller._binding_for_sender(self.sender_id)
                except (AttributeError, KeyError):
                    resolved_binding = {}
            dynamic_parts = []
            raw_prompt = str(prompt or "").strip()
            message_time = format_received_time(received_at)
            if user_message:
                if self._knowledge_sop_needed(raw_prompt, event_context):
                    dynamic_parts.append(self._knowledge_sop_prompt())
                if event_context:
                    dynamic_parts.append("# 当前附件与事件上下文\n" + event_context)
                dynamic_parts.append(self.controller.conversations.format_current_user(self.sender_id, raw_prompt, received_at))
            else:
                if event_context:
                    dynamic_parts.append("# 当前G4W事件上下文\n" + event_context)
                dynamic_parts.extend([
                    "# 当前内部事件",
                    f"时间：{message_time}",
                    f"[system/{str(delivery_kind or 'internal')}]",
                    raw_prompt,
                ])
            turn_prompt = "\n\n".join(part for part in dynamic_parts if str(part or "").strip())
            state_lock = getattr(self, "state_lock", None)
            if state_lock is None:
                self.state_lock = threading.RLock()
                state_lock = self.state_lock
            if not hasattr(self, "cancelled_rounds"):
                self.cancelled_rounds = set()
            with state_lock:
                self.active_round_id = round_id or f"round-{time.time_ns()}"
                self.active_delivery_kind = str(delivery_kind or "plain_reply")
                self.active_binding = dict(resolved_binding)
                self.round_user_intervened = False
                self.intervention_closing = False
                self.active_output_path = None
                self.agent.G4W_input_turn = 0
                self.agent.G4W_input_context = {
                    "bindingKey": str(resolved_binding.get("bindingKey") or ""),
                    "roundId": self.active_round_id,
                    "startedAtLocal": datetime.now(SHANGHAI).strftime("%H%M%S"),
                    "source": "wechat.user_message" if user_message else "G4W.internal_event",
                    "receivedAt": str(received_at or ""),
                    "deliveryKind": self.active_delivery_kind,
                    "isUserMessage": bool(user_message),
                    "pureUserMessage": raw_prompt if user_message else "",
                    "constructedPrompt": turn_prompt,
                    "cleanHistory": {
                        "mode": clean_sync.get("mode", ""),
                        "compacted": bool(clean_sync.get("compacted")),
                        "userRounds": int(clean_sync.get("userRounds", 0) or 0),
                        "messageCount": int(clean_sync.get("messageCount", 0) or 0),
                        "path": clean_sync.get("path", ""),
                    },
                }
                self.write_control_context(resolved_binding, self.active_round_id, 1)
            round_logs = getattr(self.controller, "round_logs", None)
            if round_logs is None:
                round_logs = ConductorRoundLog(self.history_file.parent / "conductor-outputs")
            output_path = round_logs.begin(
                self.sender_id,
                self.active_round_id,
                {
                    "bindingKey": resolved_binding.get("bindingKey", ""),
                    "deliveryKind": self.active_delivery_kind,
                    "source": "wechat.user_message" if user_message else "G4W.internal_event",
                    "receivedAt": received_at,
                },
            )
            with state_lock:
                self.active_output_path = output_path
            model_log = output_path.parent / "model-responses" / "model-responses.txt"
            model_log.parent.mkdir(parents=True, exist_ok=True)
            self.agent.log_path = str(model_log)
            for client in getattr(self.agent, "llmclients", []) or []:
                try:
                    client.log_path = str(model_log)
                except Exception:
                    pass
            queue = self.agent.put_task(G4WTurnPrompt(turn_prompt), source="G4W")
            last_done = ""
            current_turn = 0
            emitted_turns = set()
            emitted_texts = set()
            try:
                while True:
                    item = queue.get(timeout=1800)
                    if "next" in item:
                        round_logs.append(output_path, item.get("next", ""))
                    item_turn = int(item.get("turn", 0) or 0)
                    outputs = item.get("outputs") if isinstance(item.get("outputs"), list) else []
                    if item_turn > current_turn:
                        current_turn = item_turn
                    if item_turn > 1 and len(outputs) >= 2:
                        completed_turn = item_turn - 1
                        if completed_turn not in emitted_turns:
                            intermediate = clean_visible_reply(outputs[-2])
                            normalized = re.sub(r"\s+", " ", intermediate).strip()
                            if intermediate and normalized not in emitted_texts and self.controller.turn_enabled(self.sender_id):
                                self.controller.emit_intermediate(self.sender_id, intermediate, round_id, completed_turn)
                                emitted_texts.add(normalized)
                            emitted_turns.add(completed_turn)
                    if "done" in item:
                        last_done = item.get("done", "")
                        break
            except Exception as error:
                self._return_pending_interventions(f"Conductor Round failed: {error}")
                with state_lock:
                    active = self.active_round_id
                    self.cancelled_rounds.discard(active)
                    self.active_round_id = ""
                    self.active_delivery_kind = "plain_reply"
                    self.active_binding = {}
                failure = last_done or f"[G4W] Conductor round failed: {error}"
                round_logs.finish(output_path, failure)
                with state_lock:
                    self.active_output_path = None
                self._restore_clean_history(clean_snapshot)
                raise
            self.last_turn = max(1, current_turn)
            self._restore_clean_history(clean_snapshot)
            self._return_pending_interventions("Conductor Round completed before intervention was consumed")
            with state_lock:
                cancelled = self.active_round_id in self.cancelled_rounds
                self.cancelled_rounds.discard(self.active_round_id)
                self.last_delivery_kind = str(self.active_delivery_kind or "plain_reply")
                self.last_user_intervened = bool(self.round_user_intervened)
                self.active_round_id = ""
                self.active_delivery_kind = "plain_reply"
                self.active_binding = {}
                self.round_user_intervened = False
            round_logs.finish(output_path, last_done, cancelled=cancelled)
            with state_lock:
                self.active_output_path = None
            if cancelled:
                return ""
            reply = self.agent.G4W_final_reply or extract_last_reply(last_done)
            reply = clean_visible_reply(reply)
            try:
                handler = getattr(self.agent, "handler", None) or getattr(self, "handler", None)
                ledger = ensure_ledger(handler) if handler is not None else None
                user_message = str(
                    getattr(self.agent, "G4W_user_message", "")
                    or getattr(self, "last_user_text", "")
                    or ""
                )
                reply = sanitize_outbound_reply(reply, ledger, user_message=user_message)
            except Exception:
                pass
            if not reply and current_turn >= int(getattr(self.controller.config, "conductor_max_turns", 8)):
                reply = "本轮已达到Conductor Turn上限，但尚未生成最终回复。执行日志和任务状态均已保留。"
            return reply


class G4WController:
    # Generic persistent-worker wake routing table: capabilityId → wake method
    # on this controller (signature (sender_id, message) -> dict). Registered
    # workers get scheduler-aware wakes instead of a plain send.
    _PERSISTENT_WAKE_ROUTES = {
        "worker.l4": "_wake_l4",
    }

    def __init__(self, config, conversations, capabilities, workers, schedules=None, diary=None, timeline=None, timeline_publisher=None, outbox=None, xiaoyi=None, locations=None, l4=None, supervision=None, checkins=None, profiles=None, turn_progress=None, cache_metrics=None, input_capture=None, wechat_maintenance=None, intermediate_sink=None, session_factory=None, sop_catalog=None, events=None, short_path_mirror=None):
        self.config = config
        self.conversations = conversations
        self.capabilities = capabilities
        self.workers = workers
        self.schedules = schedules
        self.diary = diary
        self.timeline = timeline
        self.timeline_publisher = timeline_publisher
        self.outbox = outbox
        self.xiaoyi = xiaoyi
        self.locations = locations
        self.l4 = l4
        self.supervision = supervision
        self.checkins = checkins
        self.profiles = profiles
        self.turn_progress = turn_progress
        self.cache_metrics = cache_metrics
        self.input_capture = input_capture
        self.wechat_maintenance = wechat_maintenance
        self.sop_catalog = sop_catalog
        self.events = events
        self.intermediate_sink = intermediate_sink
        self.session_factory = session_factory or ConductorSession
        self.sessions: dict[str, ConductorSession] = {}
        self.round_logs = ConductorRoundLog(config.conversations_dir, short_path_mirror)
        package = Path(__file__).resolve().parents[1]
        self.instructions = InstructionManager(config)
        self.policy = (package / "templates" / "agents" / "conductor-policy.md").read_text(encoding="utf-8")

    def session(self, sender_id: str) -> ConductorSession:
        if sender_id not in self.sessions:
            self.sessions[sender_id] = self.session_factory(self, sender_id)
        return self.sessions[sender_id]

    def reset_session(self, sender_id: str, archive_history: bool = False, clean_start: bool = False) -> None:
        session = self.sessions.pop(sender_id, None)
        history_file = self.conversations.clean_history_path(sender_id)
        if session:
            session.agent.task_queue.put("EXIT")
            history_file = session.history_file
        if archive_history and history_file.exists():
            archive_dir = history_file.parent / "archives"
            archive_dir.mkdir(parents=True, exist_ok=True)
            archive = archive_dir / f"clean-window.{int(time.time())}.json"
            history_file.replace(archive)
        elif archive_history and history_file.exists():
            history_file.unlink()
        if clean_start:
            self.conversations.reset_clean_history(sender_id)

    def build_system_prompt(self, sender_id: str, agent=None) -> str:
        # Keep system memory stable; this-round retrieval hits live in round context.
        inject_query = ""
        if agent is not None:
            ctx = getattr(agent, "G4W_input_context", None) or {}
            if isinstance(ctx, dict):
                inject_query = str(ctx.get("pureUserMessage") or "").strip()
                ctx["retrievalContext"] = self.conversations.retrieval_context(
                    sender_id, query=inject_query or None
                )
        memory = self.conversations.read_memory(sender_id)
        try:
            binding = self._binding_for_sender(sender_id) if sender_id else {}
        except KeyError:
            binding = {}
        # Package-local ENV is the single identity authority.  The per-sender
        # profile is only a mirror for diagnostics and future multi-user work.
        user_name = str(self.config.user_name)
        user_identity = str(self.config.user_identity)
        user_gender = str(self.config.user_gender)
        bot_name = str(self.config.bot_name)
        persona, operations = self.instructions.load(
            user_name=user_name, user_identity=user_identity,
            user_gender=user_gender, bot_name=bot_name,
        )
        policy = render_instruction_template(
            self.policy, user_name=user_name, user_identity=user_identity,
            user_gender=user_gender, bot_name=bot_name,
        )
        return "\n\n".join(filter(None, [
            policy,
            persona,
            operations,
            "# 工具所有权\n"
            "G4W专用模型工具只负责Worker管理与必要Conductor控制。\n"
            "文件、代码、网页、SOP读取以及G4W原生业务SOP均使用GA通用工具和固定Python脚本；使用这些工具不代表继承GA桌面身份。\n"
            "API工具数组按G4W Worker控制工具在前、GA通用执行工具在后固定排序。",
            "# Persistent Worker 唤醒\n"
            "用户要求「唤醒/跑一下/让…继续工作」某个 persistent worker（如 worker.l4 记忆整理、worker.supervisor 监督等）时，"
            "用 G4W_worker_send 发给对应 worker_id 即可：系统会按 worker 类型自动路由——"
            "worker.l4 自动走完整 L4 流程（生成新窗口→语义挖掘→finalize 把 L4 insight 与新增 transcript 增量写进向量索引，"
            "embedding 服务不可用会自动拉起）；其他 persistent worker 按常规唤醒开启下一 run。无需其他特殊工具。",
            "# Embedding 设备策略\n"
            "向量 embedding 服务只按安装期决定的方式运行（5_embedding_for_G4W.bat：有 NVIDIA 装 GPU 版 torch，否则 CPU 版）。\n"
            "GPU 版 torch 环境下**禁止**用 EMBED_DEVICE=cpu 或任何方式把服务降级为 CPU 运行；"
            "CUDA 异常时如实告知用户（如「GPU 异常，向量索引未同步，请修复 GPU 或明确同意切 CPU」），由用户决定，不得私自降级。",
            "# 微信可见对话历史格式\n"
            "API history只包含用户在微信实际看到的纯净消息，不包含工具调用、内部推理或Worker内部Turn。\n"
            "历史时间使用Asia/Shanghai，消息头格式为[MM-DD HH:mm:ss][role]。",
            "# 当前G4W配置\n" + "\n".join([
                f"用户名：{user_name or '未设置'}",
                f"用户身份/称呼：{user_identity or user_name or '未设置'}",
                f"用户性别：{user_gender}",
                f"助手名字：{bot_name or '未设置'}",
                f"工作区：{binding.get('workspaceRoot', str(self.config.workspace_root))}",
            ]),
            "# G4W能力与任务路由注册表\n" + self.capabilities.prompt_summary(),
            self._sop_prompt_index(sender_id),
            "# 用户长期记忆\n" + memory if memory else "",
        ]))

    def _sop_prompt_index(self, sender_id: str = "") -> str:
        if self.sop_catalog is None:
            return "[Memory] G4W shared memory unavailable."
        structure_path = self.sop_catalog.root / "insight_fixed_structure.txt"
        try:
            structure = structure_path.read_text(encoding="utf-8-sig", errors="replace").strip()
        except Exception:
            structure = ""
        workspace_root = str(Path(self.config.workspace_root).resolve()).rstrip("\\/")
        rendered = (
            structure
            .replace("{{MEMORY_ROOT}}", str(self.sop_catalog.root))
            .replace("{{CODE_ROOT}}", str(Path(__file__).resolve().parents[1]))
            .replace("{{USER_MEMORY_INDEX}}", str(self.conversations.memory_index_path(sender_id)) if sender_id else "由用户长期记忆中的L1 Memory Index提供")
            .replace("${G4W_WORKSPACE_ROOT}/", workspace_root + "\\")
            .replace("%G4W_WORKSPACE_ROOT%/", workspace_root + "\\")
            .replace("${G4W_WORKSPACE_ROOT}", workspace_root)
            .replace("%G4W_WORKSPACE_ROOT%", workspace_root)
        )
        return "\n".join(filter(None, [
            "[Memory] (G4W Shared Memory)",
            rendered,
            f"{self.sop_catalog.index_path}:",
            self.sop_catalog.index_text(role="conductor"),
        ]))

    def reread(self, sender_id: str) -> str:
        """重载人格 / 操作 SOP / 能力注册表（**不发起模型轮次**）。

        旧实现用 ``session.run(user_message=False)`` 往会话里注入一次“合成轮次”，
        让模型回一句“已刷新”。在内部事件（非用户消息）路径上，这一轮会被挂住：
        表现为无限 ``LLM Running (Turn 1)``、上下文每轮 +2 条消息、只有 /stop 或
        /new 才能中断（用户发 /reread、看板点「人设 → 注入」都会触发）。

        而人格 / 操作 SOP / 能力注册表本来就在**每条消息构造系统提示词时重建**
        （_system_prompt → instructions.load，缓存键含 mtime/size），所以重载只需
        清缓存 + 重建索引，不需要模型参与：下一条消息自然使用新内容。
        """
        self.instructions.ensure_runtime_files()
        self.instructions.clear()
        sop_catalog = getattr(self, "sop_catalog", None)
        if sop_catalog is not None:
            sop_catalog.ensure_index()
            sop_catalog.compile_capabilities()
        reload_registry = getattr(getattr(self, "capabilities", None), "try_reload", None)
        registry = reload_registry() if callable(reload_registry) else {"ok": True, "reloaded": False}
        registry_note = "能力注册表已重新读取。" if registry.get("ok") else f"能力注册表修改无效，继续使用上一份有效配置：{registry.get('error')}"
        # 不再调用 session.run()：避免内部事件把轮次挂住（死循环根因）
        note = "当前会话会在下一条消息使用新内容。" if self.sessions.get(sender_id) is not None else "当前还没有活跃会话，发送一条普通消息即可开始。"
        return f"🔄 人格、操作SOP与能力注册表已重新读取。{registry_note}{note}"

    def identity_profile(self, sender_id: str) -> dict:
        profile = {
            "userName": self.config.user_name,
            "userIdentity": self.config.user_identity,
            "userGender": self.config.user_gender,
            "botName": self.config.bot_name,
        }
        if self.profiles is not None:
            stored = self.profiles.read().get("senders", {}).get(sender_id, {})
            for key in profile:
                if key in stored:
                    profile[key] = stored[key]
        return profile

    def update_identity(self, sender_id: str, field: str, value: str) -> dict:
        mapping = {
            "userName": ("G4W_USER_NAME", "user_name"),
            "userIdentity": ("G4W_USER_IDENTITY", "user_identity"),
            "userGender": ("G4W_USER_GENDER", "user_gender"),
            "botName": ("G4W_BOT_NAME", "bot_name"),
        }
        if field not in mapping:
            raise ValueError(f"unsupported identity field: {field}")
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("identity value cannot be empty")
        if field == "userGender" and normalized not in ("male", "female", "neutral"):
            raise ValueError("userGender must be male, female or neutral")
        env_key, config_attr = mapping[field]
        update_env_file(self.config.env_file, {env_key: normalized})
        object.__setattr__(self.config, config_attr, normalized)
        if self.profiles is not None:
            def update(state):
                entry = state.setdefault("senders", {}).setdefault(sender_id, {})
                entry[field] = normalized
                entry["updatedAt"] = time.time()
                return dict(entry)
            updated = self.profiles.update(update)
        else:
            updated = self.identity_profile(sender_id)
        self.instructions.clear()
        return updated

    def update_checkin_config(self, sender_id: str, minimum_minutes: int | None = None, maximum_minutes: int | None = None, enabled: bool = True) -> dict:
        if self.checkins is None:
            raise RuntimeError("check-in service is unavailable")
        binding = self._binding_for_sender(sender_id)
        if not enabled:
            update_env_file(self.config.env_file, {"G4W_CHECKIN_ENABLED": "0"})
            object.__setattr__(self.config, "checkin_enabled", False)
            return self.checkins.disable(binding["bindingKey"])
        minimum = max(1, int(minimum_minutes if minimum_minutes is not None else self.config.checkin_minimum_minutes))
        maximum = max(minimum, int(maximum_minutes if maximum_minutes is not None else self.config.checkin_maximum_minutes))
        update_env_file(self.config.env_file, {
            "G4W_CHECKIN_ENABLED": "1",
            "G4W_CHECKIN_MIN_INTERVAL_MS": str(minimum * 60_000),
            "G4W_CHECKIN_MAX_INTERVAL_MS": str(maximum * 60_000),
        })
        object.__setattr__(self.config, "checkin_enabled", True)
        object.__setattr__(self.config, "checkin_minimum_minutes", minimum)
        object.__setattr__(self.config, "checkin_maximum_minutes", maximum)
        return self.checkins.configure(binding["bindingKey"], sender_id, minimum, maximum, True)

    def record_cache_metric(self, sender_id: str, value: dict) -> None:
        if self.cache_metrics is not None:
            self.cache_metrics.record(sender_id, value)

    def record_input_snapshot(self, sender_id: str, value: dict) -> None:
        session = self.sessions.get(sender_id)
        if session is not None:
            session.write_control_context(turn=int(value.get("turn", 1) or 1))
        if self.input_capture is not None:
            self.input_capture.save(sender_id, value)

    def cancel_active(self, sender_id: str) -> dict:
        session = self.sessions.get(sender_id)
        if not session or not session.active_round_id:
            return {"ok": False, "cancelled": False, "message": "当前没有正在生成的回复。"}
        round_id = session.active_round_id
        cancelled = session.cancel()
        if cancelled and self.outbox is not None:
            self.outbox.cancel_round(round_id)
        return {"ok": cancelled, "cancelled": cancelled, "roundId": round_id, "message": "已停止当前回复，后台任务会继续运行。"}

    def try_intervene(self, sender_id: str, event: dict, binding: dict | None = None) -> dict:
        session = self.sessions.get(sender_id)
        if session is None or not callable(getattr(session, "try_intervene", None)):
            return {"accepted": False, "reason": "no live Conductor session"}
        return session.try_intervene(event, binding=binding)

    def turn_enabled(self, sender_id: str) -> bool:
        if self.turn_progress is None:
            return False
        binding = self._binding_for_sender(sender_id)
        return self.turn_progress.get(binding["bindingKey"])

    def emit_intermediate(self, sender_id: str, text: str, round_id: str, turn: int) -> None:
        session = self.sessions.get(sender_id)
        delivery_kind = "plain_reply"
        if session:
            with session.state_lock:
                if round_id in session.cancelled_rounds or (session.active_round_id and session.active_round_id != round_id):
                    return
                delivery_kind = session.active_delivery_kind
        if delivery_kind != "plain_reply":
            self.release_round_files(round_id, turn)
            return
        if callable(self.intermediate_sink):
            self.intermediate_sink(sender_id, text, round_id, turn, delivery_kind)
        self.release_round_files(round_id, turn)

    def release_round_files(self, round_id: str, through_turn: int) -> int:
        if self.outbox is None:
            return 0
        release = getattr(self.outbox, "release_held_files", None)
        if not callable(release):
            return 0
        return int(release(round_id, through_turn=through_turn) or 0)

    def last_turn(self, sender_id: str) -> int:
        session = self.sessions.get(sender_id)
        return max(1, int(getattr(session, "last_turn", 1) or 1))

    def last_delivery_kind(self, sender_id: str, fallback: str = "plain_reply") -> str:
        session = self.sessions.get(sender_id)
        return str(getattr(session, "last_delivery_kind", "") or fallback)

    def last_round_had_user_intervention(self, sender_id: str) -> bool:
        session = self.sessions.get(sender_id)
        return bool(getattr(session, "last_user_intervened", False))

    def handle_user_message(self, binding: dict, text: str) -> str:
        return self.handle_events(binding, [{"type": "wechat.user_message", "payload": {"text": text}}])

    def handle_worker_result(self, binding: dict, worker_id: str, result: dict) -> str:
        return self.handle_events(binding, [{"type": "worker.completed", "payload": {"workerId": worker_id, "result": result}}])

    def handle_events(self, binding: dict, events: list[dict], round_id: str = "") -> str:
        binding = dict(binding or {})
        if not binding.get("bindingKey") and binding.get("accountId") and binding.get("senderId"):
            binding["bindingKey"] = self.conversations.binding_key(binding["accountId"], binding["senderId"])
        sender_id = binding["senderId"]
        user_messages = []
        worker_reports = []
        progress_reports = []
        l4_completion_reports = []
        stalled_reports = []
        model_switch_reports = []
        pending_workers = set()
        for event in events:
            payload = event.get("payload") or {}
            if event.get("type") == "wechat.user_message":
                raw_text = payload.get("userText") if "userText" in payload else payload.get("text")
                text = str(raw_text or "").strip()
                if text:
                    received_at = str(payload.get("receivedAt") or "")
                    if not payload.get("transcriptRecorded"):
                        self.conversations.append(
                            sender_id,
                            "User",
                            text,
                            timestamp=received_at,
                            subtype="user-intervention" if payload.get("arrivedWhileBusy") else "",
                            message_id=str(payload.get("messageId") or ""),
                            parent_round_id=str(payload.get("parentRoundId") or ""),
                        )
                    user_messages.append({"text": text, "receivedAt": received_at})
            elif event.get("type") == "worker.completed":
                worker_id = str(payload.get("workerId") or "")
                try:
                    owned = self._require_owned_worker(sender_id, worker_id)
                    detail = (
                        self.get_worker(sender_id, worker_id)
                        if owned.get("capabilityId") == "worker.l4"
                        else self.workers.completion_report(worker_id)
                    )
                except Exception:
                    detail = {"id": worker_id, "runIndex": payload.get("runIndex", 0), "result": payload.get("result") or {}}
                if detail.get("capabilityId") == "worker.l4" and self.l4 is not None:
                    finalized = self.l4.finalize_worker(worker_id)
                    decision = "accept" if finalized.get("status") in ("finalized", "already_finalized") else "reject"
                    try:
                        self.workers.review(worker_id, int(detail.get("runIndex", 0) or 0), decision, json.dumps(finalized, ensure_ascii=False)[:2000])
                    except Exception:
                        pass
                    l4_completion_reports.append({"worker": detail, "finalize": finalized})
                else:
                    if worker_id:
                        pending_workers.add(worker_id)
                    worker_reports.append(detail)
            elif event.get("type") == "worker.stalled":
                stalled_reports.append(payload)
            elif event.get("type") == "worker.progress_milestone":
                worker_id = str(payload.get("workerId") or "")
                item = self.workers.get(worker_id) or {}
                if item.get("status") == "running" and int(item.get("runIndex", 0) or 0) == int(payload.get("runIndex", 0) or 0):
                    progress_reports.append(payload)
            elif event.get("type") == "worker.model_switched":
                model_switch_reports.append(payload)
        context_parts = []
        attachment_reports = []
        for event in events:
            if event.get("type") != "wechat.user_message":
                continue
            payload = event.get("payload") or {}
            if payload.get("attachments") or payload.get("attachmentFailures"):
                attachment_reports.append({
                    "messageId": payload.get("messageId", ""),
                    "attachments": payload.get("attachments") or [],
                    "attachmentFailures": payload.get("attachmentFailures") or [],
                })
        if attachment_reports:
            context_parts.append("# Attachments belonging to the current user message\n" + json.dumps(attachment_reports, ensure_ascii=False, indent=2))
        if worker_reports:
            context_parts.append(
                "# Worker completion reports awaiting review\n" +
                json.dumps(worker_reports, ensure_ascii=False, indent=2)
            )
        if stalled_reports:
            context_parts.append(
                "# Worker no-progress watchdog warnings\n" + json.dumps(stalled_reports, ensure_ascii=False, indent=2) +
                "\nCheck the Worker once. Do not repeatedly poll or review it in this round. Tell the user only if intervention is useful."
            )
        if progress_reports:
            context_parts.append(
                "# Worker progress milestones\n" + json.dumps(progress_reports, ensure_ascii=False, indent=2) +
                "\nGive one short natural progress update. Do not review, stop, restart or poll the Worker in this round."
            )
        if model_switch_reports:
            context_parts.append(
                "# Worker model hot-switch notifications\n" + json.dumps(model_switch_reports, ensure_ascii=False, indent=2) +
                "\nThe same Worker preserved its history and continued in Pro. This is an internal Conductor notification; mention it to the user only when useful."
            )
        l4_started = [event.get("payload") or {} for event in events if event.get("type") == "memory.l4_started"]
        if l4_started:
            context_parts.append(
                "# L4 maintenance started\n" + json.dumps(l4_started, ensure_ascii=False, indent=2) +
                "\nSend one short natural message saying semantic memory maintenance has started."
            )
        if l4_completion_reports:
            context_parts.append(
                "# L4 semantic maintenance completed\n" + json.dumps(l4_completion_reports, ensure_ascii=False, indent=2) +
                "\nReport status, processed dates, user message count, generated files and errors concisely."
            )
            # 用户画像维护:conductor 职责。worker 只产素材草稿,最终画像由 conductor
            # 用自己的语气复述落盘(更新/修改/保持不变)。SOP 路径动态构造。
            try:
                profile_sop = str(Path(__file__).resolve().parents[1] / "memory" / "sop" / "conductor" / "profile_review_sop.md")
            except Exception:
                profile_sop = "memory/sop/conductor/profile_review_sop.md"
            context_parts.append(
                "# 用户画像维护（conductor 职责，本轮 worker-final 顺带完成）\n"
                "L4 worker 已产出画像素材草稿。请按画像 SOP 维护用户画像：\n"
                f"1. 读画像 SOP：{profile_sop}\n"
                "2. 读素材草稿（history_insight/user_profile.draft.md，本轮 worker 产出）与现有画像"
                "（history_insight/user_profile.md，无则为首版）\n"
                "3. 按 SOP：用你自己的语气，对现有画像做**更新 / 修改 / 保持不变**；保留全部信息点，不删减\n"
                "4. 有变化 → file_write 落盘 user_profile.md；无变化 → 不写文件\n"
                "5. 汇报时一句带过画像维护结果，不展开\n"
            )
        scheduled = [event.get("payload") or {} for event in events if event.get("type") == "system.scheduled"]
        if scheduled:
            context_parts.append("# Scheduled system events due now\n" + json.dumps(scheduled, ensure_ascii=False, indent=2))
        xiaoyi_reports = [event.get("payload") or {} for event in events if event.get("type") == "xiaoyi.completed"]
        if xiaoyi_reports:
            context_parts.append("# Xiaoyi task completion reports\n" + json.dumps(xiaoyi_reports, ensure_ascii=False, indent=2))
        location_reports = [event.get("payload") or {} for event in events if event.get("type") == "location.changed"]
        if location_reports:
            context_parts.append("# Significant location changes\n" + json.dumps(location_reports, ensure_ascii=False, indent=2))
        l4_reports = [event.get("payload") or {} for event in events if event.get("type") == "memory.l4_due"]
        if l4_reports:
            context_parts.append(
                "# L4 memory maintenance due\n" + json.dumps(l4_reports, ensure_ascii=False, indent=2) +
                "\nCreate or resume the persistent worker.l4 Worker. It must propose durable user/operational memory facts only; do not let it write main memory directly."
            )
        supervision_reports = [event.get("payload") or {} for event in events if event.get("type") == "supervision.due"]
        if supervision_reports:
            context_parts.append(
                "# Self-control supervision deadlines due now\n" + json.dumps(supervision_reports, ensure_ascii=False, indent=2) +
                "\nConsult or resume worker.supervisor when judgment is useful, but keep timing, chain counts and Dida synchronization in the deterministic service."
            )
        checkin_reports = [event.get("payload") or {} for event in events if event.get("type") == "system.checkin"]
        if checkin_reports:
            maintenance_due = any(item.get("mode") == "maintenance" for item in checkin_reports)
            instruction = (
                "This is original G4W maintenance mode. Use the timeline and/or diary tools for the due fields. "
                "Do not send a companion check-in in this round; a short completion acknowledgement is enough."
                if maintenance_due else
                "Decide whether a natural context-aware message is useful. Silence is allowed when interruption would not help."
            )
            context_parts.append("# Proactive random check-in wakeup\n" + json.dumps(checkin_reports, ensure_ascii=False, indent=2) + "\n" + instruction)
        # Do not make every casual chat audit the complete historical Worker
        # ledger. Worker reports already contain their own detail, and the
        # Conductor can call worker_list when the user explicitly asks.
        needs_worker_ledger = any(
            event.get("type") in ("memory.l4_due", "supervision.due") for event in events
        )
        if needs_worker_ledger:
            workers = self.list_workers(sender_id)
            if workers:
                context_parts.append("# Current Worker ledger\n" + json.dumps(workers, ensure_ascii=False, indent=2))
        context = "\n\n".join(context_parts)
        if len(user_messages) == 1:
            prompt = user_messages[0]["text"]
            is_user_message = True
        elif user_messages:
            blocks = [f"{format_received_time(item['receivedAt'])}\n{item['text']}" for item in user_messages]
            prompt = "\n".join([
                "Multiple newer WeChat messages arrived while you were still handling the previous turn.",
                "Treat the following blocks as one ordered batch of fresh user input and respond once after considering all of them.",
                "",
                "\n\n".join(blocks),
            ])
            is_user_message = True
        elif attachment_reports:
            prompt = "用户刚刚发送了附件。只需自然确认已经收到，并询问希望你如何处理；本轮不要擅自读取或分析附件。"
            is_user_message = False
        else:
            prompt = "处理当前G4W后台事件；只做必要的调度、验收和用户沟通。"
            is_user_message = False
        if user_messages:
            delivery_kind = "plain_reply"
        elif any(event.get("type", "").startswith("worker.") for event in events):
            delivery_kind = "proactive_report"
        elif any(event.get("type") == "system.checkin" for event in events):
            delivery_kind = "checkin"
        else:
            delivery_kind = "system_reply"
        return self.session(sender_id).run(
            prompt,
            event_context=context,
            pending_review_workers=pending_workers,
            round_id=round_id,
            user_message=is_user_message,
            received_at=(user_messages[-1]["receivedAt"] if user_messages else ""),
            delivery_kind=delivery_kind,
            binding=binding,
        )

    def execute_direct(self, sender_id: str, capability_id: str, arguments: dict) -> dict:
        self.capabilities.require_route(capability_id, "direct")
        if capability_id == "reminder.manage":
            if self.schedules is None:
                raise RuntimeError("scheduler is unavailable")
            binding = self._binding_for_sender(sender_id)
            action = str(arguments.get("action") or "create")
            if action == "create":
                return self.schedules.create(
                    binding["bindingKey"],
                    sender_id,
                    arguments.get("content", ""),
                    arguments.get("due_at"),
                    kind=arguments.get("kind", "reminder"),
                    recurrence_seconds=int(arguments.get("recurrence_seconds", 0) or 0),
                )
            if action == "list":
                return {"items": self.schedules.list_for(binding["bindingKey"])}
            if action == "cancel":
                return self.schedules.cancel(binding["bindingKey"], arguments.get("job_id", ""))
            raise ValueError(f"unknown reminder action: {action}")
        if capability_id == "diary.manage":
            if self.diary is None:
                raise RuntimeError("diary store is unavailable")
            action = str(arguments.get("action") or "append")
            if action == "append":
                result = self.diary.append(
                    arguments.get("content", ""),
                    title=arguments.get("title", ""),
                    date=arguments.get("date", ""),
                    at_time=arguments.get("time", ""),
                    sender_id=sender_id,
                )
                if self.wechat_maintenance is not None:
                    self.wechat_maintenance.mark_diary_written(sender_id)
                return result
            if action == "read":
                return self.diary.read(arguments.get("date", ""), sender_id=sender_id)
            if action == "list":
                return self.diary.list_dates(arguments.get("limit", 30), sender_id=sender_id)
            raise ValueError(f"unknown diary action: {action}")
        if capability_id == "timeline.manage":
            if self.timeline is None:
                raise RuntimeError("timeline store is unavailable")
            action = str(arguments.get("action") or "read")
            if action == "read":
                return self.timeline.read(arguments.get("date", ""))
            if action == "list":
                return self.timeline.list_dates(arguments.get("limit", 30))
            if action == "taxonomy":
                from ..features.timeline_analytics import default_taxonomy
                return {"ok": True, "taxonomy": default_taxonomy()}
            if action == "repair_categories":
                return self.timeline.repair_categories()
            if action == "write":
                result = self.timeline.write(
                    arguments.get("date", ""),
                    arguments.get("events") or [],
                    mode=arguments.get("mode", "append"),
                    finalize=bool(arguments.get("finalize", False)),
                )
                if self.wechat_maintenance is not None:
                    self.wechat_maintenance.mark_timeline_written(sender_id)
                return result
            if action == "delete":
                return self.timeline.delete(arguments.get("date", ""), arguments.get("event_id", ""))
            if action == "build":
                return self.timeline_publisher.build()
            if action == "serve":
                return self.timeline_publisher.serve(arguments.get("host", "127.0.0.1"), arguments.get("port", 0))
            if action == "screenshot":
                result = self.timeline_publisher.screenshot(arguments.get("output_file", ""), arguments.get("width", 1680), arguments.get("height", 1400))
                if arguments.get("send"):
                    binding = self._binding_for_sender(sender_id)
                    round_options = self._active_round_file_options(sender_id)
                    queued = self.outbox.prepare_file(
                        binding["bindingKey"], sender_id, binding.get("contextToken", ""),
                        result["outputFile"], f"timeline-screenshot:{sender_id}:{result['outputFile']}",
                        **round_options,
                    )
                    result["deliveryId"] = queued["id"]
                    result["deliveryHeldUntilTurnComplete"] = bool(queued.get("status") == "held")
                return result
            raise ValueError(f"unknown timeline action: {action}")
        if capability_id == "file.send":
            if self.outbox is None:
                raise RuntimeError("outbox is unavailable")
            binding = self._binding_for_sender(sender_id)
            path = Path(str(arguments.get("path") or "")).expanduser().resolve()
            if not path.is_file():
                raise FileNotFoundError(f"file not found: {path}")
            roots = [self.config.state_dir.resolve(), self.config.workspace_root.resolve()]
            if not any(path == root or root in path.parents for root in roots):
                raise PermissionError("file is outside G4W allowed roots")
            dedupe = str(arguments.get("dedupe_key") or f"file-send:{sender_id}:{path}:{path.stat().st_mtime_ns}")
            queued = self.outbox.prepare_file(
                binding["bindingKey"], sender_id, binding.get("contextToken", ""),
                str(path), dedupe, **self._active_round_file_options(sender_id),
            )
            return {
                "ok": True,
                "queued": True,
                "fileName": path.name,
                "deliveryId": queued["id"],
                "deliveryHeldUntilTurnComplete": bool(queued.get("status") == "held"),
            }
        if capability_id == "xiaoyi.task":
            if self.xiaoyi is None:
                raise RuntimeError("xiaoyi service is unavailable")
            binding = self._binding_for_sender(sender_id)
            action = str(arguments.get("action") or "submit")
            if action == "submit":
                return self.xiaoyi.submit(binding["bindingKey"], sender_id, arguments.get("prompt", ""), arguments)
            if action == "list":
                return {"items": self.xiaoyi.list_for(binding["bindingKey"])}
            if action == "get":
                return self.xiaoyi.get(binding["bindingKey"], arguments.get("job_id", ""))
            if action == "health":
                return self.xiaoyi.health()
            raise ValueError(f"unknown xiaoyi action: {action}")
        if capability_id == "location.manage":
            if self.locations is None:
                raise RuntimeError("location service is unavailable")
            binding = self._binding_for_sender(sender_id)
            action = str(arguments.get("action") or "latest")
            if action == "latest":
                return self.locations.latest()
            if action == "history":
                return {"items": self.locations.history(arguments.get("limit", 20))}
            if action == "movements":
                return {"items": self.locations.movements(arguments.get("limit", 20))}
            if action == "record":
                return self.locations.record(arguments, binding["bindingKey"], sender_id)
            raise ValueError(f"unknown location action: {action}")
        if capability_id == "supervision.manage":
            if self.supervision is None:
                raise RuntimeError("supervision service is unavailable")
            binding = self._binding_for_sender(sender_id)
            action = str(arguments.get("action") or "status")
            result = self.supervision.act(binding["bindingKey"], sender_id, action, arguments)
            if action == "open":
                worker = self.workers.spawn(
                    binding["bindingKey"], sender_id, "worker.supervisor",
                    "You are the persistent self-control supervisor. Maintain CTDP reservation and execution discipline, review the deterministic session state, and report concise coaching recommendations to G4W only. Current state:\n" + json.dumps(result, ensure_ascii=False),
                    "persistent",
                )
                self.supervision.set_worker(sender_id, worker["id"])
                result = self.supervision.status(sender_id)
            return result
        if capability_id == "checkin.manage":
            if self.checkins is None:
                raise RuntimeError("check-in service is unavailable")
            binding = self._binding_for_sender(sender_id)
            action = str(arguments.get("action") or "status")
            if action == "configure":
                return self.update_checkin_config(sender_id, arguments.get("minimum_minutes"), arguments.get("maximum_minutes"), True)
            if action == "disable":
                return self.update_checkin_config(sender_id, enabled=False)
            if action == "status":
                return self.checkins.status(binding["bindingKey"])
            raise ValueError(f"unknown check-in action: {action}")
        raise PermissionError(f"direct capability is not implemented: {capability_id}")

    def set_model(self, sender_id: str, model_query) -> dict:
        binding = self._binding_for_sender(sender_id)
        session = self.sessions.get(sender_id)
        if not session:
            session = self.session(sender_id)
        query = str(model_query or "").strip()
        if query.lower() == "flash":
            query = self.config.worker_model
        elif query.lower() == "pro":
            query = self.config.pro_model
        selected = select_model_name(session.agent, query, self.config.model_no)
        session.refresh_tool_schema(selected.get("model", query))
        session.selected_model = selected
        self.conversations.update_binding(
            binding["bindingKey"], conductorModel=selected.get("model", query),
            modelNo=selected.get("index", 0), modelSource="user",
        )
        session._save_history()
        return {"modelNo": selected.get("index", 0), "model": selected.get("model", query), "name": selected.get("name", "")}

    def spawn_worker(self, sender_id: str, capability_id: str, task: str, lifecycle: str = "", model_tier: str = "") -> dict:
        binding = self._binding_for_sender(sender_id)
        return self.workers.spawn(binding["bindingKey"], sender_id, capability_id, task, lifecycle, model_tier=model_tier)

    def request_l4(self, sender_id: str, trigger: str = "manual") -> dict:
        if self.l4 is None:
            raise RuntimeError("L4 service is unavailable")
        binding = self._binding_for_sender(sender_id)
        return self.l4.request(binding["bindingKey"], sender_id, trigger=trigger)

    def send_worker(self, sender_id: str, worker_id: str, message: str) -> dict:
        self._require_owned_worker(sender_id, worker_id)
        detail = self.workers.detail(worker_id) or {}
        # Generic persistent-worker wake routing: "send" to a persistent worker
        # means "start/continue its next cycle". Workers whose run needs
        # scheduler-prepared context register a wake method in
        # _PERSISTENT_WAKE_ROUTES (e.g. worker.l4 needs a fresh manifest plus
        # the finalize three-task chain); all others keep the default send
        # (sleeping → next run, running → inject).
        if str(detail.get("lifecycle") or "") == "persistent":
            wake = type(self)._PERSISTENT_WAKE_ROUTES.get(
                str(detail.get("capabilityId") or "")
            )
            if wake:
                out = getattr(self, wake)(sender_id, message)
                out.setdefault(
                    "worker",
                    {"id": worker_id, "capabilityId": detail.get("capabilityId")},
                )
                return out
        return self.workers.send(worker_id, message)

    def _wake_l4(self, sender_id: str, message: str = "") -> dict:
        """Wake route for worker.l4: full L4 cycle with finalize chain."""
        out = self.request_l4(sender_id, trigger="manual")
        out["routedFrom"] = "send_worker:worker.l4"
        return out

    def list_workers(self, sender_id: str) -> list[dict]:
        binding = self._binding_for_sender(sender_id)
        return self.workers.list_for(binding["bindingKey"])

    def get_worker(self, sender_id: str, worker_id: str) -> dict:
        self._require_owned_worker(sender_id, worker_id)
        return self.workers.detail(worker_id)

    def review_worker(self, sender_id: str, worker_id: str, run_index: int, decision: str, note: str = "") -> dict:
        self._require_owned_worker(sender_id, worker_id)
        result = self.workers.review(worker_id, run_index, decision, note)
        return result

    def stop_worker(self, sender_id: str, worker_id: str) -> dict:
        self._require_owned_worker(sender_id, worker_id)
        return self.workers.stop(worker_id)

    def _binding_for_sender(self, sender_id: str) -> dict:
        session = self.sessions.get(sender_id)
        if session is not None:
            state_lock = getattr(session, "state_lock", None)
            if state_lock is not None:
                with state_lock:
                    active = dict(getattr(session, "active_binding", {}) or {})
            else:
                active = dict(getattr(session, "active_binding", {}) or {})
            if active.get("senderId") == sender_id:
                key = str(active.get("bindingKey") or "")
                if not key and active.get("accountId"):
                    key = self.conversations.binding_key(active["accountId"], sender_id)
                    active["bindingKey"] = key
                return {"bindingKey": key, **active}
        state = self.conversations.bindings.read().get("bindings", {})
        matches = [
            {"bindingKey": key, **value}
            for key, value in state.items()
            if value.get("senderId") == sender_id
        ]
        if matches:
            return max(matches, key=lambda value: (float(value.get("updatedAt", 0) or 0), value.get("bindingKey", "")))
        raise KeyError(f"conversation binding not found for sender {sender_id}")

    def _active_round_file_options(self, sender_id: str) -> dict:
        session = self.sessions.get(sender_id)
        if session is None:
            return {}
        state_lock = getattr(session, "state_lock", None)
        if state_lock is not None:
            with state_lock:
                round_id = str(getattr(session, "active_round_id", "") or "")
        else:
            round_id = str(getattr(session, "active_round_id", "") or "")
        if not round_id:
            return {}
        handler = getattr(getattr(session, "agent", None), "handler", None)
        turn = max(1, int(getattr(handler, "current_turn", 1) or 1))
        return {
            "round_id": round_id,
            "turn": turn,
            "round_final": False,
            "source": "conductor-tool",
            "held": True,
        }

    def _require_owned_worker(self, sender_id: str, worker_id: str) -> dict:
        item = self.workers.get(worker_id)
        if not item or item.get("senderId") != sender_id:
            raise PermissionError("worker does not belong to this conversation")
        return item


def extract_last_reply(text: str) -> str:
    value = re.sub(r"<thinking>[\s\S]*?</thinking>", "", str(text or ""), flags=re.I)
    parts = re.split(r"LLM Running \(Turn \d+\) \.\.\.", value)
    return parts[-1].strip() if parts else value.strip()


def sender_id_safe(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", str(value or ""))[:120] or "unknown"


def format_received_time(value: str = "") -> str:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except Exception:
        parsed = datetime.now(timezone.utc)
    local = parsed.astimezone(SHANGHAI)
    return local.strftime("[%Y-%m-%d %H:%M:%S Asia/Shanghai]")
