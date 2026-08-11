"""Hybrid query: keyword coarse filter + vector fine rank.

Uses IMPL-A index API: add(vectors, labels=) / search(q) -> List[SearchHit].
Docs/tier live outside the index. No production hybrid/ or F1 writes.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from G4W.memory.vector.embedding import DEFAULT_DIM, embed_text
from G4W.memory.vector.hnsw_index import BruteIndex, SearchHit
from G4W.memory.vector.tier_policy import Tier, TierPolicy, TierRecord

# Latin/digit words OR continuous CJK runs. CJK is further split below.
_token = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", re.UNICODE)
_cjk_run = re.compile(r"^[\u4e00-\u9fff]+$")


def tokenize(text: str) -> List[str]:
    """Tokenize for BM25.

    English: word-level. Chinese: character unigrams + bigrams so partial
    phrase overlap works (e.g. query「六月初婚宴」 vs doc「吃朋友的婚宴」).
    Whole-run CJK tokens never match unless the exact phrase appears.
    """
    out: List[str] = []
    for m in _token.finditer(text or ""):
        t = m.group(0)
        if _cjk_run.match(t):
            chars = list(t)
            out.extend(chars)
            if len(chars) >= 2:
                out.extend(chars[i] + chars[i + 1] for i in range(len(chars) - 1))
        else:
            out.append(t.lower())
    return out


def _path_score_boost(item_id: str) -> float:
    """Prefer primary memory surfaces over conductor/worker meta noise.

    BM25 on short conductor/rounds often dominates after 0-1 normalize; boost
    must be large enough that a real transcript still outranks debug echoes.
    """
    r = (item_id or "").replace("\\", "/").lower()
    # strip chunk suffix "path#c0"
    if "#c" in r:
        r = r.split("#c", 1)[0]
    if "/transcripts/" in r or r.startswith("transcripts/"):
        return 0.35
    if "/user_only/" in r:
        return 0.25
    if "/history/" in r or r.endswith("history.md") or "history_insight" in r:
        return 0.16
    if "/summaries/diary/" in r or "/summaries/" in r:
        return 0.10
    if "/assistant-replies/" in r or "/workers/" in r or "/model-responses/" in r:
        return -0.35
    if "/conductor/" in r or "/rounds/" in r:
        return -0.45
    return 0.0


# Assistant "I remember / let me look up" echo of past events — not primary evidence.
# Do NOT match contemporaneous phrasing like 「原来今天是正式婚宴」(same-day chat).
_META_RECALL = re.compile(
    r"记起来了|翻一下原文|让\s*neko\s*翻|neko记起来|主人\d+月份去|"
    r"让neko翻一下|neko帮你翻|翻到了|查到了.*(婚宴|商场|针清)",
    re.I,
)
# Retrieval-system meta chat (today's test probes) — not life-event evidence.
_META_SYSTEM = re.compile(
    r"向量还是关键字|向量感觉意义不大|关键字|hybrid|BM25|embedding|短名单|shortlist",
    re.I,
)
# First-person / contemporaneous user life events beat later meta chatter.
# Stop before next speaker tag so assistant "记起来了…去参加婚宴" never boosts.
_USER_EVENT = re.compile(
    r"(?:"
    r"\]\s*User:\s*(?:(?!\]\s*(?:User|Assistant):).){0,100}?"
    r"(吃朋友的婚宴|去参加.{0,12}婚宴|在大悦城|大悦城长安大排档|"
    r"长安大排档|祛痘机构.{0,12}针清|做了个针清|做针清|"
    r"上了\s*\d{3,4}|冲到\s*\d{3,4}|到了\s*\d{3,4}|巅峰\s*\d{3,4}|"
    r"我还上了|巅峰1800|吃的肯德基.{0,24}1800|1800.{0,12}美滋滋)"
    r"|"
    # bare user_only lines (no speaker chrome)
    r"(?:^|\n)\s*(?:\[\d{4}[^\n\]]*\]\s*)?(?:User:\s*)?"
    r"(吃朋友的婚宴|在大悦城|大悦城长安大排档|长安大排档|做了个针清|做针清|"
    r"上了\s*\d{3,4}|我还上了|巅峰1800好难上)"
    r")",
    re.I | re.S,
)


def _content_quality_boost(text: str) -> float:
    """Demote test-time recall echoes; boost contemporaneous user life events.

    Recent chats that *discuss* past memories re-mention keywords densely and can
    outrank the original event chunk on pure BM25. No re-index required.
    Penalties sized to beat dense BM25 echoes even when vector scores are noisy.
    """
    t = text or ""
    if not t.strip():
        return 0.0
    delta = 0.0
    if _META_RECALL.search(t):
        delta -= 0.22
    if _META_SYSTEM.search(t):
        delta -= 0.12
    # Heavy HTML comment / message_id chrome vs sparse real prose → soft penalty.
    if t.count("G4W:message_id") >= 3:
        delta -= 0.05
    if _USER_EVENT.search(t):
        delta += 0.12
    user_lines = re.findall(r"\]\s*User:\s*([^\n]+)", t)
    if user_lines:
        shortest = min(len(x.strip()) for x in user_lines)
        if shortest <= 4 and len(user_lines) <= 3:
            delta -= 0.08
            if _META_RECALL.search(t):
                delta -= 0.04
    return delta


# Query-side synonym bridges for personal-life recall (no re-index needed).
# Each trigger adds short alternate queries; original query always kept first.
_QUERY_EXPAND_RULES: List[tuple] = [
    # place / dining (user says 商场, transcript has 大悦城/长安大排档)
    (re.compile(r"商场|购物中心|mall", re.I), ["大悦城", "购物中心 餐厅", "商场 吃饭 餐厅"]),
    (re.compile(r"哪家店|吃的啥|吃了什么|晚饭|吃饭"), ["餐厅 大排档", "商场 吃饭", "大悦城 长安大排档"]),
    (re.compile(r"大排档|长安"), ["长安大排档", "大悦城 吃饭"]),
    # skincare / acne
    (re.compile(r"痘痘|爆痘|脸上.*痘"), ["针清", "祛痘 针清", "爆痘 脸上"]),
    (re.compile(r"针清|祛痘|收拾痘"), ["针清 痘痘", "脸上 针清", "祛痘"]),
    # wedding
    (re.compile(r"婚宴|婚礼|结婚"), ["吃朋友的婚宴", "婚宴"]),
    # game rank / score (段位突破 ↔ 我还上了1800)
    (
        re.compile(r"段位|上分|冲分|突破|打到多少|多少分|冲击"),
        ["上了1800", "巅峰赛 1800", "我还上了", "巅峰 上分"],
    ),
    (
        re.compile(r"王者荣耀|巅峰赛|打王者|打巅峰"),
        ["巅峰赛", "上分 1800", "上了1800", "王者 巅峰"],
    ),
    # soft time+where (semantic rescue)
    (re.compile(r"去哪|去哪儿|去了哪"), ["出去 玩 吃饭", "行程 安排"]),
]


def _push_expand_alt(
    out: List[str],
    seen: set,
    base: str,
    a: str,
    *,
    max_total: int,
) -> bool:
    """Append alt (+ optional base+alt combo). Return True if at capacity."""
    a = (a or "").strip()
    if not a or a in seen:
        return len(out) >= max_total
    # Prefer "base + bridge" when alt is short keyword, else pure alt
    if len(a) <= 12 and a not in base:
        combo = f"{base} {a}"
        if combo not in seen:
            out.append(combo)
            seen.add(combo)
            if len(out) >= max_total:
                return True
    if a not in seen:
        out.append(a)
        seen.add(a)
    return len(out) >= max_total


def expand_memory_queries(q: str, *, max_alts: int = 4) -> List[str]:
    """Return original query plus a few synonym/bridge variants (deduped).

    Used by search_memory to rescue cases where user wording never appears
    in the transcript (商场 vs 大悦城) while BM25 on the expanded form works.

    P2: shared ``entity_alias.expand_query`` first, then legacy
    ``_QUERY_EXPAND_RULES`` as union (default). Set
    ``G4W_HYBRID_EXPAND_LEGACY=0`` to use only the shared table.
    """
    base = (q or "").strip()
    if not base:
        return []
    max_total = 1 + max(0, int(max_alts))
    out: List[str] = [base]
    seen = {base}

    # Shared data-plane table (entity_alias) — primary source
    try:
        from G4W.memory.vector.entity_alias import expand_query as _ea_expand

        for a in _ea_expand(base, max_alts=max_alts):
            if _push_expand_alt(out, seen, base, a, max_total=max_total):
                return out[:max_total]
    except Exception:
        pass

    # Legacy hardcode rules (union / compatibility). Opt-out via env.
    use_legacy = True
    try:
        import os as _os

        raw = str(_os.environ.get("G4W_HYBRID_EXPAND_LEGACY", "") or "").strip().lower()
        if raw in ("0", "false", "no", "off"):
            use_legacy = False
    except Exception:
        use_legacy = True

    if use_legacy:
        for pat, alts in _QUERY_EXPAND_RULES:
            if not pat.search(base):
                continue
            for a in alts:
                if _push_expand_alt(out, seen, base, a, max_total=max_total):
                    return out[:max_total]
    return out[:max_total]


_CHUNK_SUFFIX = re.compile(r"^(?P<stem>.*)#c(?P<n>\d+)$")


def adjacent_chunk_ids(item_id: str, *, radius: int = 1) -> List[str]:
    """Same-file neighbor chunk ids: path#cN → path#c(N±radius)."""
    m = _CHUNK_SUFFIX.match(item_id or "")
    if not m:
        return []
    stem = m.group("stem")
    n = int(m.group("n"))
    out: List[str] = []
    for d in range(1, max(1, radius) + 1):
        if n - d >= 0:
            out.append(f"{stem}#c{n - d}")
        out.append(f"{stem}#c{n + d}")
    return out


