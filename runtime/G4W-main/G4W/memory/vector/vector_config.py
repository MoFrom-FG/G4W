"""Vector addon product gate: config file + installed∧enabled.

Authority (resolve order):
  1. ``<runtime>/G4W-embedding/vector_config.json``  (preferred)
  2. ``<runtime>/G4W-data/vector_config.json``       (fallback)

``vector_enabled()`` is True **only** when both ``installed`` and ``enabled``
are true. Missing file / uninstalled / enabled=false → False (default pure-text
no-op; no embed service, no HNSW inject, no silent hash fill).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

_log = logging.getLogger(__name__)

_DEFAULT_MODEL = "Qwen3-Embedding-0.6B"
_DEFAULT_DIM = 1024  # Qwen3-Embedding-0.6B; status may note if uncertain
_DEFAULT_PORT = 8081
_DEFAULT_BASE = "http://127.0.0.1:8081"

_cache_lock = threading.Lock()
_cache_payload: Optional[Dict[str, Any]] = None
_cache_path: Optional[Path] = None
_cache_mtime: float = 0.0
_cache_at: float = 0.0
_CACHE_TTL_S = 2.0


def _runtime_root() -> Path:
    """G4W/memory/vector/this.py → parents[4] = runtime (…/runtime)."""
    return Path(__file__).resolve().parents[4]


def _primary_path() -> Path:
    return _runtime_root() / "G4W-embedding" / "vector_config.json"


def _fallback_path() -> Path:
    return _runtime_root() / "G4W-data" / "vector_config.json"


def config_path() -> Path:
    """Resolved config path: primary if exists or its parent is preferred writable.

    Prefer primary path always for writes when possible; for reads, primary if
    file exists else fallback if file exists else primary (canonical default).
    """
    primary = _primary_path()
    fallback = _fallback_path()
    if primary.is_file():
        return primary
    if fallback.is_file():
        return fallback
    # Neither exists → canonical primary (callers/save may create parent).
    return primary


def default_config() -> Dict[str, Any]:
    return {
        "enabled": False,
        "installed": False,
        "backend": "st",  # sentence-transformers thin HTTP (default); tei legacy only
        "model": _DEFAULT_MODEL,
        "dim": _DEFAULT_DIM,
        "base_url": _DEFAULT_BASE,
        "port": _DEFAULT_PORT,
        "pid": None,
        "embed_health": None,
        "tei_health": None,  # compat mirror of embed_health
        "updated_at": None,
    }


def _normalize(data: Dict[str, Any]) -> Dict[str, Any]:
    base = default_config()
    if not isinstance(data, dict):
        return base
    out = dict(base)
    for k in base.keys():
        if k in data:
            out[k] = data[k]
    # accept legacy-only tei_health → embed_health
    if out.get("embed_health") is None and data.get("tei_health") is not None:
        out["embed_health"] = data.get("tei_health")
    if out.get("tei_health") is None and out.get("embed_health") is not None:
        out["tei_health"] = out.get("embed_health")
    # booleans
    out["enabled"] = bool(out.get("enabled"))
    out["installed"] = bool(out.get("installed"))
    backend = str(out.get("backend") or "st").strip().lower()
    if backend not in ("st", "tei", "http"):
        backend = "st"
    out["backend"] = backend
    try:
        out["dim"] = int(out.get("dim") or _DEFAULT_DIM)
    except (TypeError, ValueError):
        out["dim"] = _DEFAULT_DIM
    try:
        out["port"] = int(out.get("port") or _DEFAULT_PORT)
    except (TypeError, ValueError):
        out["port"] = _DEFAULT_PORT
    if out.get("base_url"):
        out["base_url"] = str(out["base_url"]).rstrip("/")
    return out


def _read_file(path: Path) -> Dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8-sig")
        data = json.loads(raw)
        return _normalize(data if isinstance(data, dict) else {})
    except FileNotFoundError:
        return default_config()
    except Exception as exc:
        _log.debug("vector_config: read fail %s: %s", path, type(exc).__name__)
        return default_config()


def _invalidate_cache() -> None:
    global _cache_payload, _cache_path, _cache_mtime, _cache_at
    with _cache_lock:
        _cache_payload = None
        _cache_path = None
        _cache_mtime = 0.0
        _cache_at = 0.0


def load_config(*, use_cache: bool = True) -> Dict[str, Any]:
    """Hot-read config dict (optional ≤2s mtime cache)."""
    global _cache_payload, _cache_path, _cache_mtime, _cache_at
    path = config_path()
    now = time.monotonic()
    if use_cache:
        with _cache_lock:
            if (
                _cache_payload is not None
                and _cache_path == path
                and (now - _cache_at) <= _CACHE_TTL_S
            ):
                try:
                    mtime = path.stat().st_mtime if path.is_file() else -1.0
                except OSError:
                    mtime = -1.0
                if mtime == _cache_mtime:
                    return dict(_cache_payload)

    if path.is_file():
        cfg = _read_file(path)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
    else:
        # try the other candidate explicitly (config_path already did, but
        # primary-missing + fallback-missing → defaults)
        other = _fallback_path() if path == _primary_path() else _primary_path()
        if other.is_file():
            cfg = _read_file(other)
            path = other
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
        else:
            cfg = default_config()
            mtime = -1.0

    cfg = dict(cfg)
    cfg["_config_path"] = str(path)
    if use_cache:
        with _cache_lock:
            _cache_payload = dict(cfg)
            _cache_path = path
            _cache_mtime = mtime
            _cache_at = now
    return cfg


def save_config(patch: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Merge patch into current config, write atomically, return full config.

    Creates parent directory as needed. Writes to existing file path if any,
    else primary ``G4W-embedding/vector_config.json``.
    """
    path = config_path()
    # If only fallback exists, keep writing there; else prefer primary.
    if not path.is_file():
        path = _primary_path()

    current = load_config(use_cache=False)
    current.pop("_config_path", None)
    if patch:
        for k, v in patch.items():
            if k.startswith("_"):
                continue
            current[k] = v
    current = _normalize(current)
    current["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = {k: v for k, v in current.items() if not k.startswith("_")}
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))
    _invalidate_cache()
    out = dict(payload)
    out["_config_path"] = str(path)
    return out


