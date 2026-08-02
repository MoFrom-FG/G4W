"""L4 finalize → Hybrid **incremental** upsert (not full rebuild).

After L4 validator merges official history_insight files, convert insight
items into embed-friendly texts and upsert into the production index:

  - HNSW / brute vectors (replace same item_id)
  - docs.json (BM25 sidecar)
  - tier_records.jsonl (insight → WARM by default; optional category tier via
    G4W_L4_TIER_BY_CATEGORY)

Fail-soft: never raise into L4 finalize path. Gate:
  G4W_L4_INDEX_UPSERT (default ON; 0/false/off disables)
  + vector_retrieval_enabled()
  + index_ready(live)

P2: embed template carries 别名/桥/time_bucket via entity_alias.
P3: category→tier when G4W_L4_TIER_BY_CATEGORY is on (default ON).

Does not index raw transcripts (original chunk layer stays separate).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

_log = logging.getLogger(__name__)

# Stable prefix so insight ids never collide with transcript paths
_ITEM_PREFIX = "l4insight/"


def l4_tier_by_category_enabled() -> bool:
    """P3: map L4 category → HOT/WARM/COLD. Default ON; 0/false/off disables."""
    raw = str(os.environ.get("G4W_L4_TIER_BY_CATEGORY", "") or "").strip().lower()
    if not raw:
        return True
    return raw not in ("0", "false", "off", "no", "disable", "disabled")


# Base tier by L4 category path (P3). emotion/* → HOT via prefix.
CATEGORY_TIER_DEFAULT: Dict[str, str] = {
    "ongoing_projects": "HOT",
    "emotion_events": "HOT",
    "user_profile.life_signals": "HOT",
    "user_profile.constraints": "WARM",
    "user_facts": "WARM",
    "user_profile.preferences": "WARM",
    "user_profile.behavior_patterns": "WARM",
    "user_profile.self_descriptions": "WARM",
    "user_profile.goals": "WARM",
    "agent_capabilities_learned": "WARM",
    "memory_lessons": "WARM",
}

# Never colder than WARM solely due to age
_FLOOR_WARM_CATEGORIES = frozenset(
    {
        "user_profile.constraints",
        "memory_lessons",
        "user_facts",
    }
)

_TIER_RANK = {"HOT": 0, "WARM": 1, "COLD": 2}


def _parse_age_days(ts: str, now: Optional[float] = None) -> Optional[float]:
    """Best-effort age in days from timestamp string; None if unparseable."""
    if not ts:
        return None
    s = str(ts).strip()
    if not s:
        return None
    now_ts = float(now if now is not None else time.time())
    # unix epoch seconds / ms
    try:
        if re.fullmatch(r"\d{10,13}", s):
            v = float(s)
            if v > 1e12:
                v /= 1000.0
            if v > 1e9:
                return max(0.0, (now_ts - v) / 86400.0)
    except Exception:
        pass
    # ISO-ish / common date
    m = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})", s)
    if not m:
        m = re.search(r"(20\d{2})(\d{2})(\d{2})", s)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        else:
            return None
    else:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        import datetime as _dt

        then = _dt.datetime(y, mo, d, tzinfo=_dt.timezone.utc).timestamp()
        return max(0.0, (now_ts - then) / 86400.0)
    except Exception:
        return None


def suggest_tier_for_l4(
    category: str,
    *,
    age_days: Optional[float] = None,
    timestamp: str = "",
    now: Optional[float] = None,
) -> str:
    """Suggest HOT/WARM/COLD for an L4 insight category (P3).

    Floor: constraints / memory_lessons / user_facts stay ≥ WARM.
    Age demote only when G4W_L4_TIER_BY_CATEGORY is enabled (caller checks).
    """
    cat = (category or "").strip()
    base = CATEGORY_TIER_DEFAULT.get(cat)
    if base is None:
        if cat.startswith("emotion/") or cat == "emotion_events":
            base = "HOT"
        else:
            base = "WARM"

    if age_days is None and timestamp:
        age_days = _parse_age_days(timestamp, now=now)

    if age_days is not None:
        # HOT TTL 30d, WARM TTL 180d (aligned with tier_policy.TTL_DAYS)
        if age_days <= 30:
            by_age = "HOT"
        elif age_days <= 180:
            by_age = "WARM"
        else:
            by_age = "COLD"
        # take colder of base vs age
        if _TIER_RANK.get(by_age, 1) > _TIER_RANK.get(base, 1):
            base = by_age

    # floor
    floor_key = cat if cat in _FLOOR_WARM_CATEGORIES else (
        "user_profile.constraints"
        if cat.endswith(".constraints")
        else cat
    )
    if floor_key in _FLOOR_WARM_CATEGORIES or cat in _FLOOR_WARM_CATEGORIES:
        if _TIER_RANK.get(base, 1) > _TIER_RANK["WARM"]:
            base = "WARM"
    if cat.startswith("emotion/"):
        # keep emotion floor at WARM (never force COLD from missing ts alone)
        pass
    return base if base in ("HOT", "WARM", "COLD") else "WARM"


def l4_index_upsert_enabled() -> bool:
    """Legacy L4 upsert env flag (default ON; explicit disable tokens turn off).

    Product total gate is ``vector_config.vector_enabled()`` (installed∧enabled,
    env kill-switch ``G4W_VECTOR_ADDON=0``). When the addon is disabled,
    ``upsert_l4_insights_to_index`` returns early **before** this flag is the
    sole decider — total switch wins over ``G4W_L4_INDEX_UPSERT``.
    """
    raw = str(os.environ.get("G4W_L4_INDEX_UPSERT", "") or "").strip().lower()
    if not raw:
        # package-local .env (same style as flags.py)
        try:
            env_path = Path(__file__).resolve().parents[3] / ".env"
            if env_path.is_file():
                for line in env_path.read_text(encoding="utf-8-sig").splitlines():
                    s = line.strip()
                    if not s or s.startswith("#") or "=" not in s:
                        continue
                    k, v = s.split("=", 1)
                    if k.strip() == "G4W_L4_INDEX_UPSERT":
                        raw = v.strip().strip("'\"").lower()
                        break
        except Exception:
            pass
    if not raw:
        return True
    return raw not in ("0", "false", "off", "no", "disable", "disabled")


def _stable_key(*parts: str) -> str:
    h = hashlib.sha1("|".join(parts).encode("utf-8", errors="replace")).hexdigest()[:16]
    return h


def _clean(s: object, max_len: int = 400) -> str:
    t = " ".join(str(s or "").split())
    if len(t) > max_len:
        t = t[: max_len - 1] + "…"
    return t


def format_insight_embed_text(
    *,
    category: str,
    summary: str,
    entities: Sequence[str] = (),
    aliases: Sequence[str] = (),
    bridges: Sequence[str] = (),
    snippet: str = "",
    source_transcript: str = "",
    timestamp: str = "",
    time_bucket: str = "",
) -> str:
    """Fixed template: category | summary | entities | aliases | bridges | …

    Designed for both BM25 (CJK keywords) and dense embed.
    New slots (P2) are optional and omitted when empty for back-compat.
    """
    parts: List[str] = []
    cat = _clean(category, 80) or "insight"
    parts.append(f"L4|{cat}")
    sm = _clean(summary, 280)
    if sm:
        parts.append(sm)
    ents = [_clean(e, 40) for e in entities if str(e or "").strip()]
    if ents:
        parts.append("实体: " + " / ".join(ents[:12]))
    als = [_clean(a, 40) for a in aliases if str(a or "").strip()]
    if als:
        parts.append("别名: " + " / ".join(als[:8]))
    brs = [_clean(b, 40) for b in bridges if str(b or "").strip()]
    if brs:
        parts.append("桥: " + " / ".join(brs[:6]))
    sn = _clean(snippet, 200)
    if sn:
        parts.append(f"原话: {sn}")
    if timestamp:
        parts.append(f"时间: {_clean(timestamp, 40)}")
    tb = _clean(time_bucket, 16)
    if tb:
        parts.append(f"月: {tb}")
    if source_transcript:
        # keep basename-ish for BM25 without huge paths
        st = str(source_transcript).replace("\\", "/")
        parts.append(f"来源: {st[-120:]}")
    return " ｜ ".join(parts)


def _item_fields(
    obj: Dict[str, Any],
) -> Tuple[str, str, str, str, List[str], List[str]]:
    """Extract (summary, snippet, source, timestamp, entities, aliases).

    P2: ``aliases`` field is returned separately (not folded into entities).
    tags/keywords still feed entities (BM25 keywords).
    """
    summary = ""
    for k in (
        "preference",
        "description",
        "summary",
        "fact",
        "name",
        "title",
        "lesson",
        "capability",
        "pattern",
        "signal",
        "rule",
        "content",
        "text",
        "value",
    ):
        if obj.get(k):
            summary = str(obj.get(k))
            break
    if not summary and obj.get("description"):
        summary = str(obj["description"])
    # prefer description as longer summary when preference is short title
    if obj.get("description") and obj.get("preference"):
        summary = f"{obj.get('preference')}: {obj.get('description')}"
    elif obj.get("description") and not summary:
        summary = str(obj["description"])

    snippet = str(obj.get("snippet") or obj.get("evidence") or "")
    source = str(
        obj.get("source_transcript")
        or (obj.get("source_transcripts") or [None])[0]
        or obj.get("user_only_source")
        or ""
    )
    ts = str(obj.get("timestamp") or obj.get("date") or obj.get("when") or "")
    entities: List[str] = []
    aliases: List[str] = []
    for k in ("entities", "tags", "keywords"):
        v = obj.get(k)
        if isinstance(v, list):
            entities.extend(str(x) for x in v if x)
        elif isinstance(v, str) and v.strip():
            entities.append(v.strip())
    av = obj.get("aliases")
    if isinstance(av, list):
        aliases.extend(str(x) for x in av if x)
    elif isinstance(av, str) and av.strip():
        aliases.append(av.strip())
    for k in ("place", "location", "person", "project", "name"):
        if obj.get(k) and k != "name":
            entities.append(str(obj[k]))
    return summary, snippet, source, ts, entities, aliases


def iter_active_knowledge_items(
    active: Dict[str, Any],
) -> Iterable[Tuple[str, Dict[str, Any]]]:
    """Yield (category_path, item_dict) from official active_knowledge.json shape."""
    if not isinstance(active, dict):
        return

    def walk_list(cat: str, items: Any) -> Iterable[Tuple[str, Dict[str, Any]]]:
        if not isinstance(items, list):
            return
        for it in items:
            if isinstance(it, dict):
                yield cat, it
            elif isinstance(it, str) and it.strip():
                yield cat, {"summary": it.strip()}

    # top-level list sections
    for key in (
        "user_facts",
        "ongoing_projects",
        "agent_capabilities_learned",
        "memory_lessons",
    ):
        yield from walk_list(key, active.get(key))

    profile = active.get("user_profile")
    if isinstance(profile, dict):
        for key in (
            "preferences",
            "behavior_patterns",
            "self_descriptions",
            "life_signals",
            "constraints",
            "goals",
        ):
            yield from walk_list(f"user_profile.{key}", profile.get(key))

    # any other list-of-dict at top level (forward compatible)
    skip = {
        "_meta",
        "user_profile",
        "user_facts",
        "ongoing_projects",
        "agent_capabilities_learned",
        "memory_lessons",
    }
    for key, val in active.items():
        if key in skip:
            continue
        if isinstance(val, list):
            yield from walk_list(key, val)


def iter_emotion_items(emotion_data: Dict[str, Any]) -> Iterable[Tuple[str, Dict[str, Any]]]:
    if not isinstance(emotion_data, dict):
        return
    events = emotion_data.get("events")
    if not isinstance(events, list):
        return
    for ev in events:
        if isinstance(ev, dict):
            yield "emotion_events", ev


def insight_items_to_docs(
    *,
    active: Optional[Dict[str, Any]] = None,
    emotion: Optional[Dict[str, Any]] = None,
    user_id: str = "",
    run_id: str = "",
    max_items: int = 400,
) -> List[Tuple[str, str, Dict[str, Any]]]:
    """Return list of (item_id, embed_text, meta).

    Only objects with some evidence or substantive summary are kept.
    """
    out: List[Tuple[str, str, Dict[str, Any]]] = []
    seen: set = set()

    streams: List[Tuple[str, Dict[str, Any]]] = []
    if active:
        streams.extend(list(iter_active_knowledge_items(active)))
    if emotion:
        streams.extend(list(iter_emotion_items(emotion)))

    for cat, obj in streams:
        summary, snippet, source, ts, entities, field_aliases = _item_fields(obj)
        if not summary and not snippet:
            # emotion may only carry note/signal-like fields already mapped
            if cat == "emotion_events":
                summary = str(
                    obj.get("note")
                    or obj.get("description")
                    or obj.get("signal")
                    or obj.get("category")
                    or ""
                )
            if not summary and not snippet:
                continue
        # require evidence for high-trust: prefer snippet or source
        # CJK summaries are short in chars; emotion events often have date only
        min_len = 4 if cat == "emotion_events" else 8
        if not snippet and not source and len(summary.strip()) < min_len:
            continue
        if cat == "emotion_events" and not snippet and not source and not ts:
            # bare label without any time/evidence — skip
            if len(summary.strip()) < 6:
                continue

        # category for emotions
        if cat == "emotion_events":
            cat_label = f"emotion/{obj.get('category') or 'other'}"
            if not summary:
                summary = str(
                    obj.get("note")
                    or obj.get("description")
                    or obj.get("signal")
                    or obj.get("category")
                    or "emotion"
                )
            intensity = obj.get("intensity")
            if intensity is not None:
                entities = list(entities) + [f"intensity={intensity}"]
            sig = obj.get("signal")
            if sig and str(sig) not in entities and str(sig) not in summary:
                entities = list(entities) + [str(sig)]
        else:
            cat_label = cat

        sk = _stable_key(user_id, cat_label, summary[:80], snippet[:80], source[-80:], ts)
        item_id = f"{_ITEM_PREFIX}{user_id or 'user'}/{cat_label}/{sk}"
        if item_id in seen:
            continue
        seen.add(item_id)

        # P2: aliases + bridges from shared table (entity_alias)
        from G4W.memory.vector.entity_alias import (
            enrich_aliases_and_bridges,
            time_bucket_from_timestamp,
        )

        aliases, bridges = enrich_aliases_and_bridges(
            entities,
            summary,
            extra_aliases=field_aliases,
        )
        time_bucket = time_bucket_from_timestamp(ts)
        text = format_insight_embed_text(
            category=cat_label,
            summary=summary,
            entities=entities,
            aliases=aliases,
            bridges=bridges,
            snippet=snippet,
            source_transcript=source,
            timestamp=ts,
            time_bucket=time_bucket,
        )
        meta = {
            "category": cat_label,
            "source_transcript": source,
            "run_id": run_id,
            "user_id": user_id,
            "aliases": aliases,
            "bridges": bridges,
            "time_bucket": time_bucket,
            "timestamp": ts,
        }
        out.append((item_id, text, meta))
        if len(out) >= max_items:
            break
    return out


def _load_docs(path: Path) -> Dict[str, str]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def _save_docs(path: Path, docs: Dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(docs, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(path))


def _append_tier_records(
    path: Path,
    rows: Sequence[Dict[str, Any]],
) -> None:
    """Append tier rows; rewrite file de-duping by item_id (last wins)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    by_id: Dict[str, Dict[str, Any]] = {}
    if path.is_file():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if isinstance(row, dict) and row.get("item_id"):
                    by_id[str(row["item_id"])] = row
        except Exception:
            pass
    for row in rows:
        iid = str(row.get("item_id") or "")
        if iid:
            by_id[iid] = row
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for row in by_id.values():
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(str(tmp), str(path))


