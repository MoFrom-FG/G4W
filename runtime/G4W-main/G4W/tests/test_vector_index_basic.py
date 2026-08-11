"""Basic tests for G4W.memory.vector (S7 default ON, insert/search, sandbox)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from G4W.memory.vector import (
    create_index,
    embed_batch,
    hash_embed,
    hnswlib_available,
    vector_retrieval_enabled,
)
from G4W.memory.vector.embedding import DEFAULT_DIM
from G4W.memory.vector.sandbox_paths import (
    DEFAULT_PROD_INDEX_DIR,
    LEGACY_PROD_INDEX_DIR,
    assert_prod_index_write_allowed,
    assert_sandbox_write_allowed,
    default_prod_index_dir,
    ensure_sandbox_dir,
    legacy_prod_index_dir,
    prod_index_staging_dir,
    resolve_vector_index_dir,
    sandbox_vector_index_root,
)


class VectorFlagsTests(unittest.TestCase):
    def test_vector_flags_default_on(self):
        """S7: unset + empty .env → True."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("G4W_VECTOR_RETRIEVAL", None)
            with mock.patch(
                "G4W.memory.vector.flags._read_env_file_value", return_value=""
            ):
                self.assertTrue(vector_retrieval_enabled())

    def test_vector_flags_explicit_off(self):
        """Rollback: =0 / false / off → False."""
        for val in ("0", "false", "off", "no"):
            with mock.patch.dict(
                os.environ, {"G4W_VECTOR_RETRIEVAL": val}, clear=False
            ):
                self.assertFalse(
                    vector_retrieval_enabled(), msg=f"expected False for {val!r}"
                )

    def test_vector_flags_on(self):
        with mock.patch.dict(os.environ, {"G4W_VECTOR_RETRIEVAL": "1"}):
            self.assertTrue(vector_retrieval_enabled())


