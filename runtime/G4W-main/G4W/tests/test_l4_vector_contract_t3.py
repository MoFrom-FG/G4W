"""T3 offline contract: expand → embed template → category tier (no net / no INDEX).

Serializes the P2+P3 data-plane used by Hybrid + L4 upsert without production I/O.
"""
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
)
from G4W.memory.vector.hybrid_query import expand_memory_queries  # noqa: E402
from G4W.memory.vector.l4_index_upsert import (  # noqa: E402
    format_insight_embed_text,
    suggest_tier_for_l4,
)


class TestExpandToTemplateChain(unittest.TestCase):
    """Query expand (P2) feeds the same alias/bridge tokens as embed template."""

    def test_joycity_expand_then_template(self):
        qs = expand_memory_queries("商场吃饭", max_alts=6)
        self.assertEqual(qs[0], "商场吃饭")
        blob = " ".join(qs)
        self.assertTrue(
            "大悦城" in blob or "长安大排档" in blob,
            msg=f"expand missing place canons: {qs}",
        )
        # Simulate L4 item that only has place entity — enrich + template
        aliases, bridges = enrich_aliases_and_bridges(["大悦城"], "周末商场吃饭")
        text = format_insight_embed_text(
            category="user_facts",
            summary="周末去大悦城吃饭",
            entities=["大悦城"],
            aliases=aliases,
            bridges=bridges,
            time_bucket="2026-06",
        )
        self.assertIn("L4|user_facts", text)
        self.assertIn("大悦城", text)
        # Template must surface alias and/or bridge slots for BM25 meet expand
        self.assertTrue(
            "别名:" in text or "桥:" in text,
            msg=f"expected alias/bridge slots in {text}",
        )
        self.assertTrue(
            any(x in text for x in ("Joy City", "商场", "购物中心", "吃饭", "大悦城商场")),
            msg=text,
        )

    def test_zhenqing_expand_and_template(self):
        qs = expand_memory_queries("针清", max_alts=6)
        blob = " ".join(qs)
        self.assertTrue(
            "痘痘" in blob or "爆痘" in blob or "祛痘" in blob,
            msg=qs,
        )
        aliases, bridges = enrich_aliases_and_bridges(["针清"], "做了针清有点疼")
        text = format_insight_embed_text(
            category="user_profile.life_signals",
            summary="去祛痘机构做了针清",
            entities=["针清"],
            aliases=aliases,
            bridges=bridges,
        )
        self.assertIn("针清", text)
        self.assertTrue("别名:" in text or "桥:" in text, msg=text)

    def test_wedding_expand_and_template(self):
        qs = expand_memory_queries("婚宴", max_alts=6)
        blob = " ".join(qs)
        self.assertTrue(
            "婚礼" in blob or "吃酒" in blob or "朋友婚礼" in blob,
            msg=qs,
        )
        aliases, bridges = enrich_aliases_and_bridges(["婚宴"], "吃朋友的婚宴")
        text = format_insight_embed_text(
            category="emotion_events",
            summary="吃朋友的婚宴",
            entities=["婚宴"],
            aliases=aliases,
            bridges=bridges,
        )
        self.assertIn("婚宴", text)
        self.assertIn("L4|emotion_events", text)


class TestP3TierMapping(unittest.TestCase):
    def test_emotion_hot_fact_warm(self):
        self.assertEqual(suggest_tier_for_l4("emotion_events"), "HOT")
        self.assertEqual(suggest_tier_for_l4("emotion/joy"), "HOT")
        self.assertEqual(suggest_tier_for_l4("user_facts"), "WARM")
        self.assertEqual(suggest_tier_for_l4("user_profile.preferences"), "WARM")

    def test_life_signals_hot_not_dead_warm(self):
        # P3 residual: insight must not always force WARM
        t = suggest_tier_for_l4("user_profile.life_signals")
        self.assertEqual(t, "HOT")

    def test_floor_constraints_not_colder_than_warm(self):
        # age demote with very old ts still floors constraints
        t = suggest_tier_for_l4(
            "user_profile.constraints",
            timestamp="2020-01-01T00:00:00",
            age_days=9999.0,
        )
        self.assertIn(t, ("HOT", "WARM"))
        self.assertNotEqual(t, "COLD")


class TestLookupApiContract(unittest.TestCase):
    """Regression: lookup_* entities must be Sequence, not bare str."""

    def test_lookup_aliases_list_not_char_iter(self):
        al = lookup_aliases(["大悦城"])
        self.assertTrue(al, msg="list entity should hit canons")
        # bare str would iterate chars and miss multi-char canons
        al_bad = lookup_aliases("大悦城")  # type: ignore[arg-type]
        # may be empty or partial; document that list form is required
        al_list = lookup_aliases(list("大悦城"))
        self.assertEqual(al_list, [])

    def test_expand_query_excludes_original(self):
        alts = expand_query("商场吃饭")
        self.assertNotIn("商场吃饭", alts)
        self.assertTrue(alts)


class TestLegacyExpandOptOut(unittest.TestCase):
    def test_legacy_off_still_has_entity_alias(self):
        with patch.dict(os.environ, {"G4W_HYBRID_EXPAND_LEGACY": "0"}, clear=False):
            qs = expand_memory_queries("商场吃饭", max_alts=6)
        self.assertEqual(qs[0], "商场吃饭")
        blob = " ".join(qs)
        self.assertTrue(
            "大悦城" in blob or "长安大排档" in blob,
            msg=f"entity_alias must remain when legacy off: {qs}",
        )


if __name__ == "__main__":
    unittest.main()
