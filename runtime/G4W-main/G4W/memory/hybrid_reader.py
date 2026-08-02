"""Production hybrid main-read (read-only).

Feature-flagged via G4W_HYBRID_MAIN_READ=1.
Reads G4W-data/hybrid/{cas,meta.sqlite} only — never writes hybrid surface.
"""
from __future__ import annotations

import math
import os
import re
import sqlite3
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


_token = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


def _read_env_file_value(name: str) -> str:
    """Read a single key from package-local .env (G4W does not dump .env into os.environ)."""
    try:
        # G4W/memory/hybrid_reader.py -> G4W-main/.env
        env_path = Path(__file__).resolve().parents[2] / ".env"
        if not env_path.exists():
            return ""
        for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == name:
                value = value.strip()
                if value[:1] == value[-1:] and value[:1] in ("'", '"'):
                    value = value[1:-1]
                return value
    except Exception:
        return ""
    return ""


def hybrid_main_read_enabled() -> bool:
    raw = str(os.environ.get("G4W_HYBRID_MAIN_READ", "") or "").strip()
    if not raw:
        raw = _read_env_file_value("G4W_HYBRID_MAIN_READ")
    raw = raw.lower()
    if not raw:
        return False
    return raw not in ("0", "false", "off", "no", "disable", "disabled")


def tokenize(text: str) -> List[str]:
    return [t.lower() for t in _token.findall(text or "")]


@dataclass
class Hit:
    chunk_id: str
    score: float
    cas_hash: str
    provenance: Dict[str, Any] = field(default_factory=dict)
    text_preview: str = ""


class CAS:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, h: str) -> Path:
        return self.root / h[:2] / h[2:4] / h

    def get(self, h: str) -> Optional[bytes]:
        p = self._path(h)
        try:
            return p.read_bytes() if p.exists() else None
        except Exception:
            return None

    def has(self, h: str) -> bool:
        return self._path(h).exists()


def _bm25(query: str, docs: Dict[str, str], k1: float = 1.5, b: float = 0.75) -> Dict[str, float]:
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


def _mock_vector(query: str, docs: Dict[str, str]) -> Dict[str, float]:
    q = Counter(tokenize(query))
    if not q:
        return {}
    qn = math.sqrt(sum(v * v for v in q.values())) or 1.0
    out: Dict[str, float] = {}
    for i, t in docs.items():
        d = Counter(tokenize(t))
        dn = math.sqrt(sum(v * v for v in d.values())) or 1.0
        dot = sum(q[k] * d.get(k, 0) for k in q)
        out[i] = dot / (qn * dn)
    return out


def _fuse(vector: Dict[str, float], lexical: Dict[str, float], alpha: float = 0.6) -> List[tuple]:
    ids = set(vector) | set(lexical)
    ranked = []
    for i in ids:
        s = alpha * vector.get(i, 0.0) + (1 - alpha) * lexical.get(i, 0.0)
        ranked.append((i, s))
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked


