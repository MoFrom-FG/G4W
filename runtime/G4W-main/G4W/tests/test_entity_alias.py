"""Unit tests: entity_alias + hybrid expand union (P2)."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_CODE = Path(__file__).resolve().parents[2]
if str(_CODE) not in sys.path:
    sys.path.insert(0, str(_CODE))

from G4W.memory.vector.entity_alias import (  # noqa: E402
    enrich_aliases_and_bridges,
    expand_query,
    lookup_aliases,
    lookup_bridges,
    time_bucket_from_timestamp,
)
from G4W.memory.vector.hybrid_query import expand_memory_queries  # noqa: E402


class TestEntityAliasTable(unittest.TestCase):
    def test_lookup_aliases_joycity(self):
        # API: entities is Sequence[str], not a bare str (str iterates chars)
        al = lookup_aliases(["大悦城"])
        self.assertTrue(
            any("Joy" in a or "joy" in a.lower() for a in al) or "Joy City" in al,
            msg=al,
        )

    def test_lookup_bridges_from_entity(self):
        br = lookup_bridges(["大悦城"], "周末去大悦城")
        self.assertTrue(
            any(x in br for x in ("商场", "购物中心", "mall")),
            msg=br,
        )

    def test_enrich_joycity_summary(self):
        aliases, bridges = enrich_aliases_and_bridges(["大悦城"], "在大悦城吃了")
        self.assertTrue(aliases or bridges)
        joined = " ".join(bridges)
        self.assertTrue("商场" in joined or "购物中心" in joined or "吃饭" in joined)

    def test_expand_query_bridge_to_canon(self):
        alts = expand_query("商场吃饭")
        blob = " ".join(alts)
        self.assertTrue(
            "大悦城" in blob or "长安大排档" in blob,
            msg=f"expected place canons in {alts}",
        )
        self.assertNotIn("商场吃饭", alts)  # excludes original

    def test_expand_query_empty(self):
        self.assertEqual(expand_query(""), [])
        self.assertEqual(expand_query("   "), [])

    def test_time_bucket(self):
        self.assertEqual(time_bucket_from_timestamp("2026-06-09T12:00:00"), "2026-06")
        self.assertEqual(time_bucket_from_timestamp("20260609"), "2026-06")
        self.assertEqual(time_bucket_from_timestamp(""), "")


class TestHybridExpandUnion(unittest.TestCase):
    def test_expand_memory_includes_entity_alias(self):
        qs = expand_memory_queries("商场吃饭", max_alts=6)
        self.assertEqual(qs[0], "商场吃饭")
        blob = " ".join(qs)
        self.assertTrue(
            "大悦城" in blob or "长安大排档" in blob,
            msg=f"entity_alias path missing in {qs}",
        )

    def test_expand_memory_legacy_union_still_works(self):
        # 针清 is in legacy rules; shared table also has skin rules
        qs = expand_memory_queries("针清", max_alts=6)
        blob = " ".join(qs)
        self.assertTrue("痘" in blob or "爆痘" in blob or "针" in blob, msg=qs)

    def test_expand_legacy_opt_out(self):
        with patch.dict(os.environ, {"G4W_HYBRID_EXPAND_LEGACY": "0"}):
            qs = expand_memory_queries("商场吃饭", max_alts=6)
            self.assertEqual(qs[0], "商场吃饭")
            # shared table alone should still rescue 商场
            blob = " ".join(qs)
            self.assertTrue("大悦城" in blob or "长安大排档" in blob, msg=qs)

    def test_expand_empty(self):
        self.assertEqual(expand_memory_queries(""), [])

    def test_expand_rank_breakthrough_prefers_transcript_phrase(self):
        """口语「段位突破」应优先扩到原文「上了1800」，勿被同义 canon 占满 cap。"""
        from G4W.memory.vector.entity_alias import expand_query

        alts = expand_query("段位突破 王者荣耀", max_alts=6)
        self.assertTrue(alts, msg=alts)
        self.assertIn("上了1800", alts)
        # high-signal phrase should rank early (first 3 slots)
        self.assertIn("上了1800", alts[:3], msg=alts)

        qs = expand_memory_queries("段位突破 王者荣耀", max_alts=6)
        self.assertEqual(qs[0], "段位突破 王者荣耀")
        blob = " ".join(qs)
        self.assertIn("上了1800", blob, msg=qs)


if __name__ == "__main__":
    unittest.main()
