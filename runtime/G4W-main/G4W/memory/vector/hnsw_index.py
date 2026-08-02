"""HNSW vector index with BruteIndex fallback.

API: create / add / search / save / load
Defaults: d=384, M=16, efConstruction=200, efSearch=50
Storage: float32 in-memory and on disk (vectors.npy / index.bin).
int8 quant helpers live in embedding.py only; not used by add/search/save/load.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np

from .embedding import DEFAULT_DIM, as_float32_unit

try:
    import hnswlib

    _HAS_HNSWLIB = True
except Exception:  # pragma: no cover
    hnswlib = None  # type: ignore
    _HAS_HNSWLIB = False


DEFAULT_M = 16
DEFAULT_EFC = 200
DEFAULT_EFS = 50


def hnswlib_available() -> bool:
    return bool(_HAS_HNSWLIB)


@dataclass
class SearchHit:
    label: str
    score: float
    index: int


@dataclass
class IndexMeta:
    """Index on-disk meta (meta.json).

    Core HNSW fields + embedding fingerprint (model / base_url).
    Unknown keys are preserved in ``extras`` so save() does not clobber
    rebuild_*/last_l4_* / provider / source patches.
    """

    dim: int = DEFAULT_DIM
    M: int = DEFAULT_M
    ef_construction: int = DEFAULT_EFC
    ef_search: int = DEFAULT_EFS
    backend: str = "brute"  # "hnswlib" | "brute"
    space: str = "cosine"
    count: int = 0
    created_at: float = field(default_factory=time.time)
    labels: List[str] = field(default_factory=list)
    # Embedding fingerprint (TASK-F)
    model: str = ""
    base_url: str = ""
    # Catch-all for non-core keys (rebuild_*, last_l4_*, provider, source, …)
    extras: Dict[str, Any] = field(default_factory=dict)

    # Keys owned by IndexMeta core fields (not stored in extras)
    _CORE_KEYS = frozenset(
        {
            "dim",
            "M",
            "ef_construction",
            "ef_search",
            "backend",
            "space",
            "count",
            "created_at",
            "labels",
            "model",
            "base_url",
            "extras",
        }
    )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "dim": self.dim,
            "M": self.M,
            "ef_construction": self.ef_construction,
            "ef_search": self.ef_search,
            "backend": self.backend,
            "space": self.space,
            "count": self.count,
            "created_at": self.created_at,
            "labels": list(self.labels),
        }
        if self.model:
            out["model"] = self.model
        if self.base_url:
            out["base_url"] = self.base_url
        # flatten extras (preserve last_l4_*, rebuild_*, provider, source, …)
        for k, v in (self.extras or {}).items():
            if k in self._CORE_KEYS:
                continue
            out[k] = v
        return out

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "IndexMeta":
        raw = dict(d or {})
        extras: Dict[str, Any] = {}
        # nested extras blob (optional)
        nested = raw.pop("extras", None)
        if isinstance(nested, dict):
            extras.update(nested)
        model = str(raw.get("model") or raw.get("embed_model") or "")
        base_url = str(raw.get("base_url") or "")
        core = {
            "dim": int(raw.get("dim", DEFAULT_DIM)),
            "M": int(raw.get("M", DEFAULT_M)),
            "ef_construction": int(raw.get("ef_construction", DEFAULT_EFC)),
            "ef_search": int(raw.get("ef_search", DEFAULT_EFS)),
            "backend": str(raw.get("backend", "brute")),
            "space": str(raw.get("space", "cosine")),
            "count": int(raw.get("count", 0)),
            "created_at": float(raw.get("created_at", time.time())),
            "labels": list(raw.get("labels") or []),
            "model": model,
            "base_url": base_url,
        }
        for k, v in raw.items():
            if k in cls._CORE_KEYS:
                continue
            # embed_model alias already folded into model
            if k == "embed_model":
                continue
            extras[k] = v
        return cls(**core, extras=extras)


class BruteIndex:
    """Pure numpy cosine brute-force; same external semantics as HnswIndex."""

    def __init__(
        self,
        dim: int = DEFAULT_DIM,
        M: int = DEFAULT_M,
        ef_construction: int = DEFAULT_EFC,
        ef_search: int = DEFAULT_EFS,
        max_elements: int = 100_000,
        model: str = "",
        base_url: str = "",
    ):
        self.dim = dim
        self.M = M
        self.ef_construction = ef_construction
        self.ef_search = ef_search
        self.max_elements = max_elements
        self._data = np.zeros((0, dim), dtype=np.float32)
        self._labels: List[str] = []
        self.backend = "brute"
        # Embedding fingerprint (TASK-F)
        self.model = str(model or "")
        self.base_url = str(base_url or "")
        self._meta_extras: Dict[str, Any] = {}

    @property
    def count(self) -> int:
        return len(self._labels)

    def add(
        self,
        vectors: np.ndarray,
        labels: Optional[Sequence[str]] = None,
        text: Optional[Union[str, Sequence[str]]] = None,
        replace: bool = False,
        **kwargs: Any,
    ) -> None:
        """Add vectors. Optional ``text`` is accepted for HybridQueryEngine compat (ignored).

        When ``replace=True`` and a label already exists, overwrite that row
        (no ghost duplicate labels / count growth).
        """
        _ = text, kwargs  # API align with B; docs live outside the index
        vecs = np.asarray(vectors, dtype=np.float32)
        if vecs.ndim == 1:
            vecs = vecs.reshape(1, -1)
        if vecs.shape[1] != self.dim:
            raise ValueError(f"dim mismatch: got {vecs.shape[1]} want {self.dim}")
        # L2 normalize for cosine
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms = np.maximum(norms, 1e-12)
        vecs = vecs / norms
        n = vecs.shape[0]
        if labels is None:
            base = self.count
            labels = [str(base + i) for i in range(n)]
        if len(labels) != n:
            raise ValueError("labels length must match vectors")
        if replace:
            label_to_i = {lab: i for i, lab in enumerate(self._labels)}
            new_vecs: List[np.ndarray] = []
            new_labs: List[str] = []
            for i in range(n):
                lab = str(labels[i])
                if lab in label_to_i:
                    self._data[label_to_i[lab]] = vecs[i]
                else:
                    new_vecs.append(vecs[i])
                    new_labs.append(lab)
            if not new_labs:
                return
            vecs = np.vstack(new_vecs)
            labels = new_labs
            n = len(labels)
        if self.count + n > self.max_elements:
            raise RuntimeError("max_elements exceeded")
        self._data = np.vstack([self._data, vecs]) if self.count else vecs.copy()
        self._labels.extend(str(x) for x in labels)

    def search(
        self, query: np.ndarray, k: int = 10, ef_search: Optional[int] = None
    ) -> List[SearchHit]:
        if self.count == 0 or k <= 0:
            return []
        q = as_float32_unit(query, dim=self.dim)
        # cosine sim = dot for unit vectors
        sims = self._data @ q
        k_eff = min(k, self.count)
        # top-k indices
        if k_eff >= self.count:
            idx = np.argsort(-sims)
        else:
            part = np.argpartition(-sims, k_eff - 1)[:k_eff]
            idx = part[np.argsort(-sims[part])]
        hits: List[SearchHit] = []
        for i in idx[:k_eff]:
            hits.append(
                SearchHit(
                    label=self._labels[int(i)],
                    score=float(sims[int(i)]),
                    index=int(i),
                )
            )
        return hits

    def save(self, directory: Union[str, Path]) -> Path:
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        np.save(d / "vectors.npy", self._data)
        # Preserve prior extras / created_at when re-saving same dir
        extras: Dict[str, Any] = dict(getattr(self, "_meta_extras", None) or {})
        created_at = time.time()
        meta_path = d / "meta.json"
        if meta_path.is_file():
            try:
                prev = IndexMeta.from_dict(
                    json.loads(meta_path.read_text(encoding="utf-8"))
                )
                created_at = float(prev.created_at or created_at)
                for k, v in (prev.extras or {}).items():
                    extras.setdefault(k, v)
                if not getattr(self, "model", ""):
                    self.model = str(prev.model or "")
                if not getattr(self, "base_url", ""):
                    self.base_url = str(prev.base_url or "")
            except Exception:
                pass
        meta = IndexMeta(
            dim=self.dim,
            M=self.M,
            ef_construction=self.ef_construction,
            ef_search=self.ef_search,
            backend="brute",
            count=self.count,
            labels=list(self._labels),
            created_at=created_at,
            model=str(getattr(self, "model", "") or ""),
            base_url=str(getattr(self, "base_url", "") or ""),
            extras=extras,
        )
        meta_path.write_text(
            json.dumps(meta.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return d

    @classmethod
    def load(cls, directory: Union[str, Path]) -> "BruteIndex":
        d = Path(directory)
        meta = IndexMeta.from_dict(
            json.loads((d / "meta.json").read_text(encoding="utf-8"))
        )
        idx = cls(
            dim=meta.dim,
            M=meta.M,
            ef_construction=meta.ef_construction,
            ef_search=meta.ef_search,
            model=str(meta.model or ""),
            base_url=str(meta.base_url or ""),
        )
        data_path = d / "vectors.npy"
        if data_path.exists():
            idx._data = np.load(str(data_path)).astype(np.float32)
        idx._labels = list(meta.labels)
        idx._meta_extras = dict(meta.extras or {})
        if idx._data.shape[0] != len(idx._labels):
            # heal count from data
            n = idx._data.shape[0]
            idx._labels = idx._labels[:n] if idx._labels else [str(i) for i in range(n)]
        return idx


class HnswIndex:
    """hnswlib-backed cosine index; falls back to BruteIndex if unavailable."""

    def __init__(
        self,
        dim: int = DEFAULT_DIM,
        M: int = DEFAULT_M,
        ef_construction: int = DEFAULT_EFC,
        ef_search: int = DEFAULT_EFS,
        max_elements: int = 100_000,
        prefer_hnsw: bool = True,
        model: str = "",
        base_url: str = "",
    ):
        self.dim = dim
        self.M = M
        self.ef_construction = ef_construction
        self.ef_search = ef_search
        self.max_elements = max_elements
        self._labels: List[str] = []
        self._label_to_id: Dict[str, int] = {}
        self._use_hnsw = bool(prefer_hnsw and _HAS_HNSWLIB)
        self._index = None
        self._brute: Optional[BruteIndex] = None
        # Embedding fingerprint + preserved meta extras (TASK-F)
        self.model: str = str(model or "")
        self.base_url: str = str(base_url or "")
        self._meta_extras: Dict[str, Any] = {}
        if self._use_hnsw:
            self._index = hnswlib.Index(space="cosine", dim=dim)
            self._index.init_index(
                max_elements=max_elements,
                ef_construction=ef_construction,
                M=M,
            )
            self._index.set_ef(ef_search)
            self.backend = "hnswlib"
        else:
            self._brute = BruteIndex(
                dim=dim,
                M=M,
                ef_construction=ef_construction,
                ef_search=ef_search,
                max_elements=max_elements,
                model=self.model,
                base_url=self.base_url,
            )
            self.backend = "brute"

    @staticmethod
    def _is_deleted_label(name: object) -> bool:
        return str(name).startswith("__deleted_")

    @property
    def count(self) -> int:
        """Live (non-soft-deleted) label count; matches BruteIndex semantics."""
        if self._brute is not None:
            return self._brute.count
        return sum(1 for lab in self._labels if not self._is_deleted_label(lab))

    def add(
        self,
        vectors: np.ndarray,
        labels: Optional[Sequence[str]] = None,
        text: Optional[Union[str, Sequence[str]]] = None,
        replace: bool = False,
        **kwargs: Any,
    ) -> None:
        """Add vectors. Optional ``text`` accepted for HybridQueryEngine compat (ignored).

        ``replace=True``: overwrite existing labels when possible.
        Brute fallback replaces in-place. hnswlib path: mark_deleted + re-add
        under a fresh internal id so searchable set has one live vector per label
        (docs remain consistent; search will not double-hit the same label).
        """
        if self._brute is not None:
            self._brute.add(vectors, labels, text=text, replace=replace, **kwargs)
            return
        _ = text, kwargs
        vecs = np.asarray(vectors, dtype=np.float32)
        if vecs.ndim == 1:
            vecs = vecs.reshape(1, -1)
        if vecs.shape[1] != self.dim:
            raise ValueError(f"dim mismatch: got {vecs.shape[1]} want {self.dim}")
        n = vecs.shape[0]
        if labels is None:
            # use internal id space (not live count) so auto labels stay unique
            base = len(self._labels)
            labels = [str(base + i) for i in range(n)]
        if len(labels) != n:
            raise ValueError("labels length must match vectors")
        ids = []
        keep_rows = []
        for i, lab in enumerate(labels):
            lab = str(lab)
            if lab in self._label_to_id:
                if not replace:
                    raise ValueError(f"duplicate label: {lab}")
                # soft-replace: delete old internal id, re-add under new id
                old_id = self._label_to_id[lab]
                assert self._index is not None
                try:
                    self._index.mark_deleted(old_id)
                except Exception:
                    pass
                # free label slot for rebind; keep list length stable for id space
                self._labels[old_id] = f"__deleted_{old_id}"
                del self._label_to_id[lab]
            new_id = len(self._labels)
            self._labels.append(lab)
            self._label_to_id[lab] = new_id
            ids.append(new_id)
            keep_rows.append(i)
        if not ids:
            return
        # hnswlib cosine expects non-normalized; it uses 1-cos internally
        assert self._index is not None
        # resize if needed
        if len(self._labels) > self._index.get_max_elements():
            self._index.resize_index(len(self._labels) + 1024)
        self._index.add_items(vecs[keep_rows], np.array(ids, dtype=np.int32))

    def search(
        self, query: np.ndarray, k: int = 10, ef_search: Optional[int] = None
    ) -> List[SearchHit]:
        if self._brute is not None:
            return self._brute.search(query, k=k, ef_search=ef_search)
        live = self.count
        if live == 0 or k <= 0:
            return []
        q = as_float32_unit(query, dim=self.dim).reshape(1, -1)
        assert self._index is not None
        efs = int(ef_search if ef_search is not None else self.ef_search)
        # hnswlib knn_query k must not exceed live (non-deleted) elements;
        # soft-replace leaves tombstones that shrink the live set.
        # Prefer over-fetch up to live, then degrade k on RuntimeError.
        want = min(max(k * 3, k), live)
        labels_arr = None
        distances = None
        k_try = want
        while k_try >= 1:
            try:
                self._index.set_ef(max(efs, k_try))
                labels_arr, distances = self._index.knn_query(q, k=k_try)
                break
            except RuntimeError:
                # "Cannot return the results in a contiguous 2D array..."
                if k_try <= 1:
                    return []
                k_try = max(1, k_try // 2)
        if labels_arr is None or distances is None:
            return []
        hits: List[SearchHit] = []
        for lab_id, dist in zip(labels_arr[0], distances[0]):
            i = int(lab_id)
            # cosine space: distance = 1 - cos_sim
            score = 1.0 - float(dist)
            name = self._labels[i] if 0 <= i < len(self._labels) else str(i)
            if self._is_deleted_label(name):
                continue
            hits.append(SearchHit(label=name, score=score, index=i))
            if len(hits) >= k:
                break
        return hits

    def save(self, directory: Union[str, Path]) -> Path:
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        if self._brute is not None:
            # keep fingerprint on brute path too
            self._brute.model = str(getattr(self, "model", "") or self._brute.model or "")
            self._brute.base_url = str(
                getattr(self, "base_url", "") or self._brute.base_url or ""
            )
            if getattr(self, "_meta_extras", None):
                merged = dict(self._brute._meta_extras or {})
                merged.update(self._meta_extras)
                self._brute._meta_extras = merged
            return self._brute.save(d)
        assert self._index is not None
        bin_path = d / "index.bin"
        self._index.save_index(str(bin_path))
        extras: Dict[str, Any] = dict(getattr(self, "_meta_extras", None) or {})
        meta_path = d / "meta.json"
        if meta_path.is_file():
            try:
                existing = IndexMeta.from_dict(
                    json.loads(meta_path.read_text(encoding="utf-8"))
                )
                for k, v in (existing.extras or {}).items():
                    extras.setdefault(k, v)
                if not getattr(self, "model", "") and existing.model:
                    self.model = existing.model
                if not getattr(self, "base_url", "") and existing.base_url:
                    self.base_url = existing.base_url
            except Exception:
                pass
        meta = IndexMeta(
            dim=self.dim,
            M=self.M,
            ef_construction=self.ef_construction,
            ef_search=self.ef_search,
            backend="hnswlib",
            count=self.count,
            labels=list(self._labels),
            model=str(getattr(self, "model", "") or ""),
            base_url=str(getattr(self, "base_url", "") or ""),
            extras=extras,
        )
        meta_path.write_text(
            json.dumps(meta.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return d

    @classmethod
    def load(cls, directory: Union[str, Path]) -> "HnswIndex":
        d = Path(directory)
        meta = IndexMeta.from_dict(
            json.loads((d / "meta.json").read_text(encoding="utf-8"))
        )
        if meta.backend == "brute" or not _HAS_HNSWLIB:
            brute = BruteIndex.load(d)
            obj = cls(
                dim=meta.dim,
                M=meta.M,
                ef_construction=meta.ef_construction,
                ef_search=meta.ef_search,
                prefer_hnsw=False,
            )
            obj._brute = brute
            obj.backend = "brute"
            obj.model = str(meta.model or getattr(brute, "model", "") or "")
            obj.base_url = str(meta.base_url or getattr(brute, "base_url", "") or "")
            obj._meta_extras = dict(
                meta.extras or getattr(brute, "_meta_extras", None) or {}
            )
            return obj
        # Build shell without init_index, then load_index only (avoids deallocate warn)
        obj = object.__new__(cls)
        obj.dim = meta.dim
        obj.M = meta.M
        obj.ef_construction = meta.ef_construction
        obj.ef_search = meta.ef_search
        obj.max_elements = max(meta.count + 1024, 100_000)
        obj._labels = list(meta.labels)
        obj._label_to_id = {lab: i for i, lab in enumerate(obj._labels)}
        obj._brute = None
        obj._index = hnswlib.Index(space="cosine", dim=meta.dim)
        obj._index.load_index(
            str(d / "index.bin"), max_elements=max(meta.count + 1024, 100_000)
        )
        obj._index.set_ef(meta.ef_search)
        obj.backend = "hnswlib"
        obj.model = str(meta.model or "")
        obj.base_url = str(meta.base_url or "")
        obj._meta_extras = dict(meta.extras or {})
        return obj


def create_index(
    dim: int = DEFAULT_DIM,
    M: int = DEFAULT_M,
    ef_construction: int = DEFAULT_EFC,
    ef_search: int = DEFAULT_EFS,
    max_elements: int = 100_000,
    prefer_hnsw: bool = True,
    model: str = "",
    base_url: str = "",
) -> HnswIndex:
    """Factory: create empty index (hnswlib if available else brute)."""
    return HnswIndex(
        dim=dim,
        M=M,
        ef_construction=ef_construction,
        ef_search=ef_search,
        max_elements=max_elements,
        prefer_hnsw=prefer_hnsw,
        model=model,
        base_url=base_url,
    )
