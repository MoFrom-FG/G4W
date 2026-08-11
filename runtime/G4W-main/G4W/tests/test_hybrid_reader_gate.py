"""Unit tests for HybridMainReader.available integrity gate (tempfile only)."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from G4W.memory.hybrid_reader import HybridMainReader


def _init_meta(meta_path: Path, chunks: int, cas_index: int) -> None:
    conn = sqlite3.connect(str(meta_path))
    try:
        conn.execute(
            "CREATE TABLE chunks ("
            "chunk_id TEXT PRIMARY KEY, cas_hash TEXT, source_path TEXT)"
        )
        conn.execute(
            "CREATE TABLE cas_index ("
            "cas_hash TEXT PRIMARY KEY, size_bytes INTEGER)"
        )
        for i in range(chunks):
            conn.execute(
                "INSERT INTO chunks(chunk_id, cas_hash, source_path) VALUES (?,?,?)",
                (f"c{i}", f"h{i}", f"p{i}"),
            )
        for i in range(cas_index):
            conn.execute(
                "INSERT INTO cas_index(cas_hash, size_bytes) VALUES (?,?)",
                (f"h{i}", 1),
            )
        conn.commit()
    finally:
        conn.close()


class HybridReaderGateTests(unittest.TestCase):
    def test_missing_surface_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r = HybridMainReader(root)
            self.assertFalse(r.available())

    def test_meta_without_cas_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "meta.sqlite").write_bytes(b"")
            r = HybridMainReader(root)
            self.assertFalse(r.available())

    def test_empty_tables_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "cas").mkdir()
            _init_meta(root / "meta.sqlite", chunks=0, cas_index=0)
            r = HybridMainReader(root)
            self.assertFalse(r.available())

    def test_count_mismatch_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "cas").mkdir()
            _init_meta(root / "meta.sqlite", chunks=2, cas_index=1)
            r = HybridMainReader(root)
            self.assertFalse(r.available())

    def test_consistent_counts_available(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "cas").mkdir()
            _init_meta(root / "meta.sqlite", chunks=3, cas_index=3)
            r = HybridMainReader(root)
            self.assertTrue(r.available())

    def test_min_chunks_floor(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "cas").mkdir()
            _init_meta(root / "meta.sqlite", chunks=2, cas_index=2)
            r = HybridMainReader(root)
            with mock.patch.dict(os.environ, {"G4W_HYBRID_MIN_CHUNKS": "5"}, clear=False):
                self.assertFalse(r.available())
            with mock.patch.dict(os.environ, {"G4W_HYBRID_MIN_CHUNKS": "1"}, clear=False):
                self.assertTrue(r.available())

    def test_corrupt_sqlite_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "cas").mkdir()
            (root / "meta.sqlite").write_bytes(b"not-a-sqlite-db")
            r = HybridMainReader(root)
            self.assertFalse(r.available())

    def test_missing_tables_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "cas").mkdir()
            conn = sqlite3.connect(str(root / "meta.sqlite"))
            conn.execute("CREATE TABLE other(x INTEGER)")
            conn.commit()
            conn.close()
            r = HybridMainReader(root)
            self.assertFalse(r.available())

    def test_search_drops_near_zero_scores(self):
        """Near-zero fuse scores must not be returned as filler hits."""
        # Avoid sqlite tempfile (Win file lock on cleanup); inject docs directly.
        r = HybridMainReader(Path("."))
        r._docs = {
            "c0": "完全无关的中文噪声文本甲乙丙",
            "c1": "另一段无关内容丁戊己",
        }
        r._meta = {
            "c0": {"cas_hash": "h0", "source_path": "p0"},
            "c1": {"cas_hash": "h1", "source_path": "p1"},
        }
        r._mtime = 1.0

        def _noop_reload():
            return None

        r._reload_if_needed = _noop_reload  # type: ignore[method-assign]
        hits = r.search("大悦城长安大排档吃了什么", top_k=5)
        self.assertTrue(all(h.score >= 0.02 for h in hits))


if __name__ == "__main__":
    unittest.main()