class VectorInsertSearchTests(unittest.TestCase):
    def test_hash_embed_dim(self):
        v = hash_embed("hello world")
        self.assertEqual(v.shape, (DEFAULT_DIM,))
        self.assertAlmostEqual(float(np.linalg.norm(v)), 1.0, places=5)

    def test_vector_insert_search_sandbox(self):
        n = 120
        texts = [f"doc-{i}-alpha beta gamma {i % 7}" for i in range(n)]
        mat = np.vstack([hash_embed(text, dim=DEFAULT_DIM) for text in texts]).astype(np.float32)
        self.assertEqual(mat.shape, (n, DEFAULT_DIM))

        idx = create_index(dim=DEFAULT_DIM, M=16, ef_construction=200, ef_search=50)
        labels = [f"id-{i}" for i in range(n)]
        idx.add(mat, labels=labels)
        self.assertEqual(idx.count, n)

        q = hash_embed(texts[42])
        hits = idx.search(q, k=10)
        self.assertGreaterEqual(len(hits), 1)
        self.assertEqual(hits[0].label, "id-42")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "vector_index"
            idx.save(root)
            self.assertTrue((root / "meta.json").exists())
            loaded = type(idx).load(root)
            self.assertEqual(loaded.count, n)
            hits2 = loaded.search(q, k=5)
            self.assertEqual(hits2[0].label, "id-42")

    def test_sandbox_path_under_bbs(self):
        with tempfile.TemporaryDirectory() as td:
            bbs = Path(td)
            d = ensure_sandbox_dir(bbs)
            self.assertTrue(d.exists())
            self.assertIn("sandbox", str(d).replace("\\", "/"))
            self.assertIn("vector_index", str(d))
            # also via env-style helper
            p = sandbox_vector_index_root(bbs)
            self.assertEqual(p, d)

    def test_backend_reported(self):
        idx = create_index()
        self.assertIn(idx.backend, ("hnswlib", "brute"))
        # hnswlib was installed in venv for this window
        if hnswlib_available():
            self.assertEqual(idx.backend, "hnswlib")

    def test_brute_replace_no_ghost_labels(self):
        from G4W.memory.vector.hnsw_index import BruteIndex

        idx = BruteIndex(dim=DEFAULT_DIM)
        v1 = hash_embed("one")
        v2 = hash_embed("two")
        idx.add(v1.reshape(1, -1), labels=["x"])
        idx.add(v2.reshape(1, -1), labels=["x"], replace=True)
        self.assertEqual(idx.count, 1)
        self.assertEqual(idx._labels, ["x"])
        hits = idx.search(v2, k=5)
        self.assertEqual(len([h for h in hits if h.label == "x"]), 1)

    def test_search_k_zero_empty(self):
        from G4W.memory.vector.hnsw_index import BruteIndex

        idx = BruteIndex(dim=DEFAULT_DIM)
        idx.add(hash_embed("a").reshape(1, -1), labels=["a"])
        self.assertEqual(idx.search(hash_embed("a"), k=0), [])
        self.assertEqual(idx.search(hash_embed("a"), k=-1), [])

    def test_hnsw_soft_replace_search_no_crash(self):
        """W4 U-FIX-1: soft-replace must not make knn_query raise; live count."""
        from G4W.memory.vector.hnsw_index import HnswIndex

        if not hnswlib_available():
            self.skipTest("hnswlib not installed")
        dim = 32
        idx = HnswIndex(dim=dim, prefer_hnsw=True, max_elements=64)
        self.assertEqual(idx.backend, "hnswlib")
        v1 = hash_embed("first-version", dim=dim)
        v2 = hash_embed("second-version", dim=dim)
        v3 = hash_embed("third-version", dim=dim)
        idx.add(v1.reshape(1, -1), labels=["dup"])
        idx.add(v2.reshape(1, -1), labels=["dup"], replace=True)
        idx.add(v3.reshape(1, -1), labels=["dup"], replace=True)
        self.assertEqual(idx.count, 1)
        for k in (1, 2, 5):
            hits = idx.search(v3, k=k)
            labels = [h.label for h in hits]
            self.assertTrue(all(not str(x).startswith("__deleted_") for x in labels))
            self.assertIn("dup", labels)
            self.assertEqual(labels.count("dup"), 1)

    def test_hnsw_multi_plus_replace_search(self):
        """Multi live docs + one soft-replace; search k up to live stays safe."""
        from G4W.memory.vector.hnsw_index import HnswIndex

        if not hnswlib_available():
            self.skipTest("hnswlib not installed")
        dim = 32
        idx = HnswIndex(dim=dim, prefer_hnsw=True, max_elements=64)
        va = hash_embed("alpha-doc", dim=dim)
        vb = hash_embed("beta-doc", dim=dim)
        vc = hash_embed("gamma-doc", dim=dim)
        vc2 = hash_embed("gamma-doc-v2", dim=dim)
        idx.add(va.reshape(1, -1), labels=["a"])
        idx.add(vb.reshape(1, -1), labels=["b"])
        idx.add(vc.reshape(1, -1), labels=["c"])
        idx.add(vc2.reshape(1, -1), labels=["c"], replace=True)
        self.assertEqual(idx.count, 3)
        for k in (1, 2, 3, 5):
            hits = idx.search(vc2, k=k)
            labels = [h.label for h in hits]
            self.assertTrue(all(not str(x).startswith("__deleted_") for x in labels))
            self.assertLessEqual(len(hits), min(k, 3))
            self.assertEqual(labels.count("c"), 1 if "c" in labels else 0)


