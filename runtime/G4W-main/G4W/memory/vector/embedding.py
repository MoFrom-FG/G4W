"""Embedding facade: deterministic hash (default) + optional remote provider.

Main path: embed_text / embed_batch → float32 unit vectors of length ``dim``.
Remote path (ARK / OpenAI-compatible local embed) uses ``requests`` only.

**No silent hash充数 (P0 / SOP):** when provider is remote (not ``hash``),
HTTP / readiness failures raise :class:`EmbeddingError` instead of falling back
to ``hash_embed``. Explicit opt-in: ``EMBEDDING_ALLOW_HASH_FALLBACK=1``.

``hash_embed`` is retained for tests, dry-run, and explicit ``EMBEDDING_PROVIDER=hash``.
int8 helpers remain optional; HybridQueryEngine does not enable them by default.
"""
from __future__ import annotations

import hashlib
import logging
import os
import struct
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np

DEFAULT_DIM = 384


class EmbeddingError(RuntimeError):
    """Visible embedding failure (no silent hash fill-in).

    ``reason`` is a short machine-readable token for upsert/build summaries.
    """

    def __init__(self, message: str, *, reason: str = "embed_failed") -> None:
        super().__init__(message)
        self.reason = reason

# OpenAI-compatible embeddings base URLs (env can override via EMBEDDING_BASE_URL).
_DEFAULT_ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"
_DEFAULT_OPENAI_BASE = "https://api.openai.com/v1"
_DEFAULT_TIMEOUT_S = 30.0
_DEFAULT_BATCH = 16

_log = logging.getLogger(__name__)
_missing_model_logged = False


def _tokens(text: str) -> List[str]:
    # lightweight split aligned with hybrid tokenize spirit
    import re

    return [t.lower() for t in re.findall(r"[\w\u4e00-\u9fff]+", text or "")]


def hash_embed(text: str, dim: int = DEFAULT_DIM) -> np.ndarray:
    """Map text → unit float32 vector of length dim via token hash accumulation."""
    vec = np.zeros(dim, dtype=np.float32)
    toks = _tokens(text)
    if not toks:
        # empty → fixed non-zero so cosine is defined
        vec[0] = 1.0
        return vec
    for t in toks:
        h = hashlib.sha256(t.encode("utf-8")).digest()
        # use 8-byte chunks as seeds into indices
        for i in range(0, min(len(h), 32), 4):
            idx = struct.unpack_from("<I", h, i)[0] % dim
            sign = 1.0 if (h[i] & 1) == 0 else -1.0
            vec[idx] += sign
    n = float(np.linalg.norm(vec))
    if n < 1e-12:
        vec[0] = 1.0
        return vec
    vec /= n
    return vec


def quantize_int8(vec: np.ndarray) -> np.ndarray:
    """Symmetric int8 quantize of a unit-ish float vector (scale = max abs)."""
    v = np.asarray(vec, dtype=np.float32).reshape(-1)
    scale = float(np.max(np.abs(v))) or 1.0
    q = np.clip(np.round(v / scale * 127.0), -127, 127).astype(np.int8)
    return q


def dequantize_int8(q: np.ndarray, scale: float = 1.0) -> np.ndarray:
    """Inverse of quantize_int8; scale should match original max-abs (default 1 for unit)."""
    return (np.asarray(q, dtype=np.float32) / 127.0) * float(scale)


VectorLike = Union[np.ndarray, Sequence[float]]


def as_float32_unit(vec: VectorLike, dim: int = DEFAULT_DIM) -> np.ndarray:
    """Normalize arbitrary vector-like to float32 length dim (pad/truncate)."""
    v = np.asarray(vec, dtype=np.float32).reshape(-1)
    if v.size < dim:
        out = np.zeros(dim, dtype=np.float32)
        out[: v.size] = v
        v = out
    elif v.size > dim:
        v = v[:dim].copy()
    n = float(np.linalg.norm(v))
    if n < 1e-12:
        v = v.copy()
        v[0] = 1.0
        return v
    return v / n


def _vector_cfg_active() -> Optional[Dict[str, Any]]:
    """Return vector_config payload when product gate is on; else None.

    Used so addon base_url/model/dim from vector_config override EMBEDDING_*
    env defaults (which often point at LM Studio). Failures stay soft.
    """
    try:
        from G4W.memory.vector.vector_config import load_config, vector_enabled

        if not vector_enabled():
            return None
        cfg = load_config(use_cache=True)
        return cfg if isinstance(cfg, dict) else None
    except Exception:
        return None


