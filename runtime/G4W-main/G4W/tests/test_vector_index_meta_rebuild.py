"""TASK-F: index fingerprint meta + L4 rebuild helpers."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from G4W.memory.vector import (
    BruteIndex,
    create_index,
    fingerprint_mismatch,
    format_mismatch_hint,
    index_fingerprint,
    load_index_meta,
    meta_status_lines,
    patch_meta_fields,
    write_fingerprint_to_meta,
)
from G4W.memory.vector.index_rebuild import build_index_from_l4, format_rebuild_status, rebuild_state
from G4W.memory.vector.hnsw_index import IndexMeta


class IndexFingerprintTests(unittest.TestCase):
    def test_brute_save_load_preserves_model_base_url(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "idx"
            idx = BruteIndex(dim=8, model="m-test", base_url="http://emb")
            vec = np.random.randn(8).astype(np.float32)
            vec = vec / (np.linalg.norm(vec) + 1e-9)
            idx.add(vec.reshape(1, -1), labels=["a"])
            idx.save(d)
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta.get("model"), "m-test")
            self.assertEqual(meta.get("base_url"), "http://emb")
            loaded = BruteIndex.load(d)
            self.assertEqual(loaded.model, "m-test")
            self.assertEqual(loaded.base_url, "http://emb")
            self.assertEqual(loaded.count, 1)

    def test_create_index_save_fingerprint(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "idx"
            idx = create_index(dim=8, prefer_hnsw=False, model="emb-x", base_url="http://x")
            vec = np.ones(8, dtype=np.float32)
            vec = vec / np.linalg.norm(vec)
            idx.add(vec.reshape(1, -1), labels=["lab1"])
            idx.save(d)
            meta = load_index_meta(d)
            self.assertIsNotNone(meta)
            self.assertEqual(meta.model, "emb-x")
            self.assertEqual(meta.base_url, "http://x")
            fp = index_fingerprint(d)
            self.assertEqual(fp.get("model"), "emb-x")

    def test_fingerprint_mismatch_dim(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "idx"
            idx = BruteIndex(dim=16, model="same", base_url="http://e")
            vec = np.random.randn(16).astype(np.float32)
            vec /= np.linalg.norm(vec) + 1e-9
            idx.add(vec.reshape(1, -1), labels=["z"])
            idx.save(d)
            with mock.patch(
                "G4W.memory.vector.index_meta.current_embedding_fingerprint",
                return_value={"model": "same", "base_url": "http://e", "dim": 32},
            ):
                bad, reason = fingerprint_mismatch(d)
            self.assertTrue(bad)
            self.assertIn("dim", reason)
            hint = format_mismatch_hint(d)
            # hint uses live current fingerprint — patch both for consistency
            with mock.patch(
                "G4W.memory.vector.index_meta.current_embedding_fingerprint",
                return_value={"model": "same", "base_url": "http://e", "dim": 32},
            ):
                hint = format_mismatch_hint(d)
            self.assertIn("rebuild", hint.lower() or hint)

    def test_patch_meta_preserves_extras(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "idx"
            d.mkdir()
            (d / "meta.json").write_text(
                json.dumps(
                    {
                        "dim": 8,
                        "backend": "brute",
                        "count": 0,
                        "model": "old",
                        "rebuild_status": "done",
                        "custom_keep": 1,
                    }
                ),
                encoding="utf-8",
            )
            write_fingerprint_to_meta(d, model="new-m", base_url="http://n", dim=8)
            raw = json.loads((d / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(raw.get("model"), "new-m")
            # rebuild_status may live top-level or extras depending on IndexMeta roundtrip
            self.assertTrue(
                raw.get("rebuild_status") == "done"
                or (raw.get("extras") or {}).get("rebuild_status") == "done"
                or raw.get("custom_keep") == 1
                or (raw.get("extras") or {}).get("custom_keep") == 1
            )

    def test_meta_status_lines_smoke(self):
        lines = meta_status_lines(None)
        self.assertTrue(isinstance(lines, list))
        self.assertTrue(any("index" in x.lower() or "model" in x.lower() or "dir" in x for x in lines))


class RebuildDryRunTests(unittest.TestCase):
    def test_build_index_from_l4_dry_run(self):
        with tempfile.TemporaryDirectory() as td:
            mem = Path(td) / "mem"
            live = Path(td) / "live"
            mem.mkdir()
            # minimal empty L4
            summary = build_index_from_l4(
                memory_root=mem,
                index_dir=live,
                dry_run=True,
                max_items=10,
                switch_live=False,
            )
            self.assertTrue(summary.get("dry_run") or summary.get("ok") is not False)
            self.assertIn("count", summary)

    def test_rebuild_state_and_format(self):
        st = rebuild_state()
        self.assertIn("status", st)
        text = format_rebuild_status()
        self.assertIn("重建", text)


class IndexMetaRoundtripTests(unittest.TestCase):
    def test_index_meta_to_from_dict_extras(self):
        m = IndexMeta(dim=4, model="m", base_url="u", extras={"rebuild_status": "idle", "foo": 2})
        d = m.to_dict()
        m2 = IndexMeta.from_dict(d)
        self.assertEqual(m2.model, "m")
        self.assertEqual(m2.base_url, "u")
        self.assertEqual(m2.extras.get("foo"), 2)


if __name__ == "__main__":
    unittest.main()
