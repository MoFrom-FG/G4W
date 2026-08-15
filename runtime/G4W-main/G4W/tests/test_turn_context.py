import json
import queue
import tempfile
import types
import unittest
from pathlib import Path

from G4W.agents.controller import G4WController, ConductorSession
from G4W.agents.handlers import clean_visible_reply
from G4W.agents.handlers import ConductorHandler
from agent_loop import StepOutcome
from G4W.agents.turn_progress import TurnProgressStore
from G4W.memory.sop_catalog import SopCatalog


class TurnAndContextTests(unittest.TestCase):
    def test_protocol_markers_are_not_visible(self):
        self.assertEqual(clean_visible_reply("LLM Running (Turn 2) ...\n\n让助手看看～"), "让助手看看～")
        self.assertEqual(clean_visible_reply("Turn 3 ...\n[ROUND END]"), "")
        self.assertEqual(clean_visible_reply("<silent/>"), "")

    def test_history_archive_envelope_is_never_exposed(self):
        fake = "长回复已归档：D:\\missing\\assistant-replies\\fake.md\n摘要：内部预览"
        self.assertEqual(clean_visible_reply(fake), "内部预览")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "assistant-replies" / "reply.md"
            path.parent.mkdir(parents=True)
            path.write_text("# Assistant Reply\n\n## Reply\n\n真正的微信回复\n", encoding="utf-8")
            archived = f"长回复已归档：{path}\n摘要：预览"
            self.assertEqual(clean_visible_reply(archived), "真正的微信回复")

    def test_turn_progress_is_persistent_and_defaults_on(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "turn.json"
            first = TurnProgressStore(path)
            self.assertTrue(first.get("account:sender"))
            first.set("account:sender", False)
            self.assertFalse(TurnProgressStore(path).get("account:sender"))
            self.assertIn("已关闭", first.status_text("account:sender"))

    def test_oversized_conductor_tool_result_is_archived_and_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            conversation = Path(td) / "conversation"
            handler = ConductorHandler.__new__(ConductorHandler)
            handler.sender_id = "sender"
            handler.current_turn = 3
            handler.parent = types.SimpleNamespace(
                G4W_input_context={"roundId": "round-1"},
            )
            handler.controller = types.SimpleNamespace(
                conversations=types.SimpleNamespace(conversation_dir=lambda sender_id: conversation),
            )

            bounded = handler._bound_tool_outcome(
                "web_scan", {"_tool_num": 1}, StepOutcome("x" * 12000, next_prompt="继续"),
            )

            self.assertLess(len(str(bounded.data)), 9000)
            self.assertIn("工具结果过长", bounded.data)
            archived = list((conversation / "runtime" / "tool-results").rglob("*.txt"))
            self.assertEqual(len(archived), 1)
            self.assertEqual(len(archived[0].read_text(encoding="utf-8").strip()), 12000)

    def test_sop_discovery_uses_catalog_files_not_conductor_tools(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "sop"
            root.mkdir(parents=True)
            (root / "global_mem_insight.txt").write_text("# Index\n\n- `demo` → `demo_sop.md`（示例）\n", encoding="utf-8")
            (root / "demo_sop.md").write_text("# Demo\n", encoding="utf-8")
            catalog = SopCatalog(root)
            self.assertTrue(catalog.read("demo_sop.md")["ok"])
            self.assertFalse(hasattr(ConductorHandler, "do_G4W_sop_read"))

    def test_conductor_long_term_update_uses_G4W_l0_sop(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "sop"
            root.mkdir(parents=True)
            (root / "global_mem_insight.txt").write_text(
                "# Index\n\nL0: memory_management_sop\n",
                encoding="utf-8",
            )
            (root / "global_mem.txt").write_text("# L2\n", encoding="utf-8")
            (root / "memory_management_sop.md").write_text("# L0\n\n只沉淀验证经验。\n", encoding="utf-8")
            handler = ConductorHandler.__new__(ConductorHandler)
            handler.sender_id = "sender"
            handler.controller = types.SimpleNamespace(sop_catalog=SopCatalog(root))

            outcome = handler.do_start_long_term_update({}, None)

            self.assertEqual(outcome.data["memoryLayer"], "G4W-sop")
            self.assertIn("只沉淀验证经验", outcome.next_prompt)
            self.assertNotIn("request_l4", outcome.next_prompt)

    def test_sop_round_requires_l0_and_demo_folder_without_l1_parsing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "sop"
            root.mkdir(parents=True)
            (root / "global_mem_insight.txt").write_text("# Index\n", encoding="utf-8")
            (root / "global_mem.txt").write_text("# L2\n", encoding="utf-8")
            (root / "memory_management_sop.md").write_text("# L0\n", encoding="utf-8")
            handler = ConductorHandler.__new__(ConductorHandler)
            handler.sender_id = "sender"
            handler.cwd = str(root)
            handler.parent = types.SimpleNamespace(G4W_input_context={"roundId": "round-sop"})
            handler.controller = types.SimpleNamespace(sop_catalog=SopCatalog(root))
            handler._sop_state_round = ""
            handler._sop_before = {}
            handler._sop_management_ready = False
            handler._ensure_sop_round_state()

            misplaced = root / "native" / "test_demo_sop.md"
            misplaced.parent.mkdir(parents=True)
            misplaced.write_text("# 测试SOP\n\n测试演示。\n", encoding="utf-8")
            self.assertIn("没有调用start_long_term_update", handler._sop_completion_issue())

            handler._sop_management_ready = True
            self.assertIn("demo/<主题>", handler._sop_completion_issue())
            demo = root / "demo" / "sample" / "test_demo_sop.md"
            demo.parent.mkdir(parents=True)
            misplaced.replace(demo)
            self.assertEqual(handler._sop_completion_issue(), "")

            formal = root / "native" / "sample" / "sample_sop.md"
            formal.parent.mkdir(parents=True)
            formal.write_text("# Sample\n", encoding="utf-8")
            self.assertEqual(handler._sop_completion_issue(), "")

    def test_completed_tool_turn_is_emitted_before_final_reply(self):
        with tempfile.TemporaryDirectory() as td:
            emitted = []

            class Conversations:
                @staticmethod
                def sync_clean_history(*args, **kwargs):
                    return {"messages": [], "mode": "unchanged", "userRounds": 0, "messageCount": 0}

            controller = types.SimpleNamespace(
                config=types.SimpleNamespace(
                    conductor_history_max_messages=48,
                    conductor_history_max_chars=60000,
                    workspace_root=Path(td),
                    long_user_prompt_chars=1500,
                ),
                conversations=Conversations(),
                turn_enabled=lambda sender_id: True,
                emit_intermediate=lambda sender_id, text, round_id, turn: emitted.append((text, round_id, turn)),
            )
            response_queue = queue.Queue()
            response_queue.put({"next": "", "turn": 1, "outputs": ["让助手看看～\n<summary>内部</summary>"]})
            response_queue.put({"next": "", "turn": 2, "outputs": ["让助手看看～\n<summary>内部</summary>", "最终回复"]})
            response_queue.put({"done": "最终回复", "turn": 2, "outputs": ["让助手看看～", "最终回复"]})
            backend = types.SimpleNamespace(history=[])
            agent = types.SimpleNamespace(
                llmclient=types.SimpleNamespace(backend=backend),
                put_task=lambda *args, **kwargs: response_queue,
                G4W_final_reply="",
            )
            session = ConductorSession.__new__(ConductorSession)
            session.controller = controller
            session.sender_id = "sender"
            session.lock = __import__("threading").RLock()
            session.agent = agent
            session.history_file = Path(td) / "history.json"
            session.last_turn = 1

            reply = session.run("处理事件", event_context="消息", round_id="round-1")

            self.assertEqual(reply, "最终回复")
            self.assertEqual(emitted, [("让助手看看～", "round-1", 1)])
            self.assertEqual(session.last_turn, 2)

    def test_silent_round_is_written_to_real_output_file(self):
        with tempfile.TemporaryDirectory() as td:
            class Conversations:
                @staticmethod
                def sync_clean_history(*args, **kwargs):
                    return {"messages": [], "mode": "unchanged", "userRounds": 0, "messageCount": 0}

            controller = types.SimpleNamespace(
                config=types.SimpleNamespace(
                    conductor_history_max_messages=48,
                    conductor_history_max_chars=60000,
                    workspace_root=Path(td),
                    long_user_prompt_chars=1500,
                    conductor_max_turns=8,
                ),
                conversations=Conversations(),
                turn_enabled=lambda sender_id: False,
                emit_intermediate=lambda *args: None,
            )
            response_queue = queue.Queue()
            raw = "LLM Running (Turn 1) ...\n\n<silent/>"
            response_queue.put({"next": raw, "turn": 1, "outputs": [raw]})
            response_queue.put({"done": raw, "turn": 1, "outputs": [raw]})
            backend = types.SimpleNamespace(history=[])
            agent = types.SimpleNamespace(
                llmclient=types.SimpleNamespace(backend=backend),
                put_task=lambda *args, **kwargs: response_queue,
                G4W_final_reply="",
            )
            session = ConductorSession.__new__(ConductorSession)
            session.controller = controller
            session.sender_id = "sender"
            session.lock = __import__("threading").RLock()
            session.agent = agent
            session.history_file = Path(td) / "history.json"
            session.last_turn = 1
            reply = session.run("checkin", event_context="system.checkin", round_id="checkin-1")
            self.assertEqual(reply, "")
            outputs = list((Path(td) / "conductor-outputs").rglob("output.txt"))
            self.assertEqual(len(outputs), 1)
            output = outputs[0].read_text(encoding="utf-8")
            self.assertIn("LLM Running (Turn 1) ...", output)
            self.assertIn("<silent/>", output)
            self.assertTrue(output.endswith("[ROUND END]\n"))

    def test_checkin_does_not_emit_visible_intermediate_turns(self):
        with tempfile.TemporaryDirectory() as td:
            class Conversations:
                @staticmethod
                def sync_clean_history(*args, **kwargs):
                    return {"messages": [], "mode": "unchanged", "userRounds": 0, "messageCount": 0}

            emitted = []
            controller = G4WController.__new__(G4WController)
            controller.config = types.SimpleNamespace(
                conductor_history_max_messages=48,
                conductor_history_max_chars=60000,
                workspace_root=Path(td),
                long_user_prompt_chars=1500,
                conductor_max_turns=8,
            )
            controller.conversations = Conversations()
            controller.sessions = {}
            controller.outbox = None
            controller.turn_enabled = lambda sender_id: True
            controller.intermediate_sink = lambda sender_id, text, round_id, turn, delivery_kind: emitted.append((text, round_id, turn, delivery_kind))
            response_queue = queue.Queue()
            response_queue.put({"next": "checkin intermediate", "turn": 1, "outputs": ["checkin intermediate"]})
            response_queue.put({"done": "checkin final", "turn": 2, "outputs": ["checkin intermediate", "checkin final"]})
            agent = types.SimpleNamespace(
                llmclient=types.SimpleNamespace(backend=types.SimpleNamespace(history=[])),
                put_task=lambda *args, **kwargs: response_queue,
                G4W_final_reply="",
            )
            session = ConductorSession.__new__(ConductorSession)
            session.controller = controller
            session.sender_id = "sender"
            session.lock = __import__("threading").RLock()
            session.agent = agent
            session.history_file = Path(td) / "history.json"
            session.last_turn = 1
            controller.sessions["sender"] = session

            reply = session.run("checkin", event_context="system.checkin", round_id="checkin-1", delivery_kind="checkin")

            self.assertEqual(reply, "checkin final")
            self.assertEqual(emitted, [])
            outputs = list((Path(td) / "conductor-outputs").rglob("output.txt"))
            self.assertEqual(len(outputs), 1)
            output = outputs[0].read_text(encoding="utf-8")
            self.assertIn("checkin final", output)

    def test_cross_round_history_uses_clean_snapshot_and_strips_tool_loop(self):
        with tempfile.TemporaryDirectory() as td:
            class Conversations:
                @staticmethod
                def sync_clean_history(*args, **kwargs):
                    return {
                        "messages": [{"role": "user", "content": [{"type": "text", "text": "[07-16 10:00:00][user]\n最近聊天"}]}],
                        "mode": "appended", "userRounds": 1, "messageCount": 1,
                    }

            controller = types.SimpleNamespace(
                config=types.SimpleNamespace(
                    conductor_history_max_messages=16,
                    conductor_history_max_chars=20000,
                    workspace_root=Path(td),
                    long_user_prompt_chars=1500,
                ),
                conversations=Conversations(),
                turn_enabled=lambda sender_id: False,
                emit_intermediate=lambda *args: None,
            )
            response_queue = queue.Queue()
            response_queue.put({"done": "完成", "turn": 1, "outputs": ["完成"]})
            captured = []
            backend = types.SimpleNamespace(history=[{"role": "user", "content": "x"}] * 20)
            agent = types.SimpleNamespace(
                llmclient=types.SimpleNamespace(backend=backend),
                put_task=None,
                G4W_final_reply="",
                G4W_turn_context="",
            )
            agent.put_task = lambda prompt, **kwargs: captured.append(str(prompt)) or response_queue
            session = ConductorSession.__new__(ConductorSession)
            session.controller = controller
            session.sender_id = "sender"
            session.lock = __import__("threading").RLock()
            session.agent = agent
            session.history_file = Path(td) / "history.json"
            session.last_turn = 1

            session.run("处理事件", event_context="当前消息", round_id="r")

            self.assertEqual(len(backend.history), 1)
            self.assertIn("最近聊天", backend.history[0]["content"][0]["text"])
            self.assertFalse(list(Path(td).glob("conductor-history.auto.*.json")))
            self.assertIn("当前消息", captured[0])
            self.assertIn("# 当前G4W事件上下文", captured[0])
            self.assertNotIn("最近聊天", captured[0])

    def test_short_user_prompt_is_passed_to_ga_without_internal_context(self):
        with tempfile.TemporaryDirectory() as td:
            class Conversations:
                @staticmethod
                def sync_clean_history(*args, **kwargs):
                    return {
                        "messages": [{"role": "assistant", "content": [{"type": "text", "text": "[07-16 10:00:00][assistant]\n历史"}]}],
                        "mode": "unchanged", "userRounds": 1, "messageCount": 1,
                    }

                @staticmethod
                def format_current_user(sender_id, body, timestamp=""):
                    return f"[07-16 10:01:00][user/current]\n{body}"

            controller = types.SimpleNamespace(
                config=types.SimpleNamespace(
                    conductor_history_max_messages=48,
                    conductor_history_max_chars=60000,
                    long_user_prompt_chars=1500,
                    workspace_root=Path(td),
                ),
                conversations=Conversations(),
                turn_enabled=lambda sender_id: False,
                emit_intermediate=lambda *args: None,
            )
            response_queue = queue.Queue()
            response_queue.put({"done": "收到", "turn": 1, "outputs": ["收到"]})
            captured = []
            backend = types.SimpleNamespace(history=[])
            agent = types.SimpleNamespace(
                llmclient=types.SimpleNamespace(backend=backend),
                put_task=lambda prompt, **kwargs: captured.append(str(prompt)) or response_queue,
                G4W_final_reply="",
                G4W_turn_context="",
            )
            session = ConductorSession.__new__(ConductorSession)
            session.controller = controller
            session.sender_id = "sender"
            session.lock = __import__("threading").RLock()
            session.agent = agent
            session.history_file = Path(td) / "history.json"
            session.last_turn = 1
            session.run("用户纯净消息", event_context="内部附件元数据", user_message=True)
            self.assertIn("[user/current]", captured[0])
            self.assertTrue(captured[0].endswith("用户纯净消息"))
            self.assertIn("内部附件元数据", captured[0])
            self.assertNotIn("Assistant:", captured[0])
            self.assertNotIn("历史", captured[0])
            self.assertEqual(backend.history[0]["role"], "assistant")

    def test_knowledge_user_turn_injects_sop_before_current_message(self):
        with tempfile.TemporaryDirectory() as td:
            class Conversations:
                @staticmethod
                def sync_clean_history(*args, **kwargs):
                    return {"messages": [], "mode": "unchanged", "userRounds": 0, "messageCount": 0}

                @staticmethod
                def format_current_user(sender_id, body, timestamp=""):
                    return f"[07-16 10:01:00][user/current]\n{body}"

            controller = types.SimpleNamespace(
                config=types.SimpleNamespace(
                    conductor_history_max_messages=48,
                    conductor_history_max_chars=60000,
                    long_user_prompt_chars=1500,
                    workspace_root=Path(td),
                ),
                conversations=Conversations(),
                turn_enabled=lambda sender_id: False,
                emit_intermediate=lambda *args: None,
            )
            response_queue = queue.Queue()
            response_queue.put({"done": "收到", "turn": 1, "outputs": ["收到"]})
            captured = []
            backend = types.SimpleNamespace(history=[])
            agent = types.SimpleNamespace(
                llmclient=types.SimpleNamespace(backend=backend),
                put_task=lambda prompt, **kwargs: captured.append(str(prompt)) or response_queue,
                G4W_final_reply="",
                G4W_turn_context="",
            )
            session = ConductorSession.__new__(ConductorSession)
            session.controller = controller
            session.sender_id = "sender"
            session.lock = __import__("threading").RLock()
            session.agent = agent
            session.history_file = Path(td) / "history.json"
            session.last_turn = 1

            session.run("查一下知识库里的根据地", user_message=True)

            self.assertIn("# G4W 知识库SOP", captured[0])
            self.assertLess(captured[0].index("# G4W 知识库SOP"), captured[0].index("[user/current]"))
            self.assertIn("G4W_knowledge_search", captured[0])
            self.assertIn("查一下知识库里的根据地", captured[0])
