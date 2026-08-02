"""Index meta.json fingerprint helpers (TASK-F).

Records model + dim (+ base_url) on the live index and compares them to the
current embedding/runtime config so ``/vector status`` can prompt rebuild.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

from .embedding import _resolve_base_url, _resolve_model, resolve_dim, resolve_provider
from .hnsw_index import IndexMeta
from .sandbox_paths import resolve_vector_index_dir


def meta_path(index_dir: Union[str, Path, None] = None) -> Path:
    d = Path(index_dir) if index_dir is not None else resolve_vector_index_dir()
    return d / "meta.json"


def load_index_meta(index_dir: Union[str, Path, None] = None) -> Optional[IndexMeta]:
    """Load IndexMeta from disk; None if missing/unreadable."""
    path = meta_path(index_dir)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        return IndexMeta.from_dict(raw)
    except Exception:
        return None


def read_meta_dict(index_dir: Union[str, Path, None] = None) -> Optional[Dict[str, Any]]:
    path = meta_path(index_dir)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except Exception:
        return None


def current_embedding_fingerprint() -> Dict[str, Any]:
    """Runtime embedding identity used for mismatch checks (no secrets)."""
    provider = ""
    try:
        provider = str(resolve_provider() or "")
    except Exception:
        provider = ""
    model = ""
    try:
        model = str(_resolve_model() or "")
    except Exception:
        model = ""
    base_url = ""
    try:
        base_url = str(_resolve_base_url(provider or "local") or "")
    except Exception:
        base_url = ""
    dim = int(resolve_dim())
    return {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "dim": dim,
    }


def index_fingerprint(
    meta_or_dir: Union[IndexMeta, str, Path, None] = None,
) -> Dict[str, Any]:
    """Fingerprint from IndexMeta or index directory path."""
    meta: Optional[IndexMeta]
    if meta_or_dir is None:
        meta = None
    elif isinstance(meta_or_dir, IndexMeta):
        meta = meta_or_dir
    else:
        meta = load_index_meta(meta_or_dir)
    if meta is None:
        return {"model": "", "base_url": "", "dim": None, "count": 0, "backend": ""}
    # embed_model may live in extras from older build_prod_index patches
    model = str(meta.model or "")
    if not model and isinstance(meta.extras, dict):
        model = str(meta.extras.get("embed_model") or meta.extras.get("model") or "")
    base_url = str(meta.base_url or "")
    if not base_url and isinstance(meta.extras, dict):
        base_url = str(meta.extras.get("base_url") or "")
    return {
        "model": model,
        "base_url": base_url,
        "dim": int(meta.dim) if meta.dim is not None else None,
        "count": int(meta.count or 0),
        "backend": str(meta.backend or ""),
        "created_at": float(meta.created_at or 0.0),
        "extras": dict(meta.extras or {}),
    }


def fingerprint_mismatch(
    index_dir: Union[str, Path, None] = None,
    *,
    meta: Optional[IndexMeta] = None,
    current: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """Return (mismatch, reason). Missing meta → not a hard mismatch (no index)."""
    m = meta if meta is not None else load_index_meta(index_dir)
    if m is None:
        return False, "no_meta"
    cur = current if current is not None else current_embedding_fingerprint()
    idx = index_fingerprint(m)
    reasons = []
    cur_dim = cur.get("dim")
    idx_dim = idx.get("dim")
    if cur_dim is not None and idx_dim is not None and int(cur_dim) != int(idx_dim):
        reasons.append(f"dim index={idx_dim} runtime={cur_dim}")
    cur_model = str(cur.get("model") or "").strip()
    idx_model = str(idx.get("model") or "").strip()
    # Only flag model when both sides known (avoid false positive on empty index model)
    if cur_model and idx_model and cur_model != idx_model:
        reasons.append(f"model index={idx_model!r} runtime={cur_model!r}")
    if not reasons:
        return False, "ok"
    return True, "; ".join(reasons)


def format_mismatch_hint(index_dir: Union[str, Path, None] = None) -> str:
    bad, reason = fingerprint_mismatch(index_dir)
    if not bad:
        return ""
    return f"⚠️ 索引与当前 embedding 不一致（{reason}）→ 建议 /vector rebuild"


def patch_meta_fields(
    index_dir: Union[str, Path],
    fields: Dict[str, Any],
    *,
    merge_extras: bool = True,
) -> Path:
    """Shallow-update meta.json (top-level keys). Preserves unknown keys."""
    d = Path(index_dir)
    path = d / "meta.json"
    data: Dict[str, Any] = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw
        except Exception:
            data = {}
    for k, v in (fields or {}).items():
        if k == "extras" and merge_extras and isinstance(v, dict):
            prev = data.get("extras") if isinstance(data.get("extras"), dict) else {}
            merged = dict(prev)
            merged.update(v)
            data["extras"] = merged
        else:
            data[k] = v
    d.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_fingerprint_to_meta(
    index_dir: Union[str, Path],
    *,
    model: str = "",
    base_url: str = "",
    dim: Optional[int] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Path:
    """Ensure model/dim/base_url are recorded on meta (rebuild / save path)."""
    fp = current_embedding_fingerprint()
    fields: Dict[str, Any] = {
        "model": str(model or fp.get("model") or ""),
        "base_url": str(base_url or fp.get("base_url") or ""),
    }
    if dim is not None:
        fields["dim"] = int(dim)
    elif fp.get("dim") is not None:
        fields["dim"] = int(fp["dim"])
    if extra:
        fields.update(extra)
    return patch_meta_fields(index_dir, fields)


def meta_status_lines(index_dir: Union[str, Path, None] = None) -> list:
    """Human lines for /vector meta|status."""
    d = Path(index_dir) if index_dir is not None else resolve_vector_index_dir()
    lines = [f"index_dir：{d}"]
    meta = load_index_meta(d)
    cur = current_embedding_fingerprint()
    if meta is None:
        lines.append("meta：缺失")
        lines.append(f"runtime model：{cur.get('model') or '-'}")
        lines.append(f"runtime dim：{cur.get('dim')}")
        return lines
    fp = index_fingerprint(meta)
    lines.append(f"backend：{fp.get('backend') or '-'}")
    lines.append(f"count：{fp.get('count')}")
    lines.append(f"index model：{fp.get('model') or '-'}")
    lines.append(f"index dim：{fp.get('dim')}")
    lines.append(f"index base_url：{fp.get('base_url') or '-'}")
    lines.append(f"runtime model：{cur.get('model') or '-'}")
    lines.append(f"runtime dim：{cur.get('dim')}")
    lines.append(f"runtime base_url：{cur.get('base_url') or '-'}")
    bad, reason = fingerprint_mismatch(meta=meta, current=cur)
    if bad:
        lines.append(f"mismatch：是（{reason}）→ 建议 /vector rebuild")
    else:
        lines.append(f"mismatch：否（{reason}）")
    # rebuild extras if present
    ex = fp.get("extras") or {}
    for key in (
        "rebuild_status",
        "rebuild_started_at",
        "rebuild_finished_at",
        "rebuild_error",
        "rebuild_source",
        "last_l4_upsert_at",
    ):
        if key in ex:
            lines.append(f"{key}：{ex[key]}")
    return lines
