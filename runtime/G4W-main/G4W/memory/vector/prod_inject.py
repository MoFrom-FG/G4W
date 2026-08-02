"""Production read-path vector inject (feature-flagged, fail-soft, default OFF).

Index root: resolve_vector_index_dir() - env G4W_VECTOR_INDEX_DIR or sandbox.
R2: HybridQueryEngine + optional docs/tier sidecars (never writes production DATA).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

from .flags import vector_retrieval_enabled
from .hnsw_index import HnswIndex
from .hybrid_query import HybridQueryEngine
from .sandbox_paths import resolve_vector_index_dir
from .tier_policy import Tier, TierRecord, default_policy


def _build_query_from_sections(sections: Sequence[str], max_chars: int = 400) -> str:
    """Compact query from memory sections; prefer hybrid_reader helper if present."""
    try:
        from G4W.memory.hybrid_reader import build_query_from_memory_sections

        q = build_query_from_memory_sections(list(sections), max_chars=max_chars)
        if q:
            return q
    except Exception:
        pass
    # fallback: join truncated section tails
    bag: List[str] = []
    for s in sections or []:
        t = str(s or "").strip()
        if t:
            bag.append(t[-200:])
    q = " ".join(bag)
    return q[:max_chars].strip()


def _index_ready(index_dir: Path) -> bool:
    d = Path(index_dir)
    if not d.is_dir():
        return False
    meta = d / "meta.json"
    if not meta.is_file():
        return False
    # brute: vectors.npy; hnswlib: index.bin
    return (d / "index.bin").is_file() or (d / "vectors.npy").is_file()


def _labels_from_index(index_dir: Path, idx: object) -> List[str]:
    """Live labels for synthetic sidecars; skip soft-deleted markers."""

    def _clean(raw) -> List[str]:
        if not raw:
            return []
        return [
            str(x)
            for x in raw
            if x is not None and not str(x).startswith("__deleted_")
        ]

    out = _clean(getattr(idx, "_labels", None))
    if out:
        return out
    brute = getattr(idx, "_brute", None)
    if brute is not None:
        out = _clean(getattr(brute, "_labels", None))
        if out:
            return out
    meta_path = Path(index_dir) / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return _clean(meta.get("labels") or [])
    except Exception:
        return []


def _load_docs(path: Path) -> Dict[str, str]:
    """Load docs.json → {item_id: text}. Missing/invalid → {}."""
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items()}
    except Exception:
        return {}


def _parse_tier(val: object) -> Tier:
    s = str(val or "HOT").strip().upper()
    try:
        return Tier(s)
    except Exception:
        return Tier.HOT


def _load_tier_records(
    path: Path, labels: Sequence[str]
) -> Dict[str, TierRecord]:
    """Load tier_records.jsonl; missing → HOT synthetic for each label."""
    now = time.time()
    records: Dict[str, TierRecord] = {}
    p = Path(path)
    if p.is_file():
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if not isinstance(row, dict):
                    continue
                iid = str(row.get("item_id") or "").strip()
                if not iid:
                    continue
                rec = TierRecord(
                    item_id=iid,
                    tier=_parse_tier(row.get("tier")),
                    created_at=float(row.get("created_at", now)),
                    last_access_at=float(row.get("last_access_at", now)),
                    size_bytes=int(row.get("size_bytes") or 0),
                    tombstone=bool(row.get("tombstone", False)),
                    extra=dict(row.get("extra") or {})
                    if isinstance(row.get("extra"), dict)
                    else {},
                )
                records[iid] = rec
        except Exception:
            records = {}

    # synthesize HOT for labels not present (also when file missing)
    for lab in labels:
        if lab not in records:
            records[lab] = TierRecord(
                item_id=lab,
                tier=Tier.HOT,
                created_at=now,
                last_access_at=now,
                size_bytes=0,
                tombstone=False,
            )
    return records


def _ensure_docs_for_labels(docs: Dict[str, str], labels: Sequence[str]) -> Dict[str, str]:
    """Plan: missing docs → still hybrid shortlist via live labels (empty text OK)."""
    out = dict(docs)
    for lab in labels:
        if lab not in out:
            out[lab] = ""
    return out


def _format_hits(
    hits: Sequence[object],
    *,
    index_dir: Path,
    backend: str,
    query: str,
    k: int,
) -> str:
    lines = [
        "## Vector Retrieval Hits",
        f"(sandbox: {index_dir}; mode=hybrid; backend={backend}; k={k})",
        f"query: {query[:120]}",
        "",
    ]
    if not hits:
        lines.append("(0 hits for query)")
        return "\n".join(lines).rstrip() + "\n"
    for i, h in enumerate(hits, 1):
        iid = getattr(h, "item_id", None) or getattr(h, "label", "?")
        score = float(getattr(h, "score", 0.0))
        tier = getattr(h, "tier", None)
        preview = (getattr(h, "text_preview", None) or "")[:80]
        row = f"{i}. `{iid}` score={score:.4f}"
        if tier:
            row += f" tier={tier}"
        lines.append(row)
        if preview.strip():
            lines.append(f"   {preview.strip()}")
    return "\n".join(lines).rstrip() + "\n"


def vector_section_for(
    memory_root: Union[str, Path],
    sender_id: str,
    sections: Optional[Sequence[str]] = None,
    *,
    query: Optional[str] = None,
    k: int = 5,
    check_flag: bool = True,
) -> str:
    """Return markdown section for hybrid vector hits (resolved index dir).

    Index root via resolve_vector_index_dir() (env INDEX_DIR or sandbox).
    Prefer ``query`` (this-round user message); else lexical bag from sections.
    Callers (e.g. conversation.read_memory) should gate on vector_retrieval_enabled();
    ``check_flag=True`` re-checks so direct callers stay safe.
    """
    try:
        if check_flag:
            # Addon product gate first (installed∧enabled); then legacy env flag.
            try:
                from .vector_config import vector_enabled as _addon_vector_enabled

                if not _addon_vector_enabled():
                    return ""
            except Exception:
                pass
            if not vector_retrieval_enabled():
                return ""

        index_dir = resolve_vector_index_dir()
        if not _index_ready(index_dir):
            return ""

        idx = HnswIndex.load(index_dir)
        if getattr(idx, "count", 0) <= 0:
            return ""

        labels = _labels_from_index(index_dir, idx)
        docs = _ensure_docs_for_labels(_load_docs(index_dir / "docs.json"), labels)
        records = _load_tier_records(index_dir / "tier_records.jsonl", labels)

        secs = list(sections or [])
        try:
            from G4W.memory.hybrid_reader import resolve_inject_query

            query = resolve_inject_query(
                query,
                secs,
                sender_id=sender_id,
                sticky="memory vector",
            )
        except Exception:
            query = str(query or "").strip() or _build_query_from_sections(secs)
            if not query:
                query = f"sender {sender_id} memory vector"

        dim = int(getattr(idx, "dim", 0) or 384)
        engine = HybridQueryEngine(
            index=idx,
            policy=default_policy(),
            records=records,
            docs=docs,
            dim=dim,
        )
        hits = engine.search(query, k=max(1, int(k)))
        backend = str(getattr(idx, "backend", "?") or "?")
        # memory_root unused intentionally — do not scan production DATA
        _ = memory_root
        return _format_hits(
            hits,
            index_dir=index_dir,
            backend=backend,
            query=query,
            k=max(1, int(k)),
        )
    except Exception as e:
        return f"## Vector Retrieval Hits\n(vector inject soft-fail: {e})\n"