class SandboxWriteGuardTests(unittest.TestCase):
    """W3 FIX-1: default deny + FORBIDDEN name fragments."""

    def test_allow_sandbox_vector_index(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "workspace" / "sandbox" / "vector_index" / "idx"
            p.mkdir(parents=True, exist_ok=True)
            out = assert_sandbox_write_allowed(p)
            self.assertEqual(out, p.resolve())

    def test_deny_daily_primary(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "daily_primary" / "shard0"
            p.mkdir(parents=True, exist_ok=True)
            with self.assertRaises(ValueError) as cm:
                assert_sandbox_write_allowed(p)
            self.assertIn("daily_primary", str(cm.exception))

    def test_deny_hybrid(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "hybrid" / "store"
            p.mkdir(parents=True, exist_ok=True)
            with self.assertRaises(ValueError) as cm:
                assert_sandbox_write_allowed(p)
            self.assertIn("hybrid", str(cm.exception))

    def test_deny_transcript_and_aggregate(self):
        with tempfile.TemporaryDirectory() as td:
            for name in ("transcript", "aggregate"):
                p = Path(td) / name / "x"
                p.mkdir(parents=True, exist_ok=True)
                with self.assertRaises(ValueError) as cm:
                    assert_sandbox_write_allowed(p)
                self.assertIn(name, str(cm.exception))

    def test_deny_non_sandbox_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "random_dir" / "idx"
            p.mkdir(parents=True, exist_ok=True)
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("G4W_VECTOR_SANDBOX_ALLOW_TEMP", None)
                with self.assertRaises(ValueError) as cm:
                    assert_sandbox_write_allowed(p)
                self.assertIn("outside sandbox", str(cm.exception).lower())

    def test_forbidden_wins_even_under_sandbox_name(self):
        """daily_primary segment under a path still denied (name fragment first)."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sandbox" / "vector_index" / "daily_primary" / "bad"
            p.mkdir(parents=True, exist_ok=True)
            with self.assertRaises(ValueError) as cm:
                assert_sandbox_write_allowed(p)
            self.assertIn("daily_primary", str(cm.exception))

    def test_temp_allow_env(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "tmp_only" / "idx"
            p.mkdir(parents=True, exist_ok=True)
            with mock.patch.dict(
                os.environ, {"G4W_VECTOR_SANDBOX_ALLOW_TEMP": "1"}, clear=False
            ):
                # Path under system temp usually contains Temp/tmp — assert uses temp markers
                out = assert_sandbox_write_allowed(p)
                self.assertEqual(out, p.resolve())


class VectorIndexDirResolveTests(unittest.TestCase):
    """S6-I1: resolve_vector_index_dir + prod write guard."""

    def test_resolve_defaults_to_sandbox(self):
        """When env unset and DEFAULT_PROD not ready → sandbox."""
        with tempfile.TemporaryDirectory() as td:
            bbs = Path(td)
            fake_prod = Path(td) / "not-ready-prod"
            fake_legacy = Path(td) / "not-ready-legacy"
            with mock.patch.dict(os.environ, {"G4W_BBS_CWD": str(bbs)}, clear=False):
                os.environ.pop("G4W_VECTOR_INDEX_DIR", None)
                with mock.patch(
                    "G4W.memory.vector.sandbox_paths.DEFAULT_PROD_INDEX_DIR",
                    fake_prod,
                ), mock.patch(
                    "G4W.memory.vector.sandbox_paths.LEGACY_PROD_INDEX_DIR",
                    fake_legacy,
                ):
                    out = resolve_vector_index_dir(bbs)
                    self.assertEqual(out, sandbox_vector_index_root(bbs))

    def test_resolve_prod_prefer_when_ready(self):
        """S7: unset env + DEFAULT_PROD ready → prod path (not sandbox)."""
        with tempfile.TemporaryDirectory() as td:
            bbs = Path(td) / "bbs"
            prod = Path(td) / "prod-idx"
            bbs.mkdir(parents=True)
            prod.mkdir(parents=True)
            (prod / "meta.json").write_text("{}", encoding="utf-8")
            (prod / "vectors.npy").write_bytes(b"x")
            with mock.patch.dict(os.environ, {"G4W_BBS_CWD": str(bbs)}, clear=False):
                os.environ.pop("G4W_VECTOR_INDEX_DIR", None)
                with mock.patch(
                    "G4W.memory.vector.sandbox_paths.DEFAULT_PROD_INDEX_DIR",
                    prod,
                ):
                    out = resolve_vector_index_dir(bbs)
                    self.assertEqual(out, prod.resolve())
                    self.assertNotEqual(out, sandbox_vector_index_root(bbs))

    def test_resolve_uses_env_index_dir(self):
        with tempfile.TemporaryDirectory() as td:
            idx = Path(td) / "prod-idx"
            idx.mkdir()
            with mock.patch.dict(
                os.environ, {"G4W_VECTOR_INDEX_DIR": str(idx)}, clear=False
            ):
                out = resolve_vector_index_dir()
                self.assertEqual(out, idx.resolve())

    def test_resolve_prefers_memory_subdir_over_legacy_root(self):
        with tempfile.TemporaryDirectory() as td:
            bbs = Path(td) / "bbs"
            base = Path(td) / "G4W-vector-index"
            legacy = base
            prod = base / "memory"
            bbs.mkdir(parents=True)
            for root in (legacy, prod):
                root.mkdir(parents=True)
                (root / "meta.json").write_text("{}", encoding="utf-8")
                (root / "vectors.npy").write_bytes(b"x")
            with mock.patch.dict(os.environ, {"G4W_BBS_CWD": str(bbs)}, clear=False):
                os.environ.pop("G4W_VECTOR_INDEX_DIR", None)
                with mock.patch(
                    "G4W.memory.vector.sandbox_paths.DEFAULT_PROD_INDEX_DIR",
                    prod,
                ), mock.patch(
                    "G4W.memory.vector.sandbox_paths.LEGACY_PROD_INDEX_DIR",
                    legacy,
                ):
                    self.assertEqual(resolve_vector_index_dir(bbs), prod.resolve())

    def test_resolve_falls_back_to_legacy_root_index(self):
        with tempfile.TemporaryDirectory() as td:
            bbs = Path(td) / "bbs"
            legacy = Path(td) / "G4W-vector-index"
            prod = legacy / "memory"
            bbs.mkdir(parents=True)
            legacy.mkdir(parents=True)
            (legacy / "meta.json").write_text("{}", encoding="utf-8")
            (legacy / "vectors.npy").write_bytes(b"x")
            with mock.patch.dict(os.environ, {"G4W_BBS_CWD": str(bbs)}, clear=False):
                os.environ.pop("G4W_VECTOR_INDEX_DIR", None)
                with mock.patch(
                    "G4W.memory.vector.sandbox_paths.DEFAULT_PROD_INDEX_DIR",
                    prod,
                ), mock.patch(
                    "G4W.memory.vector.sandbox_paths.LEGACY_PROD_INDEX_DIR",
                    legacy,
                ):
                    self.assertEqual(resolve_vector_index_dir(bbs), legacy.resolve())

    def test_default_prod_index_dir_constant(self):
        self.assertEqual(
            default_prod_index_dir(),
            Path(DEFAULT_PROD_INDEX_DIR).resolve(),
        )
        self.assertEqual(legacy_prod_index_dir(), Path(LEGACY_PROD_INDEX_DIR).resolve())
        self.assertIn("G4W-vector-index", str(DEFAULT_PROD_INDEX_DIR))
        self.assertEqual(Path(DEFAULT_PROD_INDEX_DIR).name, "memory")

    def test_assert_prod_allow_under_index_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "G4W-vector-index"
            target = root / "chunk-a"
            target.mkdir(parents=True)
            with mock.patch.dict(
                os.environ, {"G4W_VECTOR_INDEX_DIR": str(root)}, clear=False
            ):
                out = assert_prod_index_write_allowed(target)
                self.assertEqual(out, target.resolve())
                staging = prod_index_staging_dir()
                staging.mkdir(parents=True, exist_ok=True)
                self.assertEqual(
                    assert_prod_index_write_allowed(staging / "x"),
                    (staging / "x").resolve(),
                )

    def test_assert_prod_allow_bak_tree(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "G4W-vector-index"
            bak = Path(td) / "G4W-vector-index.bak.20260101"
            (bak / "meta").mkdir(parents=True)
            with mock.patch.dict(
                os.environ, {"G4W_VECTOR_INDEX_DIR": str(root)}, clear=False
            ):
                out = assert_prod_index_write_allowed(bak / "meta")
                self.assertEqual(out, (bak / "meta").resolve())

    def test_assert_prod_allows_legacy_root_but_denies_knowledge_subtree(self):
        with tempfile.TemporaryDirectory() as td:
            legacy = Path(td) / "G4W-vector-index"
            prod = legacy / "memory"
            legacy.mkdir(parents=True)
            (legacy / "meta.json").write_text("{}", encoding="utf-8")
            (legacy / "vectors.npy").write_bytes(b"x")
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("G4W_VECTOR_INDEX_DIR", None)
                with mock.patch(
                    "G4W.memory.vector.sandbox_paths.DEFAULT_PROD_INDEX_DIR",
                    prod,
                ), mock.patch(
                    "G4W.memory.vector.sandbox_paths.LEGACY_PROD_INDEX_DIR",
                    legacy,
                ):
                    self.assertEqual(
                        assert_prod_index_write_allowed(legacy / "docs.jsonl"),
                        (legacy / "docs.jsonl").resolve(),
                    )
                    with self.assertRaises(ValueError):
                        assert_prod_index_write_allowed(legacy / "knowledge" / "hnsw")

    def test_assert_prod_deny_forbidden_and_outside(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "G4W-vector-index"
            root.mkdir()
            hybrid = Path(td) / "hybrid" / "vec"
            hybrid.mkdir(parents=True)
            outside = Path(td) / "other" / "vec"
            outside.mkdir(parents=True)
            with mock.patch.dict(
                os.environ, {"G4W_VECTOR_INDEX_DIR": str(root)}, clear=False
            ):
                with self.assertRaises(ValueError) as cm:
                    assert_prod_index_write_allowed(hybrid)
                self.assertIn("forbidden", str(cm.exception).lower())
                with self.assertRaises(ValueError) as cm2:
                    assert_prod_index_write_allowed(outside)
                self.assertIn("outside", str(cm2.exception).lower())


if __name__ == "__main__":
    unittest.main()
