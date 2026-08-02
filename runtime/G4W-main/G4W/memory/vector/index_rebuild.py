"""Background full rebuild of prod vector index from L4 official sources (TASK-F).

Design constraints (SOP):
  - Source = L4 official (active_knowledge + emotion_events), not ad-hoc chat embed
  - Command must NOT block on full rebuild (async worker + status in meta)
  - Preserve tier_records.jsonl when possible; rebalance still available via HybridQueryEngine
  - Staging → backup live → switch (reuse build_prod_index helpers)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np

from .embedding import embed_batch, resolve_dim, _resolve_model, _resolve_base_url, resolve_provider
from .hnsw_index import create_index
from .index_meta import (
    current_embedding_fingerprint,
    load_index_meta,
    patch_meta_fields,
    read_meta_dict,
)
from .l4_index_upsert import insight_items_to_docs
from .sandbox_paths import (
    assert_prod_index_write_allowed,
    prod_index_staging_dir,
    prod_index_write_root,
    resolve_vector_index_dir,
)

_log = logging.getLogger(__name__)

# Process-local rebuild state (also mirrored into meta.json extras)
_lock = threading.Lock()
_state: Dict[str, Any] = {
    "status": "idle",  # idle | running | ok | error
    "started_at": None,
    "finished_at": None,
    "error": None,
    "summary": None,
    "thread_id": None,
}


def rebuild_state() -> Dict[str, Any]:
    with _lock:
        return dict(_state)


def _set_state(**kwargs: Any) -> None:
    with _lock:
        _state.update(kwargs)


def _default_memory_root() -> Path:
    """Best-effort memory data root (parent of history_insight)."""
    # Prefer env
    for key in ("G4W_MEMORY_ROOT", "G4W_DATA_DIR", "BBS_CWD"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            p = Path(raw).expanduser()
            if p.is_dir():
                return p.resolve()
    # Fall back beside DEFAULT prod index sibling DATA layout if present
    try:
        from .sandbox_paths import default_bbs_cwd

        cwd = default_bbs_cwd()
        if cwd and Path(cwd).is_dir():
            return Path(cwd).resolve()
    except Exception:
        pass
    return Path.cwd().resolve()


def _load_l4_official(
    memory_root: Path,
    user_id: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    try:
        from ..l4_safe import (
            active_knowledge_path,
            emotion_events_path,
            load_json,
        )
    except Exception:
        from G4W.memory.l4_safe import (  # type: ignore
            active_knowledge_path,
            emotion_events_path,
            load_json,
        )

    active = load_json(active_knowledge_path(memory_root, user_id), {})
    emotion = load_json(emotion_events_path(memory_root, user_id), {})
    return (
        active if isinstance(active, dict) else {},
        emotion if isinstance(emotion, dict) else {},
    )


def _copy_tier_records(src: Path, dst: Path) -> bool:
    """Copy tier_records.jsonl if present (keep tier policy continuity)."""
    for name in ("tier_records.jsonl", "tier_records.json"):
        p = src / name
        if p.is_file():
            try:
                dst.mkdir(parents=True, exist_ok=True)
                (dst / name).write_bytes(p.read_bytes())
                return True
            except OSError as exc:
                _log.warning("copy tier_records failed: %s", exc)
    return False


def _write_docs(dst: Path, docs: Dict[str, str]) -> None:
    (dst / "docs.json").write_text(
        json.dumps(docs, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def build_index_from_l4(
    *,
    memory_root: Optional[Path] = None,
    user_id: str = "default",
    index_dir: Optional[Path] = None,
    max_items: int = 4000,
    dim: Optional[int] = None,
    dry_run: bool = False,
    switch_live: bool = True,
) -> Dict[str, Any]:
    """Full rebuild from L4 official → staging → optional live switch.

    Returns summary dict (no secrets). Does not flip vector flags.
    """
    from .build_prod_index import (
        backup_live_index,
        index_ready,
        switch_staging_to_live,
        _dir_size_bytes,
    )

    root = Path(memory_root) if memory_root is not None else _default_memory_root()
    live = Path(index_dir) if index_dir is not None else prod_index_write_root()
    staging = Path(str(live) + ".staging") if index_dir is not None else prod_index_staging_dir()
    d = int(resolve_dim(dim))
    fp = current_embedding_fingerprint()
    provider = str(fp.get("provider") or "")
    model = str(fp.get("model") or "")
    base_url = str(fp.get("base_url") or "")

    summary: Dict[str, Any] = {
        "status": "started",
        "memory_root": str(root),
        "user_id": user_id,
        "live": str(live),
        "staging": str(staging),
        "dim": d,
        "model": model,
        "base_url": base_url,
        "provider": provider,
        "source": "L4_official",
        "max_items": max_items,
        "dry_run": bool(dry_run),
        "count": 0,
        "tier_records_copied": False,
    }

    active, emotion = _load_l4_official(root, user_id)
    pairs = insight_items_to_docs(
        active=active,
        emotion=emotion,
        user_id=user_id,
        run_id=f"rebuild-{int(time.time())}",
        max_items=max_items,
    )
    labels = [pid for pid, _txt, _meta in pairs]
    texts = [txt for _pid, txt, _meta in pairs]
    summary["source_items"] = len(pairs)

    if dry_run:
        summary["status"] = "dry_run"
        return summary

    assert_prod_index_write_allowed(live)
    assert_prod_index_write_allowed(staging)

    if staging.exists():
        import shutil

        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    if not texts:
        # empty but valid index
        idx = create_index(dim=d, max_elements=1024)
        # attach fingerprint
        try:
            idx.model = model
            idx.base_url = base_url
        except Exception:
            pass
        idx.save(staging)
        _write_docs(staging, {})
    else:
        vectors = embed_batch(texts)
        arr = np.asarray(vectors, dtype=np.float32)
        if arr.ndim != 2 or arr.shape[1] != d:
            # trust embedding output dim if resolve_dim lagged
            if arr.ndim == 2 and arr.shape[1] > 0:
                d = int(arr.shape[1])
                summary["dim"] = d
            else:
                raise RuntimeError(f"embed_batch bad shape: {getattr(arr, 'shape', None)}")
        idx = create_index(dim=d, max_elements=max(len(labels) + 1024, 10_000))
        try:
            idx.model = model
            idx.base_url = base_url
        except Exception:
            pass
        idx.add(arr, labels=labels)
        idx.save(staging)
        docs = {lab: txt for lab, txt in zip(labels, texts)}
        _write_docs(staging, docs)

    # Preserve tier_records from previous live if any
    if live.exists():
        summary["tier_records_copied"] = _copy_tier_records(live, staging)

    extra = {
        "provider": provider,
        "embed_model": model,
        "model": model,
        "base_url": base_url,
        "source": "L4_official",
        "rebuild_source": "L4_official",
        "max_items_cap": max_items,
        "built_at": time.time(),
        "rebuild_status": "staging_ready",
        "user_id": user_id,
    }
    patch_meta_fields(
        staging,
        {
            "model": model,
            "base_url": base_url,
            "dim": d,
            **{k: v for k, v in extra.items() if k not in ("model", "base_url")},
        },
    )

    if not index_ready(staging):
        raise RuntimeError(f"staging not ready after build: {staging}")

    summary["count"] = len(labels)
    summary["staging_bytes"] = _dir_size_bytes(staging)

    if switch_live:
        if live.exists():
            try:
                bak = backup_live_index(live)
                summary["backup"] = str(bak) if bak else None
            except Exception as exc:
                summary["backup_error"] = f"{type(exc).__name__}: {exc}"
        switch_staging_to_live(staging, live)
        patch_meta_fields(
            live,
            {
                "model": model,
                "base_url": base_url,
                "dim": d,
                "rebuild_status": "ok",
                "rebuild_finished_at": time.time(),
                "rebuild_source": "L4_official",
                "embed_model": model,
                "source": "L4_official",
            },
        )
        summary["status"] = "ok"
        summary["switched"] = True
    else:
        summary["status"] = "staging_only"
        summary["switched"] = False

    return summary


def _run_rebuild_job(kwargs: Dict[str, Any]) -> None:
    _set_state(status="running", error=None, summary=None)
    started = time.time()
    _set_state(started_at=started, finished_at=None)
    live = Path(kwargs.get("index_dir") or prod_index_write_root())
    try:
        # mark meta early so /vector status can see running
        try:
            if (live / "meta.json").is_file() or live.exists():
                patch_meta_fields(
                    live,
                    {
                        "rebuild_status": "running",
                        "rebuild_started_at": started,
                        "rebuild_error": None,
                    },
                )
        except Exception:
            pass
        summary = build_index_from_l4(**kwargs)
        _set_state(status=str(summary.get("status") or "ok"), summary=summary, finished_at=time.time())
        try:
            patch_meta_fields(
                Path(summary.get("live") or live),
                {
                    "rebuild_status": summary.get("status") or "ok",
                    "rebuild_finished_at": time.time(),
                    "rebuild_error": None,
                    "rebuild_source": "L4_official",
                },
            )
        except Exception:
            pass
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        _log.exception("index rebuild failed")
        _set_state(
            status="error",
            error=err,
            finished_at=time.time(),
            summary={"status": "error", "error": err, "trace": traceback.format_exc()[-2000:]},
        )
        try:
            if live.exists():
                patch_meta_fields(
                    live,
                    {
                        "rebuild_status": "error",
                        "rebuild_finished_at": time.time(),
                        "rebuild_error": err,
                    },
                )
        except Exception:
            pass
    finally:
        _set_state(thread_id=None)


def start_rebuild_async(
    *,
    memory_root: Optional[Path] = None,
    user_id: str = "default",
    index_dir: Optional[Path] = None,
    max_items: int = 4000,
    dim: Optional[int] = None,
) -> Dict[str, Any]:
    """Kick off background rebuild. Refuses if already running."""
    with _lock:
        if _state.get("status") == "running" and _state.get("thread_id"):
            return {
                "accepted": False,
                "reason": "already_running",
                "state": dict(_state),
            }
    kwargs = {
        "memory_root": memory_root,
        "user_id": user_id,
        "index_dir": index_dir,
        "max_items": max_items,
        "dim": dim,
        "dry_run": False,
        "switch_live": True,
    }
    t = threading.Thread(
        target=_run_rebuild_job,
        args=(kwargs,),
        name="G4W-index-rebuild",
        daemon=True,
    )
    _set_state(status="running", started_at=time.time(), finished_at=None, error=None, summary=None)
    t.start()
    _set_state(thread_id=t.ident)
    return {"accepted": True, "state": rebuild_state()}


def format_rebuild_status() -> str:
    st = rebuild_state()
    lines = [
        "🛠 索引重建状态",
        f"status：{st.get('status')}",
        f"started_at：{st.get('started_at')}",
        f"finished_at：{st.get('finished_at')}",
        f"error：{st.get('error') or '-'}",
    ]
    summary = st.get("summary") or {}
    if isinstance(summary, dict) and summary:
        for k in ("count", "source_items", "dim", "model", "live", "memory_root", "tier_records_copied"):
            if k in summary:
                lines.append(f"{k}：{summary[k]}")
    # also surface meta rebuild_* if process state idle
    try:
        meta = read_meta_dict(resolve_vector_index_dir())
        if meta:
            for k in ("rebuild_status", "rebuild_error", "rebuild_source", "rebuild_finished_at"):
                if k in meta:
                    lines.append(f"meta.{k}：{meta[k]}")
    except Exception:
        pass
    return "\n".join(lines)