def resolve_dim(dim: Optional[int] = None) -> int:
    """Parameter dim wins; else vector_config (when addon on); else EMBEDDING_DIM; else DEFAULT_DIM."""
    if dim is not None:
        return int(dim)
    cfg = _vector_cfg_active()
    if cfg is not None:
        try:
            d = int(cfg.get("dim") or 0)
            if d > 0:
                return d
        except (TypeError, ValueError):
            pass
    raw = (os.environ.get("EMBEDDING_DIM") or "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return DEFAULT_DIM


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def resolve_provider() -> str:
    """Return ``ark`` | ``openai`` | ``hash``.

    Explicit ``EMBEDDING_PROVIDER`` wins. Otherwise: openai-compatible when
    vector addon is enabled (ST thin HTTP), else ark/openai keys, else hash.
    """
    explicit = _env("EMBEDDING_PROVIDER").lower()
    if explicit in ("ark", "openai", "hash"):
        return explicit
    cfg = _vector_cfg_active()
    if cfg is not None and str(cfg.get("base_url") or "").strip():
        # Local ST embed server is OpenAI-compatible /v1/embeddings
        return "openai"
    if _env("ARK_API_KEY"):
        return "ark"
    if _env("EMBEDDING_API_KEY") or _env("OPENAI_API_KEY"):
        return "openai"
    return "hash"


def _resolve_api_key(provider: str) -> str:
    """Key by name only; never log the value."""
    k = _env("EMBEDDING_API_KEY")
    if k:
        return k
    if provider == "ark":
        return _env("ARK_API_KEY")
    if provider == "openai":
        return _env("OPENAI_API_KEY")
    return ""


def _resolve_base_url(provider: str) -> str:
    # Product gate on → prefer addon base_url from vector_config over EMBEDDING_*.
    cfg = _vector_cfg_active()
    if cfg is not None:
        vb = str(cfg.get("base_url") or "").strip()
        if vb:
            return vb.rstrip("/")
    base = _env("EMBEDDING_BASE_URL")
    if base:
        return base.rstrip("/")
    if provider == "ark":
        return _DEFAULT_ARK_BASE
    if provider == "openai":
        return _DEFAULT_OPENAI_BASE
    return ""


def _resolve_model() -> str:
    cfg = _vector_cfg_active()
    if cfg is not None:
        m = str(cfg.get("model") or "").strip()
        if m:
            return m
    return _env("EMBEDDING_MODEL")


def _timeout_s() -> float:
    raw = _env("EMBEDDING_TIMEOUT_S")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return _DEFAULT_TIMEOUT_S


def _batch_size() -> int:
    raw = _env("EMBEDDING_BATCH_SIZE")
    if raw:
        try:
            n = int(raw)
            if n > 0:
                return n
        except ValueError:
            pass
    return _DEFAULT_BATCH


def _is_local_embed_base(base: str) -> bool:
    """True for loopback OpenAI-compatible embed servers (ST thin HTTP)."""
    b = (base or "").lower()
    return "127.0.0.1" in b or "localhost" in b or "0.0.0.0" in b


# compat alias
_is_local_tei_base = _is_local_embed_base


def _allow_hash_fallback() -> bool:
    """Explicit opt-in only; default off so remote failures stay visible."""
    raw = (_env("EMBEDDING_ALLOW_HASH_FALLBACK") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _fail_or_hash(
    text: str,
    *,
    dim: int,
    provider: str,
    reason: str,
    detail: str = "",
) -> np.ndarray:
    """Hash only for provider=hash or EMBEDDING_ALLOW_HASH_FALLBACK; else raise."""
    if provider == "hash" or _allow_hash_fallback():
        if provider != "hash":
            _log.warning(
                "embedding: hash fallback allowed (%s): %s",
                reason,
                detail or reason,
            )
        return hash_embed(text, dim=dim)
    msg = detail or f"embedding failed: {reason} (provider={provider})"
    _log.error("embedding: %s", msg)
    raise EmbeddingError(msg, reason=reason)


def _remote_ready(provider: str) -> Tuple[bool, str, str, str]:
    """(ok, base, model, key). Missing model or key → not ready (no network).

    Local embed addon / localhost base allows empty API key — ST thin HTTP
    often ignores Authorization. Commercial ark/openai still require a key.
    """
    global _missing_model_logged
    if provider == "hash":
        return False, "", "", ""
    model = _resolve_model()
    key = _resolve_api_key(provider)
    base = _resolve_base_url(provider)
    if not model:
        if not _missing_model_logged:
            _log.debug(
                "embedding: EMBEDDING_MODEL unset; provider=%s (no silent hash)",
                provider,
            )
            _missing_model_logged = True
        return False, "", "", ""
    if not base:
        return False, "", "", ""
    # Local OpenAI-compatible embed: key optional
    if not key:
        if _is_local_embed_base(base) or _vector_cfg_active() is not None:
            key = "not-needed"
        else:
            return False, "", "", ""
    return True, base, model, key


def _http_embeddings(
    texts: Sequence[str],
    *,
    base: str,
    model: str,
    api_key: str,
    dim: int,
    timeout: float,
) -> Optional[np.ndarray]:
    """POST OpenAI-compatible /embeddings. On any failure return None (caller decides)."""
    if not texts:
        return np.zeros((0, dim), dtype=np.float32)
    try:
        import requests
    except ImportError:
        _log.debug("embedding: requests unavailable")
        return None

    url = f"{base.rstrip('/')}/embeddings"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    out_rows: List[np.ndarray] = [None] * len(texts)  # type: ignore[list-item]
    bs = _batch_size()
    try:
        for start in range(0, len(texts), bs):
            chunk = list(texts[start : start + bs])
            body = {"model": model, "input": chunk}
            resp = requests.post(url, headers=headers, json=body, timeout=timeout)
            if resp.status_code >= 400:
                _log.debug(
                    "embedding: HTTP %s from provider",
                    resp.status_code,
                )
                return None
            data = resp.json()
            items = data.get("data") if isinstance(data, dict) else None
            if not isinstance(items, list) or len(items) != len(chunk):
                _log.debug("embedding: bad response shape")
                return None
            # sort by index if present
            try:
                items = sorted(
                    items,
                    key=lambda x: int(x.get("index", 0)) if isinstance(x, dict) else 0,
                )
            except Exception:
                pass
            for j, item in enumerate(items):
                if not isinstance(item, dict):
                    return None
                emb = item.get("embedding")
                if emb is None:
                    return None
                vec = np.asarray(emb, dtype=np.float32).reshape(-1)
                if vec.size != dim:
                    _log.debug(
                        "embedding: dim mismatch got=%s want=%s",
                        vec.size,
                        dim,
                    )
                    return None
                out_rows[start + j] = as_float32_unit(vec, dim=dim)
    except Exception as exc:
        _log.debug("embedding: request error %s", type(exc).__name__)
        return None

    if any(r is None for r in out_rows):
        return None
    try:
        from G4W.memory.vector.embed_lifecycle import mark_embed_used

        mark_embed_used()
    except Exception:
        pass
    return np.stack(out_rows, axis=0)


def embed_text(text: str, dim: Optional[int] = None) -> np.ndarray:
    """Embed one string → unit float32 vector.

    Remote failures raise :class:`EmbeddingError` unless
    ``EMBEDDING_PROVIDER=hash`` or ``EMBEDDING_ALLOW_HASH_FALLBACK=1``.
    """
    d = resolve_dim(dim)
    provider = resolve_provider()
    if provider == "hash":
        return hash_embed(text if text is not None else "", dim=d)
    ok, base, model, key = _remote_ready(provider)
    if not ok:
        return _fail_or_hash(
            text if text is not None else "",
            dim=d,
            provider=provider,
            reason="remote_not_ready",
            detail=f"remote embedding not ready (provider={provider})",
        )
    mat = _http_embeddings(
        [text if text is not None else ""],
        base=base,
        model=model,
        api_key=key,
        dim=d,
        timeout=_timeout_s(),
    )
    if mat is None or mat.shape != (1, d):
        return _fail_or_hash(
            text if text is not None else "",
            dim=d,
            provider=provider,
            reason="remote_http_failed",
            detail=f"remote embedding HTTP/shape failed (provider={provider})",
        )
    return mat[0]


def embed_batch(
    texts: Sequence[str], dim: Optional[int] = None, as_int8: bool = False
) -> np.ndarray:
    """Return (n, dim) float32 or int8 matrix. Same fail-visible policy as embed_text."""
    d = resolve_dim(dim)
    seq = list(texts) if texts is not None else []
    if not seq:
        mat = np.zeros((0, d), dtype=np.float32)
    else:
        provider = resolve_provider()
        if provider == "hash":
            rows = [hash_embed(t if t is not None else "", dim=d) for t in seq]
            mat = np.stack(rows, axis=0)
        else:
            ok, base, model, key = _remote_ready(provider)
            mat = None
            if ok:
                mat = _http_embeddings(
                    [t if t is not None else "" for t in seq],
                    base=base,
                    model=model,
                    api_key=key,
                    dim=d,
                    timeout=_timeout_s(),
                )
            if mat is None or mat.shape != (len(seq), d):
                reason = "remote_not_ready" if not ok else "remote_http_failed"
                if _allow_hash_fallback():
                    _log.warning(
                        "embedding: batch hash fallback allowed (%s)", reason
                    )
                    rows = [
                        hash_embed(t if t is not None else "", dim=d) for t in seq
                    ]
                    mat = np.stack(rows, axis=0)
                else:
                    raise EmbeddingError(
                        f"remote embed_batch failed ({reason}, provider={provider})",
                        reason=reason,
                    )
    if as_int8:
        return np.stack([quantize_int8(r) for r in mat], axis=0)
    return mat