def upsert_l4_insights_to_index(
    *,
    active: Optional[Dict[str, Any]] = None,
    emotion: Optional[Dict[str, Any]] = None,
    user_id: str = "",
    run_id: str = "",
    index_dir: Optional[Path] = None,
    dry_run: bool = False,
    max_items: int = 400,
) -> Dict[str, Any]:
    """Upsert L4 insight docs into live production index (incremental).

    Returns summary dict; never raises (caller may still wrap).
    """
    summary: Dict[str, Any] = {
        "status": "skipped",
        "run_id": run_id,
        "user_id": user_id,
        "upserted": 0,
        "dry_run": dry_run,
    }
    try:
        # Product total gate first (installed∧enabled). Wins over legacy L4 flag.
        try:
            from .vector_config import vector_enabled as _addon_vector_enabled

            if not _addon_vector_enabled():
                summary["reason"] = "vector_addon disabled"
                return summary
        except Exception as exc:
            summary["reason"] = "vector_config_unavailable"
            summary["detail"] = f"{type(exc).__name__}: {exc}"
            return summary

        if not l4_index_upsert_enabled():
            summary["reason"] = "G4W_L4_INDEX_UPSERT disabled"
            return summary

        from .flags import vector_retrieval_enabled
        from .sandbox_paths import (
            assert_prod_index_write_allowed,
            prod_index_write_root,
            resolve_vector_index_dir,
        )
        from .build_prod_index import index_ready
        from .hnsw_index import HnswIndex
        from .embedding import EmbeddingError, embed_batch, resolve_dim

        if not vector_retrieval_enabled():
            summary["reason"] = "vector_retrieval disabled"
            return summary

        live = Path(index_dir) if index_dir is not None else Path(resolve_vector_index_dir())
        # prefer write root when env points read elsewhere
        if index_dir is None:
            try:
                wr = prod_index_write_root()
                if index_ready(wr):
                    live = Path(wr)
            except Exception:
                pass

        summary["index_dir"] = str(live)
        if not index_ready(live):
            summary["reason"] = "index not ready"
            return summary

        docs_items = insight_items_to_docs(
            active=active,
            emotion=emotion,
            user_id=user_id,
            run_id=run_id,
            max_items=max_items,
        )
        summary["candidates"] = len(docs_items)
        if not docs_items:
            summary["status"] = "empty"
            summary["reason"] = "no insight items"
            return summary

        if dry_run:
            summary["status"] = "dry_run"
            summary["sample"] = [
                {"item_id": i, "text": t[:120]} for i, t, _ in docs_items[:5]
            ]
            return summary

        # TASK-E: ensure embed server before embed/write (soft-dep: import fail -> no-op).
        # Remote embedding failures must stay visible; no implicit hash fallback.
        # summary["tei"] kept as compat alias of summary["embed"].
        summary["embed"] = {"status": "not_attempted", "ok": False}
        summary["tei"] = summary["embed"]
        try:
            try:
                from .embed_lifecycle import ensure_embed_running as _ensure_embed
            except Exception:
                from .tei_lifecycle import ensure_tei_running as _ensure_embed

            embed_out = _ensure_embed(timeout_s=30.0)
            if isinstance(embed_out, dict):
                summary["embed"] = embed_out
            else:
                summary["embed"] = {"status": "ok", "ok": True, "raw": embed_out}
            summary["tei"] = summary["embed"]
        except Exception as exc:
            summary["embed"] = {
                "status": "skipped",
                "ok": True,
                "detail": f"ensure_embed soft-dep: {type(exc).__name__}: {exc}",
            }
            summary["tei"] = summary["embed"]

        assert_prod_index_write_allowed(live)

        # load index + dim (HnswIndex exposes .dim; meta only on disk)
        idx = HnswIndex.load(live)
        dim = int(getattr(idx, "dim", 0) or 0)
        if dim <= 0:
            try:
                meta_raw = json.loads((live / "meta.json").read_text(encoding="utf-8"))
                dim = int(meta_raw.get("dim") or 0)
            except Exception:
                dim = 0
        if dim <= 0:
            dim = int(resolve_dim(None))
        labels = [i for i, _, _ in docs_items]
        texts = [t for _, t, _ in docs_items]
        vectors = embed_batch(texts, dim=dim, as_int8=False)

        # upsert vectors (replace same id)
        idx.add(vectors, labels=labels, replace=True)
        idx.save(live)

        # merge docs.json
        docs_path = live / "docs.json"
        docs = _load_docs(docs_path)
        for iid, text, _ in docs_items:
            docs[iid] = text
        _save_docs(docs_path, docs)

        # tier: P3 category→tier when flag ON; else legacy all-WARM
        now = time.time()
        use_cat_tier = l4_tier_by_category_enabled()
        tier_rows = []
        tier_counts: Dict[str, int] = {}
        for iid, text, meta in docs_items:
            cat = str(meta.get("category") or "")
            ts = str(meta.get("timestamp") or "")
            if use_cat_tier:
                tier = suggest_tier_for_l4(cat, timestamp=ts, now=now)
            else:
                tier = "WARM"
            tier_counts[tier] = tier_counts.get(tier, 0) + 1
            tier_rows.append(
                {
                    "item_id": iid,
                    "tier": tier,
                    "created_at": now,
                    "last_access_at": now,
                    "size_bytes": len(text.encode("utf-8", errors="replace")),
                    "source": "l4_insight",
                    "run_id": run_id,
                    "category": cat or meta.get("category"),
                    "source_transcript": meta.get("source_transcript"),
                    "aliases": meta.get("aliases") or [],
                    "bridges": meta.get("bridges") or [],
                    "time_bucket": meta.get("time_bucket") or "",
                }
            )
        _append_tier_records(live / "tier_records.jsonl", tier_rows)
        summary["tier_counts"] = tier_counts
        summary["tier_by_category"] = bool(use_cat_tier)

        # meta patch (non-fatal)
        try:
            meta_path = live / "meta.json"
            if meta_path.is_file():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                meta["last_l4_upsert_at"] = now
                meta["last_l4_upsert_run_id"] = run_id
                meta["last_l4_upsert_count"] = len(docs_items)
                # refresh count if available
                try:
                    meta["count"] = int(HnswIndex.load(live).count)
                except Exception:
                    pass
                meta_path.write_text(
                    json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
                )
        except Exception as exc:
            _log.warning("l4 upsert meta patch failed: %s", exc)

        summary["status"] = "ok"
        summary["upserted"] = len(docs_items)
        summary["dim"] = dim
        return summary
    except EmbeddingError as exc:
        _log.warning("l4_index_upsert embedding failed: %s", exc, exc_info=True)
        summary["status"] = "error"
        summary["reason"] = "embedding_failed"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        if getattr(exc, "reason", None):
            summary["embedding_reason"] = exc.reason
        return summary
    except Exception as exc:
        _log.warning("l4_index_upsert failed: %s", exc, exc_info=True)
        summary["status"] = "error"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        return summary


def upsert_after_l4_finalize(
    root: Path,
    user_id: str,
    run_id: str,
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Load official L4 files from memory root and upsert.

    ``root`` is the memory data root used by l4_safe (conversations parent etc.).
    """
    try:
        from ..l4_safe import (
            active_knowledge_path,
            emotion_events_path,
            load_json,
        )
    except Exception:
        # relative import when used as script
        from G4W.memory.l4_safe import (  # type: ignore
            active_knowledge_path,
            emotion_events_path,
            load_json,
        )

    active = load_json(active_knowledge_path(root, user_id), {})
    emotion = load_json(emotion_events_path(root, user_id), {})
    return upsert_l4_insights_to_index(
        active=active if isinstance(active, dict) else {},
        emotion=emotion if isinstance(emotion, dict) else {},
        user_id=user_id,
        run_id=run_id,
        dry_run=dry_run,
    )
