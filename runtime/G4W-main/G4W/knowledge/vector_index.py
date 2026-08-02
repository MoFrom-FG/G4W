from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import paths
from .store import KnowledgeStore
from G4W.memory.vector.embed_lifecycle import ensure_embed_running
from G4W.memory.vector.embedding import (
    EmbeddingError,
    embed_batch,
    embed_text,
    resolve_dim,
    resolve_provider,
)
from G4W.memory.vector.hnsw_index import HnswIndex, create_index
from G4W.memory.vector.vector_config import load_config, vector_enabled

META_FILE = "kb_vector_meta.json"
CHUNK_MAP_FILE = "chunk_map.json"
QUOTE_CHAR_LIMIT = 1200


def _index_dir() -> Path:
    return paths.vector_root() / "hnsw"


def _chunk_map_path() -> Path:
    return _index_dir() / CHUNK_MAP_FILE


def _meta_path() -> Path:
    return _index_dir() / META_FILE


def kb_vector_enabled() -> bool:
    """Product gate for KB vector retrieval. Off means no embedding and no HNSW IO."""
    return bool(vector_enabled())


def _vector_config() -> dict[str, Any]:
    cfg = load_config()
    dim = int(cfg.get("dim") or resolve_dim(None))
    return {
        "dim": dim,
        "model": str(cfg.get("model") or ""),
        "base_url": str(cfg.get("base_url") or ""),
    }


def _require_embedding_ready() -> dict[str, Any]:
    """Require `/vector on` plus a real, healthy embedding service for KB IO."""
    if not kb_vector_enabled():
        raise EmbeddingError("knowledge base requires /vector on", reason="vector_disabled")
    provider = resolve_provider()
    if provider == "hash":
        raise EmbeddingError(
            "knowledge base requires real embedding provider; hash fallback is disabled",
            reason="hash_provider_disabled",
        )
    status = ensure_embed_running()
    if not status.get("ok"):
        detail = status.get("error") or status.get("detail") or status.get("status") or "unknown"
        raise EmbeddingError(
            f"embedding service unavailable: {detail}",
            reason="embedding_unavailable",
        )
    return status


def _chunk_label(chunk: dict[str, Any]) -> str:
    return str(chunk.get("chunk_id") or f"{chunk.get('doc_id')}:{chunk.get('index', '')}")


def _load_chunk_map() -> dict[str, dict[str, Any]]:
    path = _chunk_map_path()
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def _save_chunk_map(mapping: dict[str, dict[str, Any]]) -> None:
    _index_dir().mkdir(parents=True, exist_ok=True)
    _chunk_map_path().write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _save_meta(*, count: int, source_chunks: int, backend: str) -> dict[str, Any]:
    cfg = _vector_config()
    meta = {
        "ok": True,
        "count": int(count),
        "source_chunks": int(source_chunks),
        "backend": backend,
        "dim": cfg["dim"],
        "model": cfg["model"],
        "base_url": cfg["base_url"],
        "updated_at": time.time(),
        "index_dir": str(_index_dir()),
    }
    _index_dir().mkdir(parents=True, exist_ok=True)
    _meta_path().write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def vector_status() -> dict[str, Any]:
    if not kb_vector_enabled():
        return {"enabled": False, "index_dir": str(_index_dir())}
    meta = {}
    if _meta_path().is_file():
        try:
            meta = json.loads(_meta_path().read_text(encoding="utf-8"))
        except Exception:
            meta = {"error": "invalid_meta"}
    return {"enabled": True, "index_dir": str(_index_dir()), "meta": meta}


def rebuild_vector_index(store: KnowledgeStore | None = None) -> dict[str, Any]:
    """Rebuild the KB-only vector index. Requires real embedding to be healthy."""
    embed_status = _require_embedding_ready()
    store = store or KnowledgeStore()
    chunks = store.read_chunks()
    texts = [str(chunk.get("text") or "") for chunk in chunks]
    labels = [_chunk_label(chunk) for chunk in chunks]
    cfg = _vector_config()
    index = create_index(
        dim=cfg["dim"],
        max_elements=max(1024, len(texts) + 1024),
        model=cfg["model"],
        base_url=cfg["base_url"],
    )
    if texts:
        vectors = embed_batch(texts, dim=cfg["dim"])
        index.add(vectors, labels=labels, replace=True)
    index.save(_index_dir())
    _save_chunk_map({label: chunk for label, chunk in zip(labels, chunks)})
    meta = _save_meta(count=len(labels), source_chunks=len(chunks), backend=str(index.backend))
    meta["embedding"] = {
        "status": embed_status.get("status"),
        "pid": embed_status.get("pid"),
    }
    return meta


def search_vector_knowledge(
    query: str, k: int = 5, store: KnowledgeStore | None = None
) -> dict[str, Any]:
    """Search KB vector index. Missing embedding/index state is a hard failure."""
    _require_embedding_ready()
    index_dir = _index_dir()
    if not (index_dir / "meta.json").is_file():
        raise EmbeddingError(
            "knowledge vector index missing; run /kb rebuild while /vector on",
            reason="index_missing",
        )
    store = store or KnowledgeStore()
    docs = store.load_manifest().get("documents", {})
    chunk_map = _load_chunk_map()
    if not chunk_map:
        raise EmbeddingError(
            "knowledge vector chunk map missing; run /kb rebuild",
            reason="chunk_map_missing",
        )
    cfg = _vector_config()
    index = HnswIndex.load(index_dir)
    qvec = embed_text(query, dim=cfg["dim"])
    hits = []
    for hit in index.search(qvec, k=max(1, min(int(k or 5), 20))):
        chunk = chunk_map.get(str(hit.label))
        if not chunk:
            continue
        doc = docs.get(chunk.get("doc_id"), {})
        text = " ".join(str(chunk.get("text") or "").split())
        hits.append({
            "score": round(float(hit.score), 4),
            "doc_id": chunk.get("doc_id"),
            "chunk_id": chunk.get("chunk_id"),
            "title": doc.get("title") or chunk.get("title") or chunk.get("doc_id"),
            "source": doc.get("source_path") or doc.get("stored_path"),
            "page": chunk.get("page"),
            "section": chunk.get("section") or "",
            "quote": text[:QUOTE_CHAR_LIMIT],
            "quote_truncated": len(text) > QUOTE_CHAR_LIMIT,
            "text_length": len(text),
            "tags": doc.get("tags") or [],
        })
    return {"query": query, "hits": hits, "mode": "vector", "index_dir": str(index_dir)}


__all__ = [
    "EmbeddingError",
    "kb_vector_enabled",
    "rebuild_vector_index",
    "search_vector_knowledge",
    "vector_status",
]
