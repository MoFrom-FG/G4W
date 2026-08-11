from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

from . import paths

_WORD_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


def stable_doc_id(source_path: str | Path, content: str) -> str:
    h = hashlib.sha256()
    h.update(str(source_path).replace("\\", "/").encode("utf-8", errors="ignore"))
    h.update(b"\0")
    h.update(content.encode("utf-8", errors="ignore"))
    return h.hexdigest()[:16]


def tokenize(text: str) -> list[str]:
    return [m.group(0).lower() for m in _WORD_RE.finditer(text or "")]


class KnowledgeStore:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root).expanduser().resolve() if root else paths.data_root()
        self.docs_dir = self.root / "documents"
        self.manifest_file = self.root / "manifest.json"
        self.chunks_file = self.root / "chunks.jsonl"
        self.list_map_file = self.root / "list_map.json"

    def ensure(self) -> None:
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)

    def load_manifest(self) -> dict[str, Any]:
        if not self.manifest_file.exists():
            return {"documents": {}}
        try:
            data = json.loads(self.manifest_file.read_text(encoding="utf-8"))
        except Exception:
            return {"documents": {}}
        if not isinstance(data, dict):
            return {"documents": {}}
        data.setdefault("documents", {})
        return data

    def save_manifest(self, manifest: dict[str, Any]) -> None:
        self.ensure()
        tmp = self.manifest_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.manifest_file)

    def read_chunks(self) -> list[dict[str, Any]]:
        if not self.chunks_file.exists():
            return []
        chunks: list[dict[str, Any]] = []
        for line in self.chunks_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict):
                chunks.append(obj)
        return chunks

    def write_chunks(self, chunks: list[dict[str, Any]]) -> None:
        self.ensure()
        tmp = self.chunks_file.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for chunk in chunks:
                f.write(json.dumps(chunk, ensure_ascii=False, sort_keys=True) + "\n")
        tmp.replace(self.chunks_file)

    def read_chunk_window(self, chunk_id: str, window: int = 0) -> list[dict[str, Any]]:
        chunks = self.read_chunks()
        target_index = next((i for i, c in enumerate(chunks) if c.get("chunk_id") == chunk_id), None)
        if target_index is None:
            return []
        target_doc = chunks[target_index].get("doc_id")
        same_doc_indexes = [i for i, c in enumerate(chunks) if c.get("doc_id") == target_doc]
        position = same_doc_indexes.index(target_index)
        radius = max(0, min(int(window or 0), 3))
        selected = same_doc_indexes[max(0, position - radius) : position + radius + 1]
        return [chunks[i] for i in selected]

    def read_page_chunks(self, doc_id: str, page: int | str, window: int = 0) -> list[dict[str, Any]]:
        try:
            target_page = int(page)
        except (TypeError, ValueError):
            return []
        chunks = [c for c in self.read_chunks() if c.get("doc_id") == doc_id]
        page_indexes = [i for i, c in enumerate(chunks) if c.get("page") == target_page]
        if not page_indexes:
            return []
        radius = max(0, min(int(window or 0), 3))
        selected: list[int] = []
        for i in page_indexes:
            selected.extend(range(max(0, i - radius), min(len(chunks), i + radius + 1)))
        return [chunks[i] for i in sorted(set(selected))]

    def add_document(self, source_path: str | Path, title: str, text: str, chunks: list[dict[str, Any]], tags: list[str] | None = None) -> dict[str, Any]:
        self.ensure()
        doc_id = stable_doc_id(source_path, text)
        source = Path(source_path)
        ext = source.suffix.lower() or ".txt"
        stored = self.docs_dir / f"{doc_id}{ext}"
        if source.exists() and source.is_file():
            shutil.copy2(source, stored)
        else:
            stored.write_text(text, encoding="utf-8")
        text_path = self.docs_dir / f"{doc_id}.extracted.txt"
        text_path.write_text(text, encoding="utf-8")
        now = int(time.time())
        manifest = self.load_manifest()
        doc = {
            "doc_id": doc_id,
            "title": title or source.name or doc_id,
            "source_path": str(source),
            "stored_path": str(stored),
            "text_path": str(text_path),
            "tags": tags or [],
            "created_at": now,
            "updated_at": now,
            "chunk_count": len(chunks),
        }
        manifest["documents"][doc_id] = doc
        old = [c for c in self.read_chunks() if c.get("doc_id") != doc_id]
        for i, chunk in enumerate(chunks):
            chunk.update({"doc_id": doc_id, "chunk_id": f"{doc_id}:{i:04d}", "title": doc["title"]})
        self.write_chunks(old + chunks)
        self.save_manifest(manifest)
        return doc

    def list_documents(self) -> list[dict[str, Any]]:
        docs = list(self.load_manifest().get("documents", {}).values())
        docs.sort(key=lambda d: (str(d.get("title") or ""), str(d.get("doc_id") or "")))
        mapping = {str(i + 1): d.get("doc_id") for i, d in enumerate(docs)}
        self.ensure()
        self.list_map_file.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
        return docs

    def resolve_number(self, number: str | int) -> str | None:
        try:
            mapping = json.loads(self.list_map_file.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(mapping, dict):
            return None
        return mapping.get(str(number))

    def remove_by_doc_id(self, doc_id: str) -> bool:
        manifest = self.load_manifest()
        doc = manifest.get("documents", {}).pop(doc_id, None)
        if not doc:
            return False
        stored = doc.get("stored_path")
        if stored:
            try:
                Path(stored).unlink(missing_ok=True)
            except Exception:
                pass
        text_path = doc.get("text_path")
        if text_path:
            try:
                Path(text_path).unlink(missing_ok=True)
            except Exception:
                pass
        self.write_chunks([c for c in self.read_chunks() if c.get("doc_id") != doc_id])
        self.save_manifest(manifest)
        self.list_documents()
        return True

    def rebuild_keyword_index(self) -> dict[str, Any]:
        # chunks.jsonl is the keyword/BM25 corpus for the offline path.
        chunks = self.read_chunks()
        docs = self.load_manifest().get("documents", {})
        return {"ok": True, "documents": len(docs), "chunks": len(chunks), "index": str(self.chunks_file)}
