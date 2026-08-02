"""Shared entity alias / bridge tables for L4 embed + hybrid query expand.

P2 goal: move 商场/针清/婚宴 hardcode data-plane out of hybrid_query into one table.
W1 owns this module; hybrid_query consumers land in T2-W2.
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# Cap slots so embed text does not spam
MAX_ALIASES = 8
MAX_BRIDGES = 6
MAX_EXPAND_ALTS = 4

# (canonical_substrings matched in entities/summary, bridges, optional aliases)
# Canons = high-signal triggers (entity names + distinctive symptoms).
# Bridges = softer co-occurrence terms written into embed / used by expand_query.
# Do NOT put common words like 吃饭/脸上 alone as canons — over-fires enrich.
BRIDGE_RULES: List[Tuple[List[str], List[str], List[str]]] = [
    (
        ["大悦城", "长安大排档", "Joy City", "joycity"],
        ["商场", "购物中心", "餐厅", "吃饭", "mall"],
        ["Joy City", "大悦城商场"],
    ),
    (
        # 痘痘/爆痘 as canons so summary-only「脸上爆痘了」hits without reverse-all-bridges
        ["针清", "祛痘", "水杨酸", "痘痘", "爆痘"],
        ["痘痘", "爆痘", "脸上", "医美", "护肤"],
        ["针清护理", "祛痘护理"],
    ),
    (
        ["婚宴", "婚礼", "结婚", "吃酒", "酒席"],
        ["朋友婚礼", "吃酒", "酒席", "结婚"],
        ["婚礼宴会"],
    ),
    # game rank / score breakthrough (user says 段位突破, transcript has 上了1800)
    (
        ["段位", "上分", "巅峰赛", "王者荣耀", "打巅峰", "段位突破"],
        ["1800", "上了", "冲分", "巅峰", "上分", "王者"],
        ["巅峰赛 上分", "上了1800", "我还上了"],
    ),
]

# Bridge/query term → canonical tokens to inject into expand alts
_BRIDGE_TO_CANONICAL: Dict[str, List[str]] = {}
_CANONICAL_TO_BRIDGES: Dict[str, List[str]] = {}
_CANONICAL_TO_ALIASES: Dict[str, List[str]] = {}


def _rebuild_indexes() -> None:
    _BRIDGE_TO_CANONICAL.clear()
    _CANONICAL_TO_BRIDGES.clear()
    _CANONICAL_TO_ALIASES.clear()
    for canons, bridges, aliases in BRIDGE_RULES:
        for c in canons:
            key = c.lower()
            _CANONICAL_TO_BRIDGES.setdefault(key, [])
            for b in bridges:
                if b not in _CANONICAL_TO_BRIDGES[key]:
                    _CANONICAL_TO_BRIDGES[key].append(b)
                _BRIDGE_TO_CANONICAL.setdefault(b.lower(), [])
                for cc in canons:
                    if cc not in _BRIDGE_TO_CANONICAL[b.lower()]:
                        _BRIDGE_TO_CANONICAL[b.lower()].append(cc)
            _CANONICAL_TO_ALIASES.setdefault(key, [])
            for a in aliases:
                if a not in _CANONICAL_TO_ALIASES[key]:
                    _CANONICAL_TO_ALIASES[key].append(a)


_rebuild_indexes()


def _dedupe_keep_order(items: Iterable[str], *, cap: int) -> List[str]:
    out: List[str] = []
    seen = set()
    for x in items:
        t = " ".join(str(x or "").split())
        if not t:
            continue
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
        if len(out) >= cap:
            break
    return out


def lookup_bridges(entities: Sequence[str], summary: str = "") -> List[str]:
    """Return bridge terms for entities/summary hits against BRIDGE_RULES (canon match only)."""
    blob = " ".join([*(str(e) for e in entities if e), summary or ""])
    blob_l = blob.lower()
    found: List[str] = []
    for canons, bridges, _aliases in BRIDGE_RULES:
        if any(c.lower() in blob_l for c in canons):
            for b in bridges:
                if b not in found:
                    found.append(b)
    return _dedupe_keep_order(found, cap=MAX_BRIDGES)


def lookup_aliases(entities: Sequence[str], summary: str = "") -> List[str]:
    """Static aliases for canon hits; de-duped later against entities in enrich_*."""
    blob = " ".join([*(str(e) for e in entities if e), summary or ""])
    blob_l = blob.lower()
    found: List[str] = []
    for canons, _bridges, aliases in BRIDGE_RULES:
        if any(c.lower() in blob_l for c in canons):
            for a in aliases:
                if a not in found:
                    found.append(a)
            # when hit via symptom canon (爆痘), surface care canons for BM25 meet expand
            for c in canons:
                if c not in found and c.lower() not in blob_l:
                    found.append(c)
    return _dedupe_keep_order(found, cap=MAX_ALIASES)


def enrich_aliases_and_bridges(
    entities: Sequence[str],
    summary: str = "",
    *,
    extra_aliases: Sequence[str] = (),
    extra_bridges: Sequence[str] = (),
) -> Tuple[List[str], List[str]]:
    """Return (aliases, bridges) from static rules + optional L4 extras.

    Aliases/bridges are de-duplicated against each other and against entity strings
    (case-insensitive) to avoid double-printing the same token in embed text.
    """
    ent_set = {str(e).strip().lower() for e in entities if str(e or "").strip()}
    aliases = list(extra_aliases) + lookup_aliases(entities, summary)
    bridges = list(extra_bridges) + lookup_bridges(entities, summary)

    def _filter(seq: Sequence[str], cap: int) -> List[str]:
        out: List[str] = []
        seen = set(ent_set)
        for x in seq:
            t = " ".join(str(x or "").split())
            if not t:
                continue
            k = t.lower()
            if k in seen:
                continue
            # skip if already fully contained as exact entity
            if t in {str(e) for e in entities}:
                continue
            seen.add(k)
            out.append(t)
            if len(out) >= cap:
                break
        return out

    return _filter(aliases, MAX_ALIASES), _filter(bridges, MAX_BRIDGES)


def expand_query(q: str, *, max_alts: int = MAX_EXPAND_ALTS) -> List[str]:
    """Data-driven query expand: bridge→canonical and light reverse.

    Returns alternatives **excluding** the original query (caller merges).

    Priority: high-signal *transcript phrases* first (e.g. 上了1800), then
    place canons (商场→大悦城), then soft bridges. Avoid filling the cap with
    near-synonym canons that already appear in the user query.
    """
    raw = (q or "").strip()
    if not raw:
        return []
    alts: List[str] = []
    ql = raw.lower()

    def _push(phrase: str) -> None:
        p = (phrase or "").strip()
        if not p or p == raw or p in alts:
            return
        # skip tokens already fully contained in the raw query (wastes slots)
        if p in raw or (len(p) <= 6 and p.lower() in ql):
            return
        alts.append(p)

    # --- high-value soft patterns FIRST (spoken query ↔ written transcript) ---
    # rank / score breakthrough (口语「段位突破」↔ 原文「我还上了1800」)
    if re.search(r"段位|上分|冲分|突破|打到多少|多少分|巅峰|王者", raw):
        for extra in ("上了1800", "我还上了", "巅峰赛 1800", "巅峰 上分", "上了"):
            _push(extra)
    if re.search(r"王者荣耀|巅峰赛|打王者|打巅峰", raw):
        for extra in ("上了1800", "巅峰赛", "上分 1800", "王者 巅峰"):
            _push(extra)
    # dining / place
    if re.search(r"商场|购物中心|mall", raw, re.I):
        for extra in ("大悦城", "购物中心 餐厅", "商场 吃饭 餐厅", "大悦城 吃饭"):
            _push(extra)
    if re.search(r"吃饭|晚饭|哪家店|吃的啥|吃了什么", raw):
        for extra in ("大悦城 长安大排档", "餐厅 大排档", "商场 吃饭"):
            _push(extra)

    # bridge hit → inject canons (商场 → 大悦城)
    for bridge, canons in _BRIDGE_TO_CANONICAL.items():
        if bridge in ql or bridge in raw:
            for c in canons:
                _push(c)
            if "商场" in bridge or bridge == "mall":
                for extra in ("大悦城 吃饭", "商场 餐厅"):
                    _push(extra)

    # canonical hit → prefer *aliases* (transcript-like) then bridges
    for canons, bridges, aliases in BRIDGE_RULES:
        if any(c.lower() in ql or c in raw for c in canons):
            for a in aliases:
                _push(a)
            for b in bridges[:4]:
                _push(b)

    return _dedupe_keep_order(alts, cap=max_alts)


def time_bucket_from_timestamp(ts: str) -> str:
    """Extract yyyy-mm from common timestamp strings; empty if unknown."""
    if not ts:
        return ""
    s = str(ts).strip()
    m = re.search(r"(20\d{2})[-/](\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    m = re.search(r"(20\d{2})(\d{2})(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return ""


__all__ = [
    "BRIDGE_RULES",
    "MAX_ALIASES",
    "MAX_BRIDGES",
    "enrich_aliases_and_bridges",
    "expand_query",
    "lookup_aliases",
    "lookup_bridges",
    "time_bucket_from_timestamp",
]