def vector_enabled() -> bool:
    """True only when addon is installed **and** enabled.

    Env kill-switch: ``G4W_VECTOR_ADDON=0/false/off`` forces False.
    Env force (still requires installed in file unless
    ``G4W_VECTOR_ADDON_FORCE=1``): not used for product default.
    """
    raw = str(os.environ.get("G4W_VECTOR_ADDON", "") or "").strip().lower()
    if raw in ("0", "false", "off", "no", "disable", "disabled"):
        return False
    cfg = load_config()
    return bool(cfg.get("installed")) and bool(cfg.get("enabled"))


def set_vector_enabled(on: bool) -> Dict[str, Any]:
    """Persist enabled flag; effective immediately (cache invalidated).

    Does **not** start/stop embed service (TASK-C). Optionally dual-writes
    ``G4W_VECTOR_ADDON=1/0`` into package-local ``.env`` best-effort.
    """
    cfg = save_config({"enabled": bool(on)})
    # optional dual-write .env for operators (best-effort; does not replace file gate)
    try:
        env_path = Path(__file__).resolve().parents[3] / ".env"
        _upsert_env_key(env_path, "G4W_VECTOR_ADDON", "1" if on else "0")
    except Exception as exc:
        _log.debug("vector_config: .env dual-write skip: %s", type(exc).__name__)
    # process-local: clear kill-switch when enabling; set kill when disabling
    if on:
        cur = str(os.environ.get("G4W_VECTOR_ADDON", "") or "").strip().lower()
        if cur in ("0", "false", "off", "no", "disable", "disabled"):
            os.environ["G4W_VECTOR_ADDON"] = "1"
    else:
        os.environ["G4W_VECTOR_ADDON"] = "0"
    return cfg


def status_dict() -> Dict[str, Any]:
    """Snapshot for ``/vector status`` (TASK-B) — path always visible."""
    cfg = load_config()
    path = str(cfg.get("_config_path") or config_path())
    return {
        "enabled": bool(cfg.get("enabled")),
        "installed": bool(cfg.get("installed")),
        "vector_enabled": vector_enabled(),
        "model": cfg.get("model"),
        "dim": cfg.get("dim"),
        "base_url": cfg.get("base_url"),
        "port": cfg.get("port"),
        "pid": cfg.get("pid"),
        "tei_health": cfg.get("tei_health"),
        "updated_at": cfg.get("updated_at"),
        "config_path": path,
        "dim_note": "Qwen3-Embedding-0.6B nominal 1024",
    }


def _upsert_env_key(env_path: Path, key: str, value: str) -> None:
    lines: list[str] = []
    found = False
    if env_path.is_file():
        for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
            if raw.strip().startswith("#") or "=" not in raw:
                lines.append(raw)
                continue
            k, _ = raw.split("=", 1)
            if k.strip() == key:
                lines.append(f"{key}={value}")
                found = True
            else:
                lines.append(raw)
    if not found:
        lines.append(f"{key}={value}")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def reset_cache_for_tests() -> None:
    """Test helper: drop in-process cache."""
    _invalidate_cache()
