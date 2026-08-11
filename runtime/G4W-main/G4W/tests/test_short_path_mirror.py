import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from G4W.agents.round_log import ConductorRoundLog
from G4W.core.config import Config
from G4W.core.short_path_mirror import ShortPathMirror
from G4W.core.short_path_verify import verify_short_path_mirror
from G4W.memory.conversation import ConversationStore


class ShortPathMirrorTests(unittest.TestCase):
    def test_flag_defaults_off_and_explicitly_enables(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            "os.environ", {"G4W_STATE_DIR": td}, clear=True
        ):
            self.assertFalse(Config.load().short_path_dual_write)
            with mock.patch.dict("os.environ", {"G4W_SHORT_PATH_DUAL_WRITE": "1"}):
                self.assertTrue(Config.load().short_path_dual_write)

    def test_disabled_has_no_side_effect(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "absent"
            result = ShortPathMirror(root, False).write_conversation("s", "User", "body", message_id="m")
            self.assertIsNone(result)
            self.assertFalse(root.exists())

    def test_idempotent_unicode_long_empty_restart_and_path_escape(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "m"
            mirror = ShortPathMirror(root, True)
            cases = [("空", ""), ("unicode", "你好🙂"), ("long", "文" * 100000), ("../x\\y", "safe")]
            paths = []
            for message_id, body in cases:
                first = mirror.write_conversation("../../发送者", "User", body, message_id=message_id)
                second = mirror.write_conversation("../../发送者", "User", body, message_id=message_id)
                self.assertEqual(first, second)
                paths.append(first)
            restarted = ShortPathMirror(root, True)
            self.assertEqual(paths[1], restarted.write_conversation("../../发送者", "User", "你好🙂", message_id="unicode"))
            self.assertEqual(len(list(root.rglob("*.json"))), 4)
            for path in paths:
                self.assertTrue(path.resolve().is_relative_to(root.resolve()))
                self.assertLessEqual(len(str(path.resolve())), 240)

    def test_parallel_same_record_and_round_atomic_replace(self):
        with tempfile.TemporaryDirectory() as td:
            mirror = ShortPathMirror(Path(td) / "m", True)
            with ThreadPoolExecutor(max_workers=12) as pool:
                paths = list(pool.map(lambda _: mirror.write_conversation("s", "User", "并发", message_id="same"), range(40)))
            self.assertEqual(len({str(path) for path in paths}), 1)
            self.assertEqual(len(list((Path(td) / "m").rglob("*.json"))), 1)
            round_path = mirror.write_round("s", "r1", "first")
            mirror.write_round("s", "r1", "second")
            self.assertEqual(json.loads(round_path.read_text(encoding="utf-8"))["content"], "second")
            self.assertFalse(list((Path(td) / "m").rglob("*.tmp")))

    def test_forced_collision_rejected_without_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            mirror = ShortPathMirror(Path(td) / "m", True)
            forced = [("a" * 32, "a" * 64), ("a" * 32, "a" * 32 + "b" * 32)]
            with mock.patch.object(mirror, "_identity", side_effect=forced):
                first = mirror.write_conversation("s", "User", "one", message_id="1")
                second = mirror.write_conversation("s", "User", "two", message_id="2")
            self.assertIsNotNone(first)
            self.assertIsNone(second)
            self.assertEqual(json.loads(first.read_text(encoding="utf-8"))["content"], "one")

    def test_mirror_failure_never_rolls_back_legacy_success(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            blocker = base / "blocker"
            blocker.write_text("not a directory", encoding="utf-8")
            mirror = ShortPathMirror(blocker / "m", True)
            store = ConversationStore(base / "legacy", base / "memory.json", short_path_mirror=mirror)
            self.assertTrue(store.append("sender", "User", "legacy survives", message_id="m1"))
            self.assertIn("legacy survives", store.transcript_path("sender").read_text(encoding="utf-8"))

    def test_round_log_legacy_bytes_and_verifier_diagnostics(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            mirror = ShortPathMirror(base / "m", True)
            log = ConductorRoundLog(base / "legacy", mirror)
            output = log.begin("sender", "round", {})
            log.finish(output, "answer")
            self.assertEqual(output.read_text(encoding="utf-8"), "answer\n\n[ROUND END]\n")
            manifest_path = next((base / "m").rglob("*.json"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            expected = [{"stable_id": manifest["stable_id"], "content": manifest["content"]}]
            report = verify_short_path_mirror(base / "m", expected)
            self.assertTrue(report["ok"], report)
            self.assertEqual(report["accepted_legacy_logical_count"], 1)
            self.assertGreater(report["maximum_absolute_path_length"], 0)
            bad = verify_short_path_mirror(base / "m", [{"stable_id": manifest["stable_id"], "content": "wrong"}])
            self.assertEqual(bad["content_hash_mismatches"], [manifest["stable_id"]])


if __name__ == "__main__":
    unittest.main()
