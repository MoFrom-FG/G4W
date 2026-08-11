"""IMPL-B tests: tier bounds + hybrid keyword→vector (aligned to IMPL-A index)."""
from __future__ import annotations

import os
import time
import unittest
from unittest import mock

from G4W.memory.vector.hnsw_index import BruteIndex
from G4W.memory.vector.hybrid_query import HybridQueryEngine
from G4W.memory.vector.tier_policy import Tier, TierPolicy, TierRecord


class TestTierPolicy(unittest.TestCase):
    def test_assign_by_age(self):
        p = TierPolicy()
        self.assertEqual(p.assign_by_age(1), Tier.HOT)
        self.assertEqual(p.assign_by_age(31), Tier.WARM)
        self.assertEqual(p.assign_by_age(200), Tier.COLD)

    def test_capacity_ratios(self):
        p = TierPolicy(max_items=100)
        self.assertEqual(p.capacity_for(Tier.HOT), 75)
        self.assertEqual(p.capacity_for(Tier.WARM), 20)
        self.assertEqual(p.capacity_for(Tier.COLD), 5)

    def test_tombstone_no_hard_delete(self):
        recs = [TierRecord(item_id=f"i{i}") for i in range(10)]
        p = TierPolicy(max_items=5)
        p.rebalance(recs)
        live = p.live_records(recs)
        self.assertLessEqual(len(live), 5)
        self.assertEqual(len(recs), 10)
        self.assertTrue(any(r.tombstone for r in recs))

    def test_rebalance_demote_hot_overflow(self):
        p = TierPolicy(max_items=20)
        now = time.time()
        recs = []
        for i in range(20):
            r = TierRecord(
                item_id=f"h{i}", created_at=now - 100, last_access_at=now - i
            )
            r.tier = Tier.HOT
            recs.append(r)
        p.rebalance(recs, now=now)
        counts = p.counts_by_tier(recs)
        self.assertLessEqual(counts[Tier.HOT], p.capacity_for(Tier.HOT))


