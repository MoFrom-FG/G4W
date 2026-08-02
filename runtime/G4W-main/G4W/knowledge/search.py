from __future__ import annotations

from typing import Any

from .store import KnowledgeStore
from .vector_index import search_vector_knowledge


def search_knowledge(query: str, k: int = 5, store: KnowledgeStore | None = None) -> dict[str, Any]:
    """Search the independent KB through embedding only.

    KB quality policy is intentionally strict: when `/vector on` is off, the
    embedding service cannot be started, or the vector index is missing, callers
    receive a hard failure. Do not fall back to keyword/hybrid retrieval here.
    """
    if not str(query or "").strip():
        raise ValueError("query is required")
    return search_vector_knowledge(query, k=k, store=store or KnowledgeStore())
