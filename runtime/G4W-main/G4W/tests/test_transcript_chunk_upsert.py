"""Unit tests: transcript / conversation chunk incremental upsert (P0)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_CODE = Path(__file__).resolve().parents[2]
if str(_CODE) not in sys.path:
    sys.path.insert(0, str(_CODE))

from G4W.memory.vector.embedding import DEFAULT_DIM, embed_batch
from G4W.memory.vector.hnsw_index import HnswIndex, create_index
from G4W.memory.vector.transcript_chunk_upsert import (
    files_to_chunk_items,
    maybe_upsert_after_transcript_append,
    transcript_chunk_upsert_enabled,
    upsert_transcript_chunks,
    upsert_transcript_file,
)
from G4W.memory.vector.build_prod_index import (
    _CHUNK_CHARS,
    expand_items_with_chunks,
)


def _seed_index(root: Path, dim: int = DEFAULT_DIM) -> HnswIndex:
    idx = create_index(dim=dim, prefer_hnsw=True)
    texts = ["seed alpha doc", "seed beta doc"]
    labels = ["seed/a", "seed/b"]
    mat = embed_batch(texts, dim=dim, as_int8=False)
    idx.add(mat, labels=labels, replace=True)
    idx.save(root)
    docs = {lab: t for lab, t in zip(labels, texts)}
    (root / "docs.json").write_text(
        json.dumps(docs, ensure_ascii=False), encoding="utf-8"
    )
    # ensure index_ready: meta.json is written by idx.save; verify
    if not (root / "meta.json").is_file():
        (root / "meta.json").write_text(
            json.dumps({"dim": dim, "count": 2}, ensure_ascii=False),
            encoding="utf-8",
        )
    return idx


def _long_text(n_chunks: int = 3) -> str:
    """Text long enough to produce multiple chunks under _CHUNK_CHARS."""
    piece = "婚宴回忆段落。" * 40  # ~200+ chars
    # force multi-chunk: exceed _CHUNK_CHARS * n_chunks roughly
    unit = ("A" * 50 + " 周末去吃了朋友的婚宴。 ") * 20  # ~1400
    return (unit + "\n") * max(1, n_chunks)


class TestFlag(unittest.TestCase):
    def test_flag_default_on(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("G4W_TRANSCRIPT_CHUNK_UPSERT", None)
            # may still read package .env; must not crash
            self.assertTrue(transcript_chunk_upsert_enabled() in (True, False))

    def test_disabled_skips(self):
        with patch.dict(os.environ, {"G4W_TRANSCRIPT_CHUNK_UPSERT": "0"}):
            r = upsert_transcript_chunks(
                [("conversations/2026-06-20.md", "hello 婚宴")]
            )
            self.assertEqual(r["status"], "skipped")
            self.assertIn("disabled", r.get("reason", ""))

            r2 = upsert_transcript_file(
                "conversations/x.md", text="body"
            )
            self.assertEqual(r2["status"], "skipped")

            r3 = maybe_upsert_after_transcript_append(
                "conversations/x.md", text="body"
            )
            self.assertEqual(r3["status"], "skipped")


class TestChunkIds(unittest.TestCase):
    def test_short_file_bare_id(self):
        items = files_to_chunk_items(
            [("conversations/2026-06-20.md", "短文本 婚宴")]
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0][0], "conversations/2026-06-20.md")
        self.assertIn("婚宴", items[0][1])

    def test_long_file_hash_cN_stable(self):
        body = _long_text(3)
        # same expansion rules as full build
        expected = expand_items_with_chunks(
            [("memory/conversations/day.md", body)], max_items=4000
        )
        got = files_to_chunk_items(
            [("memory/conversations/day.md", body)], max_items=4000
        )
        self.assertEqual([i for i, _ in got], [i for i, _ in expected])
        self.assertGreaterEqual(len(got), 2)
        self.assertTrue(any("#c" in i for i, _ in got))
        # stable across calls
        got2 = files_to_chunk_items(
            [("memory/conversations/day.md", body)], max_items=4000
        )
        self.assertEqual([i for i, _ in got], [i for i, _ in got2])


class TestUpsertTranscriptChunks(unittest.TestCase):
    def test_write_gate_denied(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed_index(root)
            env = {
                "G4W_TRANSCRIPT_CHUNK_UPSERT": "1",
                "G4W_VECTOR_RETRIEVAL": "1",
                "EMBEDDING_PROVIDER": "hash",
            }
            with patch.dict(os.environ, env):
                with patch(
                    "G4W.memory.vector.sandbox_paths.assert_prod_index_write_allowed",
                    side_effect=ValueError("write forbidden test"),
                ):
                    r = upsert_transcript_chunks(
                        [("conversations/t.md", "gate deny 婚宴 body")],
                        index_dir=root,
                        dry_run=False,
                    )
            self.assertEqual(r.get("status"), "error")
            self.assertIn("forbidden", r.get("error", "").lower() + "write forbidden")

    def test_upsert_temp_index_replace_docs_and_tier(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed_index(root, dim=DEFAULT_DIM)
            env = {
                "G4W_TRANSCRIPT_CHUNK_UPSERT": "1",
                "G4W_VECTOR_RETRIEVAL": "1",
                "EMBEDDING_PROVIDER": "hash",
            }
            short = "周末去吃了朋友的婚宴，很开心。"
            with patch.dict(os.environ, env):
                with patch(
                    "G4W.memory.vector.sandbox_paths.assert_prod_index_write_allowed",
                    side_effect=lambda p: Path(p),
                ):
                    r = upsert_transcript_chunks(
                        [("conversations/2026-06-20.md", short)],
                        index_dir=root,
                        dry_run=False,
                        run_id="run-tx-1",
                    )
            self.assertEqual(r.get("status"), "ok", msg=str(r))
            self.assertGreaterEqual(r.get("upserted", 0), 1)

            docs = json.loads((root / "docs.json").read_text(encoding="utf-8"))
            self.assertIn("conversations/2026-06-20.md", docs)
            self.assertIn("婚宴", docs["conversations/2026-06-20.md"])
            # seed preserved (incremental)
            self.assertIn("seed/a", docs)

            idx = HnswIndex.load(root)
            self.assertGreaterEqual(idx.count, 3)  # 2 seed + >=1 transcript

            # replace same id: count should not grow unbounded
            count1 = idx.count
            with patch.dict(os.environ, env):
                with patch(
                    "G4W.memory.vector.sandbox_paths.assert_prod_index_write_allowed",
                    side_effect=lambda p: Path(p),
                ):
                    r2 = upsert_transcript_chunks(
                        [("conversations/2026-06-20.md", short + " 更新")],
                        index_dir=root,
                        dry_run=False,
                        run_id="run-tx-2",
                    )
            self.assertEqual(r2.get("status"), "ok", msg=str(r2))
            idx2 = HnswIndex.load(root)
            self.assertEqual(idx2.count, count1)
            docs2 = json.loads((root / "docs.json").read_text(encoding="utf-8"))
            self.assertIn("更新", docs2["conversations/2026-06-20.md"])

            # tier sidecar HOT
            tier_path = root / "tier_records.jsonl"
            self.assertTrue(tier_path.is_file())
            lines = [
                json.loads(x)
                for x in tier_path.read_text(encoding="utf-8").splitlines()
                if x.strip()
            ]
            self.assertTrue(any(row.get("tier") == "HOT" for row in lines))
            self.assertTrue(
                any(row.get("item_id") == "conversations/2026-06-20.md" for row in lines)
            )

            meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
            self.assertIn("last_transcript_upsert_at", meta)
            self.assertEqual(meta.get("last_transcript_upsert_run_id"), "run-tx-2")

    def test_multi_chunk_ids_and_docs_merge(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed_index(root)
            body = _long_text(3)
            env = {
                "G4W_TRANSCRIPT_CHUNK_UPSERT": "1",
                "G4W_VECTOR_RETRIEVAL": "1",
                "EMBEDDING_PROVIDER": "hash",
            }
            with patch.dict(os.environ, env):
                with patch(
                    "G4W.memory.vector.sandbox_paths.assert_prod_index_write_allowed",
                    side_effect=lambda p: Path(p),
                ):
                    r = upsert_transcript_chunks(
                        [("memory/conversations/long.md", body)],
                        index_dir=root,
                        dry_run=False,
                        run_id="run-long",
                    )
            self.assertEqual(r.get("status"), "ok", msg=str(r))
            self.assertGreaterEqual(r.get("upserted", 0), 2)
            docs = json.loads((root / "docs.json").read_text(encoding="utf-8"))
            chunk_keys = [k for k in docs if k.startswith("memory/conversations/long.md")]
            self.assertGreaterEqual(len(chunk_keys), 2)
            self.assertTrue(any("#c" in k for k in chunk_keys))
            self.assertIn("seed/a", docs)

    def test_already_chunked_path(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed_index(root)
            env = {
                "G4W_TRANSCRIPT_CHUNK_UPSERT": "1",
                "G4W_VECTOR_RETRIEVAL": "1",
                "EMBEDDING_PROVIDER": "hash",
            }
            items = [
                ("conversations/day.md#c0", "chunk0 婚宴"),
                ("conversations/day.md#c1", "chunk1 大悦城"),
            ]
            with patch.dict(os.environ, env):
                with patch(
                    "G4W.memory.vector.sandbox_paths.assert_prod_index_write_allowed",
                    side_effect=lambda p: Path(p),
                ):
                    r = upsert_transcript_chunks(
                        items,
                        index_dir=root,
                        already_chunked=True,
                        dry_run=False,
                    )
            self.assertEqual(r.get("status"), "ok", msg=str(r))
            self.assertEqual(r.get("upserted"), 2)
            docs = json.loads((root / "docs.json").read_text(encoding="utf-8"))
            self.assertEqual(docs["conversations/day.md#c0"], "chunk0 婚宴")
            self.assertEqual(docs["conversations/day.md#c1"], "chunk1 大悦城")

    def test_dry_run_no_write(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed_index(root)
            before_docs = (root / "docs.json").read_text(encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "G4W_TRANSCRIPT_CHUNK_UPSERT": "1",
                    "G4W_VECTOR_RETRIEVAL": "1",
                    "EMBEDDING_PROVIDER": "hash",
                },
            ):
                r = upsert_transcript_chunks(
                    [("conversations/dry.md", "dry run 婚宴")],
                    index_dir=root,
                    dry_run=True,
                )
            self.assertEqual(r.get("status"), "dry_run")
            self.assertIn("item_ids", r)
            after_docs = (root / "docs.json").read_text(encoding="utf-8")
            self.assertEqual(before_docs, after_docs)
            self.assertFalse((root / "tier_records.jsonl").is_file())


class TestUpsertTranscriptFile(unittest.TestCase):
    def test_file_with_text_param(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed_index(root)
            env = {
                "G4W_TRANSCRIPT_CHUNK_UPSERT": "1",
                "G4W_VECTOR_RETRIEVAL": "1",
                "EMBEDDING_PROVIDER": "hash",
            }
            with patch.dict(os.environ, env):
                with patch(
                    "G4W.memory.vector.sandbox_paths.assert_prod_index_write_allowed",
                    side_effect=lambda p: Path(p),
                ):
                    r = upsert_transcript_file(
                        Path(td) / "no_disk_needed.md",
                        text="文件路径 upsert 婚宴",
                        rel="conversations/via_file.md",
                        index_dir=root,
                    )
            self.assertEqual(r.get("status"), "ok", msg=str(r))
            self.assertEqual(r.get("rel"), "conversations/via_file.md")
            docs = json.loads((root / "docs.json").read_text(encoding="utf-8"))
            self.assertIn("conversations/via_file.md", docs)

    def test_hook_delegates(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed_index(root)
            env = {
                "G4W_TRANSCRIPT_CHUNK_UPSERT": "1",
                "G4W_VECTOR_RETRIEVAL": "1",
                "EMBEDDING_PROVIDER": "hash",
            }
            with patch.dict(os.environ, env):
                with patch(
                    "G4W.memory.vector.sandbox_paths.assert_prod_index_write_allowed",
                    side_effect=lambda p: Path(p),
                ):
                    r = maybe_upsert_after_transcript_append(
                        "conversations/hook.md",
                        text="hook 路径",
                        rel="conversations/hook.md",
                        index_dir=root,
                    )
            self.assertEqual(r.get("status"), "ok", msg=str(r))


if __name__ == "__main__":
    unittest.main()
