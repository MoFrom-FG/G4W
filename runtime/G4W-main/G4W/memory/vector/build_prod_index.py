"""Build production vector index from DATA/memory → INDEX_DIR.

Flow: scan text corpus → embed_batch → HnswIndex → staging → index_ready
→ backup live (if any) → switch staging→live.

Uses I1 path APIs:
  - prod_index_write_root() / DEFAULT_PROD_INDEX_DIR for write target
  - assert_prod_index_write_allowed / prod_index_staging_dir
  - resolve_vector_index_dir is READ path (env or sandbox); not used as default write root

Never writes DATA/hybrid/**, never hard-deletes live without backup, does not
flip G4W_VECTOR_RETRIEVAL (default remains OFF).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from G4W.memory.vector.embedding import (
    embed_batch,
    resolve_dim,
    resolve_provider,
)
from G4W.memory.vector.hnsw_index import HnswIndex, create_index
from G4W.memory.vector.sandbox_paths import (
    DEFAULT_PROD_INDEX_DIR,
    assert_prod_index_write_allowed,
    prod_index_staging_dir,
    prod_index_write_root,
)

_log = logging.getLogger(__name__)

def _default_data_dir() -> Path:
    workspace = os.environ.get("G4W_WORKSPACE_ROOT") or os.environ.get("G4W_WORKSPACE_ROOT")
    if workspace:
        return Path(workspace) / "runtime" / "G4W-data"
    return DEFAULT_PROD_INDEX_DIR.parent / "G4W-data"


DEFAULT_DATA_DIR = _default_data_dir()

_ENV_MAX_ITEMS = "G4W_VECTOR_INDEX_MAX_ITEMS"
_ENV_DATA_DIR = "G4W_DATA_DIR"

_TEXT_EXTS = {".md", ".txt"}
_JSON_EXT = ".json"
# Allow multi-day / root transcripts (~1–2MB). Oversized still skipped.
_MAX_FILE_BYTES = 2_000_000
# Per-file read cap before chunking. MUST cover full daily transcripts so tail
# events (e.g. 群里问好) are embedded — old 12_000 cut mid-file and dropped recall.
# Chunking (_CHUNK_*) + max_items still bound index size.
_MAX_TEXT_CHARS = 2_000_000
_CHUNK_CHARS = 900
_CHUNK_OVERLAP = 120
_SOFT_INDEX_BYTES = 500 * 1024 * 1024  # 500MB soft cap


def resolve_data_dir() -> Path:
    raw = (os.environ.get(_ENV_DATA_DIR) or "").strip()
    if raw:
        return Path(raw).expanduser()
    return DEFAULT_DATA_DIR


def default_max_items() -> int:
    raw = (os.environ.get(_ENV_MAX_ITEMS) or "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return 5000


def _read_text_file(path: Path, max_chars: int = _MAX_TEXT_CHARS) -> str:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    raw = raw.strip()
    return raw[:max_chars] if len(raw) > max_chars else raw


def _json_text_fields(path: Path, max_chars: int = _MAX_TEXT_CHARS) -> str:
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return ""
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return ""
    parts: List[str] = []
    secret_keys = ("password", "secret", "api_key", "token", "authorization")

    def walk(obj: Any, depth: int = 0) -> None:
        if depth > 6 or sum(len(p) for p in parts) >= max_chars:
            return
        if isinstance(obj, str):
            s = obj.strip()
            if s and len(s) < 8000:
                parts.append(s)
        elif isinstance(obj, dict):
            for k, v in obj.items():
                if str(k).lower() in secret_keys:
                    continue
                walk(v, depth + 1)
        elif isinstance(obj, list):
            for it in obj[:50]:
                walk(it, depth + 1)

    walk(data)
    joined = "\n".join(parts).strip()
    return joined[:max_chars] if joined else ""


def _is_noise_corpus(rel_posix: str) -> bool:
    """Conductor/worker/assistant echoes pollute BM25 and ANN with query echoes."""
    r = (rel_posix or "").replace("\\", "/").lower()
    return (
        "/conductor/" in r
        or "/rounds/" in r
        or "/assistant-replies/" in r
        or "/workers/" in r
        or "/model-responses/" in r
        or "/model_responses/" in r
        or "model_responses_" in r
        or "/tool-results/" in r
        or "/tool_results/" in r
        or "/runtime/" in r
    )


def _corpus_priority(rel_posix: str) -> int:
    """Lower = more important for semantic recall (transcripts first)."""
    r = (rel_posix or "").replace("\\", "/").lower()
    # Hard-exclude noise: never enter the candidate list (see scan_memory_items).
    if _is_noise_corpus(r):
        return 99
    if "/transcripts/" in r or r.startswith("transcripts/"):
        return 0
    if "/user_only/" in r:
        return 0
    if "/history/" in r or r.endswith("history.md") or r.endswith("history_insight.md"):
        return 1
    if "/summaries/diary/" in r:
        return 1
    if "/summaries/" in r:
        return 2
    if r.startswith("conversations/") and r.endswith(".md"):
        return 3
    if r.endswith(".md") or r.endswith(".txt"):
        return 4
    # JSON registries / bindings / indexes are noisy for fuzzy personal recall
    return 9


def _chunk_text(
    text: str,
    *,
    chunk_chars: int = _CHUNK_CHARS,
    overlap: int = _CHUNK_OVERLAP,
) -> List[str]:
    """Split long docs so local events (e.g. 婚宴) are not diluted in one vector."""
    t = (text or "").strip()
    if not t:
        return []
    if len(t) <= chunk_chars:
        return [t]
    step = max(1, chunk_chars - max(0, overlap))
    chunks: List[str] = []
    i = 0
    n = len(t)
    while i < n:
        piece = t[i : i + chunk_chars].strip()
        if piece:
            chunks.append(piece)
        if i + chunk_chars >= n:
            break
        i += step
    return chunks or [t[:chunk_chars]]


def expand_items_with_chunks(
    files: Sequence[Tuple[str, str]],
    max_items: int,
) -> List[Tuple[str, str]]:
    """Expand (path, text) → chunked (path#cN, chunk) under max_items budget.

    Priority order of *files* is preserved; each file yields #c0, #c1, ... until
    the global cap is hit. Short files stay as bare path (no #c0) for stable ids.
    """
    out: List[Tuple[str, str]] = []
    cap = max(1, int(max_items))
    for rel, text in files:
        if len(out) >= cap:
            break
        chunks = _chunk_text(text)
        if len(chunks) == 1:
            out.append((rel, chunks[0]))
            continue
        for ci, ch in enumerate(chunks):
            if len(out) >= cap:
                break
            out.append((f"{rel}#c{ci}", ch))
    return out


def scan_memory_items(
    data_dir: Path,
    max_items: int,
    *,
    max_file_bytes: int = _MAX_FILE_BYTES,
) -> List[Tuple[str, str]]:
    """Return (item_id, text) from DATA/memory. item_id = posix relpath[#cN].

    Prefer conversation transcripts and markdown history over JSON noise so a
    max_items cap does not starve personal-memory paths. Long files are chunked
    so sparse personal events survive embedding.
    """
    memory_root = Path(data_dir) / "memory"
    if not memory_root.is_dir():
        _log.warning("memory root missing: %s", memory_root)
        return []

    candidates: List[Tuple[int, str, str]] = []  # (priority, rel, text)
    for root, dirs, files in os.walk(memory_root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fn in sorted(files):
            ext = os.path.splitext(fn)[1].lower()
            if ext not in _TEXT_EXTS and ext != _JSON_EXT:
                continue
            fp = Path(root) / fn
            try:
                sz = fp.stat().st_size
            except OSError:
                continue
            if sz <= 0 or sz > max_file_bytes:
                continue
            if ext in _TEXT_EXTS:
                text = _read_text_file(fp)
            else:
                text = _json_text_fields(fp)
            if not text or len(text) < 8:
                continue
            rel = fp.relative_to(memory_root).as_posix()
            if _is_noise_corpus(rel):
                continue
            pri = _corpus_priority(rel)
            if pri >= 99:
                continue
            candidates.append((pri, rel, text))

    candidates.sort(key=lambda x: (x[0], x[1]))
    # Soft file cap larger than item cap so chunking of top-priority files
    # still fills the index budget (transcripts first).
    file_cap = max(int(max_items), min(len(candidates), int(max_items) * 2))
    files: List[Tuple[str, str]] = [
        (rel, text) for _, rel, text in candidates[:file_cap]
    ]
    return expand_items_with_chunks(files, max_items=max(1, int(max_items)))


def index_ready(directory: Path) -> bool:
    d = Path(directory)
    if not (d / "meta.json").is_file():
        return False
    return (d / "index.bin").is_file() or (d / "vectors.npy").is_file()


def _provider_meta() -> Tuple[str, str]:
    """Honest provider + model (never log secrets).

    Prefer embedding facade resolvers so vector_config (ST addon) wins over
    stale EMBEDDING_MODEL env (often LM Studio / empty). Empty model with a
    non-hash provider used to be mis-labeled as hash and hid real ST builds.
    """
    from G4W.memory.vector.embedding import _resolve_model

    provider = resolve_provider()
    model = (_resolve_model() or "").strip()
    if provider == "hash":
        return "hash", "hash"
    if not model:
        # remote selected but model missing → embed soft-fails / not ready
        return "hash", "hash"
    return provider, model


def _patch_meta_extra(directory: Path, extra: Dict[str, Any]) -> None:
    meta_path = Path(directory) / "meta.json"
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    data.update(extra)
    meta_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _dir_size_bytes(directory: Path) -> int:
    total = 0
    for root, _, files in os.walk(directory):
        for fn in files:
            try:
                total += (Path(root) / fn).stat().st_size
            except OSError:
                pass
    return total


def prod_index_backup_root(live: Optional[Path] = None) -> Path:
    """Directory for full-index snapshots (not beside live; keeps runtime root clean).

    Default: ``<INDEX_DIR.parent>/temp/G4W-temp``
    e.g. ``D:\\Agent\\G4W\\runtime\\temp\\G4W-temp``.
    Override: env ``G4W_VECTOR_INDEX_BAK_DIR``.
    """
    raw = str(os.environ.get("G4W_VECTOR_INDEX_BAK_DIR", "") or "").strip()
    if raw:
        return Path(raw)
    live_p = Path(live) if live is not None else prod_index_write_root()
    return Path(live_p).resolve().parent / "temp" / "G4W-temp"


def backup_live_index(live: Path) -> Optional[Path]:
    """Copy live → ``<backup_root>/<live.name>.bak.<ts>``.

    Returns bak path or None if nothing to backup.
    Snapshots live under runtime/temp/G4W-temp (not as siblings of INDEX_DIR).
    """
    live = Path(live)
    if not live.is_dir():
        return None
    try:
        if not any(live.iterdir()):
            return None
    except OSError:
        return None
    ts = time.strftime("%Y%m%d_%H%M%S")
    bak_root = prod_index_backup_root(live)
    bak_root.mkdir(parents=True, exist_ok=True)
    bak = bak_root / f"{live.name}.bak.{ts}"
    if bak.exists():
        bak = bak_root / f"{live.name}.bak.{ts}_{os.getpid()}"
    shutil.copytree(live, bak)
    return bak


def switch_staging_to_live(staging: Path, live: Path) -> None:
    """Replace live with staging via rename swap. No rmtree(live) without prior backup."""
    staging = Path(staging)
    live = Path(live)
    if not index_ready(staging):
        raise RuntimeError(f"staging not ready: {staging}")

    live.parent.mkdir(parents=True, exist_ok=True)
    swap = Path(str(live) + f".swap.{os.getpid()}")
    if swap.exists():
        shutil.rmtree(swap)

    if live.exists():
        os.replace(str(live), str(swap))
        try:
            os.replace(str(staging), str(live))
        except OSError:
            if not live.exists() and swap.exists():
                os.replace(str(swap), str(live))
            raise
        try:
            shutil.rmtree(swap)
        except OSError as exc:
            _log.warning("could not remove swap dir %s: %s", swap, exc)
    else:
        os.replace(str(staging), str(live))


def build_index(
    *,
    data_dir: Optional[Path] = None,
    index_dir: Optional[Path] = None,
    max_items: Optional[int] = None,
    dim: Optional[int] = None,
    dry_run: bool = False,
    write_docs: bool = True,
) -> Dict[str, Any]:
    """Build capped prod index. Returns summary dict (no secrets)."""
    data = Path(data_dir) if data_dir is not None else resolve_data_dir()
    # Write root: explicit arg > prod_index_write_root (env or DEFAULT_PROD_INDEX_DIR)
    live = Path(index_dir) if index_dir is not None else prod_index_write_root()
    cap = int(max_items) if max_items is not None else default_max_items()
    d = resolve_dim(dim)
    provider, embed_model = _provider_meta()

    staging = prod_index_staging_dir()
    # if caller passed custom index_dir, stage as sibling of that path
    if index_dir is not None:
        staging = Path(str(live) + ".staging")

    assert_prod_index_write_allowed(live)
    assert_prod_index_write_allowed(staging)

    items = scan_memory_items(data, max_items=cap)
    summary: Dict[str, Any] = {
        "data_dir": str(data),
        "index_dir": str(live),
        "staging": str(staging),
        "max_items": cap,
        "scanned": len(items),
        "dim": d,
        "provider": provider,
        "embed_model": embed_model,
        "dry_run": dry_run,
        "default_prod_index_dir": str(DEFAULT_PROD_INDEX_DIR),
    }
    if not items:
        summary["status"] = "empty_corpus"
        return summary

    labels = [it[0] for it in items]
    texts = [it[1] for it in items]
    vectors = embed_batch(texts, dim=d, as_int8=False)
    if vectors.shape != (len(texts), d):
        raise RuntimeError(f"embed_batch shape {vectors.shape} != ({len(texts)}, {d})")

    # Re-check honesty after embed
    provider2, model2 = _provider_meta()
    summary["provider"] = provider2
    summary["embed_model"] = model2

    if dry_run:
        summary["status"] = "dry_run"
        summary["sample_labels"] = labels[:5]
        return summary

    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)

    idx = create_index(dim=d, max_elements=max(cap + 1024, 10_000))
    idx.add(np.asarray(vectors, dtype=np.float32), labels=labels)
    idx.save(staging)

    extra = {
        "provider": summary["provider"],
        "embed_model": summary["embed_model"],
        "source": "DATA/memory",
        "max_items_cap": cap,
        "built_at": time.time(),
    }
    _patch_meta_extra(staging, extra)

    if write_docs:
        docs = {lab: txt for lab, txt in items}
        (staging / "docs.json").write_text(
            json.dumps(docs, ensure_ascii=False), encoding="utf-8"
        )

    if not index_ready(staging):
        raise RuntimeError(f"staging failed index_ready: {staging}")

    loaded = HnswIndex.load(staging)
    summary["backend"] = getattr(loaded, "backend", "?")
    if hasattr(loaded, "_brute") and loaded._brute is not None:
        summary["backend"] = "brute"
    elif hasattr(loaded, "_index") and loaded._index is not None:
        summary["backend"] = "hnswlib"
    summary["count"] = int(loaded.count)
    if loaded.count != len(labels):
        raise RuntimeError(f"load count {loaded.count} != {len(labels)}")

    size_b = _dir_size_bytes(staging)
    summary["staging_bytes"] = size_b
    if size_b > _SOFT_INDEX_BYTES:
        summary["status"] = "over_soft_size"
        summary["note"] = f"staging {size_b} > soft {_SOFT_INDEX_BYTES}; not switching"
        return summary

    bak = backup_live_index(live)
    summary["backup"] = str(bak) if bak else None
    switch_staging_to_live(staging, live)

    if not index_ready(live):
        raise RuntimeError(f"live not ready after switch: {live}")
    live_loaded = HnswIndex.load(live)
    summary["live_count"] = int(live_loaded.count)
    summary["status"] = "ok"
    return summary


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Build production G4W vector index")
    ap.add_argument("--data-dir", type=str, default=None)
    ap.add_argument("--index-dir", type=str, default=None)
    ap.add_argument("--max-items", type=int, default=None)
    ap.add_argument("--dim", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-docs", action="store_true")
    args = ap.parse_args(list(argv) if argv is not None else None)

    if not (os.environ.get("EMBEDDING_PROVIDER") or "").strip():
        os.environ.setdefault("EMBEDDING_PROVIDER", "hash")

    try:
        summary = build_index(
            data_dir=Path(args.data_dir) if args.data_dir else None,
            index_dir=Path(args.index_dir) if args.index_dir else None,
            max_items=args.max_items,
            dim=args.dim,
            dry_run=args.dry_run,
            write_docs=not args.no_docs,
        )
    except Exception as exc:
        print(
            json.dumps(
                {"status": "error", "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary.get("status") in ("ok", "dry_run", "empty_corpus") else 2


if __name__ == "__main__":
    sys.exit(main())
