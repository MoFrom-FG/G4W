"""TASK-G: query/registry path respects vector_enabled product gate."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from G4W.memory.vector import embedding as emb
from G4W.memory.vector import vector_config as vc


class EmbeddingVectorConfigOverrideTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.primary = self.root / "G4W-embedding" / "vector_config.json"
        self.fallback = self.root / "G4W-data" / "vector_config.json"
        vc.reset_cache_for_tests()
        os.environ.pop("G4W_VECTOR_ADDON", None)
        # Clear embedding env so config wins
        for k in (
            "EMBEDDING_PROVIDER",
            "EMBEDDING_BASE_URL",
            "EMBEDDING_MODEL",
            "EMBEDDING_DIM",
            "EMBEDDING_API_KEY",
            "ARK_API_KEY",
            "OPENAI_API_KEY",
        ):
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        vc.reset_cache_for_tests()
        os.environ.pop("G4W_VECTOR_ADDON", None)
        for k in (
            "EMBEDDING_PROVIDER",
            "EMBEDDING_BASE_URL",
            "EMBEDDING_MODEL",
            "EMBEDDING_DIM",
            "EMBEDDING_API_KEY",
            "ARK_API_KEY",
            "OPENAI_API_KEY",
        ):
            os.environ.pop(k, None)
        self._tmp.cleanup()

    def _patch_paths(self):
        return mock.patch.multiple(
            vc,
            _primary_path=lambda: self.primary,
            _fallback_path=lambda: self.fallback,
            _runtime_root=lambda: self.root,
        )

    def _write_cfg(self, **kwargs):
        payload = {
            "enabled": True,
            "installed": True,
            "model": "BAAI/bge-m3",
            "dim": 1024,
            "base_url": "http://127.0.0.1:8080",
        }
        payload.update(kwargs)
        self.primary.parent.mkdir(parents=True, exist_ok=True)
        self.primary.write_text(json.dumps(payload), encoding="utf-8")
        vc.reset_cache_for_tests()

    def test_addon_off_ignores_vector_config_for_dim(self):
        with self._patch_paths():
            self._write_cfg(enabled=False, installed=True, dim=1024)
            os.environ["EMBEDDING_DIM"] = "384"
            self.assertEqual(emb.resolve_dim(), 384)

    def test_addon_on_prefers_config_dim_over_env(self):
        with self._patch_paths():
            self._write_cfg(dim=1024)
            os.environ["EMBEDDING_DIM"] = "384"
            self.assertEqual(emb.resolve_dim(), 1024)

    def test_addon_on_prefers_config_base_and_model(self):
        with self._patch_paths():
            self._write_cfg(
                base_url="http://127.0.0.1:9999",
                model="tei-model-x",
            )
            os.environ["EMBEDDING_BASE_URL"] = "http://lmstudio:1234/v1"
            os.environ["EMBEDDING_MODEL"] = "lm-studio-model"
            self.assertEqual(emb.resolve_provider(), "openai")
            ok, base, model, key = emb._remote_ready("openai")
            self.assertTrue(ok)
            self.assertEqual(base, "http://127.0.0.1:9999")
            self.assertEqual(model, "tei-model-x")
            # local TEI allows empty key
            self.assertEqual(key, "not-needed")

    def test_commercial_openai_still_needs_key_when_addon_off(self):
        with self._patch_paths():
            # no config / addon off
            os.environ["EMBEDDING_PROVIDER"] = "openai"
            os.environ["EMBEDDING_BASE_URL"] = "https://api.openai.com/v1"
            os.environ["EMBEDDING_MODEL"] = "text-embedding-3-small"
            ok, *_ = emb._remote_ready("openai")
            self.assertFalse(ok)


class ConversationInjectGateTests(unittest.TestCase):
    def tearDown(self) -> None:
        vc.reset_cache_for_tests()
        os.environ.pop("G4W_VECTOR_ADDON", None)

    def test_read_memory_skips_vector_section_when_addon_off(self):
        from G4W.memory.conversation import ConversationStore

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mem = root / "memory"
            store = ConversationStore(root=root, memory_root=mem)
            with mock.patch(
                "G4W.memory.vector.vector_config.vector_enabled",
                return_value=False,
            ):
                with mock.patch(
                    "G4W.memory.vector.prod_inject.vector_section_for",
                    return_value="## Vector Memory\nSHOULD_NOT_APPEAR",
                ) as vsec:
                    with mock.patch(
                        "G4W.memory.vector.flags.vector_retrieval_enabled",
                        return_value=True,
                    ):
                        stable = store.read_memory("sender1", query="hello vector")
                        text = store.retrieval_context("sender1", query="hello vector")
            self.assertNotIn("SHOULD_NOT_APPEAR", stable)
            self.assertNotIn("SHOULD_NOT_APPEAR", text)
            vsec.assert_not_called()

    def test_read_memory_calls_vector_when_addon_on_and_legacy_on(self):
        from G4W.memory.conversation import ConversationStore

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mem = root / "memory"
            store = ConversationStore(root=root, memory_root=mem)
            with mock.patch(
                "G4W.memory.vector.vector_config.vector_enabled",
                return_value=True,
            ):
                with mock.patch(
                    "G4W.memory.vector.vector_retrieval_enabled",
                    return_value=True,
                ):
                    with mock.patch(
                        "G4W.memory.vector.prod_inject.vector_section_for",
                        return_value="## Vector Memory\nHIT",
                    ) as vsec:
                        stable = store.read_memory("sender1", query="hello")
                        text = store.retrieval_context("sender1", query="hello")
            self.assertNotIn("HIT", stable)
            self.assertIn("HIT", text)
            vsec.assert_called()


class HandlerMemorySearchGateTests(unittest.TestCase):
    def _handler(self):
        from G4W.agents.handlers import ConductorHandler

        class _C:
            conversations = None

        h = ConductorHandler.__new__(ConductorHandler)
        h.controller = _C()
        h.sender_id = "u1"
        return h

    def _payload(self, outcome):
        payload = getattr(outcome, "data", None)
        if payload is None and isinstance(outcome, dict):
            payload = outcome
        return payload

    def test_default_scope_is_hybrid_and_skips_vector_path(self):
        h = self._handler()
        with mock.patch(
            "G4W.memory.vector.flags.vector_retrieval_enabled",
            side_effect=AssertionError("default search must not touch vector"),
        ):
            outcome = h.do_G4W_memory_search({"query": "q", "k": 3}, response=None)
        payload = self._payload(outcome)
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload.get("scope"), "hybrid")
        self.assertEqual(payload.get("vector"), {"enabled": False, "hits": []})

    def test_invalid_scope_falls_back_to_hybrid_and_skips_vector_path(self):
        h = self._handler()
        with mock.patch(
            "G4W.memory.vector.flags.vector_retrieval_enabled",
            side_effect=AssertionError("invalid scope must not touch vector"),
        ):
            outcome = h.do_G4W_memory_search(
                {"query": "q", "scope": "bad", "k": 3}, response=None
            )
        payload = self._payload(outcome)
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload.get("scope"), "hybrid")
        self.assertEqual(payload.get("vector"), {"enabled": False, "hits": []})

    def test_vector_scope_notes_addon_disabled(self):
        """do_G4W_memory_search is ConductorHandler method; gate via stub."""
        h = self._handler()

        with mock.patch(
            "G4W.memory.vector.vector_config.vector_enabled",
            return_value=False,
        ):
            with mock.patch(
                "G4W.memory.vector.flags.vector_retrieval_enabled",
                return_value=True,
            ):
                outcome = h.do_G4W_memory_search(
                    {"query": "q", "scope": "vector", "k": 3}, response=None
                )
        payload = self._payload(outcome)
        self.assertIsInstance(payload, dict)
        vec = payload.get("vector") or {}
        self.assertEqual(vec.get("note"), "vector_addon disabled")
        self.assertFalse(vec.get("hits"))


if __name__ == "__main__":
    unittest.main()
