"""Transcript / conversation **chunk** incremental upsert (not full rebuild).

P0 — original-text layer for Hybrid INDEX:

  - Split file text with ``build_prod_index._chunk_text`` / ``expand_items_with_chunks``
  - Stable item_id identical to full build: ``rel_posix`` (single chunk) or
    ``rel_posix#cN`` (multi-chunk)
  - HNSW / brute ``add(..., replace=True)`` + merge ``docs.json`` +
    ``tier_records.jsonl`` (transcript default **HOT**)

**Trigger points (documented; wiring is optional):**

  - **Primary**: day-level / batch maintenance or CLI calling
    ``upsert_transcript_file`` / ``upsert_transcript_chunks``
  - **Optional**: fail-soft hook after a conversation day file is appended
    (``maybe_upsert_after_transcript_append``) — exported but **not** auto-bound
    into conversation write path here (keep message path light)

Gates:

  - ``G4W_TRANSCRIPT_CHUNK_UPSERT`` (default ON; 0/false/off disables)
  - ``vector_retrieval_enabled()``
  - live index ``index_ready``
  - ``assert_prod_index_write_allowed`` before any disk write

Does **not** call full ``build_index``. Never hard-deletes. Fail-soft: returns
summary dict, does not raise into callers.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

_log = logging.getLogger(__name__)

# Env flag (mirrors G4W_L4_INDEX_UPSERT style)
_ENV_FLAG = "G4W_TRANSCRIPT_CHUNK_UPSERT"

PathLike = Union[str, Path]
# (item_id, text) after expansion; or input (rel_posix, full_text) before expansion
FileItem = Tuple[str, str]


def transcript_chunk_upsert_enabled() -> bool:
    """Default ON; explicit disable tokens turn off.

    Rollback: ``G4W_TRANSCRIPT_CHUNK_UPSERT=0``
    """
    raw = str(os.environ.get(_ENV_FLAG, "") or "").strip().lower()
    if not raw:
        try:
            env_path = Path(__file__).resolve().parents[3] / ".env"
            if env_path.is_file():
                for line in env_path.read_text(encoding="utf-8-sig").splitlines():
                    s = line.strip()
                    if not s or s.startswith("#") or "=" not in s:
                        continue
                    k, v = s.split("=", 1)
                    if k.strip() == _ENV_FLAG:
                        raw = v.strip().strip("'\"").lower()
                        break
        except Exception:
            pass
    if not raw:
        return True
    return raw not in ("0", "false", "off", "no", "disable", "disabled")


def _to_rel_posix(path: PathLike, *, data_root: Optional[Path] = None) -> str:
    """Normalize to forward-slash relative id (matches full-build item ids)."""
    p = Path(path)
    s = str(path).replace("\\", "/")
    # already looks relative with forward slashes and no drive
    if not p.is_absolute() and ":" not in s[:3]:
        return s.lstrip("./")
    try:
        if data_root is not None:
            rel = p.resolve().relative_to(Path(data_root).resolve())
            return str(rel).replace("\\", "/")
    except Exception:
        pass
    # fallback: keep tail that looks like memory-relative (conversations/...)
    parts = s.replace("\\", "/").split("/")
    for i, part in enumerate(parts):
        if part in ("memory", "conversations", "transcripts", "user_only", "history"):
            # if 'memory' include from next; else from this part
            if part == "memory" and i + 1 < len(parts):
                return "/".join(parts[i + 1 :])
            return "/".join(parts[i:])
    return p.name


def files_to_chunk_items(
    files: Sequence[FileItem],
    *,
    max_items: int = 4000,
) -> List[FileItem]:
    """Expand (rel, full_text) → (item_id, chunk) via full-build chunk rules."""
    from .build_prod_index import expand_items_with_chunks

    cleaned: List[FileItem] = []
    for rel, text in files:
        rid = str(rel or "").replace("\\", "/").lstrip("./")
        if not rid:
            continue
        cleaned.append((rid, str(text or "")))
    if not cleaned:
        return []
    return expand_items_with_chunks(cleaned, max_items=max_items)


def upsert_transcript_chunks(
    items: Sequence[FileItem],
    *,
    index_dir: Optional[Path] = None,
    dry_run: bool = False,
    max_items: int = 4000,
    already_chunked: bool = False,
    run_id: str = "",
    source: str = "transcript_chunk",
    default_tier: str = "HOT",
) -> Dict[str, Any]:
    """Upsert transcript chunk docs into live production index (incremental).

    Parameters
    ----------
    items:
        Sequence of ``(rel_posix, text)``. By default each *file* text is
        re-chunked so ids match ``expand_items_with_chunks``. Pass
        ``already_chunked=True`` if items are already ``(item_id, chunk_text)``.
    index_dir:
        Injectable live INDEX root (tests). Default: prod write root if ready,
        else ``resolve_vector_index_dir()``.
    dry_run:
        Build candidate list only; no embed / no write.
    default_tier:
        Sidecar tier for new/updated transcript rows (default **HOT** — fresh
        original-text layer; L4 insights stay WARM in P1).

    Returns summary dict; **never raises**.
    """
    summary: Dict[str, Any] = {
        "status": "skipped",
        "upserted": 0,
        "dry_run": dry_run,
        "run_id": run_id,
        "source": source,
        "default_tier": default_tier,
    }
    try:
        if not transcript_chunk_upsert_enabled():
            summary["reason"] = f"{_ENV_FLAG} disabled"
            return summary

        from .flags import vector_retrieval_enabled
        from .sandbox_paths import (
            assert_prod_index_write_allowed,
            prod_index_write_root,
            resolve_vector_index_dir,
        )
        from .build_prod_index import index_ready
        from .hnsw_index import HnswIndex
        from .embedding import embed_batch, resolve_dim
        from .l4_index_upsert import _append_tier_records, _load_docs, _save_docs

        if not vector_retrieval_enabled():
            summary["reason"] = "vector_retrieval disabled"
            return summary

        live = Path(index_dir) if index_dir is not None else Path(resolve_vector_index_dir())
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

        if already_chunked:
            docs_items: List[FileItem] = []
            for iid, text in items:
                iid_s = str(iid or "").replace("\\", "/").lstrip("./")
                t = str(text or "").strip()
                if iid_s and t:
                    docs_items.append((iid_s, t))
                if len(docs_items) >= max(1, int(max_items)):
                    break
        else:
            docs_items = files_to_chunk_items(items, max_items=max_items)

        summary["candidates"] = len(docs_items)
        if not docs_items:
            summary["status"] = "empty"
            summary["reason"] = "no chunk items"
            return summary

        if dry_run:
            summary["status"] = "dry_run"
            summary["sample"] = [
                {"item_id": i, "text": t[:120]} for i, t in docs_items[:5]
            ]
            summary["item_ids"] = [i for i, _ in docs_items]
            return summary

        assert_prod_index_write_allowed(live)

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

        labels = [i for i, _ in docs_items]
        texts = [t for _, t in docs_items]
        vectors = embed_batch(texts, dim=dim, as_int8=False)

        idx.add(vectors, labels=labels, replace=True)
        idx.save(live)

        docs_path = live / "docs.json"
        docs = _load_docs(docs_path)
        for iid, text in docs_items:
            docs[iid] = text
        _save_docs(docs_path, docs)

        now = time.time()
        tier_rows = []
        for iid, text in docs_items:
            tier_rows.append(
                {
                    "item_id": iid,
                    "tier": default_tier,
                    "created_at": now,
                    "last_access_at": now,
                    "size_bytes": len(text.encode("utf-8", errors="replace")),
                    "source": source,
                    "run_id": run_id,
                }
            )
        _append_tier_records(live / "tier_records.jsonl", tier_rows)

        try:
            meta_path = live / "meta.json"
            if meta_path.is_file():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                meta["last_transcript_upsert_at"] = now
                meta["last_transcript_upsert_run_id"] = run_id
                meta["last_transcript_upsert_count"] = len(docs_items)
                try:
                    meta["count"] = int(HnswIndex.load(live).count)
                except Exception:
                    pass
                meta_path.write_text(
                    json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
                )
        except Exception as exc:
            _log.warning("transcript upsert meta patch failed: %s", exc)

        summary["status"] = "ok"
        summary["upserted"] = len(docs_items)
        summary["dim"] = dim
        return summary
    except Exception as exc:
        _log.warning("transcript_chunk_upsert failed: %s", exc, exc_info=True)
        summary["status"] = "error"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        return summary


def upsert_transcript_file(
    path: PathLike,
    text: Optional[str] = None,
    *,
    rel: Optional[str] = None,
    data_root: Optional[Path] = None,
    index_dir: Optional[Path] = None,
    dry_run: bool = False,
    max_items: int = 4000,
    run_id: str = "",
    encoding: str = "utf-8",
) -> Dict[str, Any]:
    """Upsert one transcript/conversation file as chunked items.

    - ``path``: filesystem path to read when ``text`` is None, or used to derive
      relative id when ``rel`` is not given.
    - ``text``: optional in-memory content (skip disk read).
    - ``rel``: explicit item-id prefix (posix); else derived via ``_to_rel_posix``.
    """
    summary: Dict[str, Any] = {
        "status": "skipped",
        "upserted": 0,
        "path": str(path),
    }
    try:
        if not transcript_chunk_upsert_enabled():
            summary["reason"] = f"{_ENV_FLAG} disabled"
            return summary

        p = Path(path)
        body = text
        if body is None:
            if not p.is_file():
                summary["status"] = "error"
                summary["error"] = f"file not found: {p}"
                return summary
            try:
                body = p.read_text(encoding=encoding, errors="replace")
            except Exception as exc:
                summary["status"] = "error"
                summary["error"] = f"read failed: {exc}"
                return summary

        rel_id = (rel or "").replace("\\", "/").lstrip("./")
        if not rel_id:
            rel_id = _to_rel_posix(p if p.is_absolute() or p.exists() else path, data_root=data_root)

        r = upsert_transcript_chunks(
            [(rel_id, body)],
            index_dir=index_dir,
            dry_run=dry_run,
            max_items=max_items,
            already_chunked=False,
            run_id=run_id or f"file:{rel_id}",
            source="transcript_file",
        )
        r["path"] = str(path)
        r["rel"] = rel_id
        return r
    except Exception as exc:
        _log.warning("upsert_transcript_file failed: %s", exc, exc_info=True)
        summary["status"] = "error"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        return summary


def maybe_upsert_after_transcript_append(
    path: PathLike,
    text: Optional[str] = None,
    *,
    rel: Optional[str] = None,
    index_dir: Optional[Path] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Optional fail-soft hook after a day transcript is appended.

    Intentionally **not** wired into conversation writers in this module —
    call from maintenance or an explicit integration point. Always swallows
    exceptions into the summary dict.
    """
    try:
        if not transcript_chunk_upsert_enabled():
            return {"status": "skipped", "reason": f"{_ENV_FLAG} disabled"}
        return upsert_transcript_file(
            path,
            text=text,
            rel=rel,
            index_dir=index_dir,
            dry_run=dry_run,
            run_id="hook:transcript_append",
        )
    except Exception as exc:
        _log.warning("maybe_upsert_after_transcript_append: %s", exc, exc_info=True)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


