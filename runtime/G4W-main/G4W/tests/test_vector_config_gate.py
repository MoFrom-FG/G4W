"""TASK-A: vector_config product gate — no TEI, pure unit tests."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from G4W.memory.vector import vector_config as vc
from G4W.memory.vector.l4_index_upsert import upsert_l4_insights_to_index
from G4W.memory.vector.prod_inject import vector_section_for


class VectorConfigApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.primary = self.root / "G4W-embedding" / "vector_config.json"
        self.fallback = self.root / "G4W-data" / "vector_config.json"
        vc.reset_cache_for_tests()
        # kill-switch clear
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

    def test_missing_config_vector_enabled_false(self):
        with self._patch_paths():
            self.assertFalse(self.primary.exists())
            self.assertFalse(vc.vector_enabled())
            cfg = vc.load_config()
            self.assertFalse(cfg["enabled"])
            self.assertFalse(cfg["installed"])

    def test_enabled_false_installed_true_still_off(self):
        with self._patch_paths():
            self.primary.parent.mkdir(parents=True)
            self.primary.write_text(
                json.dumps({"enabled": False, "installed": True}),
                encoding="utf-8",
            )
            vc.reset_cache_for_tests()
            self.assertFalse(vc.vector_enabled())

    def test_installed_and_enabled_true(self):
        with self._patch_paths():
            self.primary.parent.mkdir(parents=True)
            self.primary.write_text(
                json.dumps({"enabled": True, "installed": True, "dim": 1024}),
                encoding="utf-8",
            )
            vc.reset_cache_for_tests()
            self.assertTrue(vc.vector_enabled())

    def test_set_vector_enabled_persists(self):
        with self._patch_paths():
            # need installed for effective on
            out = vc.save_config({"installed": True, "enabled": False})
            self.assertTrue(self.primary.is_file() or self.fallback.is_file())
            self.assertFalse(vc.vector_enabled())
            out2 = vc.set_vector_enabled(True)
            self.assertTrue(out2["enabled"])
            vc.reset_cache_for_tests()
            self.assertTrue(vc.vector_enabled())
            vc.set_vector_enabled(False)
            vc.reset_cache_for_tests()
            self.assertFalse(vc.vector_enabled())

    def test_fallback_path_used_when_primary_missing(self):
        with self._patch_paths():
            self.fallback.parent.mkdir(parents=True)
            self.fallback.write_text(
                json.dumps({"enabled": True, "installed": True}),
                encoding="utf-8",
            )
            vc.reset_cache_for_tests()
            p = vc.config_path()
            self.assertEqual(p, self.fallback)
            self.assertTrue(vc.vector_enabled())

    def test_env_kill_switch(self):
        with self._patch_paths():
            self.primary.parent.mkdir(parents=True)
            self.primary.write_text(
                json.dumps({"enabled": True, "installed": True}),
                encoding="utf-8",
            )
            vc.reset_cache_for_tests()
            os.environ["G4W_VECTOR_ADDON"] = "0"
            self.assertFalse(vc.vector_enabled())

    def test_status_dict_has_path(self):
        with self._patch_paths():
            st = vc.status_dict()
            self.assertIn("config_path", st)
            self.assertIn("vector_enabled", st)
            self.assertFalse(st["vector_enabled"])


class UpsertInjectGateTests(unittest.TestCase):
    def setUp(self) -> None:
        vc.reset_cache_for_tests()
        os.environ.pop("G4W_VECTOR_ADDON", None)

    def tearDown(self) -> None:
        vc.reset_cache_for_tests()
        os.environ.pop("G4W_VECTOR_ADDON", None)

    def test_upsert_no_op_when_addon_off(self):
        with mock.patch(
            "G4W.memory.vector.vector_config.vector_enabled",
            return_value=False,
        ):
            with mock.patch(
                "G4W.memory.vector.l4_index_upsert.l4_index_upsert_enabled",
                return_value=True,
            ):
                summary = upsert_l4_insights_to_index(
                    run_id="t",
                    user_id="u",
                    active={"items": []},
                    dry_run=True,
                )
        self.assertEqual(summary.get("status"), "skipped")
        self.assertEqual(summary.get("reason"), "vector_addon disabled")
        self.assertEqual(summary.get("upserted"), 0)

    def test_vector_section_empty_when_addon_off(self):
        with mock.patch(
            "G4W.memory.vector.vector_config.vector_enabled",
            return_value=False,
        ):
            # legacy flag ON would previously inject — addon gate must win
            with mock.patch(
                "G4W.memory.vector.prod_inject.vector_retrieval_enabled",
                return_value=True,
            ):
                out = vector_section_for(
                    memory_root=".",
                    sender_id="u1",
                    query="hello",
                    k=3,
                    check_flag=True,
                )
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
