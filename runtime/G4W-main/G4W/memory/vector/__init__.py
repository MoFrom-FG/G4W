"""Vector retrieval capability with dependency-light package imports.

Importing a lightweight submodule such as :mod:`vector_config` must work in
the minimal GA environment, which intentionally does not install numpy.  The
heavier vector implementation is loaded lazily only when one of its exported
symbols is actually requested.
"""
from __future__ import annotations

from importlib import import_module


_EXPORTS = {
    # embedding
    "DEFAULT_DIM": ("embedding", "DEFAULT_DIM"),
    "dequantize_int8": ("embedding", "dequantize_int8"),
    "embed_batch": ("embedding", "embed_batch"),
    "embed_text": ("embedding", "embed_text"),
    "hash_embed": ("embedding", "hash_embed"),
    "quantize_int8": ("embedding", "quantize_int8"),
    # flags / product gate
    "vector_retrieval_enabled": ("flags", "vector_retrieval_enabled"),
    "config_path": ("vector_config", "config_path"),
    "load_config": ("vector_config", "load_config"),
    "save_config": ("vector_config", "save_config"),
    "set_vector_enabled": ("vector_config", "set_vector_enabled"),
    "status_dict": ("vector_config", "status_dict"),
    "vector_enabled": ("vector_config", "vector_enabled"),
    # index / query
    "BruteIndex": ("hnsw_index", "BruteIndex"),
    "HnswIndex": ("hnsw_index", "HnswIndex"),
    "SearchHit": ("hnsw_index", "SearchHit"),
    "create_index": ("hnsw_index", "create_index"),
    "hnswlib_available": ("hnsw_index", "hnswlib_available"),
    "HybridHit": ("hybrid_query", "HybridHit"),
    "HybridQueryEngine": ("hybrid_query", "HybridQueryEngine"),
    "expand_memory_queries": ("hybrid_query", "expand_memory_queries"),
    "vector_section_for": ("prod_inject", "vector_section_for"),
    # paths / tier policy
    "DEFAULT_PROD_INDEX_DIR": ("sandbox_paths", "DEFAULT_PROD_INDEX_DIR"),
    "DEFAULT_VECTOR_INDEX_BASE_DIR": ("sandbox_paths", "DEFAULT_VECTOR_INDEX_BASE_DIR"),
    "LEGACY_PROD_INDEX_DIR": ("sandbox_paths", "LEGACY_PROD_INDEX_DIR"),
    "assert_prod_index_write_allowed": ("sandbox_paths", "assert_prod_index_write_allowed"),
    "assert_sandbox_write_allowed": ("sandbox_paths", "assert_sandbox_write_allowed"),
    "default_bbs_cwd": ("sandbox_paths", "default_bbs_cwd"),
    "default_prod_index_dir": ("sandbox_paths", "default_prod_index_dir"),
    "ensure_sandbox_dir": ("sandbox_paths", "ensure_sandbox_dir"),
    "legacy_prod_index_dir": ("sandbox_paths", "legacy_prod_index_dir"),
    "prod_index_staging_dir": ("sandbox_paths", "prod_index_staging_dir"),
    "resolve_vector_index_dir": ("sandbox_paths", "resolve_vector_index_dir"),
    "sandbox_vector_index_root": ("sandbox_paths", "sandbox_vector_index_root"),
    "Tier": ("tier_policy", "Tier"),
    "TierPolicy": ("tier_policy", "TierPolicy"),
    "TierRecord": ("tier_policy", "TierRecord"),
    "default_policy": ("tier_policy", "default_policy"),
    # L4 / aliases / transcript upsert
    "format_insight_embed_text": ("l4_index_upsert", "format_insight_embed_text"),
    "insight_items_to_docs": ("l4_index_upsert", "insight_items_to_docs"),
    "l4_index_upsert_enabled": ("l4_index_upsert", "l4_index_upsert_enabled"),
    "l4_tier_by_category_enabled": ("l4_index_upsert", "l4_tier_by_category_enabled"),
    "suggest_tier_for_l4": ("l4_index_upsert", "suggest_tier_for_l4"),
    "upsert_after_l4_finalize": ("l4_index_upsert", "upsert_after_l4_finalize"),
    "upsert_l4_insights_to_index": ("l4_index_upsert", "upsert_l4_insights_to_index"),
    "enrich_aliases_and_bridges": ("entity_alias", "enrich_aliases_and_bridges"),
    "expand_query": ("entity_alias", "expand_query"),
    "lookup_aliases": ("entity_alias", "lookup_aliases"),
    "lookup_bridges": ("entity_alias", "lookup_bridges"),
    "time_bucket_from_timestamp": ("entity_alias", "time_bucket_from_timestamp"),
    "files_to_chunk_items": ("transcript_chunk_upsert", "files_to_chunk_items"),
    "maybe_upsert_after_transcript_append": ("transcript_chunk_upsert", "maybe_upsert_after_transcript_append"),
    "transcript_chunk_upsert_enabled": ("transcript_chunk_upsert", "transcript_chunk_upsert_enabled"),
    "upsert_transcript_chunks": ("transcript_chunk_upsert", "upsert_transcript_chunks"),
    "upsert_transcript_file": ("transcript_chunk_upsert", "upsert_transcript_file"),
    # index metadata / rebuild
    "current_embedding_fingerprint": ("index_meta", "current_embedding_fingerprint"),
    "fingerprint_mismatch": ("index_meta", "fingerprint_mismatch"),
    "format_mismatch_hint": ("index_meta", "format_mismatch_hint"),
    "index_fingerprint": ("index_meta", "index_fingerprint"),
    "load_index_meta": ("index_meta", "load_index_meta"),
    "meta_status_lines": ("index_meta", "meta_status_lines"),
    "patch_meta_fields": ("index_meta", "patch_meta_fields"),
    "read_meta_dict": ("index_meta", "read_meta_dict"),
    "write_fingerprint_to_meta": ("index_meta", "write_fingerprint_to_meta"),
    "build_index_from_l4": ("index_rebuild", "build_index_from_l4"),
    "format_rebuild_status": ("index_rebuild", "format_rebuild_status"),
    "rebuild_state": ("index_rebuild", "rebuild_state"),
    "start_rebuild_async": ("index_rebuild", "start_rebuild_async"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(import_module(f"{__name__}.{module_name}"), attribute_name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