def transcript_files_since(
    transcripts_root: PathLike,
    watermark: float,
) -> List[Path]:
    """Transcript files (``**/*.md``) with mtime strictly after ``watermark``.

    Sorted ascending by mtime for deterministic item ids. Never raises;
    returns [] on any failure.
    """
    try:
        root = Path(transcripts_root)
        if not root.is_dir():
            return []
        files = []
        for p in root.rglob("*.md"):
            try:
                if p.is_file() and p.stat().st_mtime > watermark:
                    files.append(p)
            except Exception:
                continue
        files.sort(key=lambda p: p.stat().st_mtime)
        return files
    except Exception:
        _log.warning("transcript_files_since failed", exc_info=True)
        return []


def read_transcript_watermark(index_dir: Optional[Path] = None) -> float:
    """``last_transcript_upsert_at`` from index meta.json (0.0 when absent)."""
    try:
        from .sandbox_paths import resolve_vector_index_dir

        live = Path(index_dir) if index_dir is not None else Path(resolve_vector_index_dir())
        meta_path = live / "meta.json"
        if meta_path.is_file():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            try:
                return float(meta.get("last_transcript_upsert_at") or 0.0)
            except (TypeError, ValueError):
                return 0.0
    except Exception:
        pass
    return 0.0


def upsert_transcript_window(
    *,
    transcripts_root: PathLike,
    index_dir: Optional[Path] = None,
    dry_run: bool = False,
    max_items: int = 4000,
    run_id: str = "",
) -> Dict[str, Any]:
    """Upsert transcript files newer than the index watermark.

    Task-3 of the L4 finalize chain: advances the transcript hard-evidence
    layer from ``last_transcript_upsert_at`` to the latest transcript file, so
    the vector index no longer lags behind chat history.

    - Gates: product ``vector_enabled()`` (off → skipped), retrieval flag,
      ``G4W_TRANSCRIPT_CHUNK_UPSERT``, index ready.
    - Files are processed one by one (oldest first); each file is chunked with
      full-build rules so ids stay deterministic and re-runs are idempotent.
    - Returns summary dict; **never raises**.
    """
    summary: Dict[str, Any] = {
        "status": "skipped",
        "upserted": 0,
        "window_files": 0,
        "dry_run": dry_run,
    }
    try:
        if not transcript_chunk_upsert_enabled():
            summary["reason"] = f"{_ENV_FLAG} disabled"
            return summary

        # Product total gate: /vector off → no embed / no write (mirrors
        # upsert_l4_insights_to_index). Hybrid (keyword) stays the default path.
        try:
            from .vector_config import vector_enabled as _addon_vector_enabled

            if not _addon_vector_enabled():
                summary["reason"] = "vector_addon disabled"
                return summary
        except Exception as exc:
            summary["reason"] = "vector_config_unavailable"
            summary["detail"] = f"{type(exc).__name__}: {exc}"
            return summary

        watermark = read_transcript_watermark(index_dir)
        summary["watermark"] = watermark
        files = transcript_files_since(transcripts_root, watermark)
        summary["window_files"] = len(files)
        if not files:
            summary["status"] = "empty"
            summary["reason"] = "no transcripts newer than watermark"
            return summary

        total = 0
        per_file: List[Dict[str, Any]] = []
        errors: List[str] = []
        for p in files:
            r = upsert_transcript_file(
                p,
                # NOTE: do NOT pass data_root=transcripts_root here — item ids
                # must be the full memory-relative form
                # (conversations/<sender>/transcripts/yyyy/mm/yyyy-mm-dd.md)
                # so retrieval filters (is_transcript / sender ownership) and
                # file_read verification work. _to_rel_posix falls back to the
                # "memory" segment marker automatically.
                index_dir=index_dir,
                dry_run=dry_run,
                max_items=max_items,
                run_id=run_id or "window:transcript",
            )
            per_file.append(
                {
                    "path": str(p),
                    "status": r.get("status"),
                    "upserted": int(r.get("upserted") or 0),
                    "reason": r.get("reason") or r.get("error") or "",
                }
            )
            total += int(r.get("upserted") or 0)
            if r.get("status") == "error":
                errors.append(str(p))
                summary["embedding_reason"] = r.get("embedding_reason") or (
                    "remote_http_failed" if "remote" in str(r.get("error") or "") else "error"
                )
        summary["upserted"] = total
        summary["files"] = per_file
        summary["status"] = "done" if not errors else "partial"
        if errors:
            summary["error_files"] = errors
            summary["error"] = f"{len(errors)} file(s) failed: {errors[0]}"
        return summary
    except Exception as exc:
        _log.warning("upsert_transcript_window failed: %s", exc, exc_info=True)
        summary["status"] = "error"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        return summary
