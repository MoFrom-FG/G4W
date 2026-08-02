"""Dry-run: sandbox hybrid query demo (no production DATA)."""
from __future__ import annotations

import argparse
import json
import os
import sys

from G4W.memory.vector.hnsw_index import create_index
from G4W.memory.vector.hybrid_query import HybridQueryEngine


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="", help="sandbox root label only")
    args = ap.parse_args(argv)

    # Prefer create_index (hnswlib if available, else Brute) — IMPL-D align
    eng = HybridQueryEngine(index=create_index(dim=384), dim=384)
    samples = [
        ("d1", "vector retrieval hybrid keyword coarse filter"),
        ("d2", "HNSW index float32 cosine search sandbox"),
        ("d3", "tier policy HOT WARM COLD capacity bounds"),
        ("d4", "G4W memory hybrid reader gate"),
        ("d5", "unrelated cooking recipe pasta tomato"),
    ]
    for iid, text in samples:
        eng.upsert(iid, text)
    eng.rebalance()
    hits = eng.search("hybrid vector HNSW", k=3)
    out = {
        "hits": [
            {
                "id": h.item_id,
                "score": h.score,
                "tier": h.tier,
                "stages": h.stages,
            }
            for h in hits
        ],
        "root": args.root or os.environ.get("BBS_CWD", ""),
        "flag_note": "G4W_VECTOR_RETRIEVAL default OFF (demo local only)",
        "index_backend": getattr(eng.index, "backend", "?"),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
