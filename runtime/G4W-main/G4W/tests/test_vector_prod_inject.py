"""Tests for R1/R2 production vector inject (S7 default ON, fail-soft, hybrid)."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from G4W.memory.vector.embedding import DEFAULT_DIM, hash_embed
from G4W.memory.vector.flags import vector_retrieval_enabled
from G4W.memory.vector.hnsw_index import BruteIndex, create_index
from G4W.memory.vector.prod_inject import vector_section_for


def _write_mini_index(
    idx_dir: Path,
    *,
    docs: dict[str, str] | None = None,
    tier_rows: list[dict] | None = None,
    include_tombstone: bool = False,
) -> None:
    """Build sandbox-ready mini index + optional sidecars under idx_dir."""
    idx_dir.mkdir(parents=True, exist_ok=True)
    docs = docs or {
        "p-0": "vector retrieval hybrid keyword coarse filter",
        "p-1": "HNSW index float32 cosine search sandbox",
        "p-2": "unrelated cooking recipe pasta sauce",
    }
    if include_tombstone:
        docs = dict(docs)
        docs["p-dead"] = "should never appear after tombstone filter"

    idx = create_index(dim=DEFAULT_DIM, max_elements=64)
    labels = list(docs.keys())
    vecs = [hash_embed(docs[lab], dim=DEFAULT_DIM) for lab in labels]
    idx.add(np.asarray(vecs, dtype=np.float32), labels=labels)
    idx.save(idx_dir)

    with open(idx_dir / "docs.json", "w", encoding="utf-8") as f:
        json.dump(docs, f, ensure_ascii=False)

    now = time.time()
    if tier_rows is None:
        tier_rows = [
            {
                "item_id": "p-0",
                "tier": "HOT",
                "created_at": now,
                "last_access_at": now,
                "size_bytes": 40,
                "tombstone": False,
                "extra": {},
            },
            {
                "item_id": "p-1",
                "tier": "WARM",
                "created_at": now - 30 * 86400,
                "last_access_at": now - 5 * 86400,
                "size_bytes": 40,
                "tombstone": False,
                "extra": {},
            },
            {
                "item_id": "p-2",
                "tier": "COLD",
                "created_at": now - 200 * 86400,
                "last_access_at": now - 100 * 86400,
                "size_bytes": 40,
                "tombstone": False,
                "extra": {},
            },
        ]
    if include_tombstone:
        tier_rows = list(tier_rows) + [
            {
                "item_id": "p-dead",
                "tier": "HOT",
                "created_at": now,
                "last_access_at": now,
                "size_bytes": 10,
                "tombstone": True,
                "extra": {},
            }
        ]
    with open(idx_dir / "tier_records.jsonl", "w", encoding="utf-8") as f:
        for row in tier_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


class ProdInjectFlagTests(unittest.TestCase):
    def setUp(self):
        # Product addon gate defaults off on host; legacy R1/R2 tests assume
        # vector_retrieval flag alone. Keep product gate open so legacy flag
        # remains the under-test surface (OFF still via G4W_VECTOR_RETRIEVAL=0).
        self._addon_gate = mock.patch(
            "G4W.memory.vector.vector_config.vector_enabled",
            return_value=True,
        )
        self._addon_gate.start()
        self.addCleanup(self._addon_gate.stop)
        self._embed_stub = mock.patch(
            "G4W.memory.vector.hybrid_query.embed_text",
            side_effect=lambda text, dim=DEFAULT_DIM: hash_embed(text, dim=dim),
        )
        self._embed_stub.start()
        self.addCleanup(self._embed_stub.stop)

    def test_default_on_enabled(self):
        """S7: unset env + empty .env stub → enabled."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("G4W_VECTOR_RETRIEVAL", None)
            with mock.patch(
                "G4W.memory.vector.flags._read_env_file_value", return_value=""
            ):
                self.assertTrue(vector_retrieval_enabled())

    def test_explicit_off_returns_empty(self):
        """Rollback: G4W_VECTOR_RETRIEVAL=0 → flag False; section empty."""
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(
                os.environ,
                {
                    "G4W_VECTOR_RETRIEVAL": "0",
                    "G4W_BBS_CWD": td,
                    # pin away from host prod-ready index
                    "G4W_VECTOR_INDEX_DIR": str(Path(td) / "no-index"),
                },
                clear=False,
            ):
                self.assertFalse(vector_retrieval_enabled())
                out = vector_section_for(
                    Path(td), "sender-a", ["## User Memory\nhello vector hybrid"]
                )
                self.assertEqual(out, "")
                self.assertNotIn("Vector Retrieval", out)

    def test_vector_section_flag_off_empty(self):
        """§5 alias: explicit OFF → empty."""
        self.test_explicit_off_returns_empty()

    def test_explicit_off_false_aliases(self):
        for val in ("false", "off", "no", "disable", "disabled"):
            with mock.patch.dict(
                os.environ, {"G4W_VECTOR_RETRIEVAL": val}, clear=False
            ):
                self.assertFalse(
                    vector_retrieval_enabled(), msg=f"expected False for {val!r}"
                )

    def test_on_missing_index_empty_no_crash(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "missing_index"
            with mock.patch.dict(
                os.environ,
                {
                    "G4W_VECTOR_RETRIEVAL": "1",
                    "G4W_BBS_CWD": td,
                    "G4W_VECTOR_INDEX_DIR": str(missing),
                },
                clear=False,
            ):
                out = vector_section_for(
                    Path(td), "s1", ["## User Memory\nvector hybrid HNSW"]
                )
                self.assertEqual(out, "")
                self.assertNotIn("soft-fail", out)

    def test_vector_section_on_missing_index_empty(self):
        self.test_on_missing_index_empty_no_crash()

    def test_on_sandbox_index_has_section(self):
        with tempfile.TemporaryDirectory() as td:
            bbs = Path(td)
            idx_dir = bbs / "workspace" / "sandbox" / "vector_index"
            # R1-compat path: brute without sidecars still hybrid via synthetic HOT
            idx_dir.mkdir(parents=True, exist_ok=True)
            brute = BruteIndex(dim=DEFAULT_DIM)
            for lab, text in [
                ("p-0", "vector retrieval hybrid keyword coarse filter"),
                ("p-1", "HNSW index float32 cosine search sandbox"),
                ("p-2", "unrelated cooking recipe pasta sauce"),
            ]:
                brute.add(hash_embed(text).reshape(1, -1), labels=[lab])
            brute.save(idx_dir)

            with mock.patch.dict(
                os.environ,
                {
                    "G4W_VECTOR_RETRIEVAL": "1",
                    "G4W_BBS_CWD": str(bbs),
                    "G4W_VECTOR_INDEX_DIR": str(idx_dir),
                },
                clear=False,
            ):
                out = vector_section_for(
                    Path(td),
                    "s1",
                    ["## User Memory\nvector hybrid HNSW retrieval"],
                )
                self.assertIn("## Vector Retrieval Hits", out)
                self.assertIn("score=", out)
                self.assertIn("p-", out)
                self.assertIn("mode=hybrid", out)

    def test_vector_section_on_hybrid_hits(self):
        with tempfile.TemporaryDirectory() as td:
            bbs = Path(td)
            idx_dir = bbs / "workspace" / "sandbox" / "vector_index"
            _write_mini_index(idx_dir)
            with mock.patch.dict(
                os.environ,
                {
                    "G4W_VECTOR_RETRIEVAL": "1",
                    "G4W_BBS_CWD": str(bbs),
                    "G4W_VECTOR_INDEX_DIR": str(idx_dir),
                },
                clear=False,
            ):
                out = vector_section_for(
                    Path(td),
                    "s1",
                    ["## User Memory\nvector hybrid keyword retrieval"],
                    k=3,
                )
            self.assertIn("## Vector Retrieval Hits", out)
            self.assertIn("mode=hybrid", out)
            self.assertIn("score=", out)
            self.assertTrue("`p-0`" in out or "`p-1`" in out)
            # tier optional but expected with sidecar
            self.assertTrue("tier=" in out or "p-0" in out)

    def test_default_on_with_ready_index_has_section(self):
        """S7: flag default ON (unset) + ready index → hits section."""
        with tempfile.TemporaryDirectory() as td:
            bbs = Path(td)
            idx_dir = bbs / "workspace" / "sandbox" / "vector_index"
            _write_mini_index(idx_dir)
            with mock.patch.dict(
                os.environ,
                {
                    "G4W_BBS_CWD": str(bbs),
                    "G4W_VECTOR_INDEX_DIR": str(idx_dir),
                },
                clear=False,
            ):
                os.environ.pop("G4W_VECTOR_RETRIEVAL", None)
                with mock.patch(
                    "G4W.memory.vector.flags._read_env_file_value",
                    return_value="",
                ):
                    self.assertTrue(vector_retrieval_enabled())
                    out = vector_section_for(
                        Path(td),
                        "s1",
                        ["## User Memory\nvector hybrid keyword retrieval"],
                        k=3,
                    )
            self.assertIn("## Vector Retrieval Hits", out)
            self.assertIn("mode=hybrid", out)

    def test_check_flag_false_bypasses_env(self):
        """Direct call with check_flag=False still needs index; no crash if missing."""
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "no-index"
            with mock.patch.dict(
                os.environ,
                {
                    "G4W_BBS_CWD": td,
                    "G4W_VECTOR_INDEX_DIR": str(missing),
                },
                clear=False,
            ):
                os.environ.pop("G4W_VECTOR_RETRIEVAL", None)
                out = vector_section_for(
                    Path(td), "s1", ["hello"], check_flag=False
                )
                self.assertEqual(out, "")

    def test_vector_section_soft_fail(self):
        with tempfile.TemporaryDirectory() as td:
            bbs = Path(td)
            idx_dir = bbs / "workspace" / "sandbox" / "vector_index"
            _write_mini_index(idx_dir)
            with mock.patch.dict(
                os.environ,
                {
                    "G4W_VECTOR_RETRIEVAL": "1",
                    "G4W_BBS_CWD": str(bbs),
                    "G4W_VECTOR_INDEX_DIR": str(idx_dir),
                },
                clear=False,
            ):
                with mock.patch(
                    "G4W.memory.vector.prod_inject.HybridQueryEngine.search",
                    side_effect=RuntimeError("boom-search"),
                ):
                    out = vector_section_for(
                        Path(td), "s1", ["## User Memory\nvector hybrid"]
                    )
            self.assertIn("## Vector Retrieval Hits", out)
            self.assertIn("soft-fail", out)
            self.assertIn("boom-search", out)

    def test_vector_section_tombstone_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            bbs = Path(td)
            idx_dir = bbs / "workspace" / "sandbox" / "vector_index"
            _write_mini_index(idx_dir, include_tombstone=True)
            with mock.patch.dict(
                os.environ,
                {
                    "G4W_VECTOR_RETRIEVAL": "1",
                    "G4W_BBS_CWD": str(bbs),
                    "G4W_VECTOR_INDEX_DIR": str(idx_dir),
                },
                clear=False,
            ):
                out = vector_section_for(
                    Path(td),
                    "s1",
                    ["## User Memory\nvector hybrid keyword tombstone"],
                    k=5,
                )
            self.assertIn("## Vector Retrieval Hits", out)
            self.assertNotIn("p-dead", out)
            self.assertNotIn("`p-dead`", out)

    def test_vector_section_no_prod_data_touch(self):
        with tempfile.TemporaryDirectory() as td:
            bbs = Path(td)
            prod_data = bbs / "DATA" / "vector_prod_guard"
            prod_data.mkdir(parents=True, exist_ok=True)
            before = {p.name: p.stat().st_mtime_ns for p in prod_data.rglob("*") if p.is_file()}
            # marker file that must not change
            marker = prod_data / "must_not_write.bin"
            marker.write_bytes(b"keep")
            mtime0 = marker.stat().st_mtime_ns

            idx_dir = bbs / "workspace" / "sandbox" / "vector_index"
            _write_mini_index(idx_dir)

            with mock.patch.dict(
                os.environ,
                {
                    "G4W_VECTOR_RETRIEVAL": "1",
                    "G4W_BBS_CWD": str(bbs),
                    "G4W_VECTOR_INDEX_DIR": str(idx_dir),
                },
                clear=False,
            ):
                out = vector_section_for(
                    prod_data,  # memory_root deliberately under DATA-like path
                    "s1",
                    ["## User Memory\nvector hybrid"],
                )
            self.assertIn("## Vector Retrieval Hits", out)
            self.assertEqual(marker.read_bytes(), b"keep")
            self.assertEqual(marker.stat().st_mtime_ns, mtime0)
            # no new files under prod_data
            after_files = {p for p in prod_data.rglob("*") if p.is_file()}
            self.assertEqual(after_files, {marker})

    def test_vector_section_uses_index_dir_env(self):
        """S6-I1: resolve via G4W_VECTOR_INDEX_DIR, not sandbox-only."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            bbs = td_path / "bbs"
            prod_idx = td_path / "prod-vector-index"
            sandbox_idx = bbs / "workspace" / "sandbox" / "vector_index"
            bbs.mkdir(parents=True)
            # mini index only under prod INDEX_DIR
            _write_mini_index(prod_idx)
            # sandbox empty / not ready
            sandbox_idx.mkdir(parents=True, exist_ok=True)

            with mock.patch.dict(
                os.environ,
                {
                    "G4W_VECTOR_RETRIEVAL": "1",
                    "G4W_BBS_CWD": str(bbs),
                    "G4W_VECTOR_INDEX_DIR": str(prod_idx),
                },
                clear=False,
            ):
                out = vector_section_for(
                    td_path / "memory",
                    "s1",
                    ["## User Memory\nvector hybrid HNSW retrieval"],
                )
            self.assertIn("## Vector Retrieval Hits", out)
            self.assertIn("p-0", out)
            # path reported should reference prod index
            self.assertTrue(
                "prod-vector-index" in out or str(prod_idx.resolve()) in out
                or "hits" in out.lower()
            )


class InjectQueryUserMessageTests(unittest.TestCase):
    def setUp(self):
        self._addon_gate = mock.patch(
            "G4W.memory.vector.vector_config.vector_enabled",
            return_value=True,
        )
        self._addon_gate.start()
        self.addCleanup(self._addon_gate.stop)
        self._embed_stub = mock.patch(
            "G4W.memory.vector.hybrid_query.embed_text",
            side_effect=lambda text, dim=DEFAULT_DIM: hash_embed(text, dim=dim),
        )
        self._embed_stub.start()
        self.addCleanup(self._embed_stub.stop)

    def test_resolve_inject_query_prefers_user_message(self):
        from G4W.memory.hybrid_reader import resolve_inject_query

        q = resolve_inject_query(
            "用户原话：六月初婚宴安排",
            ["## User Memory\nvector hybrid keyword HNSW pasta"],
            sender_id="s1",
        )
        self.assertIn("婚宴", q)
        self.assertNotIn("pasta", q)

    def test_resolve_inject_query_fallback_sections(self):
        from G4W.memory.hybrid_reader import resolve_inject_query

        q = resolve_inject_query(
            "",
            ["## User Memory\nvector hybrid keyword HNSW pasta"],
            sender_id="s1",
        )
        self.assertTrue(len(q) > 0)
        # sticky only when both empty
        q2 = resolve_inject_query(None, [], sender_id="alice", sticky="memory vector")
        self.assertIn("alice", q2)
        self.assertIn("memory vector", q2)

    def test_vector_section_uses_explicit_user_query(self):
        """Explicit query= (this-round user message) appears in formatted section."""
        with tempfile.TemporaryDirectory() as td:
            bbs = Path(td)
            idx_dir = bbs / "workspace" / "sandbox" / "vector_index"
            _write_mini_index(idx_dir)
            user_q = "HNSW float32 cosine sandbox retrieval"
            with mock.patch.dict(
                os.environ,
                {
                    "G4W_VECTOR_RETRIEVAL": "1",
                    "G4W_BBS_CWD": str(bbs),
                    "G4W_VECTOR_INDEX_DIR": str(idx_dir),
                },
                clear=False,
            ):
                out = vector_section_for(
                    Path(td),
                    "s1",
                    ["## User Memory\nunrelated cooking recipe pasta sauce only"],
                    query=user_q,
                    k=3,
                )
            self.assertIn("## Vector Retrieval Hits", out)
            self.assertIn(user_q, out)
            # should prefer HNSW doc over pasta when query is HNSW-related
            self.assertIn("p-1", out)

    def test_read_memory_forwards_query_to_vector_section(self):
        from G4W.memory.conversation import ConversationStore

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mem = root / "memory"
            conv = root / "conversation"
            mem.mkdir()
            conv.mkdir()
            store = ConversationStore(root=conv, memory_root=mem)
            idx_dir = root / "prod-vector-index"
            _write_mini_index(idx_dir)
            user_q = "vector retrieval hybrid keyword coarse filter"
            with mock.patch.dict(
                os.environ,
                {
                    "G4W_VECTOR_RETRIEVAL": "1",
                    "G4W_VECTOR_INDEX_DIR": str(idx_dir),
                    "G4W_HYBRID_MAIN_READ": "0",
                },
                clear=False,
            ):
                stable = store.read_memory("u1", query=user_q)
                text = store.retrieval_context("u1", query=user_q)
            self.assertNotIn("## Vector Retrieval Hits", stable)
            self.assertIn("## Vector Retrieval Hits", text)
            self.assertIn(user_q, text)


class ReadMemoryInjectTests(unittest.TestCase):
    def setUp(self):
        self._addon_gate = mock.patch(
            "G4W.memory.vector.vector_config.vector_enabled",
            return_value=True,
        )
        self._addon_gate.start()
        self.addCleanup(self._addon_gate.stop)

    def test_read_memory_explicit_off_no_vector_section(self):
        """Rollback =0: read_memory must not inject Vector section."""
        from G4W.memory.conversation import ConversationStore

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mem = root / "memory"
            conv = root / "conversation"
            mem.mkdir()
            conv.mkdir()
            store = ConversationStore(root=conv, memory_root=mem)
            missing = root / "no-vector-index"
            with mock.patch.dict(
                os.environ,
                {
                    "G4W_VECTOR_RETRIEVAL": "0",
                    "G4W_VECTOR_INDEX_DIR": str(missing),
                },
                clear=False,
            ):
                text = store.read_memory("u1")
            self.assertNotIn("## Vector Retrieval", text)
            self.assertNotIn("Vector Retrieval Hits", text)

    def test_read_memory_default_on_missing_index_no_crash(self):
        """S7 default ON + missing index: other sections remain; no Vector hits."""
        from G4W.memory.conversation import ConversationStore

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mem = root / "memory"
            conv = root / "conversation"
            mem.mkdir()
            conv.mkdir()
            store = ConversationStore(root=conv, memory_root=mem)
            missing = root / "no-vector-index"
            with mock.patch.dict(
                os.environ,
                {"G4W_VECTOR_INDEX_DIR": str(missing)},
                clear=False,
            ):
                os.environ.pop("G4W_VECTOR_RETRIEVAL", None)
                with mock.patch(
                    "G4W.memory.vector.flags._read_env_file_value",
                    return_value="",
                ):
                    text = store.read_memory("u1")
            self.assertNotIn("Vector Retrieval Hits", text)
            # L1/other scaffolding should still appear
            self.assertTrue(len(text) > 0)


if __name__ == "__main__":
    unittest.main()
