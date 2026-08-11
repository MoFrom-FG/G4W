"""TASK-H: integration / gap-fill tests for vector addon (hot-reload, no-op, off→stop_tei, /vector tri-state).

Default product code unchanged. Mock TEI/embed; no production index writes.
"""
from __future__ import annotations

import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from G4W.memory.vector import vector_config as vc
from G4W.memory.vector.l4_index_upsert import upsert_l4_insights_to_index
from G4W.memory.vector.prod_inject import vector_section_for
from G4W.wechat import commands as commands_mod
from G4W.wechat.commands import format_vector_status, handle_vector_command


class HotReloadConfigTests(unittest.TestCase):
    """SOP H: config 热切 — set_vector_enabled / load 立即反映."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.primary = self.root / "G4W-embedding" / "vector_config.json"
        self.fallback = self.root / "G4W-data" / "vector_config.json"
        vc.reset_cache_for_tests()
        os.environ.pop("G4W_VECTOR_ADDON", None)

    def tearDown(self) -> None:
        vc.reset_cache_for_tests()
        os.environ.pop("G4W_VECTOR_ADDON", None)
        self._tmp.cleanup()

    def _patch_paths(self):
        return mock.patch.multiple(
            vc,
            _primary_path=lambda: self.primary,
            _fallback_path=lambda: self.fallback,
            _runtime_root=lambda: self.root,
        )

    def test_set_vector_enabled_hot_reflects_without_manual_reset(self):
        """After set_vector_enabled, vector_enabled() reflects immediately (cache invalidated)."""
        with self._patch_paths():
            vc.save_config({"installed": True, "enabled": False})
            self.assertFalse(vc.vector_enabled())
            out = vc.set_vector_enabled(True)
            self.assertTrue(out.get("enabled"))
            # no reset_cache_for_tests — product path must invalidate
            self.assertTrue(vc.vector_enabled())
            vc.set_vector_enabled(False)
            self.assertFalse(vc.vector_enabled())

    def test_external_file_write_mtime_invalidates_cache(self):
        """Direct file rewrite with mtime change is visible on next load (mtime gate)."""
        with self._patch_paths():
            self.primary.parent.mkdir(parents=True, exist_ok=True)
            self.primary.write_text(
                json.dumps({"installed": True, "enabled": False}),
                encoding="utf-8",
            )
            vc.reset_cache_for_tests()
            self.assertFalse(vc.vector_enabled())
            # warm cache
            _ = vc.load_config(use_cache=True)
            self.primary.write_text(
                json.dumps({"installed": True, "enabled": True}),
                encoding="utf-8",
            )
            # force mtime advance if FS granularity collapses writes
            st = self.primary.stat()
            os.utime(self.primary, (st.st_atime, st.st_mtime + 2.0))
            self.assertTrue(vc.vector_enabled())


class NoOpWhenDisabledTests(unittest.TestCase):
    """SOP H: enabled=false → upsert/search 真 no-op（无 TEI/embed/写索引）."""

    def setUp(self) -> None:
        vc.reset_cache_for_tests()
        os.environ.pop("G4W_VECTOR_ADDON", None)

    def tearDown(self) -> None:
        vc.reset_cache_for_tests()
        os.environ.pop("G4W_VECTOR_ADDON", None)

    def test_upsert_no_embed_no_tei_when_addon_off(self):
        """Product gate off → skip before TEI/embed (patch at definition sites)."""
        with mock.patch(
            "G4W.memory.vector.vector_config.vector_enabled",
            return_value=False,
        ), mock.patch(
            "G4W.memory.vector.tei_lifecycle.ensure_tei_running",
        ) as m_tei, mock.patch(
            "G4W.memory.vector.embedding.embed_batch",
        ) as m_emb, mock.patch.dict(
            os.environ, {"G4W_L4_INDEX_UPSERT": "1"}
        ):
            summary = upsert_l4_insights_to_index(
                run_id="h-noop",
                user_id="u",
                active={
                    "items": [
                        {
                            "id": "x1",
                            "category": "user_facts",
                            "summary": "should not embed",
                        }
                    ]
                },
                dry_run=False,
            )
        self.assertEqual(summary.get("status"), "skipped")
        self.assertEqual(summary.get("reason"), "vector_addon disabled")
        self.assertEqual(summary.get("upserted"), 0)
        m_tei.assert_not_called()
        m_emb.assert_not_called()

    def test_vector_section_no_search_when_addon_off(self):
        """vector_section_for returns empty; HybridQueryEngine.search never called."""
        with mock.patch(
            "G4W.memory.vector.vector_config.vector_enabled",
            return_value=False,
        ), mock.patch(
            "G4W.memory.vector.prod_inject.HybridQueryEngine",
        ) as m_engine:
            out = vector_section_for(
                memory_root=".",
                sender_id="u1",
                query="hello hybrid",
                k=3,
                check_flag=True,
            )
        self.assertEqual(out, "")
        m_engine.assert_not_called()


class OffStopsTeiTests(unittest.TestCase):
    """SOP H: /vector off → stop_tei 调用链."""

    def test_handle_off_calls_stop_tei_once(self):
        state = {
            "enabled": True,
            "installed": True,
            "model": "Qwen3-Embedding-0.6B",
            "dim": 1024,
            "base_url": "http://127.0.0.1:8080",
            "port": 8080,
            "pid": 4242,
            "tei_health": "ok",
            "updated_at": "2026-07-25T00:00:00+08:00",
            "path": "D:/fake/vector_config.json",
        }
        mod = types.SimpleNamespace()
        stop_calls = []

        def config_path():
            return Path(state["path"])

        def load_config():
            return dict(state)

        def vector_enabled():
            return bool(state["installed"] and state["enabled"])

        def set_vector_enabled(on: bool):
            state["enabled"] = bool(on)
            return dict(state)

        def stop_tei():
            stop_calls.append(state.get("pid"))
            state["pid"] = None
            return {"ok": True, "stopped": True, "reason": "mock"}

        mod.config_path = config_path
        mod.load_config = load_config
        mod.vector_enabled = vector_enabled
        mod.set_vector_enabled = set_vector_enabled
        mod.stop_tei = stop_tei

        with mock.patch.object(commands_mod, "_import_vector_config_api", return_value=mod):
            with mock.patch.object(commands_mod, "_import_stop_tei", return_value=stop_tei):
                text = handle_vector_command("off")
        self.assertEqual(stop_calls, [4242])
        self.assertFalse(state["enabled"])
        self.assertIn("已关闭", text)
        self.assertTrue("TEI" in text or "stop" in text.lower())


class VectorTriStateTests(unittest.TestCase):
    """SOP H: /vector 三态 — 未装 / 已装关 / 已装开 文案可区分."""

    def _fake(self, *, installed: bool, enabled: bool):
        state = {
            "enabled": enabled,
            "installed": installed,
            "model": "Qwen3-Embedding-0.6B",
            "dim": 1024,
            "base_url": "http://127.0.0.1:8080",
            "port": 8080,
            "pid": None,
            "tei_health": None,
            "updated_at": "2026-07-25T00:00:00+08:00",
            "path": "D:/fake/vector_config.json",
        }
        mod = types.SimpleNamespace()
        mod.config_path = lambda: Path(state["path"])
        mod.load_config = lambda: dict(state)
        mod.vector_enabled = lambda: bool(state["installed"] and state["enabled"])
        mod.set_vector_enabled = lambda on: state.update(enabled=bool(on)) or dict(state)
        mod._state = state
        return mod

    def _status(self, installed: bool, enabled: bool) -> str:
        fake = self._fake(installed=installed, enabled=enabled)
        with mock.patch.object(commands_mod, "_import_vector_config_api", return_value=fake):
            with mock.patch(
                "G4W.memory.vector.index_meta.meta_status_lines",
                return_value=[],
            ):
                with mock.patch(
                    "G4W.memory.vector.index_meta.format_mismatch_hint",
                    return_value="",
                ):
                    return handle_vector_command("status")

    def test_three_states_distinguishable(self):
        not_installed = self._status(False, False)
        installed_off = self._status(True, False)
        installed_on = self._status(True, True)

        self.assertIn("有效总闸：关", not_installed)
        self.assertIn("installed：False", not_installed)
        self.assertIn("5_embedding_for_G4W.bat", not_installed)

        self.assertIn("有效总闸：关", installed_off)
        self.assertIn("installed：True", installed_off)
        self.assertIn("enabled：False", installed_off)
        self.assertNotIn("5_embedding_for_G4W.bat", installed_off)

        self.assertIn("有效总闸：开", installed_on)
        self.assertIn("installed：True", installed_on)
        self.assertIn("enabled：True", installed_on)

        # pairwise uniqueness of key signatures
        sig = lambda s: (
            "开" in s.split("有效总闸：")[1][:2] if "有效总闸：" in s else s,
            "installed：True" in s,
            "enabled：True" in s,
            "5_embedding_for_G4W.bat" in s,
        )
        self.assertNotEqual(sig(not_installed), sig(installed_off))
        self.assertNotEqual(sig(installed_off), sig(installed_on))
        self.assertNotEqual(sig(not_installed), sig(installed_on))

    def test_format_vector_status_tri_state_fields(self):
        a = format_vector_status(
            {"installed": False, "enabled": False, "path": "p"}, False
        )
        b = format_vector_status(
            {"installed": True, "enabled": False, "path": "p"}, False
        )
        c = format_vector_status(
            {"installed": True, "enabled": True, "path": "p"}, True
        )
        self.assertIn("有效总闸：关", a)
        self.assertIn("installed：False", a)
        self.assertIn("installed：True", b)
        self.assertIn("enabled：False", b)
        self.assertIn("有效总闸：开", c)


class L4IncrementalEvidenceTests(unittest.TestCase):
    """SOP H: L4 增量 — 引用既有 sandbox upsert 行为（replace same id）.

    Full path covered by test_l4_index_upsert.UpsertFlowTests.test_upsert_temp_index_replace_and_docs;
    here we only assert gate+skip contract remains stable for H evidence matrix.
    """

    def test_addon_off_skips_before_any_index_io(self):
        with mock.patch(
            "G4W.memory.vector.vector_config.vector_enabled",
            return_value=False,
        ), mock.patch(
            "G4W.memory.vector.build_prod_index.index_ready",
            side_effect=AssertionError("index_ready must not be called"),
        ), mock.patch.dict(
            os.environ, {"G4W_L4_INDEX_UPSERT": "1"}
        ):
            summary = upsert_l4_insights_to_index(
                run_id="h-l4",
                user_id="u",
                active={"items": []},
                dry_run=False,
            )
        self.assertEqual(summary.get("status"), "skipped")
        self.assertEqual(summary.get("reason"), "vector_addon disabled")


if __name__ == "__main__":
    unittest.main()
