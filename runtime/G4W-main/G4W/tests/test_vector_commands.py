"""Unit tests for /vector WeChat command (TASK-B)."""
from __future__ import annotations

import tempfile
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from G4W.wechat import commands as commands_mod
from G4W.wechat.commands import (
    format_vector_status,
    handle_vector_command,
    help_text,
    parse_command,
)
from G4W.core.config import Config
from G4W.core.service import G4WService


class FakeChannel:
    def __init__(self):
        self.sent = []

    def get_min_chunk_chars(self):
        return 10

    def set_min_chunk_chars(self, value):
        return value

    def send_text(self, sender_id, text, context_token="", delivery_id="", **kwargs):
        self.sent.append((sender_id, text, delivery_id))
        return {"deliveredText": text, "deferredText": ""}


def _make_fake_vc(*, enabled=False, installed=False, path=None):
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
        "path": str(path or (Path(tempfile.gettempdir()) / "G4W-embedding" / "vector_config.json")),
    }
    mod = types.SimpleNamespace()

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
        state["pid"] = None
        return {"ok": True, "stopped": True, "reason": "fake"}

    mod.config_path = config_path
    mod.load_config = load_config
    mod.vector_enabled = vector_enabled
    mod.set_vector_enabled = set_vector_enabled
    mod.stop_tei = stop_tei
    mod._state = state
    return mod


