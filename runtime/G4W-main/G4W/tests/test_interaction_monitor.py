import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from G4W.core.config import Config
from G4W.cli.monitor import ModelLogMonitor
from G4W.wechat.weixin import WeixinChannel


class InteractionMonitorTests(unittest.TestCase):
    def test_typing_uses_config_ticket_then_sends_status(self):
        with tempfile.TemporaryDirectory() as td:
            config = Config(state_dir=Path(td))
            config.ensure_dirs()
            channel = WeixinChannel(config)
            channel.account = {"accountId": "account", "token": "secret", "baseUrl": "https://example.invalid"}
            calls = []

            def fake_post(base_url, endpoint, token, payload, timeout=15, timeout_is_empty=False):
                calls.append((endpoint, payload))
                return {"typing_ticket": "ticket"} if endpoint.endswith("getconfig") else {}

            with mock.patch("G4W.wechat.weixin._post", side_effect=fake_post):
                channel.send_typing("sender", 1, "ctx")
            self.assertEqual([item[0] for item in calls], ["ilink/bot/getconfig", "ilink/bot/sendtyping"])
            self.assertEqual(calls[0][1]["context_token"], "ctx")
            self.assertEqual(calls[1][1]["typing_ticket"], "ticket")
            self.assertEqual(calls[1][1]["status"], 1)

    def test_typing_keepalive_always_clears_status(self):
        with tempfile.TemporaryDirectory() as td:
            config = Config(state_dir=Path(td))
            config.ensure_dirs()
            channel = WeixinChannel(config)
            statuses = []
            channel.send_typing = lambda sender_id, status=1, context_token="": statuses.append(status)
            with channel.typing_keepalive("sender", "ctx", interval_seconds=60):
                pass
            self.assertEqual(statuses, [1, 0])

    def test_weixin_holds_after_eight_and_flushes_on_round_final(self):
        with tempfile.TemporaryDirectory() as td:
            config = Config(state_dir=Path(td))
            config.ensure_dirs()
            channel = WeixinChannel(config)
            channel.account = {"accountId": "account", "token": "secret", "baseUrl": "https://example.invalid"}
            channel.set_min_chunk_chars(1)
            calls = []

            def fake_post(base_url, endpoint, token, payload, timeout=15, timeout_is_empty=False):
                calls.append(payload["msg"]["item_list"][0]["text_item"]["text"])
                return {}

            text = "\n\n".join(f"这是第{index}个测试句子" for index in range(1, 12))
            with mock.patch("G4W.wechat.weixin._post", side_effect=fake_post), mock.patch("G4W.wechat.weixin.time.sleep"):
                live = channel.send_text("sender", text, "ctx", delivery_id="live", round_id="round", round_final=False)
                final = channel.send_text("sender", "", "ctx", delivery_id="final", round_id="round", round_final=True)
            self.assertEqual(live["deliveredCount"], 8)
            self.assertTrue(live["heldText"])
            self.assertEqual(final["deliveredCount"], 1)
            self.assertEqual(len(calls), 9)
            self.assertIn("第11个", calls[-1])
            audit = channel.delivery_audit.read()["deliveries"]
            self.assertEqual(len(audit), 2)
            self.assertEqual(audit[-1]["deliveredChunks"][0]["bubble"], 9)

    def test_model_monitor_renders_real_ga_round_once(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            output_dir = state / "memory" / "conversations" / "sender" / "conductor" / "rounds" / "2026" / "07" / "16" / "round-1"
            output_dir.mkdir(parents=True)
            (output_dir / "output.txt").write_text(
                "LLM Running (Turn 1) ...\n\n<silent/>\n\n[ROUND END]\n", encoding="utf-8"
            )
            monitor = ModelLogMonitor(state, state / "G4W.pid")
            output = io.StringIO()
            with redirect_stdout(output):
                monitor.drain()
                monitor.drain()
            text = output.getvalue()
            self.assertEqual(text.count("LLM Running (Turn 1) ..."), 1)
            self.assertEqual(text.count("<silent/>"), 1)
            self.assertEqual(text.count("[ROUND END]"), 1)

    def test_monitor_switches_to_new_output_that_restarts_at_turn_one(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            rounds = state / "memory" / "conversations" / "sender" / "conductor" / "rounds" / "2026" / "07" / "16"
            output_dir = rounds / "round-1"
            output_dir.mkdir(parents=True)
            (output_dir / "output.txt").write_text("LLM Running (Turn 1) ...\n\n第一轮\n\n[ROUND END]\n", encoding="utf-8")
            monitor = ModelLogMonitor(state, state / "G4W.pid")
            output = io.StringIO()
            with redirect_stdout(output):
                monitor.drain()
                second = rounds / "round-2"
                second.mkdir()
                (second / "output.txt").write_text("LLM Running (Turn 1) ...\n\n第二轮\n\n[ROUND END]\n", encoding="utf-8")
                monitor.drain()
            text = output.getvalue()
            self.assertEqual(text.count("LLM Running (Turn 1) ..."), 2)
            self.assertEqual(text.count("[ROUND END]"), 2)


if __name__ == "__main__":
    unittest.main()
