import json
import threading
import time
import traceback
from contextlib import nullcontext
from pathlib import Path

from .capabilities import CapabilityRegistry
from ..wechat.commands import CommandRouter, parse_command
from .config import Config, GA_APP_DIR
from .short_path_mirror import ShortPathMirror
from ..agents.controller import G4WController
from ..memory.conversation import ConversationStore
from .records import DiaryStore, TimelineStore
from ..features.location import LocationService
from ..memory.maintenance import L4MaintenanceService
from ..memory.wechat_maintenance import WechatMaintenanceService
from ..features.supervision import DidaCli, SelfControlService
from ..memory.checkin import CheckinService
from ..memory.todo import TodoStore
from ..features.timeline_publish import TimelinePublisher
from .scheduler import ScheduledStore
from .storage import DeferredReplyStore, EventStore, JsonStore, OutboxStore
from ..agents.turn_progress import TurnProgressStore
from ..agents.worker_turn import WorkerTurnStore
from ..agents.input_capture import InputCaptureStore
from .cache_metrics import CacheMetricsStore
from .dashboard_control import DashboardControlMailbox
from ..agents.ga_adapter import model_catalog
from ..memory.migration import archive_obsolete_state, migrate_portable_wechat_layout
from ..memory.sop_catalog import SopCatalog
from ..wechat.weixin_delivery import build_effective_reply_text, format_deferred_reply_batch
from ..wechat.weixin import WeixinChannel
from ..agents.workers import WorkerManager
from ..features.xiaoyi import XiaoyiService