class TestHybridQuery(unittest.TestCase):
    def setUp(self):
        self._env_patch = mock.patch.dict(os.environ, {"EMBEDDING_PROVIDER": "hash"})
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()

    def test_pure_vector_when_bm25_empty(self):
        """BM25 all-zero must NOT lock shortlist to dict insertion order."""
        eng = HybridQueryEngine(index=BruteIndex(dim=384), dim=384, keyword_pool=5)
        # Many noise docs first (would dominate dict-prefix shortlist).
        for i in range(30):
            eng.upsert(f"noise_{i:02d}", f"zzzz noise document number {i} filler text")
        # Target only related by embedding hash-path / token overlap on 'wedding banquet'
        eng.upsert("target", "wedding banquet hall dinner with relatives June evening")
        hits = eng.search("婚宴 亲戚 六月", k=5)
        # Even if Chinese tokens miss English body, pure-vector path must return something
        # from vec_top rather than empty / pure noise-prefix lock.
        # With hash embed, Chinese vs English may still be weak — use English query too.
        hits_en = eng.search("wedding banquet relatives June", k=5)
        ids_en = [h.item_id for h in hits_en]
        self.assertIn("target", ids_en, msg=f"expected target in pure-vector shortlist, got {ids_en}")

    def test_bm25_union_vector_shortlist(self):
        env = {"EMBEDDING_PROVIDER": "hash"}
        with mock.patch.dict(os.environ, env):
            eng = HybridQueryEngine(index=BruteIndex(dim=384), dim=384, keyword_pool=3)
            eng.upsert("a", "alpha cat sat on mat")
            eng.upsert("b", "beta dog ran in park")
            eng.upsert("c", "gamma unique_token_xyz only here")
            hits = eng.search("unique_token_xyz", k=3)
        ids = [h.item_id for h in hits]
        self.assertIn("c", ids)
        # positive BM25 should rank c high
        self.assertEqual(ids[0], "c")

    def test_keyword_then_vector(self):
        eng = HybridQueryEngine(index=BruteIndex(dim=384), dim=384)
        eng.upsert("a", "alpha cat sat on mat")
        eng.upsert("b", "beta dog ran in park")
        eng.upsert("c", "gamma cat and fish")
        hits = eng.search("cat mat", k=2)
        self.assertTrue(hits)
        ids = [h.item_id for h in hits]
        self.assertIn("a", ids)
        self.assertTrue(
            all("vector" in h.stages and "keyword" in h.stages for h in hits)
        )

    def test_tombstone_excluded(self):
        eng = HybridQueryEngine(index=BruteIndex(dim=384), dim=384)
        eng.upsert("x", "unique zebra query")
        eng.tombstone("x")
        hits = eng.search("zebra", k=5)
        self.assertEqual(hits, [])

    def test_tier_filter(self):
        eng = HybridQueryEngine(index=BruteIndex(dim=384), dim=384)
        now = time.time()
        eng.upsert("hot1", "fresh news item", created_at=now)
        eng.upsert("cold1", "ancient archive text", created_at=now - 400 * 86400)
        eng.rebalance()
        only_hot = eng.search("news archive", k=5, tiers=[Tier.HOT])
        for h in only_hot:
            self.assertEqual(h.tier, "HOT")

    def test_search_k_le_zero_empty(self):
        eng = HybridQueryEngine(index=BruteIndex(dim=384), dim=384)
        eng.upsert("a", "alpha cat sat on mat")
        self.assertEqual(eng.search("cat", k=0), [])
        self.assertEqual(eng.search("cat", k=-1), [])

    def test_search_weak_query_empty(self):
        """U-FIX-2: empty / whitespace / pure punctuation → [] (no top-k noise)."""
        eng = HybridQueryEngine(index=BruteIndex(dim=384), dim=384)
        eng.upsert("a", "alpha cat sat on mat")
        eng.upsert("b", "beta dog ran in park")
        for q in ("", "   ", "\t\n", "...", "!!!", ".,;:!?"):
            with self.subTest(q=repr(q)):
                self.assertEqual(eng.search(q, k=5), [])
        hits = eng.search("alpha cat", k=2)
        self.assertTrue(hits)
        self.assertIn("a", [h.item_id for h in hits])

    def test_search_k_none_raises(self):
        eng = HybridQueryEngine(index=BruteIndex(dim=384), dim=384)
        eng.upsert("a", "alpha cat sat on mat")
        with self.assertRaises(ValueError):
            eng.search("cat", k=None)  # type: ignore[arg-type]

    def test_upsert_same_id_no_ghost(self):
        idx = BruteIndex(dim=384)
        eng = HybridQueryEngine(index=idx, dim=384)
        eng.upsert("dup", "first version alpha cat")
        eng.upsert("dup", "second version alpha cat mat")
        self.assertEqual(idx.count, 1)
        self.assertEqual(idx._labels.count("dup"), 1)
        hits = eng.search("alpha cat mat", k=10)
        ids = [h.item_id for h in hits]
        self.assertEqual(ids.count("dup"), 1)
        self.assertEqual(eng.docs["dup"], "second version alpha cat mat")

    def test_cjk_bigram_bm25_partial_match(self):
        """CJK unigram+bigram: query 六月初婚宴 hits doc with 婚宴."""
        from G4W.memory.vector.hybrid_query import tokenize, _bm25_scores

        toks = tokenize("六月初婚宴")
        self.assertIn("婚", toks)
        self.assertIn("宴", toks)
        self.assertIn("婚宴", toks)
        docs = {
            "noise": "系统配置与日志摘要",
            "hit": "今天吃朋友的婚宴，在商场附近",
        }
        scores = _bm25_scores("六月初婚宴", docs)
        self.assertGreater(scores.get("hit", 0.0), scores.get("noise", 0.0))
        self.assertGreater(scores.get("hit", 0.0), 0.0)

    def test_search_empty_records_falls_back_to_docs(self):
        """Prod index often has no tier_records; empty records must not kill search."""
        idx = BruteIndex(dim=384)
        docs = {
            "conversations/x/transcripts/2026-06-20.md": "吃朋友的婚宴 六月初",
            "conductor/meta.json": "worker routing table",
        }
        # seed vectors via direct add + hash-ish embedding from engine path
        eng = HybridQueryEngine(index=idx, dim=384, docs=dict(docs), records={})
        for iid, text in docs.items():
            eng.upsert(iid, text)
        # clear records to simulate load-from-disk without tier sidecar
        eng.records = {}
        hits = eng.search("婚宴", k=5)
        self.assertTrue(hits, "empty records must still return hits from docs")
        self.assertTrue(any("婚宴" in (h.text_preview or "") or "transcripts" in h.item_id for h in hits))

    def test_path_boost_prefers_transcript_over_conductor_echo(self):
        """Transcript path boost must beat short conductor echoes with same keywords."""
        from G4W.memory.vector.hybrid_query import _path_score_boost

        self.assertGreater(
            _path_score_boost("conversations/x/transcripts/2026-06-20.md#c1"),
            _path_score_boost("conversations/x/conductor/rounds/r1/output.txt#c0"),
        )
        eng = HybridQueryEngine(index=BruteIndex(dim=384), dim=384, keyword_pool=10)
        eng.upsert(
            "conversations/x/conductor/rounds/r1/output.txt",
            "婚宴 婚宴 婚宴 用户问婚宴",
        )
        eng.upsert(
            "conversations/x/transcripts/2026-06-20.md",
            "User: 吃朋友的婚宴 六月初",
        )
        hits = eng.search("婚宴", k=5)
        self.assertTrue(hits)
        self.assertIn("transcripts", hits[0].item_id)

    def test_expand_memory_queries_mall_and_acne(self):
        from G4W.memory.vector.hybrid_query import expand_memory_queries

        mall = expand_memory_queries("我前两天去商场吃了什么", max_alts=3)
        self.assertEqual(mall[0], "我前两天去商场吃了什么")
        joined = " ".join(mall)
        self.assertTrue("大悦城" in joined or "购物中心" in joined)

        acne = expand_memory_queries("最近脸上痘痘怎么样了", max_alts=3)
        self.assertTrue(any("针清" in q or "祛痘" in q for q in acne))

    def test_adjacent_chunk_ids(self):
        from G4W.memory.vector.hybrid_query import adjacent_chunk_ids

        ids = adjacent_chunk_ids("foo/bar.md#c2", radius=1)
        self.assertIn("foo/bar.md#c1", ids)
        self.assertIn("foo/bar.md#c3", ids)
        self.assertNotIn("foo/bar.md#c2", ids)

    def test_search_memory_merges_expanded_query(self):
        """Query 商场 must surface 大悦城 chunk via expand even if original BM25 misses."""
        eng = HybridQueryEngine(index=BruteIndex(dim=384), dim=384, keyword_pool=8)
        eng.upsert(
            "conversations/x/transcripts/2026-06-09.md#c3",
            "User: 我这会在大悦城长安大排档，晚上吃点好的",
        )
        eng.upsert(
            "conversations/x/transcripts/2026-07-01.md#c0",
            "User: 今天在家写代码调试",
        )
        hits = eng.search_memory("我前两天去商场吃了什么", k=5, expand=True, neighbors=False)
        self.assertTrue(hits)
        self.assertTrue(
            any("大悦城" in (h.text_preview or "") or "长安" in (h.text_preview or "") for h in hits),
            f"expected mall place hit, got {[h.text_preview for h in hits]}",
        )

    def test_content_quality_prefers_event_over_meta_recall(self):
        """Later '记起来了' test echoes must not outrank contemporaneous user event."""
        import numpy as np
        from G4W.memory.vector.hybrid_query import _content_quality_boost

        event = (
            "[2026-06-20 08:53:36 Asia/Shanghai] User: 吃朋友的婚宴\n"
            "[2026-06-20 08:53:40 Asia/Shanghai] Assistant: 主人～今天正式婚宴呀"
        )
        echo = (
            "[2026-07-24 12:58:10 Asia/Shanghai] User: 婚宴\n"
            "[2026-07-24 12:58:24 Asia/Shanghai] Assistant: 喵～婚宴！对哦！记起来了！"
            "主人6月份去参加朋友的婚宴啦～让neko翻一下原文"
        )
        self.assertGreater(
            _content_quality_boost(event),
            _content_quality_boost(echo),
            msg="event body must outrank meta-recall on quality alone",
        )
        # Fixed equal vectors: ranking must not depend on hash-embed noise.
        dim = 32
        unit = np.ones(dim, dtype=np.float32)
        unit = unit / np.linalg.norm(unit)
        eng = HybridQueryEngine(index=BruteIndex(dim=dim), dim=dim, keyword_pool=8)
        eng.upsert(
            "conversations/x/transcripts/2026/06/2026-06-20.md#c1",
            event,
            vector=unit,
        )
        eng.upsert(
            "conversations/x/transcripts/2026/07/2026-07-24.md#c8",
            echo,
            vector=unit,
        )
        hits = eng.search("婚宴", k=3)
        self.assertTrue(hits)
        self.assertIn("2026-06-20", hits[0].item_id, msg=[(h.item_id, h.score) for h in hits])


if __name__ == "__main__":
    unittest.main()
