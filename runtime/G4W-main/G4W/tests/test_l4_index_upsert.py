"""Unit tests: L4 finalize → Hybrid incremental upsert (not full rebuild)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Ensure package importable when run directly from this file.
import sys

_CODE = Path(__file__).resolve().parents[2]
if str(_CODE) not in sys.path:
    sys.path.insert(0, str(_CODE))

from G4W.memory.vector.embedding import DEFAULT_DIM, EmbeddingError, embed_batch, hash_embed
from G4W.memory.vector.hnsw_index import HnswIndex, create_index
from G4W.memory.vector.l4_index_upsert import (
    format_insight_embed_text,
    insight_items_to_docs,
    l4_index_upsert_enabled,
    l4_tier_by_category_enabled,
    suggest_tier_for_l4,
    upsert_l4_insights_to_index,
)


def _seed_index(root: Path, dim: int = DEFAULT_DIM) -> HnswIndex:
    idx = create_index(dim=dim, prefer_hnsw=True)
    texts = ["seed alpha doc", "seed beta doc"]
    labels = ["seed/a", "seed/b"]
    with patch.dict(os.environ, {"EMBEDDING_PROVIDER": "hash"}, clear=False):
        mat = embed_batch(texts, dim=dim, as_int8=False)
    idx.add(mat, labels=labels, replace=True)
    idx.save(root)
    docs = {lab: t for lab, t in zip(labels, texts)}
    (root / "docs.json").write_text(
        json.dumps(docs, ensure_ascii=False), encoding="utf-8"
    )
    return idx


class TestFormatInsight(unittest.TestCase):
    def test_template_contains_category_and_summary(self):
        t = format_insight_embed_text(
            category="user_profile.preferences",
            summary="喜欢吃朋友的婚宴",
            entities=["婚宴", "朋友"],
            snippet="周末去吃了婚宴",
            source_transcript="conversations/2026-06-20.md",
            timestamp="2026-06-20",
        )
        self.assertIn("L4|", t)
        self.assertIn("婚宴", t)
        self.assertIn("实体:", t)
        self.assertIn("原话:", t)
        self.assertIn("来源:", t)

    def test_template_p2_aliases_bridges_time_bucket(self):
        t = format_insight_embed_text(
            category="user_facts",
            summary="在大悦城吃了长安大排档",
            entities=["大悦城"],
            aliases=["Joy City", "大悦城商场"],
            bridges=["商场", "购物中心"],
            timestamp="2026-06-09",
            time_bucket="2026-06",
        )
        self.assertIn("别名:", t)
        self.assertIn("Joy City", t)
        self.assertIn("桥:", t)
        self.assertIn("商场", t)
        self.assertIn("月: 2026-06", t)


class TestInsightItemsToDocs(unittest.TestCase):
    def test_stable_ids_and_text(self):
        active = {
            "user_profile": {
                "preferences": [
                    {
                        "preference": "婚宴",
                        "description": "喜欢参加朋友的婚宴",
                        "entities": ["婚宴"],
                        "source_transcript": "conversations/2026-06-20.md",
                    }
                ]
            },
            "user_facts": [
                {"fact": "在大悦城吃了长安大排档", "timestamp": "2026-06-09"}
            ],
        }
        emotion = {
            "events": [
                {
                    "summary": "脸上痘痘好转",
                    "signal": "祛痘",
                    "date": "2026-07-06",
                }
            ]
        }
        docs = insight_items_to_docs(
            active=active, emotion=emotion, user_id="u1", run_id="r1"
        )
        self.assertGreaterEqual(len(docs), 3, msg=f"docs={docs}")
        ids = [d[0] for d in docs]
        self.assertTrue(all(i.startswith("l4insight/") for i in ids))
        # stable: same input → same ids
        docs2 = insight_items_to_docs(
            active=active, emotion=emotion, user_id="u1", run_id="r1"
        )
        self.assertEqual([d[0] for d in docs], [d[0] for d in docs2])
        joined = " ".join(d[1] for d in docs)
        self.assertIn("婚宴", joined)
        self.assertTrue(
            any("痘" in d[1] or "emotion" in d[0] for d in docs),
            msg=f"emotion missing: {docs}",
        )

    def test_p2_meta_aliases_bridges_and_template(self):
        active = {
            "user_facts": [
                {
                    "fact": "在大悦城吃了长安大排档",
                    "timestamp": "2026-06-09",
                    "entities": ["大悦城"],
                }
            ]
        }
        docs = insight_items_to_docs(active=active, user_id="u1", run_id="r1")
        self.assertEqual(len(docs), 1)
        _id, text, meta = docs[0]
        self.assertIn("别名:", text)
        self.assertIn("桥:", text)
        self.assertTrue(meta.get("aliases") or meta.get("bridges"))
        self.assertEqual(meta.get("time_bucket"), "2026-06")
        blob = " ".join(meta.get("bridges") or [])
        self.assertTrue(
            "商场" in blob or "购物中心" in blob or "吃饭" in text,
            msg=f"bridges missing: {meta}",
        )


class TestP3CategoryTier(unittest.TestCase):
    def test_flag_default_on(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("G4W_L4_TIER_BY_CATEGORY", None)
            self.assertTrue(l4_tier_by_category_enabled())

    def test_suggest_tier_floor_and_emotion(self):
        # constraints / memory_lessons floor WARM
        self.assertEqual(
            suggest_tier_for_l4("user_profile.constraints", timestamp="2020-01-01"),
            "WARM",
        )
        self.assertEqual(
            suggest_tier_for_l4("memory_lessons", timestamp="2020-01-01"),
            "WARM",
        )
        # emotion defaults HOT
        self.assertEqual(
            suggest_tier_for_l4("emotion_events", timestamp="2026-07-01"),
            "HOT",
        )
        # recent user_facts WARM
        self.assertEqual(
            suggest_tier_for_l4("user_facts", timestamp="2026-06-09"),
            "WARM",
        )


class TestUpsertL4Insights(unittest.TestCase):
    def test_flag_default_on(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("G4W_L4_INDEX_UPSERT", None)
            # may still read package .env; just call without crash
            _ = l4_index_upsert_enabled()

    def test_vector_addon_off_skips_before_legacy(self):
        """Product total gate wins: no TEI, no embed path."""
        with patch(
            "G4W.memory.vector.vector_config.vector_enabled",
            return_value=False,
        ), patch(
            "G4W.memory.vector.tei_lifecycle.ensure_tei_running",
        ) as m_tei, patch.dict(
            os.environ, {"G4W_L4_INDEX_UPSERT": "1"}
        ):
            r = upsert_l4_insights_to_index(
                active={"user_facts": [{"fact": "x"}]},
                user_id="u",
                run_id="r",
            )
        self.assertEqual(r["status"], "skipped")
        self.assertEqual(r.get("reason"), "vector_addon disabled")
        m_tei.assert_not_called()

    def test_disabled_skips(self):
        """Legacy L4 flag off after total gate ON → skip with L4 reason."""
        with patch(
            "G4W.memory.vector.vector_config.vector_enabled",
            return_value=True,
        ), patch.dict(os.environ, {"G4W_L4_INDEX_UPSERT": "0"}):
            r = upsert_l4_insights_to_index(
                active={"user_facts": [{"fact": "x"}]},
                user_id="u",
                run_id="r",
            )
            self.assertEqual(r["status"], "skipped")
            self.assertIn("G4W_L4_INDEX_UPSERT", r.get("reason", ""))

    def test_vector_config_import_failure_fails_closed_without_writes(self):
        active = {"user_facts": [{"fact": "vector_config import failure must not write"}]}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed_index(root, dim=DEFAULT_DIM)
            before_docs = (root / "docs.json").read_text(encoding="utf-8")
            before_count = HnswIndex.load(root).count

            real_import = __import__

            def import_side_effect(name, globals=None, locals=None, fromlist=(), level=0):
                if level == 1 and name == "vector_config" and globals and globals.get("__package__") == "G4W.memory.vector":
                    raise ImportError("mock vector_config unavailable")
                return real_import(name, globals, locals, fromlist, level)

            with patch("builtins.__import__", side_effect=import_side_effect), patch(
                "G4W.memory.vector.tei_lifecycle.ensure_tei_running"
            ) as m_tei, patch.dict(
                os.environ,
                {
                    "G4W_L4_INDEX_UPSERT": "1",
                    "G4W_VECTOR_RETRIEVAL": "1",
                    "EMBEDDING_PROVIDER": "hash",
                },
            ):
                r = upsert_l4_insights_to_index(
                    active=active,
                    user_id="u",
                    run_id="import-fail",
                    index_dir=root,
                    dry_run=False,
                )

            self.assertEqual(r.get("status"), "skipped")
            self.assertEqual(r.get("reason"), "vector_config_unavailable")
            self.assertIn("ImportError", r.get("detail", ""))
            m_tei.assert_not_called()
            self.assertEqual((root / "docs.json").read_text(encoding="utf-8"), before_docs)
            self.assertFalse((root / "tier_records.jsonl").exists())
            self.assertEqual(HnswIndex.load(root).count, before_count)

    def test_upsert_temp_index_replace_and_docs(self):
        active = {
            "user_profile": {
                "preferences": [
                    {
                        "preference": "婚宴",
                        "description": "喜欢吃朋友的婚宴",
                        "entities": ["婚宴", "朋友"],
                        "source_transcript": "conversations/2026-06-20.md",
                        "timestamp": "2026-06-20",
                    }
                ]
            }
        }
        tei_ok = {"status": "already_running", "ok": True, "pid": 1}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed_index(root, dim=DEFAULT_DIM)
            env = {
                "G4W_L4_INDEX_UPSERT": "1",
                "G4W_VECTOR_RETRIEVAL": "1",
                "EMBEDDING_PROVIDER": "hash",
            }
            gate = patch(
                "G4W.memory.vector.vector_config.vector_enabled",
                return_value=True,
            )
            tei = patch(
                "G4W.memory.vector.tei_lifecycle.ensure_tei_running",
                return_value=tei_ok,
            )
            with gate, tei, patch.dict(os.environ, env):
                with patch(
                    "G4W.memory.vector.l4_index_upsert.assert_prod_index_write_allowed",
                    side_effect=lambda p: Path(p),
                    create=True,
                ):
                    with patch(
                        "G4W.memory.vector.sandbox_paths.assert_prod_index_write_allowed",
                        side_effect=lambda p: Path(p),
                    ):
                        r = upsert_l4_insights_to_index(
                            active=active,
                            user_id="u1",
                            run_id="run-test-1",
                            index_dir=root,
                            dry_run=False,
                        )
            self.assertEqual(r.get("status"), "ok", msg=str(r))
            self.assertGreaterEqual(r.get("upserted", 0), 1)
            self.assertTrue((r.get("tei") or {}).get("ok"), msg=str(r.get("tei")))

            docs = json.loads((root / "docs.json").read_text(encoding="utf-8"))
            insight_keys = [k for k in docs if k.startswith("l4insight/")]
            self.assertGreaterEqual(len(insight_keys), 1)
            self.assertIn("婚宴", docs[insight_keys[0]])
            self.assertIn("seed/a", docs)

            idx = HnswIndex.load(root)
            self.assertGreaterEqual(idx.count, 3)

            count1 = idx.count
            with gate, tei, patch.dict(os.environ, env):
                with patch(
                    "G4W.memory.vector.sandbox_paths.assert_prod_index_write_allowed",
                    side_effect=lambda p: Path(p),
                ):
                    r2 = upsert_l4_insights_to_index(
                        active=active,
                        user_id="u1",
                        run_id="run-test-2",
                        index_dir=root,
                        dry_run=False,
                    )
            self.assertEqual(r2.get("status"), "ok", msg=str(r2))
            idx2 = HnswIndex.load(root)
            self.assertEqual(idx2.count, count1)

            tier_path = root / "tier_records.jsonl"
            self.assertTrue(tier_path.is_file())
            lines = [
                json.loads(x)
                for x in tier_path.read_text(encoding="utf-8").splitlines()
                if x.strip()
            ]
            self.assertTrue(any(row.get("tier") == "WARM" for row in lines))

            meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
            self.assertIn("last_l4_upsert_at", meta)
            self.assertEqual(meta.get("last_l4_upsert_run_id"), "run-test-2")

    def test_embedding_failure_returns_error_without_writing_insight_docs(self):
        active = {"user_facts": [{"fact": "TEI down should not write hash vectors"}]}
        env = {
            "G4W_VECTOR_ADDON": "1",
            "EMBEDDING_PROVIDER": "openai",
            "EMBEDDING_BASE_URL": "http://127.0.0.1:18080/v1",
            "EMBEDDING_MODEL": "mock-emb",
            "EMBEDDING_API_KEY": "x",
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed_index(root)
            before_docs = json.loads((root / "docs.json").read_text(encoding="utf-8"))
            self.assertEqual(sorted(before_docs), ["seed/a", "seed/b"])

            addon_gate = patch(
                "G4W.memory.vector.vector_config.vector_enabled",
                return_value=True,
            )
            gate = patch(
                "G4W.memory.vector.l4_index_upsert.l4_index_upsert_enabled",
                return_value=True,
            )
            tei = patch(
                "G4W.memory.vector.tei_lifecycle.ensure_tei_running",
                return_value={"status": "error", "ok": False, "detail": "down"},
                create=True,
            )
            with addon_gate, gate, tei, patch.dict(os.environ, env):
                with patch(
                    "G4W.memory.vector.sandbox_paths.assert_prod_index_write_allowed",
                    side_effect=lambda p: Path(p),
                ):
                    with patch(
                        "G4W.memory.vector.embedding.embed_batch",
                        side_effect=EmbeddingError("remote embed_batch failed", reason="mock_down"),
                    ):
                        r = upsert_l4_insights_to_index(
                            active=active,
                            user_id="u1",
                            run_id="run-fail",
                            index_dir=root,
                            dry_run=False,
                        )

            self.assertEqual(r.get("status"), "error", msg=str(r))
            self.assertEqual(r.get("reason"), "embedding_failed", msg=str(r))
            self.assertEqual(r.get("embedding_reason"), "mock_down", msg=str(r))
            docs = json.loads((root / "docs.json").read_text(encoding="utf-8"))
            self.assertEqual(docs, before_docs)
            self.assertFalse((root / "tier_records.jsonl").exists())
            self.assertEqual(HnswIndex.load(root).count, 2)

    def test_dry_run_no_write(self):
        active = {"user_facts": [{"fact": "只是 dry run 事实"}]}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed_index(root)
            before_docs = (root / "docs.json").read_text(encoding="utf-8")
            with patch(
                "G4W.memory.vector.vector_config.vector_enabled",
                return_value=True,
            ), patch.dict(
                os.environ,
                {
                    "G4W_L4_INDEX_UPSERT": "1",
                    "G4W_VECTOR_RETRIEVAL": "1",
                    "EMBEDDING_PROVIDER": "hash",
                },
            ):
                r = upsert_l4_insights_to_index(
                    active=active,
                    user_id="u",
                    run_id="dry",
                    index_dir=root,
                    dry_run=True,
                )
            self.assertEqual(r.get("status"), "dry_run")
            after_docs = (root / "docs.json").read_text(encoding="utf-8")
            self.assertEqual(before_docs, after_docs)

    def test_ensure_tei_invoked_when_addon_on(self):
        """When total gate ON, ensure_tei_running is called before embed/write."""
        active = {"user_facts": [{"fact": "tei probe fact", "timestamp": "2026-07-01"}]}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed_index(root, dim=DEFAULT_DIM)
            with patch(
                "G4W.memory.vector.vector_config.vector_enabled",
                return_value=True,
            ), patch(
                "G4W.memory.vector.embed_lifecycle.ensure_embed_running",
                return_value={"status": "started", "ok": True, "pid": 42},
            ) as m_embed, patch.dict(
                os.environ,
                {
                    "G4W_L4_INDEX_UPSERT": "1",
                    "G4W_VECTOR_RETRIEVAL": "1",
                    "EMBEDDING_PROVIDER": "hash",
                },
            ), patch(
                "G4W.memory.vector.sandbox_paths.assert_prod_index_write_allowed",
                side_effect=lambda p: Path(p),
            ):
                r = upsert_l4_insights_to_index(
                    active=active,
                    user_id="u",
                    run_id="tei-1",
                    index_dir=root,
                    dry_run=False,
                )
            self.assertEqual(r.get("status"), "ok", msg=str(r))
            m_embed.assert_called()
            self.assertEqual((r.get("tei") or {}).get("status"), "started")

    def test_ensure_tei_not_called_when_addon_off(self):
        with patch(
            "G4W.memory.vector.vector_config.vector_enabled",
            return_value=False,
        ), patch(
            "G4W.memory.vector.tei_lifecycle.ensure_tei_running",
        ) as m_tei, patch.dict(
            os.environ, {"G4W_L4_INDEX_UPSERT": "1"}
        ):
            r = upsert_l4_insights_to_index(
                active={"user_facts": [{"fact": "x"}]},
                user_id="u",
                run_id="r",
            )
        self.assertEqual(r["status"], "skipped")
        self.assertEqual(r.get("reason"), "vector_addon disabled")
        m_tei.assert_not_called()



if __name__ == "__main__":
    unittest.main()
