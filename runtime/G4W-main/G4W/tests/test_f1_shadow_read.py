"""F1 shadow-read unit tests (tempfile only; no production DATA).

Locks default legacy bit-identical behaviour and covers daily_primary
shadow/compare RO paths introduced in T1-W1.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from G4W.core.config import Config
from G4W.memory.conversation import ConversationStore


def _store(root: Path, f1_read_path: str = "legacy") -> ConversationStore:
    return ConversationStore(
        root / "conversations",
        root / "memory",
        recent_pairs=20,
        f1_read_path=f1_read_path,
    )


def _dual_write_fixture(store: ConversationStore, sender: str = "sender-f1") -> str:
    """Append a short dual-write conversation; returns sender_id."""
    store.append(sender, "User", "F1 user one", "2026-07-20T02:00:00Z")
    store.append(sender, "Assistant", "F1 assistant one", "2026-07-20T02:00:01Z")
    store.append(sender, "User", "F1 user two", "2026-07-21T03:00:00Z")
    store.append(sender, "Assistant", "F1 assistant two", "2026-07-21T03:00:01Z")
    return sender


class F1ShadowReadTests(unittest.TestCase):
    """F1-T1..T5 — shadow read / default legacy lock."""

    def test_f1_t1_default_legacy_matches_aggregate_only(self):
        """F1-T1: default/legacy read_transcript == aggregate transcript.md bit-identical."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = _store(root)  # default legacy
            self.assertEqual(store.f1_read_path, "legacy")
            sender = _dual_write_fixture(store)

            legacy_direct = store.transcript_path(sender).read_text(encoding="utf-8")
            via_api = store.read_transcript(sender)
            via_mode = store.read_transcript(sender, mode="legacy")
            via_legacy_helper = store._read_legacy_transcript(sender)

            self.assertEqual(via_api, legacy_direct)
            self.assertEqual(via_mode, legacy_direct)
            self.assertEqual(via_legacy_helper, legacy_direct)
            # recent/_blocks route through read_transcript under default legacy
            recent = store.recent(sender)
            self.assertIn("F1 user one", recent)
            self.assertIn("F1 assistant two", recent)

    def test_f1_t2_shadow_compare_after_dual_write(self):
        """F1-T2: dual-write fixture → shadow_compare reports lengths/hash; both non-empty."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = _store(root)
            sender = _dual_write_fixture(store)

            cmp_ = store.shadow_compare_transcript(sender)
            self.assertEqual(cmp_["sender_id"], sender)
            self.assertEqual(cmp_["f1_read_path_default"], "legacy")
            self.assertTrue(cmp_["legacy_exists"])
            self.assertGreater(cmp_["legacy_chars"], 0)
            self.assertGreater(cmp_["daily_file_count"], 0)
            self.assertGreater(cmp_["daily_chars"], 0)
            self.assertIn("legacy_sha256_10", cmp_)
            self.assertIn("daily_sha256_10", cmp_)
            self.assertEqual(len(cmp_["legacy_sha256_10"]), 10)
            self.assertEqual(len(cmp_["daily_sha256_10"]), 10)

            # daily stitch via read_transcript(mode=daily_primary)
            daily_text = store.read_transcript(sender, mode="daily_primary")
            self.assertEqual(len(daily_text), cmp_["daily_chars"])
            # Headers differ (aggregate vs daily) so equal_text may be False;
            # still both must contain body content.
            self.assertIn("F1 user one", daily_text)
            self.assertIn("F1 assistant two", daily_text)
            legacy_text = store.read_transcript(sender, mode="legacy")
            self.assertIn("F1 user one", legacy_text)
            # Explicit equal_text flag matches raw string compare
            self.assertEqual(cmp_["equal_text"], legacy_text == daily_text)

    def test_f1_t3_missing_daily_fail_soft_empty(self):
        """F1-T3: no daily files → daily_primary returns empty string (fail-soft)."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = _store(root)
            sender = "sender-no-daily"
            # Write ONLY aggregate (bypass append dual-write)
            agg = store.transcript_path(sender)
            agg.parent.mkdir(parents=True, exist_ok=True)
            body = "# G4W Transcript\nsender: sender-no-daily\n\n[2026-07-20 10:00:00 Asia/Shanghai] User:\nsolo aggregate\n\n"
            agg.write_text(body, encoding="utf-8")

            self.assertEqual(store.list_daily_transcript_paths(sender), [])
            daily = store.read_transcript(sender, mode="daily_primary")
            self.assertEqual(daily, "")
            legacy = store.read_transcript(sender, mode="legacy")
            self.assertEqual(legacy, body)

            cmp_ = store.shadow_compare_transcript(sender)
            self.assertEqual(cmp_["daily_file_count"], 0)
            self.assertEqual(cmp_["daily_chars"], 0)
            self.assertFalse(cmp_["equal_text"])
            self.assertGreater(cmp_["legacy_chars"], 0)

    def test_f1_t4_shadow_and_daily_primary_zero_writes(self):
        """F1-T4: shadow_compare + daily_primary read leave tree mtimes/file count unchanged."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = _store(root)
            sender = _dual_write_fixture(store)

            def snapshot(base: Path):
                files = sorted(
                    (str(p.relative_to(base)).replace("\\", "/"), p.stat().st_mtime_ns, p.stat().st_size)
                    for p in base.rglob("*")
                    if p.is_file()
                )
                return files

            before = snapshot(root)
            # RO operations
            _ = store.read_transcript(sender, mode="daily_primary")
            _ = store.shadow_compare_transcript(sender)
            _ = store.list_daily_transcript_paths(sender)
            _ = store._read_daily_primary_transcript(sender)
            after = snapshot(root)

            self.assertEqual(before, after, "shadow/daily_primary path must not write any files")

    def test_f1_t5_legacy_flag_skips_daily_main_path(self):
        """F1-T5: f1_read_path=legacy → read_transcript does not call daily primary helper."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = _store(root, f1_read_path="legacy")
            sender = _dual_write_fixture(store)

            with mock.patch.object(
                store, "_read_daily_primary_transcript", wraps=store._read_daily_primary_transcript
            ) as daily_spy:
                with mock.patch.object(
                    store, "_read_legacy_transcript", wraps=store._read_legacy_transcript
                ) as legacy_spy:
                    text = store.read_transcript(sender)  # mode=None → legacy
                    self.assertGreater(len(text), 0)
                    legacy_spy.assert_called()
                    daily_spy.assert_not_called()

            # Explicit mode override still reaches daily without flipping store default
            with mock.patch.object(
                store, "_read_daily_primary_transcript", wraps=store._read_daily_primary_transcript
            ) as daily_spy2:
                text2 = store.read_transcript(sender, mode="daily_primary")
                self.assertGreater(len(text2), 0)
                daily_spy2.assert_called()
            self.assertEqual(store.f1_read_path, "legacy")

    def test_f1_config_default_legacy_and_env_mapping(self):
        """Config.load default is legacy; env daily_primary maps correctly (isolated env)."""
        # Ensure process pollution isolation via mock.patch.dict
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("G4W_F1_READ_PATH", None)
            cfg = Config.load()
            self.assertEqual(cfg.f1_read_path, "legacy")

        with mock.patch.dict(os.environ, {"G4W_F1_READ_PATH": "daily_primary"}):
            cfg = Config.load()
            self.assertEqual(cfg.f1_read_path, "daily_primary")

        with mock.patch.dict(os.environ, {"G4W_F1_READ_PATH": "legacy"}):
            cfg = Config.load()
            self.assertEqual(cfg.f1_read_path, "legacy")

        # Store constructor normalizes aliases
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            s = _store(root, f1_read_path="daily")
            self.assertEqual(s.f1_read_path, "daily_primary")
            s2 = _store(root, f1_read_path="unknown-mode")
            self.assertEqual(s2.f1_read_path, "legacy")


if __name__ == "__main__":
    unittest.main()
