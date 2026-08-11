import json
import tempfile
import types
import unittest
from pathlib import Path

from G4W.core.cache_metrics import CacheMetricsStore
from G4W.agents.input_capture import InputCaptureStore
from G4W.core.capabilities import CapabilityRegistry
from G4W.core.config import Config
from G4W.agents import ga_adapter
from G4W.agents.ga_adapter import load_ga_tool_schema, mark_tool_ownership, merge_tool_schemas, resolve_model
from G4W.core.service import G4WService
from G4W.core.storage import EventStore
from G4W.agents.workers import WorkerManager


PACKAGE = Path(__file__).resolve().parents[1]


def test_registry(root: Path):
    return CapabilityRegistry.from_sop_root(PACKAGE / "memory" / "sop", root / "compiled-capabilities.json")


class FakeChannel:
    def __init__(self):
        self.sent = []

    def send_text(self, sender_id, text, context_token="", delivery_id="", **kwargs):
        self.sent.append((sender_id, text, delivery_id))
        return {"deliveredText": text, "deferredText": ""}


class ActiveSession:
    def __init__(self, round_id="round-active"):
        self.active_round_id = round_id
        self.cancelled = False

    def cancel(self):
        self.cancelled = True
        return True


class AlignmentTests(unittest.TestCase):
    def test_conductor_default_turn_limit_matches_ga_task_mode(self):
        self.assertEqual(Config(state_dir=Path("state")).conductor_max_turns, 180)

    def test_input_capture_is_opt_in_and_uses_round_turn_directories(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            captures = InputCaptureStore(root / "input.json", root / "conversations")
            payload = {
                "turn": 2,
                "model": "deepseek-v4-flash",
                "systemPrompt": "system",
                "systemFingerprint": "abc",
                "tools": [{"name": "file_read"}],
                "historyBeforeCall": [{"role": "user", "content": "old"}],
                "messages": [{"role": "user", "content": "current"}],
                "canonicalMessages": [{"role": "system", "content": "system"}],
                "roundContext": {
                    "bindingKey": "account:sender", "roundId": "round-1", "startedAtLocal": "120001",
                    "isUserMessage": True, "pureUserMessage": "你好", "constructedPrompt": "完整构造",
                },
            }
            self.assertEqual(captures.save("sender", payload), "")
            captures.set("account:sender", True)
            target = Path(captures.save("sender", payload))
            self.assertTrue(target.is_file())
            self.assertEqual(target.name, "turn02.json")
            self.assertEqual(target.parent.name, "inputs")
            self.assertEqual(target.parent.parent.name, "round-1")
            saved = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(saved["schemaVersion"], 3)
            self.assertEqual(saved["contextSources"]["pureUserMessage"], "你好")
            self.assertEqual(saved["submittedRequest"]["system"], "system")
            self.assertEqual(saved["submittedRequest"]["tools"][0]["name"], "file_read")
            self.assertEqual(saved["submittedRequest"]["history"][0]["content"], "old")
            self.assertEqual(saved["submittedRequest"]["currentMessages"][0]["content"], "current")
            self.assertEqual(saved["requestLayout"]["stableComponents"], ["system", "tools", "history的既有前缀"])
            self.assertNotIn("canonicalMessages", saved)
            self.assertNotIn("controllerSystemPrompt", saved)
            self.assertNotIn("constructedPrompt", json.dumps(saved, ensure_ascii=False))

    def test_large_worker_completion_uses_reference_and_review_returns_ack(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manager = WorkerManager(root / "workers", test_registry(root), EventStore(root / "events.json"))
            worker_dir = root / "workers" / "worker-large"
            worker_dir.mkdir(parents=True)
            result = {"status": "completed", "summary": "完成", "details": {"text": "x" * 5000}}
            (worker_dir / "result-1.json").write_text(json.dumps(result), encoding="utf-8")

            def seed(state):
                state["workers"]["worker-large"] = {
                    "id": "worker-large", "bindingKey": "account:sender", "senderId": "sender",
                    "capabilityId": "worker.research", "lifecycle": "ephemeral", "status": "completed",
                    "task": "研究", "runIndex": 1, "dir": str(worker_dir), "result": result,
                    "review": {"runIndex": 1, "state": "pending"},
                }
            manager.state.update(seed)

            report = manager.completion_report("worker-large")
            self.assertFalse(report["resultInline"])
            self.assertNotIn("result", report)
            self.assertEqual(report["resultFile"], str(worker_dir / "result-1.json"))
            review = manager.review("worker-large", 1, "accept", "ok")
            self.assertEqual(review, {"ok": True, "workerId": "worker-large", "runIndex": 1, "decision": "accept"})

    def setUp(self):
        self.package = Path(__file__).resolve().parents[1]

    def test_full_ga_tools_are_merged_with_conductor_tools(self):
        G4W_tools = json.loads((self.package / "agents" / "conductor_tools.json").read_text(encoding="utf-8"))
        merged = merge_tool_schemas(load_ga_tool_schema("deepseek-v4-flash"), G4W_tools)
        names = [(item.get("function") or {}).get("name") for item in merged]
        for required in (
            "code_run", "file_read", "file_write", "file_patch", "web_scan", "web_execute_js",
            "update_working_checkpoint", "ask_user", "start_long_term_update", "G4W_worker_spawn",
        ):
            self.assertIn(required, names)
        self.assertEqual(len(names), len(set(names)))

    def test_tool_schema_groups_G4W_before_ga_with_visible_ownership(self):
        G4W_tools = json.loads((self.package / "agents" / "conductor_tools.json").read_text(encoding="utf-8"))
        merged = merge_tool_schemas(
            mark_tool_ownership(G4W_tools, "G4W原生/控制平面"),
            mark_tool_ownership(load_ga_tool_schema("deepseek-v4-flash"), "GA借用/执行平面"),
        )
        names = [(item.get("function") or {}).get("name") for item in merged]
        first_ga = names.index("code_run")
        self.assertGreater(first_ga, 0)
        for item in merged[:first_ga]:
            self.assertTrue((item.get("function") or {}).get("description", "").startswith("[G4W原生/控制平面]"))
        for item in merged[first_ga:]:
            self.assertTrue((item.get("function") or {}).get("description", "").startswith("[GA借用/执行平面]"))

    def test_input_capture_history_is_empty_on_turn_one_and_kept_inside_same_round(self):
        captured = []
        agent = types.SimpleNamespace(
            G4W_input_sink=captured.append,
            G4W_input_turn=0,
            G4W_input_context={"roundId": "round-1"},
            G4W_system_fingerprint="controller",
        )
        client = types.SimpleNamespace(backend=types.SimpleNamespace(history=[], model="deepseek-v4-flash"))
        ga_adapter._thread_context.agent = agent
        try:
            ga_adapter._capture_input(client, [{"role": "system", "content": "system"}, {"role": "user", "content": "current"}], [])
            client.backend.history = [{"role": "assistant", "content": [{"type": "tool_use", "id": "tool-1"}]}]
            ga_adapter._capture_input(client, [{"role": "user", "content": "tool result"}], [])
        finally:
            ga_adapter._thread_context.agent = None

        self.assertEqual(captured[0]["historyBeforeCall"], [])
        self.assertEqual(captured[1]["historyBeforeCall"], client.backend.history)

    def test_native_input_capture_keeps_effective_full_system_after_turn_one(self):
        captured = []
        agent = types.SimpleNamespace(
            G4W_input_sink=captured.append,
            G4W_input_turn=0,
            G4W_input_context={"roundId": "round-1"},
            G4W_system_fingerprint="controller",
        )
        client = ga_adapter.llmcore.NativeToolClient.__new__(ga_adapter.llmcore.NativeToolClient)
        thinking = client._thinking_prompt()
        full_system = f"G4W完整人格\n\n{thinking}"
        client.backend = types.SimpleNamespace(history=[], model="deepseek-v4-flash", system=thinking)
        ga_adapter._thread_context.agent = agent
        try:
            ga_adapter._capture_input(
                client,
                [{"role": "system", "content": "G4W完整人格"}, {"role": "user", "content": "第一轮"}],
                [],
            )
            client.backend.system = full_system
            client.backend.history = [{"role": "user", "content": [{"type": "text", "text": "第一轮"}]}]
            ga_adapter._capture_input(client, [{"role": "user", "content": "工具结果"}], [])
        finally:
            ga_adapter._thread_context.agent = None

        self.assertEqual(captured[0]["systemPrompt"], full_system)
        self.assertEqual(captured[1]["systemPrompt"], full_system)
        self.assertEqual(captured[0]["systemFingerprint"], captured[1]["systemFingerprint"])

    def test_model_resolution_prefers_names_not_index_zero(self):
        clients = [
            types.SimpleNamespace(backend=types.SimpleNamespace(model="deepseek-v4-pro", name="pro")),
            types.SimpleNamespace(backend=types.SimpleNamespace(model="deepseek-v4-flash", name="flash")),
        ]
        agent = types.SimpleNamespace(llmclients=clients)
        self.assertEqual(resolve_model(agent, "flash", 0)["index"], 1)
        self.assertEqual(resolve_model(agent, "deepseek-v4-pro", 1)["index"], 0)

    def test_worker_defaults_flash_and_persistent_model_is_sticky(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            events = EventStore(root / "events.json")
            manager = WorkerManager(root / "workers", test_registry(root), events)
            manager._start = lambda item, task: None
            new_worker = manager.spawn("a:s", "sender", "worker.weather", "weather")
            self.assertEqual(new_worker["modelTier"], "flash")
            persistent = {
                "id": "persistent", "bindingKey": "a:s", "senderId": "sender", "capabilityId": "worker.l4",
                "lifecycle": "persistent", "status": "sleeping", "task": "old", "runIndex": 1,
                "dir": str(root / "workers" / "persistent"), "modelTier": "pro", "modelName": "deepseek-v4-pro",
            }
            manager.state.update(lambda state: state.setdefault("workers", {}).update({"persistent": persistent}))
            manager.spawn("a:s", "sender", "worker.l4", "continue")
            self.assertEqual(manager.get("persistent")["modelTier"], "pro")
            manager.spawn("a:s", "sender", "worker.l4", "switch", model_tier="flash")
            self.assertEqual(manager.get("persistent")["modelTier"], "flash")

    def test_worker_progress_emits_every_crossed_five_turn_milestone(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            events = EventStore(root / "events.json")
            manager = WorkerManager(root / "workers", test_registry(root), events)
            worker_dir = root / "workers" / "w"
            worker_dir.mkdir(parents=True)
            (worker_dir / "progress.json").write_text(json.dumps({"turn": 11, "summary": "working"}), encoding="utf-8")
            item = {
                "id": "w", "bindingKey": "a:s", "senderId": "sender", "capabilityId": "worker.weather",
                "lifecycle": "ephemeral", "status": "running", "runIndex": 1, "dir": str(worker_dir),
                "progressReporting": True, "lastProgressMilestone": 0,
            }
            manager.state.update(lambda state: state.setdefault("workers", {}).update({"w": item}))
            emitted = manager.scan_progress_milestones(5)
            self.assertEqual([item["milestone"] for item in emitted], [5, 10])
            self.assertEqual(manager.scan_progress_milestones(5), [])
            pending = [event for event in events.store.read()["events"] if event["type"] == "worker.progress_milestone"]
            self.assertEqual([event["payload"]["milestone"] for event in pending], [5, 10])

    def test_stop_cancels_only_active_conductor_round(self):
        with tempfile.TemporaryDirectory() as td:
            service = G4WService(Config(state_dir=Path(td)), channel=FakeChannel(), session_factory=lambda *_: None)
            service.conversations.bind("account", "sender", "ctx")
            session = ActiveSession()
            service.controller.sessions["sender"] = session
            service.outbox.prepare("account:sender", "sender", "ctx", "middle", "middle", round_id="round-active", source="conductor-intermediate")
            service.outbox.prepare("account:sender", "sender", "ctx", "worker stays", "worker", round_id="worker-round", source="worker")
            result = service.controller.cancel_active("sender")
            self.assertTrue(result["cancelled"])
            self.assertTrue(session.cancelled)
            messages = {item["dedupeKey"]: item for item in service.outbox.store.read()["messages"]}
            self.assertEqual(messages["middle"]["status"], "cancelled")
            self.assertEqual(messages["worker"]["status"], "pending")

    def test_cache_metrics_detect_stable_prefix_and_model_changes(self):
        with tempfile.TemporaryDirectory() as td:
            metrics = CacheMetricsStore(Path(td) / "cache.json")
            for index in range(25):
                metrics.record("sender", {
                    "inputTokens": 1000, "cachedTokens": 950, "ratio": 0.95,
                    "model": "deepseek-v4-flash", "systemFingerprint": "stable",
                    "cachePhase": "round_first" if index % 2 == 0 else "tool_turn",
                    "source": "wechat.user_message" if index % 3 else "G4W.internal_event",
                })
            status = metrics.status("sender")
            self.assertGreaterEqual(status["rollingRatio"], 0.9)
            self.assertEqual(status["rollingSamples"], 20)
            self.assertGreaterEqual(status["firstTurn"]["ratio"], 0.9)
            self.assertGreaterEqual(status["toolTurn"]["ratio"], 0.9)
            self.assertGreater(status["userMessage"]["samples"], 0)
            self.assertGreater(status["internalEvent"]["samples"], 0)
            self.assertEqual(status["invalidationReason"], "")
            metrics.record("sender", {
                "inputTokens": 1000, "cachedTokens": 0, "ratio": 0,
                "model": "deepseek-v4-pro", "systemFingerprint": "stable",
            })
            self.assertEqual(metrics.status("sender")["invalidationReason"], "model_changed")


if __name__ == "__main__":
    unittest.main()
