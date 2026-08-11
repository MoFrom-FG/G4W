"""TASK-C: embed_lifecycle unit tests (mocked; no real ST process).

Module name kept for history; imports public aliases from tei_lifecycle shim
and patches private helpers on embed_lifecycle (where they live).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from G4W.memory.vector import embed_lifecycle as el
from G4W.memory.vector import tei_lifecycle as tl
from G4W.memory.vector import vector_config as vc


class EmbedLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.primary = self.root / "G4W-embedding" / "vector_config.json"
        self.fallback = self.root / "G4W-data" / "vector_config.json"
        self.emb_root = self.root / "G4W-embedding"
        self.emb_root.mkdir(parents=True, exist_ok=True)
        vc.reset_cache_for_tests()

    def tearDown(self) -> None:
        vc.reset_cache_for_tests()
        self._tmp.cleanup()

    def _patch_paths(self):
        return mock.patch.multiple(
            vc,
            _primary_path=lambda: self.primary,
            _fallback_path=lambda: self.fallback,
            _runtime_root=lambda: self.root,
        )

    def _write_cfg(self, **kwargs):
        data = vc.default_config()
        data.update(kwargs)
        self.primary.parent.mkdir(parents=True, exist_ok=True)
        self.primary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        vc.reset_cache_for_tests()

    def test_embed_health_ok_via_health_endpoint(self):
        self._write_cfg(base_url="http://127.0.0.1:18080", port=18080)
        with self._patch_paths():
            with mock.patch.object(
                el, "_http_get", return_value=(200, json.dumps({"ok": True, "model": "x"}))
            ):
                r = tl.tei_health(base_url="http://127.0.0.1:18080")
        self.assertTrue(r["ok"])
        self.assertEqual(r["method"], "health")
        self.assertEqual(r["base_url"], "http://127.0.0.1:18080")

    def test_embed_health_fallback_embeddings(self):
        self._write_cfg(base_url="http://127.0.0.1:18080", port=18080)
        emb_body = json.dumps(
            {"data": [{"embedding": [0.1, 0.2], "index": 0}], "model": "x"}
        )
        with self._patch_paths():
            with mock.patch.object(
                el, "_http_get", side_effect=OSError("health down")
            ):
                with mock.patch.object(
                    el, "_http_post_json", return_value=(200, emb_body)
                ):
                    r = el.embed_health(base_url="http://127.0.0.1:18080")
        self.assertTrue(r["ok"])
        self.assertEqual(r["method"], "embeddings")

    def test_ensure_disabled_when_gate_off(self):
        self._write_cfg(installed=True, enabled=False)
        with self._patch_paths():
            r = tl.ensure_tei_running(timeout_s=1.0)
        self.assertFalse(r["ok"])
        self.assertEqual(r["status"], "disabled")

    def test_ensure_already_running(self):
        self._write_cfg(
            installed=True,
            enabled=True,
            base_url="http://127.0.0.1:18080",
            port=18080,
            pid=4242,
        )
        with self._patch_paths():
            with mock.patch.object(
                el,
                "embed_health",
                return_value={"ok": True, "detail": "health ok", "method": "health"},
            ):
                r = el.ensure_embed_running(timeout_s=1.0)
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], "already_running")

    def test_desired_embed_device_defaults_to_cuda(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(el._desired_embed_device(), "cuda")

    def test_ensure_restarts_healthy_cpu_service(self):
        self._write_cfg(
            installed=True,
            enabled=True,
            base_url="http://127.0.0.1:18080",
            port=18080,
            pid=None,
        )
        with self._patch_paths():
            with mock.patch.object(
                el,
                "embed_health",
                return_value={
                    "ok": True,
                    "detail": "health ok",
                    "method": "health",
                    "body": {"device": "cpu"},
                },
            ):
                with mock.patch.object(el, "stop_embed") as stop:
                    with mock.patch.object(
                        el,
                        "discover_embed_launchers",
                        return_value={"model_dirs": [], "bats": []},
                    ):
                        with mock.patch.object(
                            el,
                            "_build_launch_command",
                            return_value=(None, "no launcher", False),
                        ):
                            r = el.ensure_embed_running(timeout_s=1.0)
        stop.assert_called_once_with(pid=None, port=18080)
        self.assertFalse(r["ok"])
        self.assertEqual(r["health_mismatch"], "device_mismatch")

    def test_stop_skips_non_embed_port_holder(self):
        self._write_cfg(installed=True, enabled=True, pid=None, port=18080)
        with self._patch_paths():
            with mock.patch.object(el, "_pids_listening_on_port", return_value=[1234]):
                with mock.patch.object(el, "_pid_alive", return_value=True):
                    with mock.patch.object(
                        el,
                        "_process_cmdline",
                        return_value=r"C:\Program Files\nginx\nginx.exe",
                    ):
                        with mock.patch.object(el, "_terminate_pid") as term:
                            with mock.patch.object(
                                el,
                                "embed_health",
                                return_value={"ok": False, "detail": "down"},
                            ):
                                r = tl.stop_tei()
        term.assert_not_called()
        self.assertTrue(
            any(s.get("reason") == "not_embed_cmdline" for s in r["skipped"])
        )

    def test_stop_terminates_embed_pid(self):
        self._write_cfg(installed=True, enabled=True, pid=9999, port=18080)
        with self._patch_paths():
            with mock.patch.object(el, "_pids_listening_on_port", return_value=[]):
                with mock.patch.object(el, "_pid_alive", return_value=True):
                    with mock.patch.object(
                        el,
                        "_process_cmdline",
                        return_value=r"D:\x\.venv\Scripts\python.exe server.py",
                    ):
                        with mock.patch.object(
                            el,
                            "_terminate_pid",
                            return_value={"pid": 9999, "killed": True},
                        ) as term:
                            with mock.patch.object(
                                el,
                                "embed_health",
                                return_value={"ok": False, "detail": "down"},
                            ):
                                r = el.stop_embed()
                                cfg = vc.load_config(use_cache=False)
        self.assertTrue(r["ok"])
        self.assertTrue(r["stopped"])
        self.assertEqual(len(r["actions"]), 1)
        term.assert_called()
        self.assertIsNone(cfg.get("pid"))

    def test_discover_finds_bat(self):
        bat = self.emb_root / "start_embed.bat"
        bat.write_text("@echo off\n", encoding="utf-8")
        d = tl.discover_tei_launchers(self.emb_root)
        self.assertTrue(d["root_exists"])
        self.assertTrue(
            any(str(bat) == x or x.endswith("start_embed.bat") for x in d["bats"])
        )
        # legacy name still discovered if present
        leg = self.emb_root / "start_tei.bat"
        leg.write_text("@echo off\n", encoding="utf-8")
        d2 = el.discover_embed_launchers(self.emb_root)
        self.assertTrue(any(x.endswith("start_tei.bat") for x in d2["bats"]))


if __name__ == "__main__":
    unittest.main()
