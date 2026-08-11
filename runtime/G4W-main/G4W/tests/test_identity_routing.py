import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from G4W.core.capabilities import CapabilityRegistry
from G4W.core.config import Config
from G4W.agents.controller import G4WController
from G4W.memory.conversation import ConversationStore
from G4W.memory.sop_catalog import SopCatalog
from G4W.core.service import G4WService


class FakeWorkers:
    def __init__(self):
        self.spawned = []
    def spawn(self, binding_key, sender_id, capability_id, task, lifecycle, model_tier=""):
        self.spawned.append((binding_key, sender_id, capability_id, task, lifecycle, model_tier))
        return {"id": "w1", "status": "running"}
    def list_for(self, binding_key): return []
    def get(self, worker_id): return {"id": worker_id, "senderId": "sender"}
    def detail(self, worker_id): return {"id": worker_id, "senderId": "sender", "runIndex": 1, "result": {}}
    def review(self, worker_id, run_index, decision, note): return {"id": worker_id, "runIndex": run_index, "reviewState": decision}
    def send(self, worker_id, message): return {"id": worker_id, "status": "running"}
    def stop(self, worker_id): return {"id": worker_id, "status": "cancelled"}


class FakeSession:
    prompts = []

    def __init__(self, controller, sender_id):
        self.sender_id = sender_id

    def run(self, prompt, event_context="", pending_review_workers=None, round_id="", user_message=False, received_at="", delivery_kind="plain_reply", binding=None):
        self.prompts.append((self.sender_id, prompt, event_context, set(pending_review_workers or []), dict(binding or {})))
        return "同一个总管会话的自然回复"


class FakeChannel:
    def __init__(self):
        self.sent = []

    def send_text(self, sender_id, text, context_token="", delivery_id="", **kwargs):
        self.sent.append((sender_id, text, context_token, delivery_id))
        return {"deliveredText": text, "deferredText": ""}


class FakeSupervision:
    def __init__(self): self.worker_id = ""
    def act(self, binding_key, sender_id, action, arguments): return {"session": {"state": "awaiting_selection"}, "chains": {}}
    def set_worker(self, sender_id, worker_id): self.worker_id = worker_id
    def status(self, sender_id): return {"session": {"state": "awaiting_selection", "workerId": self.worker_id}, "chains": {}}