class HybridMainReader:
    """In-process cached hybrid search over production hybrid surface (read-only)."""

    def __init__(self, hybrid_root: Path, top_k: int = 5, max_section_chars: int = 3500):
        self.hybrid_root = Path(hybrid_root)
        self.cas = CAS(self.hybrid_root / "cas")
        self.meta_path = self.hybrid_root / "meta.sqlite"
        self.top_k = max(1, int(top_k))
        self.max_section_chars = max(500, int(max_section_chars))
        self._lock = threading.RLock()
        self._docs: Dict[str, str] = {}
        self._meta: Dict[str, Dict[str, Any]] = {}
        self._mtime: float = -1.0
        self._loaded_at: float = 0.0
        self._load_error: str = ""

    def available(self) -> bool:
        """Surface + optional integrity gate for inject path.

        Gate1: meta.sqlite + cas/ exist
        Gate2: chunks count == cas_index count and chunks >= 1
        Optional: G4W_HYBRID_MIN_CHUNKS (default 0 = no floor)

        Read-only sqlite (mode=ro). Never writes hybrid surface.
        Fail closed for integrity errors (available=False → inject soft-empty).
        """
        if not (self.meta_path.exists() and (self.hybrid_root / "cas").exists()):
            return False
        try:
            min_chunks_raw = str(os.environ.get("G4W_HYBRID_MIN_CHUNKS", "") or "").strip()
            if not min_chunks_raw:
                min_chunks_raw = _read_env_file_value("G4W_HYBRID_MIN_CHUNKS")
            try:
                min_chunks = int(min_chunks_raw) if min_chunks_raw else 0
            except ValueError:
                min_chunks = 0
            conn = sqlite3.connect(f"file:{self.meta_path.as_posix()}?mode=ro", uri=True)
            try:
                chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
                cas_idx = conn.execute("SELECT COUNT(*) FROM cas_index").fetchone()[0]
            finally:
                conn.close()
            if chunks is None or cas_idx is None:
                return False
            if int(chunks) < 1 or int(chunks) != int(cas_idx):
                return False
            if min_chunks > 0 and int(chunks) < min_chunks:
                return False
            return True
        except Exception:
            # integrity probe failed → not available for inject
            return False

    def _reload_if_needed(self) -> None:
        with self._lock:
            if not self.available():
                self._docs = {}
                self._meta = {}
                self._load_error = "hybrid surface missing"
                return
            try:
                mtime = self.meta_path.stat().st_mtime
            except Exception as e:
                self._load_error = f"stat meta: {e}"
                return
            if self._docs and mtime == self._mtime:
                return
            try:
                conn = sqlite3.connect(f"file:{self.meta_path.as_posix()}?mode=ro", uri=True)
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT chunk_id, cas_hash, source_path, capture_run_id, ttl_mark, meta_json FROM chunks"
                ).fetchall()
                conn.close()
            except Exception as e:
                self._load_error = f"open meta: {e}"
                return
            docs: Dict[str, str] = {}
            meta: Dict[str, Dict[str, Any]] = {}
            for row in rows:
                cid = str(row["chunk_id"] or "")
                h = str(row["cas_hash"] or "")
                if not cid or not h:
                    continue
                raw = self.cas.get(h)
                if raw is None:
                    continue
                try:
                    text = raw.decode("utf-8", errors="replace")
                except Exception:
                    text = ""
                if not text.strip():
                    continue
                docs[cid] = text
                meta[cid] = {
                    "cas_hash": h,
                    "source_path": row["source_path"],
                    "capture_run_id": row["capture_run_id"],
                    "ttl_mark": row["ttl_mark"],
                }
            self._docs = docs
            self._meta = meta
            self._mtime = mtime
            self._loaded_at = time.time()
            self._load_error = ""

    def search(self, query: str, top_k: Optional[int] = None) -> List[Hit]:
        self._reload_if_needed()
        k = int(top_k or self.top_k)
        q = (query or "").strip()
        if not q or not self._docs:
            return []
        v = _mock_vector(q, self._docs)
        l = _bm25(q, self._docs)
        # Overfetch then drop near-zero fuse scores so CAS filler cannot
        # drown the vector path (mock cosine on unrelated CJK runs ≈ 0).
        ranked = _fuse(v, l, alpha=0.6)[: max(k * 4, 12)]
        min_score = 0.02
        hits: List[Hit] = []
        for cid, score in ranked:
            sc = float(score)
            if sc < min_score:
                continue
            m = self._meta.get(cid, {})
            hits.append(
                Hit(
                    chunk_id=cid,
                    score=sc,
                    cas_hash=str(m.get("cas_hash") or ""),
                    provenance={
                        "source_path": m.get("source_path"),
                        "capture_run_id": m.get("capture_run_id"),
                        "ttl_mark": m.get("ttl_mark"),
                    },
                    text_preview=(self._docs.get(cid) or "")[:240],
                )
            )
            if len(hits) >= k:
                break
        return hits

    def format_section(self, query: str, top_k: Optional[int] = None) -> str:
        """Markdown section for system prompt injection. Empty string on miss/error."""
        if not hybrid_main_read_enabled():
            return ""
        if not self.available():
            return ""
        try:
            hits = self.search(query, top_k=top_k)
        except Exception as e:
            return f"## Hybrid Memory Hits\n(hybrid search error: {e})"
        if not hits:
            if self._load_error:
                return f"## Hybrid Memory Hits\n(hybrid load note: {self._load_error})"
            return ""
        lines = [
            "## Hybrid Memory Hits",
            f"query: {query[:120]}",
            f"hits: {len(hits)} / corpus={len(self._docs)}",
            "",
        ]
        used = sum(len(x) for x in lines)
        for i, h in enumerate(hits, 1):
            body = (h.text_preview or "").replace("\r\n", "\n").strip()
            if len(body) > 280:
                body = body[:280] + "…"
            block = (
                f"### hit {i} score={h.score:.4f} id={h.chunk_id}\n"
                f"src={h.provenance.get('source_path') or ''}\n"
                f"{body}\n"
            )
            if used + len(block) > self.max_section_chars:
                lines.append(f"(truncated remaining hits for length budget)")
                break
            lines.append(block)
            used += len(block)
        return "\n".join(lines).strip()


_reader_cache: Dict[str, HybridMainReader] = {}
_reader_lock = threading.Lock()


def get_reader(hybrid_root: Path) -> HybridMainReader:
    key = str(Path(hybrid_root).resolve())
    with _reader_lock:
        r = _reader_cache.get(key)
        if r is None:
            r = HybridMainReader(Path(hybrid_root))
            _reader_cache[key] = r
        return r


def build_query_from_memory_sections(sections: List[str], max_chars: int = 400) -> str:
    """Derive a compact lexical query from already-loaded memory text."""
    bag: Counter = Counter()
    for s in sections:
        bag.update(tokenize(s or ""))
    # prefer mid-length Chinese/ASCII tokens, drop pure digits/noise
    scored = []
    for t, c in bag.most_common(80):
        if t.isdigit():
            continue
        if len(t) < 2:
            continue
        scored.append((c, t))
    terms = [t for _, t in scored[:24]]
    q = " ".join(terms)
    return q[:max_chars].strip()


def resolve_inject_query(
    user_query: Optional[str],
    sections: Optional[List[str]],
    *,
    sender_id: str = "",
    sticky: str = "memory transcript",
    max_chars: int = 400,
) -> str:
    """Prefer this-round user message; else lexical bag from memory sections."""
    q = str(user_query or "").strip()
    if q:
        return q[:max_chars]
    q = build_query_from_memory_sections(list(sections or []), max_chars=max_chars)
    if q:
        return q
    return f"sender {sender_id} {sticky}".strip()


def hybrid_section_for(
    memory_root: Path,
    sender_id: str,
    sections: List[str],
    query: Optional[str] = None,
) -> str:
    """Entry used by ConversationStore.read_memory. Fail-soft.

    Prefer ``query`` (typically this-round user message). When empty, fall back
    to lexical terms extracted from already-loaded memory sections.
    """
    try:
        if not hybrid_main_read_enabled():
            return ""
        hybrid_root = Path(memory_root).resolve().parent / "hybrid"
        reader = get_reader(hybrid_root)
        resolved = resolve_inject_query(
            query,
            sections,
            sender_id=sender_id,
            sticky="memory transcript",
        )
        return reader.format_section(resolved)
    except Exception as e:
        return f"## Hybrid Memory Hits\n(hybrid inject soft-fail: {e})"
