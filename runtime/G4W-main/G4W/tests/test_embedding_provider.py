"""S5 embedding provider tests: hash fallback + mock HTTP soft-fail (no live keys)."""
from __future__ import annotations

import os
import unittest
from unittest import mock

import numpy as np
import requests

from G4W.memory.vector.embedding import (
    DEFAULT_DIM,
    EmbeddingError,
    embed_batch,
    embed_text,
    hash_embed,
)


def _unit_ok(v: np.ndarray, dim: int = DEFAULT_DIM) -> None:
    assert v.shape == (dim,), v.shape
    assert v.dtype == np.float32
    n = float(np.linalg.norm(v))
    assert abs(n - 1.0) < 1e-5, n


class EmbeddingHashParityTests(unittest.TestCase):
    """No key / provider=hash → same as hash_embed baseline."""

    def test_embed_batch_hash_provider_matches_hash_embed(self):
        texts = ["hello world", "向量检索", ""]
        env = {"EMBEDDING_PROVIDER": "hash"}
        with mock.patch.dict(os.environ, env, clear=False):
            for k in (
                "EMBEDDING_MODEL",
                "EMBEDDING_API_KEY",
                "ARK_API_KEY",
                "OPENAI_API_KEY",
            ):
                os.environ.pop(k, None)
            mat = embed_batch(texts, dim=DEFAULT_DIM)
        self.assertEqual(mat.shape, (len(texts), DEFAULT_DIM))
        self.assertEqual(mat.dtype, np.float32)
        for i, t in enumerate(texts):
            expected = hash_embed(t, dim=DEFAULT_DIM)
            np.testing.assert_allclose(mat[i], expected, rtol=1e-5, atol=1e-5)

    def test_embed_text_hash_when_no_model(self):
        with mock.patch.dict(os.environ, {"EMBEDDING_PROVIDER": "hash"}, clear=False):
            os.environ.pop("EMBEDDING_MODEL", None)
            v = embed_text("parity-check", dim=DEFAULT_DIM)
        _unit_ok(v)
        np.testing.assert_allclose(v, hash_embed("parity-check"), rtol=1e-5, atol=1e-5)


class EmbeddingMockHttpTests(unittest.TestCase):
    """Mock requests.post (local import in _http_embeddings): success / 4xx / timeout."""

    def test_mock_success_returns_unit_float32(self):
        dim = DEFAULT_DIM
        raw = np.ones(dim, dtype=np.float32).tolist()

        class _Resp:
            status_code = 200

            def json(self):
                return {"data": [{"embedding": raw, "index": 0}]}

        env = {
            "EMBEDDING_PROVIDER": "openai",
            "EMBEDDING_MODEL": "text-embedding-mock",
            "EMBEDDING_API_KEY": "sk-test-not-real",
            "EMBEDDING_BASE_URL": "https://example.invalid/v1",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch("requests.post", return_value=_Resp()) as post:
                v = embed_text("mock-ok", dim=dim)
        _unit_ok(v, dim)
        post.assert_called()
        # unit vector of ones is all equal ~1/sqrt(dim)
        expected_scale = 1.0 / float(np.sqrt(dim))
        self.assertAlmostEqual(float(v[0]), expected_scale, places=5)

    def test_mock_batch_success_shape(self):
        dim = 32
        texts = ["a", "b", "c"]
        rng = np.random.RandomState(0)

        class _Resp:
            status_code = 200

            def json(self):
                return {
                    "data": [
                        {
                            "embedding": rng.randn(dim).astype(np.float32).tolist(),
                            "index": i,
                        }
                        for i in range(len(texts))
                    ]
                }

        env = {
            "EMBEDDING_PROVIDER": "ark",
            "EMBEDDING_MODEL": "mock-model",
            "EMBEDDING_API_KEY": "test-key-name-only",
            "EMBEDDING_BASE_URL": "https://example.invalid/api/v3",
            "EMBEDDING_DIM": str(dim),
        }
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch("requests.post", return_value=_Resp()):
                mat = embed_batch(texts, dim=dim)
        self.assertEqual(mat.shape, (3, dim))
        self.assertEqual(mat.dtype, np.float32)
        for i in range(3):
            n = float(np.linalg.norm(mat[i]))
            self.assertAlmostEqual(n, 1.0, places=4)

    def test_mock_4xx_remote_failure_raises_no_hash_fallback(self):
        class _Resp:
            status_code = 401

            def json(self):
                return {"error": "unauthorized"}

        env = {
            "EMBEDDING_PROVIDER": "openai",
            "EMBEDDING_MODEL": "text-embedding-mock",
            "EMBEDDING_API_KEY": "sk-bad",
            "EMBEDDING_BASE_URL": "https://example.invalid/v1",
            "EMBEDDING_ALLOW_HASH_FALLBACK": "0",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch("requests.post", return_value=_Resp()):
                with self.assertRaises(EmbeddingError) as cm:
                    embed_text("remote-fail-4xx", dim=DEFAULT_DIM)
        self.assertEqual(cm.exception.reason, "remote_http_failed")

    def test_mock_timeout_remote_failure_raises_no_hash_fallback(self):
        env = {
            "EMBEDDING_PROVIDER": "openai",
            "EMBEDDING_MODEL": "text-embedding-mock",
            "EMBEDDING_API_KEY": "sk-timeout",
            "EMBEDDING_BASE_URL": "https://example.invalid/v1",
            "EMBEDDING_ALLOW_HASH_FALLBACK": "0",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch(
                "requests.post",
                side_effect=requests.Timeout("timed out"),
            ):
                with self.assertRaises(EmbeddingError) as cm:
                    embed_text("remote-fail-timeout", dim=DEFAULT_DIM)
        self.assertEqual(cm.exception.reason, "remote_http_failed")

    def test_explicit_hash_fallback_opt_in_keeps_legacy_shape(self):
        class _Resp:
            status_code = 401

            def json(self):
                return {"error": "unauthorized"}

        env = {
            "EMBEDDING_PROVIDER": "openai",
            "EMBEDDING_MODEL": "text-embedding-mock",
            "EMBEDDING_API_KEY": "sk-bad",
            "EMBEDDING_BASE_URL": "https://example.invalid/v1",
            "EMBEDDING_ALLOW_HASH_FALLBACK": "1",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch("requests.post", return_value=_Resp()):
                v = embed_text("explicit-fallback", dim=DEFAULT_DIM)
        _unit_ok(v)
        np.testing.assert_allclose(
            v, hash_embed("explicit-fallback", dim=DEFAULT_DIM), rtol=1e-5, atol=1e-5
        )


class HybridSmokeWithEmbedTests(unittest.TestCase):
    """hybrid_query upsert/search with embed facade (hash provider)."""

    def test_hybrid_upsert_search_smoke(self):
        from G4W.memory.vector.hybrid_query import HybridQueryEngine
        from G4W.memory.vector.hnsw_index import BruteIndex

        dim = 32
        idx = BruteIndex(dim=dim)
        eng = HybridQueryEngine(index=idx, dim=dim)
        with mock.patch.dict(os.environ, {"EMBEDDING_PROVIDER": "hash"}, clear=False):
            eng.upsert("id1", "alpha document about cats")
            eng.upsert("id2", "beta document about dogs")
            hits = eng.search("cats", k=2)
        self.assertTrue(len(hits) >= 1)
        ids = [h.item_id for h in hits]
        self.assertIn("id1", ids)


if __name__ == "__main__":
    unittest.main()
