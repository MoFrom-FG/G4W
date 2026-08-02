"""Backward-compat shim: TEI lifecycle names → ST embed_lifecycle.

Windows product default is Sentence-Transformers thin HTTP
(``embed_lifecycle``). Old imports of ``ensure_tei_running`` / ``stop_tei`` /
``tei_health`` keep working via re-exports.

New code should import from ``G4W.memory.vector.embed_lifecycle``.
"""
from __future__ import annotations

from G4W.memory.vector.embed_lifecycle import (  # noqa: F401
    discover_embed_launchers,
    discover_tei_launchers,
    embed_health,
    embedding_root,
    ensure_embed_running,
    ensure_tei_running,
    stop_embed,
    stop_tei,
    tei_health,
)

__all__ = [
    "ensure_embed_running",
    "stop_embed",
    "embed_health",
    "discover_embed_launchers",
    "embedding_root",
    "ensure_tei_running",
    "stop_tei",
    "tei_health",
    "discover_tei_launchers",
]
