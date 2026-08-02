"""Feature flag for vector retrieval capability.

G4W_VECTOR_RETRIEVAL defaults ON (S7). Explicit 0/false/off disables.
Mirrors hybrid_reader.hybrid_main_read_enabled style (env or package-local .env).
"""
from __future__ import annotations

import os
from pathlib import Path


def _read_env_file_value(name: str) -> str:
    """Read a single key from package-local .env (G4W does not dump .env into os.environ)."""
    try:
        # G4W/memory/vector/flags.py -> G4W-main/.env
        env_path = Path(__file__).resolve().parents[3] / ".env"
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


def vector_retrieval_enabled() -> bool:
    """True when unset (S7 default ON); False only for explicit off values.

    Explicit disable tokens: 0, false, off, no, disable, disabled (case-insensitive).
    Rollback: G4W_VECTOR_RETRIEVAL=0
    """
    raw = str(os.environ.get("G4W_VECTOR_RETRIEVAL", "") or "").strip()
    if not raw:
        raw = _read_env_file_value("G4W_VECTOR_RETRIEVAL")
    raw = raw.lower()
    if not raw:
        return True
    return raw not in ("0", "false", "off", "no", "disable", "disabled")
