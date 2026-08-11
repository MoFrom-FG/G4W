import tempfile
import unittest
from unittest import mock
from pathlib import Path

from G4W.core.config import Config
from G4W.memory.migration import bind_legacy_user, migrate_legacy, migrate_portable_wechat_layout
from G4W.wechat.weixin import WeixinChannel, WeixinError, _post


class MigrationWeixinTests(unittest.TestCase):
    def test_portable_layout_uses_wechat_memory_as_authority_then_archives_sources(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "memory" / "users").mkdir(parents=True)
            (root / "memory" / "users" / "same.md").write_text("old", encoding="utf-8")
            (root / "wechat-memory" / "users").mkdir(parents=True)
            (root / "wechat-memory" / "users" / "same.md").write_text("authoritative", encoding="utf-8")
            (root / "conversations" / "sender").mkdir(parents=True)
            (root / "conversations" / "sender" / "transcript.md").write_text("legacy conversation", encoding="utf-8")
            first = migrate_portable_wechat_layout(root)
            second = migrate_portable_wechat_layout(root)
            self.assertEqual((root / "memory" / "users" / "same.md").read_text(encoding="utf-8"), "authoritative")
            self.assertTrue((root / "memory" / "conversations" / "sender" / "transcript.md").is_file())
            self.assertTrue((root / "legacy-import" / "portable-layout" / "wechat-memory").is_dir())
            self.assertTrue(first["ok"])
            self.assertTrue(second["alreadyMigrated"])

    def test_migration_excludes_runtime_files(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as target_dir:
            source = Path(source_dir)
            (source / "wechat-memory" / "users").mkdir(parents=True)
            (source / "wechat-memory" / "users" / "old.md").write_text("memory", encoding="utf-8")
            (source / "G4W-bridge.pid").write_text("123", encoding="utf-8")
            (source / "sessions.json").write_text("{}", encoding="utf-8")
            result = migrate_legacy(source, Path(target_dir))
            legacy = Path(result["legacyDir"])
            self.assertTrue((legacy / "wechat-memory" / "users" / "old.md").exists())
            self.assertFalse((legacy / "G4W-bridge.pid").exists())
            self.assertFalse((legacy / "sessions.json").exists())

    def test_bind_legacy_converts_transcript(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old = root / "legacy-import" / "wechat-memory"
            (old / "users").mkdir(parents=True)
            (old / "users" / "old.md").write_text("# User\n旧记忆", encoding="utf-8")
            transcript = old / "conversations" / "old" / "transcripts" / "2026" / "07"
            transcript.mkdir(parents=True)
            (transcript / "2026-07-14.md").write_text("[2026-07-14 10:00:00 Asia/Shanghai] User:\n你好\n\n[2026-07-14 10:00:01 Asia/Shanghai] Assistant:\n在呢\n", encoding="utf-8")
            current = root / "memory" / "conversations" / "new" / "transcript.md"
            current.parent.mkdir(parents=True)
            current.write_text("# G4W Transcript\n\n[2026-07-14 11:00:00 Asia/Shanghai] User:\n新对话\n", encoding="utf-8")
            bind_legacy_user(root, "new", "old")
            self.assertIn("旧记忆", (root / "memory" / "users" / "new.md").read_text(encoding="utf-8"))
            merged = (root / "memory" / "conversations" / "new" / "transcript.md").read_text(encoding="utf-8")
            self.assertIn("你好", merged)
            self.assertIn("新对话", merged)

    def test_weixin_deduplicates_message(self):
        with tempfile.TemporaryDirectory() as td:
            config = Config(state_dir=Path(td))
            config.ensure_dirs()
            channel = WeixinChannel(config)
            raw = {"from_user_id": "sender", "message_id": "m1", "context_token": "ctx", "item_list": [{"type": 1, "text_item": {"text": "hello"}}]}
            self.assertIsNotNone(channel.normalize(raw, "account"))
            self.assertIsNone(channel.normalize(raw, "account"))

    def test_stale_account_id_falls_back_to_only_saved_account(self):
        with tempfile.TemporaryDirectory() as td:
            config = Config(state_dir=Path(td), account_id="old-account")
            config.ensure_dirs()
            channel = WeixinChannel(config)
            channel.accounts.write({"accounts": {"new-account": {"accountId": "new-account", "token": "secret", "baseUrl": "https://example.invalid"}}})
            self.assertEqual(channel.resolve_account()["accountId"], "new-account")

    def test_long_poll_timeout_is_an_empty_update(self):
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("read timed out")):
            self.assertEqual(_post("https://example.invalid", "getupdates", "token", {}, timeout_is_empty=True), {})
            with self.assertRaises(WeixinError):
                _post("https://example.invalid", "sendmessage", "token", {}, timeout_is_empty=False)


if __name__ == "__main__":
    unittest.main()
