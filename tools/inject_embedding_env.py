#!/usr/bin/env python3
"""Emit set KEY=VALUE lines for embedding/vector keys from package-local .env.

Used by start_G4W_ga.bat so embedding.py (os.environ only) sees config.
"""
from __future__ import annotations

import sys
from pathlib import Path

PORTABLE_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PORTABLE_ROOT / "runtime" / "G4W-main" / ".env"

KEYS = (
    "EMBEDDING_PROVIDER",
    "EMBEDDING_BASE_URL",
    "EMBEDDING_MODEL",
    "EMBEDDING_API_KEY",
    "EMBEDDING_DIM",
    "EMBEDDING_TIMEOUT_S",
    "EMBEDDING_BATCH_SIZE",
    "G4W_VECTOR_RETRIEVAL",
    "G4W_VECTOR_INDEX_DIR",
)


def main() -> int:
    if not ENV_FILE.is_file():
        return 0
    # minimal parse (no dotenv); ignore comments/blank
    data: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip()
        if k in KEYS:
            data[k] = v.strip().strip('"').strip("'")
    # Windows cmd: write SET lines; empty values still set if present
    for k in KEYS:
        if k not in data:
            continue
        v = data[k]
        # escape for cmd set "K=V" — strip CR and reject raw newlines
        v = v.replace("\r", "").replace("\n", "")
        print(f'set "{k}={v}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