class VectorCommandUnitTests(unittest.TestCase):
    def test_vector_config_import_does_not_require_numpy(self):
        code = """
import builtins

real_import = builtins.__import__

def guarded_import(name, *args, **kwargs):
    if name == "numpy" or name.startswith("numpy."):
        raise AssertionError("lightweight vector_config import touched numpy")
    return real_import(name, *args, **kwargs)

builtins.__import__ = guarded_import
from G4W.memory.vector import vector_config
assert callable(vector_config.load_config)
assert callable(vector_config.vector_enabled)
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_parse_vector(self):
        self.assertEqual(parse_command("/vector"), ("vector", ""))
        self.assertEqual(parse_command("/vector on"), ("vector", "on"))
        self.assertEqual(parse_command(" /VECTOR status "), ("vector", "status"))

    def test_help_lists_vector(self):
        text = help_text()
        self.assertIn("/vector", text)
        self.assertIn("【能力】", text)

    def test_format_status_shows_fields(self):
        cfg = {
            "enabled": True,
            "installed": True,
            "model": "Qwen3-Embedding-0.6B",
            "dim": 1024,
            "base_url": "http://127.0.0.1:8080",
            "port": 8080,
            "pid": 1234,
            "tei_health": "ok",
            "path": "/tmp/vc.json",
            "updated_at": "t",
        }
        text = format_vector_status(cfg, True)
        self.assertIn("有效总闸：开", text)
        self.assertIn("model：Qwen3-Embedding-0.6B", text)
        self.assertIn("pid：1234", text)
        self.assertIn("/tmp/vc.json", text)

    def test_format_status_env_kill_switch_overrides_json(self):
        cfg = {
            "enabled": True,
            "installed": True,
            "model": "Qwen3-Embedding-0.6B",
            "dim": 1024,
            "base_url": "http://127.0.0.1:8080",
            "port": 8080,
            "pid": None,
            "tei_health": None,
            "path": "/tmp/vc.json",
            "updated_at": "t",
        }
        with mock.patch.dict("os.environ", {"G4W_VECTOR_ADDON": "0"}, clear=False):
            text = format_vector_status(cfg, False)
        self.assertIn("有效总闸：关", text)
        self.assertIn("G4W_VECTOR_ADDON=0", text)
        self.assertIn("覆盖 json", text)
        self.assertIn("installed∧enabled=true", text)

    def test_handle_usage_unknown_subcmd(self):
        text = handle_vector_command("maybe")
        self.assertIn("用法：/vector", text)

    def test_status_not_installed(self):
        fake = _make_fake_vc(installed=False, enabled=False)
        with mock.patch.object(commands_mod, "_import_vector_config_api", return_value=fake):
            text = handle_vector_command("status")
        self.assertIn("有效总闸：关", text)
        self.assertIn("installed：False", text)
        self.assertIn("5_embedding_for_G4W.bat", text)
        self.assertIn("index meta：skipped", text)
        self.assertNotIn("numpy", text.lower())

    def test_on_without_install_writes_enabled_but_gate_off(self):
        fake = _make_fake_vc(installed=False, enabled=False)
        with mock.patch.object(commands_mod, "_import_vector_config_api", return_value=fake):
            text = handle_vector_command("on")
        self.assertTrue(fake._state["enabled"])
        self.assertFalse(fake.vector_enabled())
        self.assertIn("5_embedding_for_G4W.bat", text)
        self.assertIn("有效总闸：关", text)

    def test_on_installed_enables_gate(self):
        fake = _make_fake_vc(installed=True, enabled=False)
        with mock.patch.object(commands_mod, "_import_vector_config_api", return_value=fake):
            text = handle_vector_command("on")
        self.assertTrue(fake.vector_enabled())
        self.assertIn("已开启", text)
        self.assertIn("有效总闸：开", text)

    def test_off_disables_and_calls_stop_tei(self):
        fake = _make_fake_vc(installed=True, enabled=True)
        fake._state["pid"] = 999
        stop_calls = []

        def stop_tei():
            stop_calls.append(1)
            fake._state["pid"] = None
            return {"ok": True, "stopped": True}

        with mock.patch.object(commands_mod, "_import_vector_config_api", return_value=fake):
            with mock.patch.object(commands_mod, "_import_stop_tei", return_value=stop_tei):
                text = handle_vector_command("off")
        self.assertFalse(fake._state["enabled"])
        self.assertFalse(fake.vector_enabled())
        self.assertEqual(stop_calls, [1])
        self.assertIn("已关闭", text)
        self.assertIn("embed stop", text)

    def test_gate_not_ready_friendly_error(self):
        with mock.patch.object(
            commands_mod,
            "_import_vector_config_api",
            side_effect=ImportError("no vector_config"),
        ):
            text = handle_vector_command("status")
        # Import failure or explicit not-ready message
        self.assertTrue(
            ("总闸未就绪" in text)
            or ("向量配置模块不可用" in text)
            or ("vector_config" in text.lower())
            or ("不可用" in text)
        )


class VectorCommandRouterTests(unittest.TestCase):
    def test_router_dispatches_vector(self):
        fake = _make_fake_vc(installed=True, enabled=False)
        with tempfile.TemporaryDirectory() as td:
            service = G4WService(
                Config(state_dir=Path(td)),
                channel=FakeChannel(),
                session_factory=lambda *_: None,
            )
            binding = service.conversations.bind("account", "sender", "ctx")
            with mock.patch.object(commands_mod, "_import_vector_config_api", return_value=fake):
                text = service.commands.execute(binding, "/vector status")
            self.assertIn("向量外挂状态", text)
            self.assertIn("有效总闸：关", text)



class VectorMetaRebuildCommandTests(unittest.TestCase):
    def test_help_mentions_meta_rebuild(self):
        text = help_text()
        self.assertIn("/vector", text)
        self.assertTrue("meta" in text or "rebuild" in text)

    def test_meta_subcommand(self):
        fake = _make_fake_vc(installed=True, enabled=True)
        with mock.patch.object(commands_mod, "_import_vector_config_api", return_value=fake):
            with mock.patch(
                "G4W.memory.vector.index_meta.meta_status_lines",
                return_value=["index_dir：/tmp/x", "mismatch：否"],
            ):
                with mock.patch(
                    "G4W.memory.vector.index_meta.format_mismatch_hint",
                    return_value="",
                ):
                    with mock.patch(
                        "G4W.memory.vector.index_rebuild.format_rebuild_status",
                        return_value="🛠 索引重建状态\nstatus：idle",
                    ):
                        text = handle_vector_command("meta")
        self.assertIn("向量索引 meta", text)
        self.assertIn("index_dir", text)

    def test_rebuild_status_subcommand(self):
        with mock.patch(
            "G4W.memory.vector.index_rebuild.format_rebuild_status",
            return_value="🛠 索引重建状态\nstatus：idle",
        ):
            text = handle_vector_command("rebuild status")
        self.assertIn("索引重建状态", text)
        self.assertIn("idle", text)

    def test_rebuild_start_dry(self):
        fake_summary = {
            "ok": True,
            "count": 0,
            "dry_run": True,
            "source_items": 0,
            "status": "dry_run",
            "memory_root": "/tmp/m",
            "live": "/tmp/l",
        }
        with mock.patch(
            "G4W.memory.vector.index_rebuild.build_index_from_l4",
            return_value=fake_summary,
        ):
            with mock.patch(
                "G4W.memory.vector.index_rebuild.start_rebuild_async",
                return_value={"ok": True, "started": True, "dry_run": True, "summary": fake_summary},
            ):
                text = handle_vector_command("rebuild dry")
        self.assertTrue(
            "重建" in text or "dry" in text.lower() or "ok" in text.lower() or "启动" in text or "预览" in text
        )

    def test_status_includes_meta_tail(self):
        fake = _make_fake_vc(installed=True, enabled=False)
        with mock.patch.object(commands_mod, "_import_vector_config_api", return_value=fake):
            with mock.patch(
                "G4W.memory.vector.index_meta.meta_status_lines",
                return_value=["mismatch：否"],
            ):
                with mock.patch(
                    "G4W.memory.vector.index_meta.format_mismatch_hint",
                    return_value="",
                ):
                    text = handle_vector_command("status")
        self.assertIn("向量外挂状态", text)
        self.assertTrue("索引" in text and ("指纹" in text or "meta" in text.lower() or "mismatch" in text.lower() or "model" in text))



if __name__ == "__main__":
    unittest.main()