class G4WService:
    def __init__(self, config: Config, channel=None, session_factory=None):
        self.config = config
        config.ensure_dirs()
        migrate_portable_wechat_layout(config.state_dir)
        self.events = EventStore(config.state_dir / "events.json")
        self.recovered_interventions = self.events.recover_intervening()
        self.outbox = OutboxStore(config.state_dir / "outbox.json")
        self.delivery_lock = threading.RLock()
        self._file_delivery_slots = threading.BoundedSemaphore(2)
        self._outbox_thread_lock = threading.RLock()
        self._outbox_thread = None
        self._outbox_last_heartbeat = 0.0
        self._outbox_last_error = ""
        self._outbox_last_error_at = 0.0
        self._outbox_error_count = 0
        self._outbox_next_recovery = 0.0
        self._round_deferred_prefixes: dict[str, str] = {}
        self._last_worker_watchdog = 0.0
        self.deferred = DeferredReplyStore(config.state_dir / "deferred-replies.json")
        self.turn_progress = TurnProgressStore(config.state_dir / "turn-progress-config.json")
        self.worker_turn = WorkerTurnStore(config.state_dir / "worker-turn-config.json")
        self.input_capture = InputCaptureStore(config.state_dir / "input-capture-config.json", config.conversations_dir)
        self.cache_metrics = CacheMetricsStore(config.state_dir / "cache-metrics.json")
        self.dashboard_control = DashboardControlMailbox(config.state_dir)
        self.schedules = ScheduledStore(config.state_dir / "schedules.json")
        self.diary = DiaryStore(
            config.diary_dir,
            config.state_dir / "legacy-import" / "diary",
            conversation_root=config.conversations_dir,
        )
        self.timeline = TimelineStore(
            config.timeline_dir / "timeline-facts.json",
            config.state_dir / "legacy-import" / "timeline" / "timeline-facts.json",
        )
        self.timeline_publisher = TimelinePublisher(
            self.timeline, config.timeline_dir,
            locale=config.timeline_locale, theme=config.timeline_theme,
        )
        self.locations = LocationService(
            config.state_dir / "locations.json", self.events,
            config.location_history_limit, config.location_major_move_meters,
            list(config.location_known_places),
        )
        self.supervision = SelfControlService(
            config.state_dir / "self-control.json", self.events, DidaCli(config.dida_command),
            config.supervision_default_delay_minutes, config.supervision_default_focus_minutes,
        )
        self.checkins = CheckinService(config.state_dir / "checkin-config.json")
        self.todos = TodoStore(config.state_dir / "todo-state.json")
        self.wechat_maintenance = WechatMaintenanceService(config.state_dir / "wechat-maintenance-state.json")
        self.profiles = JsonStore(config.state_dir / "profiles.json", {"senders": {}})
        self.short_path_mirror = ShortPathMirror(
            config.state_dir / "short-path-mirror", config.short_path_dual_write,
        )
        self.conversations = ConversationStore(
            config.conversations_dir,
            config.memory_dir,
            config.recent_pairs,
            recent_max_chars=config.recent_transcript_max_chars,
            long_assistant_reply_chars=config.long_assistant_reply_chars,
            long_user_prompt_chars=config.long_user_prompt_chars,
            short_path_mirror=self.short_path_mirror,
            f1_read_path=getattr(config, "f1_read_path", "legacy") or "legacy",
            stop_aggregate_write=bool(getattr(config, "stop_aggregate_write", False)),
        )
        self._sync_checkins_from_env()
        self.sop_catalog = SopCatalog(config.sop_dir, config.capabilities_file)
        self.sop_catalog.ensure_index()
        self.sop_catalog.ensure_facts()
        self.sop_catalog.compile_capabilities()
        self.capabilities = CapabilityRegistry(config.capabilities_file)
        self.workers = WorkerManager(
            config.workers_dir, self.capabilities, self.events, config.worker_timeout_seconds,
            default_model=config.worker_model, pro_model=config.pro_model,
            conversations_root=config.conversations_dir,
            state_path=config.worker_registry_file,
            ga_memory_root=config.ga_worker_memory_dir,
        )
        self.l4 = L4MaintenanceService(
            config.state_dir, config.l4_min_new_user_turns,
            config.l4_min_new_transcript_files, config.l4_cooldown_seconds,
            config.l4_sample_rate,
        )
        self.l4.attach_workers(self.workers)
        self.l4.attach_events(self.events)
        self.cleanup_report = archive_obsolete_state(config.state_dir, config.external_archive_dir)
        self.xiaoyi = XiaoyiService(config.xiaoyi_jobs_dir, config.xiaoyi_bridge_url, self.events)
        self.controller = G4WController(
            config,
            self.conversations,
            self.capabilities,
            self.workers,
            schedules=self.schedules,
            diary=self.diary,
            timeline=self.timeline,
            timeline_publisher=self.timeline_publisher,
            outbox=self.outbox,
            xiaoyi=self.xiaoyi,
            locations=self.locations,
            l4=self.l4,
            supervision=self.supervision,
            checkins=self.checkins,
            profiles=self.profiles,
            turn_progress=self.turn_progress,
            cache_metrics=self.cache_metrics,
            input_capture=self.input_capture,
            wechat_maintenance=self.wechat_maintenance,
            intermediate_sink=self._queue_intermediate,
            session_factory=session_factory,
            sop_catalog=self.sop_catalog,
            events=self.events,
            short_path_mirror=self.short_path_mirror,
        )
        self.channel = channel or WeixinChannel(config)
        self.commands = CommandRouter(self)

    def login(self):
        return self.channel.login()

    def _sync_checkins_from_env(self) -> None:
        for binding_key, binding in self.conversations.bindings.read().get("bindings", {}).items():
            sender_id = str(binding.get("senderId") or "")
            if not sender_id:
                continue
            if self.config.checkin_enabled:
                self.checkins.configure(
                    binding_key, sender_id,
                    self.config.checkin_minimum_minutes,
                    self.config.checkin_maximum_minutes,
                    True,
                )
            else:
                self.checkins.disable(binding_key)

    def doctor(self) -> dict:
        account_error = ""
        try:
            account = self.channel.resolve_account()
        except Exception as error:
            account, account_error = None, str(error)
        model_config = next((path for path in (GA_APP_DIR / "mykey.py", GA_APP_DIR / "mykey.json") if path.is_file()), None)
        model_error = "" if model_config else "No GA model config found. Create runtime/app/mykey.py or mykey.json"
        return {
            "ok": not account_error and not model_error,
            "stateDir": str(self.config.state_dir),
            "workspaceRoot": str(self.config.workspace_root),
            "envFile": str(self.config.env_file),
            "pathsAreLocationDerived": True,
            "account": {k: v for k, v in (account or {}).items() if k != "token"},
            "accountError": account_error,
            "modelConfig": str(model_config) if model_config else "",
            "modelError": model_error,
            "capabilities": len(self.capabilities.capabilities),
            "workers": len(self.workers.state.read().get("workers", {})),
            "didaCli": {"command": self.supervision.dida.command, "available": self.supervision.dida.available()},
        }

    def _sender_to_binding(self) -> dict:
        """senderId -> bindingKey 映射(用于 todo 到点事件路由)。"""
        mapping = {}
        for binding_key, binding in self.conversations.bindings.read().get("bindings", {}).items():
            sender_id = str(binding.get("senderId") or "")
            if sender_id:
                mapping[sender_id] = binding_key
        return mapping

    def _migrate_legacy_reminders(self) -> int:
        """启动时把旧 schedules.json 的 reminder job 并入 todo(幂等)。"""
        return TodoStore.migrate_from_schedules(
            self.schedules,
            self.config.state_dir / "todo-state.json",
            self._sender_to_binding(),
        )

    def run(self):
        account = None
        while account is None:
            try:
                account = self.channel.resolve_account()
            except Exception as error:
                print(f"[G4W] waiting for test WeChat login: {error}")
                time.sleep(5)
        print(f"[G4W] Python conductor started account={account['accountId']} state={self.config.state_dir}")
        migrated = self._migrate_legacy_reminders()
        if migrated:
            print(f"[G4W] todo: migrated {migrated} legacy reminders")
        if self.config.location_enabled:
            status = self.locations.start_server(self.config.location_host, self.config.location_port, self.config.location_token)
            print(f"[G4W] location server started http://{status['host']}:{status['port']}")
        threading.Thread(target=self._poll_updates_loop, daemon=True, name="G4W-weixin-poll").start()
        self._ensure_outbox_thread()
        while True:
            if self._outbox_thread is None or not self._outbox_thread.is_alive():
                print("[G4W] outbox thread was not alive; restarting it")
                self._ensure_outbox_thread()
            for message in self.channel.process_attachment_retries():
                self._enqueue_inbound(message)
            self.xiaoyi.poll()
            if time.time() - self._last_worker_watchdog >= 5:
                self.workers.scan_model_switches()
                self.workers.scan_progress_milestones(5)
                self.workers.scan_stalled(self.config.worker_stall_seconds)
                self._last_worker_watchdog = time.time()
            self.supervision.process_due()
            self.checkins.emit_due(self.events, self.l4, self.wechat_maintenance, self.config.user_name, todo_service=self.todos)
            self.schedules.emit_due(self.events, limit=20)
            self.todos.emit_due(self.events, self._sender_to_binding(), limit=20)
            self.process_dashboard_requests(limit=10)
            self.process_events(limit=20)
            time.sleep(0.2)

    def _dashboard_models(self, sender_id: str) -> dict:
        session = self.controller.session(sender_id)
        rows = list(session.agent.list_llms())
        catalog = {int(item.get("index", -1)): item for item in model_catalog(session.agent)}
        models = []
        for index, label, current in rows:
            meta = catalog.get(int(index), {})
            models.append({
                "index": int(index),
                "label": str(label or meta.get("name") or meta.get("model") or index),
                "model": str(meta.get("model") or meta.get("name") or label or index),
                "name": str(meta.get("name") or ""),
                "current": bool(current),
            })
        current = next((item for item in models if item["current"]), None)
        return {"models": models, "current": current or {}, "senderId": sender_id}

    def process_dashboard_requests(self, limit: int = 10) -> int:
        processed = 0
        for request in self.dashboard_control.pending(limit=limit):
            payload = request.get("payload") if isinstance(request.get("payload"), dict) else {}
            action = str(request.get("action") or "")
            try:
                sender_id = str(payload.get("senderId") or "").strip()
                if action in {"list_models", "set_model"} and not sender_id:
                    raise ValueError("当前没有可用的微信会话")
                if action == "list_models":
                    result = {"ok": True, **self._dashboard_models(sender_id)}
                elif action == "set_model":
                    selected = self.controller.set_model(sender_id, payload.get("query"))
                    result = {"ok": True, "selected": selected, **self._dashboard_models(sender_id)}
                elif action == "set_model_defaults":
                    worker_model = str(payload.get("workerModel") or "").strip()
                    pro_model = str(payload.get("proModel") or "").strip()
                    if worker_model:
                        self.workers.default_model = worker_model
                    if pro_model:
                        self.workers.pro_model = pro_model
                    result = {
                        "ok": True,
                        "workerModel": self.workers.default_model,
                        "proModel": self.workers.pro_model,
                    }
                else:
                    raise ValueError(f"未知看板控制操作：{action}")
            except Exception as error:
                result = {"ok": False, "error": str(error)}
            self.dashboard_control.complete(request, result)
            processed += 1
        return processed

    def _ensure_outbox_thread(self) -> bool:
        with self._outbox_thread_lock:
            if self._outbox_thread is not None and self._outbox_thread.is_alive():
                return False
            self._outbox_thread = threading.Thread(
                target=self._outbox_loop,
                daemon=True,
                name="G4W-outbox",
            )
            self._outbox_thread.start()
            return True

    def _outbox_loop(self, stop_event=None):
        while stop_event is None or not stop_event.is_set():
            self._outbox_last_heartbeat = time.time()
            delay = 0.1
            try:
                if time.time() >= self._outbox_next_recovery:
                    recovered = self.outbox.recover_stale_sending(120)
                    if recovered:
                        print(f"[G4W] recovered {recovered} stale outbox delivery item(s)")
                    self._outbox_next_recovery = time.time() + 30
                self.deliver_outbox(limit=10, asynchronous_files=True)
            except Exception as error:
                self._outbox_last_error = str(error)[:500]
                self._outbox_last_error_at = time.time()
                self._outbox_error_count += 1
                print(f"[G4W] outbox loop error; retrying automatically: {error}")
                traceback.print_exc()
                delay = 1.0
            self._outbox_last_heartbeat = time.time()
            if stop_event is not None:
                stop_event.wait(delay)
            else:
                time.sleep(delay)

    def outbox_health(self) -> dict:
        thread = self._outbox_thread
        heartbeat_age = max(0.0, time.time() - self._outbox_last_heartbeat) if self._outbox_last_heartbeat else None
        return {
            "alive": bool(thread and thread.is_alive()),
            "lastHeartbeatAt": self._outbox_last_heartbeat,
            "heartbeatAgeSeconds": heartbeat_age,
            "lastError": self._outbox_last_error,
            "lastErrorAt": self._outbox_last_error_at,
            "errorCount": self._outbox_error_count,
        }

    def _poll_updates_loop(self):
        while True:
            try:
                for message in self.channel.get_updates():
                    if not message.get("attachmentPending"):
                        self._enqueue_inbound(message)
            except Exception as error:
                print(f"[G4W] get_updates error: {error}")
                time.sleep(2)

    def _enqueue_inbound(self, message: dict):
        message_text = format_inbound_message(message)
        self.conversations.bind(message["accountId"], message["senderId"], message.get("contextToken", ""))
        key = self.conversations.binding_key(message["accountId"], message["senderId"])
        sender_id = message["senderId"]
        command = parse_command(message_text)
        # Proactive content is always superseded by real user activity. Normal
        # replies from a live Round are handled after the intervention claim so
        # they can retain their original order.
        with self.delivery_lock:
            cancelled = self._cancel_legacy_checkins_for_sender(sender_id)
            cancelled.extend(self.outbox.cancel_superseded_for_sender(sender_id))
        for stale in cancelled:
            self._defer_cancelled_proactive(stale, "superseded by new user activity")
            print(
                "[G4W] cancelled stale proactive delivery "
                f"id={stale.get('id', '')} sender={sender_id} "
                f"age={max(0.0, time.time() - float(stale.get('createdAt', time.time()) or time.time())):.1f}s"
            )
        # A real user message supersedes any proactive check-in that was
        # queued during the preceding idle period but has not started yet.
        for binding_key in self.conversations.binding_keys_for_sender(sender_id):
            self.events.cancel_pending(binding_key, "system.checkin", "superseded by new user activity")
        if not self.checkins.status(key):
            if self.config.checkin_enabled:
                self.checkins.configure(
                    key, message["senderId"],
                    self.config.checkin_minimum_minutes,
                    self.config.checkin_maximum_minutes,
                )
            else:
                self.checkins.disable(key)
        self.wechat_maintenance.mark_user_message(
            sender_id, str(message.get("text") or ""), message.get("receivedAt", "")
        )
        if command and command[0] == "stop" and not message.get("savedAttachments"):
            result = self.controller.cancel_active(sender_id)
            with self.delivery_lock:
                stale_messages = self.outbox.defer_pending_for_sender(sender_id)
                for stale in stale_messages:
                    stale_text = build_effective_reply_text(stale.get("deferredPrefix", ""), stale.get("text", ""))
                    if stale_text:
                        self.deferred.add(
                            key,
                            sender_id,
                            stale_text,
                            kind=stale.get("deferredKind", "plain_reply"),
                        )
            self.outbox.prepare(
                key, sender_id, message.get("contextToken", ""), result["message"],
                dedupe_key=f"stop-reply:{message['accountId']}:{message['messageId']}",
                round_id=f"stop:{message['messageId']}", turn=1, round_final=True, source="command",
            )
            return
        session = self.controller.sessions.get(sender_id)
        parent_round_id = ""
        if session is not None:
            state_lock = getattr(session, "state_lock", None)
            if state_lock is not None:
                with state_lock:
                    parent_round_id = str(getattr(session, "active_round_id", "") or "")
            else:
                parent_round_id = str(getattr(session, "active_round_id", "") or "")
        arrived_while_busy = bool(parent_round_id)
        event_type = "wechat.command" if command and not message.get("savedAttachments") else "wechat.user_message"
        raw_user_text = str(message.get("text") or "").strip()
        dedupe_key = f"wechat.user_message:{message['accountId']}:{message['messageId']}"
        existing_event = self.events.find_by_dedupe_key(dedupe_key)
        if existing_event:
            return existing_event
        deferred_replies = []
        if not arrived_while_busy:
            with self.delivery_lock:
                stale_messages = self.outbox.defer_pending_for_sender(sender_id)
                for stale in stale_messages:
                    stale_text = build_effective_reply_text(stale.get("deferredPrefix", ""), stale.get("text", ""))
                    if stale_text:
                        self.deferred.add(
                            key,
                            sender_id,
                            stale_text,
                            kind=stale.get("deferredKind", "plain_reply"),
                        )
            deferred_replies = self.deferred.pop_all(sender_id)
        event = self.events.enqueue(
            event_type,
            key,
            {
                "text": message_text,
                "userText": raw_user_text,
                "messageId": message["messageId"],
                "receivedAt": message.get("receivedAt", ""),
                "attachments": message.get("savedAttachments") or [],
                "attachmentFailures": message.get("attachmentFailures") or [],
                "arrivedWhileBusy": arrived_while_busy,
                "parentRoundId": parent_round_id,
                "transcriptRecorded": bool(raw_user_text) if event_type == "wechat.user_message" else False,
                "deferredReplies": deferred_replies,
            },
            dedupe_key=dedupe_key,
        )
        if event_type == "wechat.user_message":
            self.conversations.append(
                sender_id,
                "User",
                raw_user_text,
                timestamp=message.get("receivedAt", ""),
                subtype="user-intervention" if arrived_while_busy else "",
                message_id=str(message.get("messageId") or ""),
                parent_round_id=parent_round_id,
            )

        intervention = (
            self.controller.try_intervene(
                sender_id,
                event,
                binding={"bindingKey": key, **(self.conversations.get_binding(key) or {})},
            )
            if event_type == "wechat.user_message" and arrived_while_busy
            else {"accepted": False}
        )
        accepted = bool(intervention.get("accepted"))
        active_round_id = str(intervention.get("roundId") or parent_round_id or "") if accepted else ""

        # Preserve the live Round's pending bubbles and files. Older unrelated
        # replies still become the original-style deferred prefix.
        if arrived_while_busy:
            with self.delivery_lock:
                if accepted:
                    self.outbox.retarget_round(
                        sender_id,
                        active_round_id,
                        key,
                        message.get("contextToken", ""),
                    )
                stale_messages = self.outbox.defer_pending_for_sender(
                    sender_id,
                    exclude_round_id=active_round_id,
                )
                for stale in stale_messages:
                    stale_text = build_effective_reply_text(stale.get("deferredPrefix", ""), stale.get("text", ""))
                    if stale_text:
                        self.deferred.add(
                            key,
                            sender_id,
                            stale_text,
                            kind=stale.get("deferredKind", "plain_reply"),
                        )
            deferred_replies = self.deferred.pop_all(sender_id)
        if accepted:
            if deferred_replies:
                self._append_round_deferred_prefix(active_round_id, format_deferred_reply_batch(deferred_replies))
            self.events.patch(event["id"], payload={
                "interventionAccepted": True,
                "interventionTargetTurn": intervention.get("targetTurn", 1),
            })
        elif arrived_while_busy and deferred_replies:
            self.events.patch(event["id"], payload={"deferredReplies": deferred_replies})

    def process_events(self, limit: int = 20):
        processed = 0
        while processed < limit:
            pending = self.events.pending(limit=200)
            interactive = [event for event in pending if event.get("type") in ("wechat.user_message", "wechat.command")]
            first = interactive[0] if interactive else (pending[0] if pending else None)
            if not first:
                return
            batch = [first]
            suppressed = []
            try:
                binding = self.conversations.get_binding(first["bindingKey"])
                if not binding:
                    raise RuntimeError(f"binding missing: {first['bindingKey']}")
                if first.get("type") == "wechat.user_message" and (first.get("payload") or {}).get("arrivedWhileBusy"):
                    queued = self.events.pending_for_binding(first["bindingKey"], limit=20)
                    batch = [event for event in queued if event.get("type") == "wechat.user_message" and (event.get("payload") or {}).get("arrivedWhileBusy")]
                    batch.sort(key=lambda event: (str((event.get("payload") or {}).get("receivedAt") or ""), event.get("createdAt", 0)))
                elif first.get("type") not in ("wechat.user_message", "wechat.command"):
                    queued = [
                        event for event in self.events.pending_for_binding(first["bindingKey"], limit=100)
                        if event.get("type") not in ("wechat.user_message", "wechat.command")
                    ]
                    batch, suppressed = self._coalesce_background_events(queued)
                    if not self.worker_turn.get(first["bindingKey"]):
                        kept = []
                        for event in batch:
                            event_type = str(event.get("type") or "")
                            if event_type in ("worker.progress_milestone", "worker.stalled"):
                                suppressed.append(event)
                            else:
                                kept.append(event)
                        batch = kept
                    for event in suppressed:
                        self.events.mark(event["id"], "done")
                command_events = [event for event in batch if event.get("type") == "wechat.command"]
                normal_events = [event for event in batch if event.get("type") != "wechat.command"]
                for event in command_events:
                    command_payload = event.get("payload") or {}
                    command_text = str(command_payload.get("text") or "")
                    self.conversations.append(
                        binding["senderId"], "User", command_text,
                        timestamp=command_payload.get("receivedAt", ""), message_id=str(event["id"]),
                    )
                    command_reply = self.commands.execute(binding, command_text)
                    prefix = format_deferred_reply_batch(command_payload.get("deferredReplies") or []) if command_payload.get("deferredReplies") else ""
                    if command_reply:
                        self.outbox.prepare(
                            first["bindingKey"], binding["senderId"], binding.get("contextToken", ""), command_reply,
                            dedupe_key=f"command-reply:{event['id']}", round_id=event["id"], turn=1,
                            round_final=True, source="command", deferred_prefix=prefix,
                        )
                    elif prefix:
                        self.outbox.prepare(
                            first["bindingKey"], binding["senderId"], binding.get("contextToken", ""), "",
                            dedupe_key=f"command-deferred:{event['id']}", round_id=event["id"], turn=1,
                            round_final=True, source="command", deferred_prefix=prefix,
                        )
                    self.events.mark(event["id"], "done")
                    self.checkins.touch_after_round(first["bindingKey"])
                if normal_events:
                    typing_factory = getattr(self.channel, "typing_keepalive", None)
                    typing_context = typing_factory(binding["senderId"], binding.get("contextToken", "")) if callable(typing_factory) else nullcontext()
                    with typing_context:
                        round_id = normal_events[0]["id"]
                        deferred_items = []
                        for event in normal_events:
                            deferred_items.extend((event.get("payload") or {}).get("deferredReplies") or [])
                        if deferred_items:
                            self._round_deferred_prefixes[round_id] = format_deferred_reply_batch(deferred_items)
                        reply = self.controller.handle_events(binding, normal_events, round_id=round_id)
                        user_intervened = self.controller.last_round_had_user_intervention(binding["senderId"])
                        if reply or user_intervened or any(event.get("type") == "wechat.user_message" for event in normal_events):
                            # session.run() has already written the real
                            # [ROUND END] marker before returning here.  Any
                            # visible Conductor reply (normal chat, check-in,
                            # Worker feedback, maintenance) restarts the idle
                            # timer from this completed round.
                            self.checkins.touch_after_round(first["bindingKey"])
                        default_delivery_kind = self._delivery_kind(normal_events)
                        delivery_kind = self.controller.last_delivery_kind(binding["senderId"], default_delivery_kind)
                        message_subtype = "" if user_intervened else self._message_subtype(normal_events)
                        event_ids = ":".join(event["id"] for event in normal_events)
                        final_turn = self.controller.last_turn(binding["senderId"])
                        self.outbox.prepare(
                            first["bindingKey"], binding["senderId"], binding.get("contextToken", ""), reply or "",
                            dedupe_key=f"event-reply:{event_ids}", round_id=round_id,
                            turn=final_turn, round_final=True,
                            source="conductor" if reply else "conductor-finalize",
                            deferred_kind=delivery_kind,
                            deferred_prefix=self._take_round_deferred_prefix(round_id),
                            cancel_on_user_activity=(delivery_kind == "checkin"),
                            expires_after_seconds=600 if delivery_kind == "checkin" else 0,
                            message_subtype=message_subtype,
                        )
                        # A file created in the final Turn has no later Turn
                        # boundary to release it. Queue the final text first,
                        # then release any remaining held files for this round.
                        self.controller.release_round_files(round_id, final_turn)
                        for event in normal_events:
                            self.events.mark(event["id"], "done")
            except Exception as error:
                traceback.print_exc()
                self._round_deferred_prefixes.pop(str((batch[0] if batch else first).get("id") or ""), None)
                for event in batch:
                    self.events.mark(event["id"], "failed", str(error))
            processed += len(batch)

    def deliver_outbox(self, limit: int = 10, asynchronous_files: bool = False):
        for _ in range(limit):
            with self.delivery_lock:
                message = self.outbox.claim_next_pending()
            if not message:
                return
            if asynchronous_files and message.get("kind", "text") == "file":
                threading.Thread(
                    target=self._deliver_file_claim,
                    args=(message,),
                    daemon=True,
                    name=f"G4W-file-{str(message.get('id') or '')[:8]}",
                ).start()
                continue
            if not self._deliver_claimed(message):
                return

    def _deliver_file_claim(self, message: dict) -> None:
        with self._file_delivery_slots:
            self._deliver_claimed(message)

    def _deliver_claimed(self, message: dict) -> bool:
        try:
            current = next((item for item in self.outbox.store.read().get("messages", []) if item.get("id") == message.get("id")), {})
            if current.get("status") != "sending":
                return True
            now = time.time()
            expires_at = float(current.get("expiresAt", 0) or 0)
            is_checkin = self._is_checkin_delivery(current)
            if is_checkin and not expires_at:
                expires_at = float(current.get("createdAt", 0) or 0) + 600
            latest_inbound_at = self.conversations.latest_inbound_at(message.get("senderId", ""))
            stale_reason = ""
            if expires_at and now >= expires_at:
                stale_reason = "proactive delivery expired before send"
            elif is_checkin and latest_inbound_at > float(current.get("createdAt", 0) or 0):
                stale_reason = "proactive delivery superseded by newer user activity"
            if stale_reason:
                self._defer_cancelled_proactive(message, stale_reason)
                self.outbox.mark_cancelled(message["id"], stale_reason)
                print(
                    "[G4W] deferred stale proactive delivery "
                    f"id={message.get('id', '')} sender={message.get('senderId', '')} reason={stale_reason}"
                )
                return True
            try:
                latest_binding = self.controller._binding_for_sender(message.get("senderId", ""))
            except (KeyError, AttributeError):
                latest_binding = self.conversations.get_binding(message["bindingKey"]) or {}
            latest_context_token = latest_binding.get("contextToken", "") or message.get("contextToken", "")
            if message.get("kind", "text") == "file":
                result = self.channel.send_file(
                    message["senderId"], Path(message["filePath"]), latest_context_token,
                    delivery_id=message["id"],
                )
            else:
                from ..agents.handlers import clean_visible_reply
                outgoing = str(message.get("text") or "")
                if str(message.get("source") or "").startswith("conductor"):
                    outgoing = clean_visible_reply(outgoing)
                outgoing = build_effective_reply_text(message.get("deferredPrefix", ""), outgoing)
                result = self.channel.send_text(
                    message["senderId"],
                    outgoing,
                    latest_context_token,
                    delivery_id=message["id"],
                    round_id=message.get("roundId", ""),
                    round_final=bool(message.get("roundFinal", True)),
                    source=message.get("source", "conductor"),
                    turn=message.get("turn", 1),
                    deferred_kind=message.get("deferredKind", "plain_reply"),
                    preserve_block=bool(message.get("deferredPrefix")),
                )
            self.outbox.mark_sent(message["id"], result if isinstance(result, dict) else {})
            if message.get("kind", "text") != "file" and isinstance(result, dict):
                delivered_text = str(result.get("deliveredText") or "").strip()
                if delivered_text:
                    delivered_chunks = result.get("deliveredChunks") or []
                    delivered_at = (delivered_chunks[-1].get("sentAt") if delivered_chunks else time.time())
                    try:
                        self.conversations.append(
                            message["senderId"], "Assistant", delivered_text,
                            timestamp=delivered_at, subtype=message.get("messageSubtype", ""),
                            message_id=str(message["id"]), parent_round_id=str(message.get("roundId") or ""),
                        )
                    except Exception as transcript_error:
                        print(f"[G4W] transcript append error after successful delivery: {transcript_error}")
            age = max(0.0, time.time() - float(message.get("createdAt", time.time()) or time.time()))
            if int(message.get("attempts", 0) or 0) > 0 or age >= 5:
                print(
                    "[G4W] delivered delayed outbox message "
                    f"id={message.get('id', '')} source={message.get('source', '')} "
                    f"sender={message.get('senderId', '')} attempts={message.get('attempts', 0)} age={age:.1f}s"
                )
            deferred_text = str((result or {}).get("deferredText") or "") if isinstance(result, dict) else ""
            if deferred_text:
                self.deferred.add(
                    message["bindingKey"], message["senderId"], deferred_text,
                    kind=message.get("deferredKind", "plain_reply"),
                )
            return True
        except Exception as error:
            self.outbox.mark_retry(message["id"], str(error))
            return False

    def _is_checkin_delivery(self, message: dict) -> bool:
        if message.get("cancelOnUserActivity") or message.get("deferredKind") == "checkin":
            return True
        round_id = str(message.get("roundId") or "")
        if not round_id:
            return False
        return any(
            event.get("id") == round_id and event.get("type") == "system.checkin"
            for event in self.events.store.read().get("events", [])
        )

    def _defer_cancelled_proactive(self, message: dict, reason: str = "") -> bool:
        if message.get("kind", "text") != "text":
            return False
        if not self._is_checkin_delivery(message):
            return False
        text = str(message.get("text") or "").strip()
        sender_id = str(message.get("senderId") or "")
        if not text or not sender_id:
            return False
        self.deferred.add(
            str(message.get("bindingKey") or ""),
            sender_id,
            text,
            kind=message.get("deferredKind", "checkin") or "checkin",
        )
        return True

    def _cancel_legacy_checkins_for_sender(self, sender_id: str) -> list[dict]:
        cancelled = []
        for item in self.outbox.store.read().get("messages", []):
            if item.get("senderId") != sender_id or item.get("status") not in ("pending", "held"):
                continue
            if item.get("cancelOnUserActivity") or not self._is_checkin_delivery(item):
                continue
            reason = "legacy check-in superseded by new user activity"
            self._defer_cancelled_proactive(item, reason)
            if self.outbox.mark_cancelled(item.get("id", ""), reason):
                cancelled.append({**item, "status": "cancelled"})
        return cancelled

    def _queue_intermediate(self, sender_id: str, text: str, round_id: str, turn: int, delivery_kind: str = "plain_reply"):
        from ..agents.handlers import clean_visible_reply
        body = clean_visible_reply(text)
        if not body:
            return
        binding = self.controller._binding_for_sender(sender_id)
        self.outbox.prepare(
            binding["bindingKey"], sender_id, binding.get("contextToken", ""), body,
            dedupe_key=f"intermediate:{round_id}:{turn}", round_id=round_id,
            turn=turn, round_final=False, source="conductor-intermediate",
            deferred_kind=delivery_kind,
            deferred_prefix=self._take_round_deferred_prefix(round_id),
            cancel_on_user_activity=(delivery_kind == "checkin"),
            expires_after_seconds=600 if delivery_kind == "checkin" else 0,
            message_subtype=("checkin" if delivery_kind == "checkin" else "worker-progress" if delivery_kind == "proactive_report" else ""),
        )

    def _take_round_deferred_prefix(self, round_id: str) -> str:
        return self._round_deferred_prefixes.pop(str(round_id or ""), "")

    def _append_round_deferred_prefix(self, round_id: str, text: str) -> None:
        target = str(round_id or "")
        value = str(text or "").strip()
        if not target or not value:
            return
        with self.delivery_lock:
            current = str(self._round_deferred_prefixes.get(target) or "").strip()
            self._round_deferred_prefixes[target] = "\n\n".join(part for part in (current, value) if part)

    @staticmethod
    def _delivery_kind(events: list[dict]) -> str:
        if any(event.get("type") == "wechat.user_message" for event in events):
            return "plain_reply"
        if any(str(event.get("type") or "").startswith("worker.") for event in events):
            return "proactive_report"
        if any(event.get("type") == "system.checkin" for event in events):
            return "checkin"
        return "system_reply"

    @staticmethod
    def _message_subtype(events: list[dict]) -> str:
        event_types = [str(event.get("type") or "") for event in events]
        if "system.checkin" in event_types:
            return "checkin"
        if "worker.completed" in event_types:
            return "worker-final"
        if any(value.startswith("worker.") for value in event_types):
            return "worker-progress"
        return ""

    @staticmethod
    def _coalesce_background_events(events: list[dict]) -> tuple[list[dict], list[dict]]:
        ordered = sorted(events, key=lambda event: event.get("createdAt", 0))
        completed = {}
        progress = {}
        stalled = {}
        checkins = []
        kept = []
        suppressed = []
        for event in ordered:
            event_type = str(event.get("type") or "")
            payload = event.get("payload") or {}
            worker_id = str(payload.get("workerId") or "")
            if event_type == "worker.completed" and worker_id:
                previous = completed.get(worker_id)
                if previous:
                    suppressed.append(previous)
                completed[worker_id] = event
            elif event_type == "worker.progress_milestone" and worker_id:
                previous = progress.get(worker_id)
                if previous:
                    old_milestone = int((previous.get("payload") or {}).get("milestone", 0) or 0)
                    new_milestone = int(payload.get("milestone", 0) or 0)
                    if new_milestone >= old_milestone:
                        suppressed.append(previous)
                        progress[worker_id] = event
                    else:
                        suppressed.append(event)
                else:
                    progress[worker_id] = event
            elif event_type == "worker.stalled" and worker_id:
                previous = stalled.get(worker_id)
                if previous:
                    suppressed.append(previous)
                stalled[worker_id] = event
            elif event_type == "system.checkin":
                checkins.append(event)
            else:
                kept.append(event)
        for worker_id, event in progress.items():
            if worker_id in completed:
                suppressed.append(event)
            else:
                kept.append(event)
        for worker_id, event in stalled.items():
            if worker_id in completed:
                suppressed.append(event)
            else:
                kept.append(event)
        kept.extend(completed.values())
        has_worker_event = any(str(event.get("type") or "").startswith("worker.") for event in kept)
        if checkins:
            if has_worker_event:
                suppressed.extend(checkins)
            else:
                kept.append(checkins[-1])
                suppressed.extend(checkins[:-1])
        return sorted(kept, key=lambda event: event.get("createdAt", 0)), suppressed


def format_inbound_message(message: dict) -> str:
    parts = []
    text = str(message.get("text") or "").strip()
    if text:
        parts.append(text)
    saved = message.get("savedAttachments") or []
    failed = message.get("attachmentFailures") or []
    if saved:
        lines = ["[微信附件已保存]"]
        for item in saved:
            label = item.get("sourceFileName") or item.get("fileName") or item.get("kind", "file")
            lines.append(f"- {item.get('kind', 'file')}: {label} | {item.get('absolutePath', '')}")
        parts.append("\n".join(lines))
    if failed:
        lines = ["[微信附件保存失败]"]
        for item in failed:
            lines.append(f"- {item.get('sourceFileName') or item.get('kind', 'file')}: {item.get('reason', 'unknown error')}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts) or "[收到一条无法解析的微信消息]"