class IdentityRoutingTests(unittest.TestCase):
    def setUp(self):
        self.package = Path(__file__).resolve().parents[1]

    def _registry(self, root: Path):
        return CapabilityRegistry.from_sop_root(self.package / "memory" / "sop", root / "compiled-capabilities.json")

    def test_conductor_prompt_has_no_ga_identity(self):
        with tempfile.TemporaryDirectory() as td:
            config = Config(state_dir=Path(td))
            conversations = ConversationStore(config.conversations_dir, config.memory_dir)
            registry = self._registry(Path(td))
            sop_catalog = SopCatalog(self.package / "memory" / "sop")
            controller = G4WController(config, conversations, registry, FakeWorkers(), sop_catalog=sop_catalog)
            prompt = controller.build_system_prompt("sender")
            self.assertIn("G4W", prompt)
            self.assertNotIn("物理级全能执行者", prompt)
            self.assertIn("你不是GenericAgent桌面人格", prompt)
            self.assertNotIn("Recent visible WeChat transcript", prompt)
            self.assertNotIn("Current event context", prompt)
            self.assertIn("[Memory] (G4W Shared Memory)", prompt)
            self.assertIn("# [Global Memory Insight]", prompt)
            self.assertNotIn("# G4W SOP导航", prompt)
            self.assertLess(prompt.index("[Memory] (G4W Shared Memory)"), prompt.index("# 用户长期记忆"))

    def test_registry_enforces_direct_worker_boundary(self):
        with tempfile.TemporaryDirectory() as td:
            registry = self._registry(Path(td))
            registry.require_route("reminder.manage", "direct")
            registry.require_route("worker.general", "worker")
            with self.assertRaises(PermissionError):
                registry.require_route("worker.general", "direct")

    def test_worker_tools_are_not_exposed_to_conductor_as_ga_tools(self):
        tools = json.loads((self.package / "agents" / "conductor_tools.json").read_text(encoding="utf-8"))
        names = {item["function"]["name"] for item in tools}
        self.assertNotIn("code_run", names)
        self.assertNotIn("file_write", names)
        self.assertIn("G4W_worker_spawn", names)
        self.assertIn("G4W_memory_search", names)
        self.assertNotIn("G4W_direct_execute", names)
        self.assertFalse(any(name.startswith("G4W_sop_") for name in names))
        self.assertTrue(
            all(
                name.startswith("G4W_worker_") or name == "G4W_memory_search"
                for name in names
            )
        )


    def test_worker_spawn_stays_on_bound_conversation(self):
        with tempfile.TemporaryDirectory() as td:
            config = Config(state_dir=Path(td))
            conversations = ConversationStore(config.conversations_dir, config.memory_dir)
            conversations.bind("account", "sender", "ctx")
            registry = self._registry(Path(td))
            workers = FakeWorkers()
            controller = G4WController(config, conversations, registry, workers)
            controller.spawn_worker("sender", "worker.weather", "查天气")
            self.assertEqual(workers.spawned[0][0], "account:sender")

    def test_sender_binding_fallback_prefers_most_recent_account(self):
        with tempfile.TemporaryDirectory() as td:
            config = Config(state_dir=Path(td))
            conversations = ConversationStore(config.conversations_dir, config.memory_dir)
            conversations.bind("old-account", "sender", "old-ctx")
            conversations.bind("new-account", "sender", "new-ctx")
            registry = self._registry(Path(td))
            controller = G4WController(config, conversations, registry, FakeWorkers())

            binding = controller._binding_for_sender("sender")

            self.assertEqual(binding["bindingKey"], "new-account:sender")
            self.assertEqual(binding["contextToken"], "new-ctx")

    def test_active_round_pins_binding_and_orders_text_file_final(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = Config(state_dir=root / "state", workspace_root=root)
            service = G4WService(config, channel=FakeChannel(), session_factory=FakeSession)
            service.conversations.bind("old-account", "sender", "old-ctx")
            current = service.conversations.bind("new-account", "sender", "new-ctx")
            current = {"bindingKey": "new-account:sender", **current}
            session = SimpleNamespace(
                state_lock=threading.RLock(),
                active_round_id="round-1",
                active_binding=current,
                active_delivery_kind="plain_reply",
                cancelled_rounds=set(),
                agent=SimpleNamespace(handler=SimpleNamespace(current_turn=1)),
            )
            service.controller.sessions["sender"] = session
            file_path = root / "timeline.png"
            file_path.write_bytes(b"png")

            queued = service.controller.execute_direct("sender", "file.send", {"path": str(file_path)})
            service.controller.emit_intermediate("sender", "Turn 1先回复", "round-1", 1)
            final = service.outbox.prepare(
                current["bindingKey"], "sender", "new-ctx", "Turn 2最终回复", "final-1",
                round_id="round-1", turn=2, round_final=True,
            )

            state = service.outbox.store.read()["messages"]
            self.assertTrue(queued["deliveryHeldUntilTurnComplete"])
            self.assertTrue(all(item["bindingKey"] == "new-account:sender" for item in state))
            first = service.outbox.next_pending()
            self.assertEqual(first["text"], "Turn 1先回复")
            service.outbox.mark_sent(first["id"])
            second = service.outbox.next_pending()
            self.assertEqual(second["id"], queued["deliveryId"])
            service.outbox.mark_sent(second["id"])
            self.assertEqual(service.outbox.next_pending()["id"], final["id"])

    def test_supervision_open_creates_persistent_supervisor_worker(self):
        with tempfile.TemporaryDirectory() as td:
            config = Config(state_dir=Path(td))
            conversations = ConversationStore(config.conversations_dir, config.memory_dir)
            conversations.bind("account", "sender", "ctx")
            workers = FakeWorkers()
            supervision = FakeSupervision()
            controller = G4WController(config, conversations, self._registry(Path(td)), workers, supervision=supervision)
            result = controller.execute_direct("sender", "supervision.manage", {"action": "open"})
            self.assertEqual(workers.spawned[0][2], "worker.supervisor")
            self.assertEqual(workers.spawned[0][4], "persistent")
            self.assertEqual(result["session"]["workerId"], "w1")

    def test_user_and_worker_events_return_to_same_conversation(self):
        with tempfile.TemporaryDirectory() as td:
            FakeSession.prompts = []
            config = Config(state_dir=Path(td))
            channel = FakeChannel()
            service = G4WService(config, channel=channel, session_factory=FakeSession)
            binding = service.conversations.bind("account", "sender", "ctx")
            key = service.conversations.binding_key("account", "sender")
            service.events.enqueue("wechat.user_message", key, {"text": "帮我查天气", "messageId": "m1"})
            service.events.enqueue("worker.completed", key, {"workerId": "w1", "result": {"status": "completed", "summary": "晴"}})

            service.process_events(limit=10)
            service.deliver_outbox(limit=10)

            self.assertEqual([item[0] for item in FakeSession.prompts], [binding["senderId"], binding["senderId"]])
            self.assertEqual(len(channel.sent), 2)
            self.assertTrue(all(item[0] == "sender" and item[2] == "ctx" for item in channel.sent))
            transcript = service.conversations.transcript_path("sender").read_text(encoding="utf-8")
            self.assertNotIn('"summary": "晴"', transcript)
            self.assertEqual(transcript.count("Assistant:"), 2)

    def test_worker_review_is_scoped_to_owned_worker(self):
        with tempfile.TemporaryDirectory() as td:
            config = Config(state_dir=Path(td))
            conversations = ConversationStore(config.conversations_dir, config.memory_dir)
            conversations.bind("account", "sender", "ctx")
            registry = self._registry(Path(td))
            controller = G4WController(config, conversations, registry, FakeWorkers())
            result = controller.review_worker("sender", "w1", 1, "accept", "ok")
            self.assertEqual(result["reviewState"], "accept")

    def test_due_reminder_wakes_same_conversation_and_uses_outbox(self):
        with tempfile.TemporaryDirectory() as td:
            FakeSession.prompts = []
            config = Config(state_dir=Path(td))
            channel = FakeChannel()
            service = G4WService(config, channel=channel, session_factory=FakeSession)
            service.conversations.bind("account", "sender", "ctx")
            key = service.conversations.binding_key("account", "sender")
            service.schedules.create(key, "sender", "喝水", time.time() - 1)
            service.schedules.emit_due(service.events)
            service.process_events(limit=10)
            service.deliver_outbox(limit=10)
            self.assertEqual(len(channel.sent), 1)
            self.assertIn("Scheduled system events", FakeSession.prompts[0][2])

    def test_background_queue_keeps_latest_progress_and_drops_it_on_completion(self):
        events = [
            {"id": "p5", "type": "worker.progress_milestone", "createdAt": 1, "payload": {"workerId": "w1", "milestone": 5}},
            {"id": "p10", "type": "worker.progress_milestone", "createdAt": 2, "payload": {"workerId": "w1", "milestone": 10}},
            {"id": "check1", "type": "system.checkin", "createdAt": 3, "payload": {"fireIndex": 1}},
            {"id": "done", "type": "worker.completed", "createdAt": 4, "payload": {"workerId": "w1", "runIndex": 1}},
            {"id": "check2", "type": "system.checkin", "createdAt": 5, "payload": {"fireIndex": 2}},
        ]
        kept, suppressed = G4WService._coalesce_background_events(events)
        self.assertEqual([event["id"] for event in kept], ["done"])
        self.assertEqual({event["id"] for event in suppressed}, {"p5", "p10", "check1", "check2"})

    def test_deferred_worker_report_is_prefixed_to_next_user_reply(self):
        with tempfile.TemporaryDirectory() as td:
            FakeSession.prompts = []
            config = Config(state_dir=Path(td))
            channel = FakeChannel()
            service = G4WService(config, channel=channel, session_factory=FakeSession)
            service.conversations.bind("account", "sender", "old-ctx")
            key = service.conversations.binding_key("account", "sender")
            service.deferred.add(key, "sender", "Worker已经到Turn 10", kind="proactive_report")
            service._enqueue_inbound({
                "accountId": "account", "senderId": "sender", "contextToken": "new-ctx",
                "messageId": "new-message", "text": "现在怎么样了", "receivedAt": "2026-07-15T12:00:00+08:00",
                "savedAttachments": [], "attachmentFailures": [],
            })
            service.process_events(limit=10)
            service.deliver_outbox(limit=10)
            self.assertEqual(len(channel.sent), 1)
            sent_text = channel.sent[0][1]
            self.assertIn("===== 期间模型主动汇报 =====", sent_text)
            self.assertIn("Worker已经到Turn 10", sent_text)
            self.assertIn("===== 本轮模型回复 =====", sent_text)
            self.assertTrue(sent_text.endswith("同一个总管会话的自然回复"))

    def test_inbound_moves_stale_outbox_text_into_next_reply(self):
        with tempfile.TemporaryDirectory() as td:
            FakeSession.prompts = []
            config = Config(state_dir=Path(td))
            channel = FakeChannel()
            service = G4WService(config, channel=channel, session_factory=FakeSession)
            binding = service.conversations.bind("account", "sender", "old-ctx")
            stale = service.outbox.prepare(
                binding["accountId"] + ":" + binding["senderId"], "sender", "old-ctx",
                "上一轮没有送达", "stale-message", deferred_kind="system_reply",
            )
            service.outbox.mark_retry(stale["id"], "expired context token")

            service._enqueue_inbound({
                "accountId": "account", "senderId": "sender", "contextToken": "new-ctx",
                "messageId": "new-message", "text": "新消息", "receivedAt": "2026-07-15T12:00:00+08:00",
                "savedAttachments": [], "attachmentFailures": [],
            })
            service.process_events(limit=10)
            service.deliver_outbox(limit=10)

            self.assertEqual(len(channel.sent), 1)
            self.assertEqual(channel.sent[0][2], "new-ctx")
            self.assertIn("===== 期间模型主动联系 =====", channel.sent[0][1])
            self.assertIn("上一轮没有送达", channel.sent[0][1])
            state = service.outbox.store.read()["messages"]
            stale_state = next(item for item in state if item["id"] == stale["id"])
            self.assertEqual(stale_state["status"], "deferred")

    def test_inbound_cancels_stale_checkin_across_account_bindings(self):
        with tempfile.TemporaryDirectory() as td:
            FakeSession.prompts = []
            config = Config(state_dir=Path(td))
            channel = FakeChannel()
            service = G4WService(config, channel=channel, session_factory=FakeSession)
            old = service.conversations.bind("old-account", "sender", "old-ctx")
            stale = service.outbox.prepare(
                "old-account:sender", "sender", "old-ctx", "过期的主动问候", "stale-checkin",
                deferred_kind="checkin", cancel_on_user_activity=True, expires_after_seconds=600,
            )
            service.outbox.mark_retry(stale["id"], "temporary send failure")

            service._enqueue_inbound({
                "accountId": "new-account", "senderId": "sender", "contextToken": "new-ctx",
                "messageId": "new-message", "text": "我回来了", "receivedAt": "2026-07-16T10:56:49+08:00",
                "savedAttachments": [], "attachmentFailures": [],
            })

            state = service.outbox.store.read()["messages"]
            stale_state = next(item for item in state if item["id"] == stale["id"])
            self.assertEqual(stale_state["status"], "cancelled")
            event = service.events.store.read()["events"][0]
            self.assertEqual(service.deferred.count("sender"), 0)
            self.assertEqual(event["payload"]["deferredReplies"][0]["text"], "过期的主动问候")
            self.assertGreater(service.conversations.latest_inbound_at("sender"), float(stale["createdAt"]))

            service.process_events(limit=10)
            service.deliver_outbox(limit=10)
            self.assertEqual(len(channel.sent), 1)
            self.assertIn("===== 期间模型主动联系 =====", channel.sent[0][1])
            self.assertIn("过期的主动问候", channel.sent[0][1])

    def test_inbound_also_cancels_legacy_checkin_without_new_outbox_flags(self):
        with tempfile.TemporaryDirectory() as td:
            config = Config(state_dir=Path(td))
            channel = FakeChannel()
            service = G4WService(config, channel=channel, session_factory=FakeSession)
            service.conversations.bind("old-account", "sender", "old-ctx")
            event = service.events.enqueue("system.checkin", "old-account:sender", {})
            stale = service.outbox.prepare(
                "old-account:sender", "sender", "old-ctx", "旧版本主动问候", "legacy-checkin",
                round_id=event["id"], deferred_kind="system_reply",
            )

            service._enqueue_inbound({
                "accountId": "new-account", "senderId": "sender", "contextToken": "new-ctx",
                "messageId": "new-message", "text": "新消息", "receivedAt": "2026-07-16T11:00:00+08:00",
                "savedAttachments": [], "attachmentFailures": [],
            })

            state = service.outbox.store.read()["messages"]
            self.assertEqual(next(item for item in state if item["id"] == stale["id"])["status"], "cancelled")
            event = service.events.store.read()["events"][1]
            self.assertEqual(service.deferred.count("sender"), 0)
            self.assertEqual(event["payload"]["deferredReplies"][0]["text"], "旧版本主动问候")

            service.process_events(limit=10)
            service.deliver_outbox(limit=10)
            self.assertEqual(len(channel.sent), 1)
            self.assertIn("===== 期间模型主动联系 =====", channel.sent[0][1])
            self.assertIn("旧版本主动问候", channel.sent[0][1])

    def test_expired_checkin_is_not_sent_or_written_to_transcript(self):
        with tempfile.TemporaryDirectory() as td:
            config = Config(state_dir=Path(td))
            channel = FakeChannel()
            service = G4WService(config, channel=channel, session_factory=FakeSession)
            service.conversations.bind("account", "sender", "ctx")
            item = service.outbox.prepare(
                "account:sender", "sender", "ctx", "已经过期的问候", "expired-checkin",
                deferred_kind="checkin", cancel_on_user_activity=True, expires_after_seconds=600,
            )
            service.outbox.store.update(lambda state: next(
                value.update({"expiresAt": time.time() - 1})
                for value in state["messages"] if value["id"] == item["id"]
            ))

            service.deliver_outbox(limit=10)

            self.assertEqual(channel.sent, [])
            state = service.outbox.store.read()["messages"]
            self.assertEqual(next(value for value in state if value["id"] == item["id"])["status"], "cancelled")
            self.assertFalse(service.conversations.transcript_path("sender").exists())

    def test_delivery_refreshes_context_token_from_current_binding(self):
        with tempfile.TemporaryDirectory() as td:
            config = Config(state_dir=Path(td))
            channel = FakeChannel()
            service = G4WService(config, channel=channel, session_factory=FakeSession)
            service.conversations.bind("account", "sender", "old-ctx")
            key = service.conversations.binding_key("account", "sender")
            service.outbox.prepare(key, "sender", "old-ctx", "待发送回复", "pending")
            service.conversations.bind("account", "sender", "new-ctx")

            service.deliver_outbox(limit=10)

            self.assertEqual(len(channel.sent), 1)
            self.assertEqual(channel.sent[0][2], "new-ctx")


if __name__ == "__main__":
    unittest.main()
