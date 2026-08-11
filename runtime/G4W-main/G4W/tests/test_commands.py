import tempfile
import unittest
from pathlib import Path

from G4W.wechat.commands import help_text, parse_command
from G4W.core.config import Config
from G4W.core.service import G4WService


class FakeChannel:
    def __init__(self): self.sent = []
    def get_min_chunk_chars(self): return 10
    def set_min_chunk_chars(self, value): return value
    def send_text(self, sender_id, text, context_token="", delivery_id="", **kwargs):
        self.sent.append((sender_id, text, delivery_id)); return {"deliveredText": text, "deferredText": ""}


class CommandTests(unittest.TestCase):
    def test_parse_command(self):
        self.assertEqual(parse_command(" /CHUNK 50 "), ("chunk", "50"))
        self.assertIsNone(parse_command("hello"))

    def test_help_preserves_original_grouped_weixin_menu(self):
        text = help_text()
        self.assertIn("/help", text)
        self.assertIn("【工作区与线程】", text)
        self.assertIn("【个人配置】", text)
        self.assertNotIn("【审批与控制】", text)
        self.assertNotIn("/yes", text)
        self.assertIn("/chunk", text)
        self.assertIn("【能力】", text)
        self.assertIn("/switch <threadId>", text)
        self.assertIn("/turn status", text)
        self.assertIn("/l4compress", text)
        self.assertIn("/checkin", text)
        self.assertIn("/identity <identity>", text)

    def test_help_command_bypasses_model(self):
        with tempfile.TemporaryDirectory() as td:
            channel = FakeChannel()
            service = G4WService(Config(state_dir=Path(td)), channel=channel, session_factory=lambda *_: (_ for _ in ()).throw(AssertionError("model should not start")))
            service.conversations.bind("account", "sender", "ctx")
            key = service.conversations.binding_key("account", "sender")
            service.events.enqueue("wechat.command", key, {"text": "/help", "messageId": "m1"})
            service.process_events()
            service.deliver_outbox()
            self.assertEqual(len(channel.sent), 1)
            self.assertIn("/status", channel.sent[0][1])

    def test_turn_command_persists_real_intermediate_reply_setting(self):
        with tempfile.TemporaryDirectory() as td:
            service = G4WService(Config(state_dir=Path(td)), channel=FakeChannel(), session_factory=lambda *_: None)
            binding = service.conversations.bind("account", "sender", "ctx")
            self.assertIn("已开启", service.commands.execute(binding, "/turn status"))
            self.assertIn("已关闭", service.commands.execute(binding, "/turn off"))
            self.assertFalse(service.turn_progress.get("account:sender"))
            self.assertIn("已开启", service.commands.execute(binding, "/turn on"))
            self.assertTrue(service.turn_progress.get("account:sender"))

    def test_worker_turn_command_persists_worker_progress_reporting_setting(self):
        with tempfile.TemporaryDirectory() as td:
            service = G4WService(Config(state_dir=Path(td)), channel=FakeChannel(), session_factory=lambda *_: None)
            binding = service.conversations.bind("account", "sender", "ctx")
            self.assertIn("已开启", service.commands.execute(binding, "/worker_turn status"))
            self.assertIn("已关闭", service.commands.execute(binding, "/worker_turn off"))
            self.assertFalse(service.worker_turn.get("account:sender"))
            self.assertIn("已开启", service.commands.execute(binding, "/worker_turn on"))
            self.assertTrue(service.worker_turn.get("account:sender"))

    def test_input_off_stops_only_input_snapshots(self):
        with tempfile.TemporaryDirectory() as td:
            service = G4WService(Config(state_dir=Path(td)), channel=FakeChannel(), session_factory=lambda *_: None)
            binding = service.conversations.bind("account", "sender", "ctx")
            self.assertIn("已开启", service.commands.execute(binding, "/input on"))
            text = service.commands.execute(binding, "/input off")
            self.assertIn("已关闭", text)
            self.assertIn("不会新增conductor/rounds/.../inputs/turnNN.json", text)
            self.assertIn("output.txt", text)
            self.assertFalse(service.input_capture.get("account:sender"))

    def test_status_shows_next_random_checkin_without_removed_rows(self):
        with tempfile.TemporaryDirectory() as td:
            service = G4WService(Config(state_dir=Path(td)), channel=FakeChannel(), session_factory=lambda *_: None)
            binding = service.conversations.bind("account", "sender", "ctx")
            service.checkins.configure("account:sender", "sender", 10, 10, True)
            text = service.commands.execute(binding, "/status")
            self.assertIn("定时提醒：0", text)
            self.assertIn("随机check-in：已开启", text)
            self.assertRegex(text, r"下次随机check-in：\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
            self.assertNotIn("提醒/check-in", text)
            self.assertNotIn("负一屏推送", text)
            self.assertNotIn("滴答CLI", text)
            self.assertNotIn("待投递消息", text)

    def test_bind_checkin_profile_model_and_l4_commands(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            channel = FakeChannel()
            service = G4WService(Config(state_dir=root / "state", workspace_root=root, env_path=root / ".env"), channel=channel, session_factory=lambda *_: None)
            binding = service.conversations.bind("account", "sender", "ctx")
            self.assertIn("已绑定", service.commands.execute(binding, f"/bind {root}"))
            self.assertIn("30-60", service.commands.execute(binding, "/checkin 30-60"))
            self.assertIn("Edmond", service.commands.execute(binding, "/name Edmond"))
            self.assertIn("主人", service.commands.execute(binding, "/identity 主人"))
            self.assertIn("G4W_USER_NAME=Edmond", (root / ".env").read_text(encoding="utf-8"))
            self.assertIn("G4W_USER_IDENTITY=主人", (root / ".env").read_text(encoding="utf-8"))
            self.assertEqual(service.profiles.read()["senders"]["sender"]["userName"], "Edmond")
            self.assertEqual(service.profiles.read()["senders"]["sender"]["userIdentity"], "主人")
            self.assertIn("deepseek-v4-flash", service.commands.execute(binding, "/model"))
            self.assertIn("bootstrap", service.commands.execute(binding, "/l4compress"))


if __name__ == "__main__": unittest.main()