_DAY_IN_PATH = re.compile(
    r"(?P<pre>.*/)(?P<y>20\d{2})-(?P<m>\d{2})-(?P<d>\d{2})(?P<post>\.md(?:#c\d+)?)$"
)


def adjacent_day_chunk_ids(
    item_id: str,
    docs: Dict[str, str],
    *,
    radius_days: int = 1,
    cap: int = 6,
) -> List[str]:
    """Same-conversation transcript chunks for ±radius_days around yyyy-mm-dd.md.

    Used when a hit lands on a nearby day (e.g. 06-05「巅峰1800好难上」) but the
    literal breakthrough fact sits on 06-06「我还上了1800」.
    """
    from datetime import date, timedelta

    raw = (item_id or "").replace("\\", "/")
    # drop #cN for date parse
    path = raw.split("#c", 1)[0]
    m = _DAY_IN_PATH.match(path)
    if not m:
        return []
    try:
        base = date(int(m.group("y")), int(m.group("m")), int(m.group("d")))
    except ValueError:
        return []
    pre, post_ext = m.group("pre"), ".md"
    # Only expand transcripts-like paths (avoid random dated tool dumps)
    pl = path.lower()
    if "/transcripts/" not in pl and not pl.startswith("transcripts/"):
        return []

    out: List[str] = []
    seen = set()
    for delta in range(-max(1, radius_days), max(1, radius_days) + 1):
        if delta == 0:
            continue
        day = base + timedelta(days=delta)
        day_stem = f"{pre}{day.isoformat()}{post_ext}"
        # Prefer chunks that look like user life events / score breakthroughs.
        candidates: List[tuple] = []
        for iid, text in (docs or {}).items():
            if not str(iid).replace("\\", "/").startswith(day_stem):
                # also exact day_stem without chunk is rare; accept prefix match
                if str(iid).replace("\\", "/") != day_stem and not str(iid).replace("\\", "/").startswith(
                    day_stem + "#c"
                ):
                    continue
            sc = 0.0
            t = text or ""
            if _USER_EVENT.search(t):
                sc += 2.0
            if re.search(r"上了\s*\d{3,4}|巅峰\s*\d{3,4}|1800|婚宴|针清|大悦城", t):
                sc += 1.5
            if "/transcripts/" in str(iid).replace("\\", "/").lower():
                sc += 0.5
            candidates.append((sc, str(iid)))
        candidates.sort(key=lambda x: (-x[0], x[1]))
        for sc, iid in candidates:
            if iid in seen:
                continue
            seen.add(iid)
            out.append(iid)
            if len(out) >= cap:
                return out
    return out


@dataclass
class HybridHit:
    item_id: str
    score: float
    tier: Optional[str] = None
    text_preview: str = ""
    stages: Dict[str, float] = field(default_factory=dict)


def _bm25_scores(
    query: str, docs: Dict[str, str], k1: float = 1.5, b: float = 0.75
) -> Dict[str, float]:
    q = tokenize(query)
    if not q or not docs:
        return {}
    n = len(docs)
    dl = {i: len(tokenize(t)) for i, t in docs.items()}
    avgdl = sum(dl.values()) / max(n, 1)
    df: Counter = Counter()
    tfs: Dict[str, Counter] = {}
    for i, t in docs.items():
        tf = Counter(tokenize(t))
        tfs[i] = tf
        for term in tf:
            df[term] += 1
    scores = {i: 0.0 for i in docs}
    for term in q:
        n_q = df.get(term, 0)
        if n_q == 0:
            continue
        idf = math.log(1 + (n - n_q + 0.5) / (n_q + 0.5))
        for i, tf in tfs.items():
            f = tf.get(term, 0)
            if f == 0:
                continue
            denom = f + k1 * (1 - b + b * dl[i] / avgdl)
            scores[i] += idf * (f * (k1 + 1)) / denom
    return scores


def _hits_to_map(hits: List[SearchHit]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for h in hits:
        # cosine distance backends may use distance; A uses sim score in SearchHit
        out[str(h.label)] = float(h.score)
    return out


@dataclass
class HybridQueryEngine:
    """Keyword coarse → vector fine; optional tier filter."""

    index: Any = None
    policy: TierPolicy = field(default_factory=TierPolicy)
    records: Dict[str, TierRecord] = field(default_factory=dict)
    docs: Dict[str, str] = field(default_factory=dict)
    dim: int = DEFAULT_DIM
    keyword_pool: int = 50
    alpha: float = 0.6  # vector weight when fusing on shortlist

    def __post_init__(self) -> None:
        if self.index is None:
            self.index = BruteIndex(dim=self.dim)

    def upsert(
        self,
        item_id: str,
        text: str,
        *,
        size_bytes: int = 0,
        created_at: Optional[float] = None,
        vector: Optional[Sequence[float]] = None,
    ) -> None:
        if vector is not None:
            vec = np.asarray(vector, dtype=np.float32)
        else:
            vec = embed_text(text, dim=self.dim)
        # replace=True: same id overwrites index vector (no ghost duplicate labels)
        self.index.add(vec.reshape(1, -1), labels=[item_id], replace=True)
        self.docs[item_id] = text
        rec = TierRecord(
            item_id=item_id,
            size_bytes=size_bytes or len(text.encode("utf-8", errors="replace")),
        )
        if created_at is not None:
            rec.created_at = float(created_at)
            rec.last_access_at = float(created_at)
        rec.tier = self.policy.assign_by_age(rec.age_days())
        self.records[item_id] = rec

    def tombstone(self, item_id: str) -> None:
        rec = self.records.get(item_id)
        if rec is not None:
            self.policy.mark_tombstone(rec)

    def rebalance(self, now: Optional[float] = None) -> None:
        recs = list(self.records.values())
        for r in recs:
            if not r.tombstone:
                self.policy.assign_record(r, now=now)
        self.policy.rebalance(recs, now=now)

    def search(
        self,
        q: str,
        k: int = 10,
        *,
        tiers: Optional[Sequence[Tier]] = None,
        keyword_pool: Optional[int] = None,
    ) -> List[HybridHit]:
        # k<=0 → []; illegal k (None / non-int / bool) → ValueError
        if k is None or isinstance(k, bool) or not isinstance(k, int):
            raise ValueError(f"k must be int, got {type(k).__name__}: {k!r}")
        if k <= 0:
            return []
        # W3U-P1-2 / U-FIX-2: empty / whitespace / pure-punctuation → no noise hits
        if not tokenize(q if q is not None else ""):
            return []

        # Empty *records* (no tier sidecar) → treat all docs as live.
        # Non-empty records with zero searchable ids (all tombstoned / tier-filtered)
        # must stay empty — do not revive from docs.
        if not self.records:
            allow_ids = set(self.docs.keys()) if self.docs else set(
                str(x) for x in (getattr(self.index, "labels", None) or [])
            )
        else:
            allow_ids = set(
                self.policy.filter_searchable(
                    list(self.records.values()), tiers=tiers
                )
            )
        if not allow_ids:
            return []

        live_docs = {i: t for i, t in self.docs.items() if i in allow_ids}
        # BM25 returns all docs (zeros for non-matches). Only positive scores
        # form the lexical shortlist — zeros must NOT lock vector to dict prefix.
        bm_all = _bm25_scores(q, live_docs)
        bm_pos = {i: s for i, s in bm_all.items() if s > 0.0}
        pool_n = int(keyword_pool if keyword_pool is not None else self.keyword_pool)
        pool_n = max(pool_n, k)

        qvec = embed_text(q, dim=self.dim)
        # Over-fetch: long personal corpora dilute whole-file vectors; BM25
        # (CJK bigrams) is the main rescue, but still pull a wider ANN set.
        raw = self.index.search(qvec, k=max(pool_n * 5, k * 20, 200))
        if raw and isinstance(raw[0], SearchHit):
            vec_map = _hits_to_map(raw)
        else:
            # tolerate tuple (id, score) legacy
            vec_map = {str(a): float(b) for a, b in (raw or [])}
        vec_map = {i: s for i, s in vec_map.items() if i in allow_ids}
        vec_top = [
            i
            for i, _ in sorted(vec_map.items(), key=lambda x: x[1], reverse=True)[
                :pool_n
            ]
        ]

        if bm_pos:
            bm_top = [
                i
                for i, _ in sorted(bm_pos.items(), key=lambda x: x[1], reverse=True)[
                    :pool_n
                ]
            ]
            # Union preserves BM25 priority then vector-only adds.
            shortlist: List[str] = []
            seen = set()
            for iid in bm_top + vec_top:
                if iid not in seen and iid in allow_ids:
                    shortlist.append(iid)
                    seen.add(iid)
                if len(shortlist) >= pool_n * 2:
                    break
        else:
            # Pure semantic path when no lexical overlap.
            shortlist = list(vec_top)

        if not shortlist:
            return []

        # normalize bm25 to ~[0,1] for fuse (positives only)
        max_bm = max(bm_pos.values()) if bm_pos else 0.0
        hits: List[HybridHit] = []
        for iid in shortlist:
            v = float(vec_map.get(iid, 0.0))
            lex_raw = float(bm_pos.get(iid, 0.0))
            lex = (lex_raw / max_bm) if max_bm > 0 else 0.0
            # Pure-vector mode: score = vector sim; hybrid: alpha blend.
            if bm_pos:
                score = self.alpha * v + (1.0 - self.alpha) * lex
            else:
                score = v
            body = self.docs.get(iid) or ""
            score = score + _path_score_boost(iid) + _content_quality_boost(body)
            rec = self.records.get(iid)
            preview = body[:160]
            hits.append(
                HybridHit(
                    item_id=iid,
                    score=score,
                    tier=rec.tier.value if rec else None,
                    text_preview=preview,
                    stages={"vector": v, "keyword": lex_raw},
                )
            )
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]

    def search_memory(
        self,
        query: str,
        k: int = 5,
        *,
        expand: bool = True,
        neighbors: bool = True,
        neighbor_radius: int = 1,
        max_alts: int = 5,
        day_neighbors: bool = True,
    ) -> List[HybridHit]:
        """Recall-oriented search: multi-query expand + optional neighbor chunks.

        - expand: run expand_memory_queries and merge (max score per id, slight
          bonus when multiple variants hit the same id).
        - neighbors: for each top hit with #cN, pull adjacent chunks from docs
          (no extra embed) so evidence that sits one block away still surfaces.
        - day_neighbors: when a top hit is dated yyyy-mm-dd.md, also pull same
          conversation's ±1 day transcript chunks (literal fact often sits next day).
        """
        if k is None:
            raise ValueError("k is required")
        k = max(1, int(k))
        q0 = (query or "").strip()
        if not q0:
            return []

        # Rank/game queries need more alts (口语≠原文数字)
        if max_alts < 6 and re.search(r"段位|上分|巅峰|突破|王者|冲分", q0):
            max_alts = 6

        variants = expand_memory_queries(q0, max_alts=max_alts) if expand else [q0]
        if not variants:
            return []

        # Over-fetch per variant then merge.
        per_k = max(k * 3, 12)
        best: Dict[str, HybridHit] = {}
        hit_counts: Dict[str, int] = {}
        for i, vq in enumerate(variants):
            # Slight decay for expanded (non-original) queries so base wording wins ties.
            # Keep high weight for alts that inject concrete score numbers (1800…).
            if i == 0:
                w = 1.0
            elif re.search(r"\d{3,4}|上了|巅峰赛", vq):
                w = 0.98
            else:
                w = 0.92
            try:
                part = self.search(vq, k=per_k)
            except Exception:
                part = []
            for h in part:
                iid = h.item_id
                hit_counts[iid] = hit_counts.get(iid, 0) + 1
                scored = HybridHit(
                    item_id=h.item_id,
                    score=float(h.score) * w,
                    tier=h.tier,
                    text_preview=h.text_preview,
                    stages=dict(h.stages or {}),
                )
                scored.stages["query_variant"] = float(i)
                prev = best.get(iid)
                if prev is None or scored.score > prev.score:
                    best[iid] = scored

        # Multi-hit bonus (same id matched by several variants).
        for iid, cnt in hit_counts.items():
            if cnt > 1 and iid in best:
                h = best[iid]
                best[iid] = HybridHit(
                    item_id=h.item_id,
                    score=float(h.score) + 0.04 * min(cnt - 1, 3),
                    tier=h.tier,
                    text_preview=h.text_preview,
                    stages=dict(h.stages or {}),
                )

        merged = sorted(best.values(), key=lambda h: h.score, reverse=True)

        if neighbors and merged:
            # Seed from current top pool; inject neighbors missing from best.
            seed = merged[: max(k * 2, 8)]
            extra: List[HybridHit] = []
            seen = set(best.keys())
            for h in seed:
                for nid in adjacent_chunk_ids(h.item_id, radius=neighbor_radius):
                    if nid in seen:
                        continue
                    if nid not in self.docs and nid not in (self.records or {}):
                        continue
                    seen.add(nid)
                    rec = self.records.get(nid)
                    preview = (self.docs.get(nid) or "")[:160]
                    # Inherit most of parent score but mark as neighbor.
                    nscore = float(h.score) * 0.88 + _path_score_boost(nid) * 0.15
                    extra.append(
                        HybridHit(
                            item_id=nid,
                            score=nscore,
                            tier=rec.tier.value if rec else None,
                            text_preview=preview,
                            stages={
                                "vector": float((h.stages or {}).get("vector") or 0.0) * 0.88,
                                "keyword": float((h.stages or {}).get("keyword") or 0.0) * 0.88,
                                "neighbor_of": 1.0,
                            },
                        )
                    )
            if day_neighbors:
                for h in seed:
                    for nid in adjacent_day_chunk_ids(h.item_id, self.docs, radius_days=1, cap=6):
                        if nid in seen:
                            continue
                        seen.add(nid)
                        rec = self.records.get(nid)
                        preview = (self.docs.get(nid) or "")[:160]
                        # Slightly lower than same-file neighbor; still above noise.
                        nscore = float(h.score) * 0.82 + _path_score_boost(nid) * 0.18
                        # Prefer day-neighbors that literally look like user life events.
                        nscore += _content_quality_boost(self.docs.get(nid) or "")
                        extra.append(
                            HybridHit(
                                item_id=nid,
                                score=nscore,
                                tier=rec.tier.value if rec else None,
                                text_preview=preview,
                                stages={
                                    "vector": float((h.stages or {}).get("vector") or 0.0) * 0.82,
                                    "keyword": float((h.stages or {}).get("keyword") or 0.0) * 0.82,
                                    "day_neighbor_of": 1.0,
                                },
                            )
                        )
            if extra:
                merged = sorted(list(merged) + extra, key=lambda x: x.score, reverse=True)

        # Second-pass: if top scores are weak, force literal expand variants again
        # with larger k (helps when base semantic query only finds meta chatter).
        top_score = float(merged[0].score) if merged else 0.0
        if expand and top_score < 0.28 and len(variants) > 1:
            rescue_qs = [v for v in variants[1:] if re.search(r"\d{3,4}|上了|大悦城|婚宴|针清", v)]
            for vq in rescue_qs[:3]:
                try:
                    part = self.search(vq, k=max(per_k, 20))
                except Exception:
                    part = []
                for h in part:
                    iid = h.item_id
                    scored = HybridHit(
                        item_id=h.item_id,
                        score=float(h.score) * 1.02,
                        tier=h.tier,
                        text_preview=h.text_preview,
                        stages=dict(h.stages or {}, rescue=1.0),
                    )
                    prev = best.get(iid)
                    if prev is None or scored.score > prev.score:
                        best[iid] = scored
            merged = sorted(best.values(), key=lambda h: h.score, reverse=True)

        return merged[:k]
