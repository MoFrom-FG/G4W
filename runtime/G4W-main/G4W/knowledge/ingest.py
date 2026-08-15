from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .store import KnowledgeStore

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_MOJIBAKE_RE = re.compile(r"[\u00a1-\u00ff]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def text_quality_report(text: str) -> dict[str, Any]:
    """Return coarse extraction quality metrics for the KB ingest gate."""
    compact = re.sub(r"\s+", "", text or "")
    total = len(compact)
    replacement = compact.count("\ufffd")
    controls = len(_CONTROL_RE.findall(compact))
    mojibake = len(_MOJIBAKE_RE.findall(compact))
    return {
        "chars": total,
        "replacement_chars": replacement,
        "control_chars": controls,
        "mojibake_chars": mojibake,
        "replacement_ratio": (replacement / total) if total else 0.0,
        "control_ratio": (controls / total) if total else 0.0,
        "mojibake_ratio": (mojibake / total) if total else 0.0,
    }


def validate_extracted_text(text: str) -> dict[str, Any]:
    report = text_quality_report(text)
    total = int(report["chars"])
    if total < 20:
        return {"ok": False, "reason": "document has too little extractable text", **report}
    if report["replacement_chars"] > max(3, total * 0.01):
        return {"ok": False, "reason": "extracted text contains too many replacement characters", **report}
    if report["control_chars"] > max(3, total * 0.005):
        return {"ok": False, "reason": "extracted text contains too many control characters", **report}
    if report["mojibake_chars"] >= 12 and report["mojibake_ratio"] > 0.03:
        return {"ok": False, "reason": "extracted text appears garbled or mojibake", **report}
    return {"ok": True, "reason": "ok", **report}


def _read_pdf(path: Path) -> tuple[str, list[dict[str, Any]]]:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        try:
            from PyPDF2 import PdfReader  # type: ignore
        except Exception as error:
            raise RuntimeError("PDF import requires pypdf or PyPDF2 to be installed") from error
    reader = PdfReader(str(path))
    pages: list[dict[str, Any]] = []
    parts: list[str] = []
    for i, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        pages.append({"page": i, "text": text})
        parts.append(f"\n\n[page {i}]\n{text}")
    return "".join(parts).strip(), pages


def extract_text(path: str | Path) -> tuple[str, list[dict[str, Any]]]:
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        return _read_pdf(p)
    if suffix == ".docx":
        return _read_docx(p)
    if suffix == ".doc":
        raise ValueError("旧版 .doc（二进制）不支持提取，请另存为 .docx 或 PDF")
    text = p.read_text(encoding="utf-8", errors="replace")
    return text, [{"page": None, "text": text}]


def _read_docx(p: Path) -> tuple[str, list[dict[str, Any]]]:
    """.docx 文本提取：纯标准库（zipfile + XML），无需任何第三方依赖。

    段落按 <w:p> 提取，标题（pStyle=HeadingN）转 Markdown # 前缀，
    便于知识库预览与检索。旧版二进制 .doc 不支持。
    """
    try:
        import zipfile
        from xml.etree import ElementTree as ET

        with zipfile.ZipFile(p) as archive:
            xml_bytes = archive.read("word/document.xml")
    except Exception as exc:
        raise ValueError(f"无法解析 .docx：{type(exc).__name__}: {exc}") from exc
    root = ET.fromstring(xml_bytes)
    lines: list[str] = []
    for para in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
        style = ""
        ppr = para.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}pPr")
        if ppr is not None:
            pstyle = ppr.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}pStyle")
            if pstyle is not None:
                style = str(pstyle.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val") or "")
        parts: list[str] = []
        for node in para.iter():
            if node.tag.endswith("}t"):
                parts.append(node.text or "")
        text = "".join(parts).strip()
        if not text:
            continue
        heading = 0
        low = style.lower()
        if "heading" in low:
            digits = "".join(ch for ch in low if ch.isdigit())
            heading = int(digits[:1]) if digits else 1
        if heading:
            lines.append(f"{'#' * heading} {text}")
        else:
            lines.append(text)
    text = "\n".join(lines).strip()
    return text, [{"page": None, "text": text}]


def chunk_text(text: str, max_chars: int = 1200, overlap: int = 160) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    current_section = ""
    current_page: int | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer
        joined = "\n".join(buffer).strip()
        if not joined:
            buffer = []
            return
        start = 0
        while start < len(joined):
            end = min(len(joined), start + max_chars)
            piece = joined[start:end].strip()
            if piece:
                chunks.append({"text": piece, "section": current_section, "page": current_page})
            if end >= len(joined):
                break
            start = max(0, end - overlap)
        buffer = []

    for raw in text.splitlines():
        line = raw.rstrip()
        page_m = re.match(r"^\[page\s+(\d+)\]$", line.strip(), re.I)
        if page_m:
            flush()
            current_page = int(page_m.group(1))
            continue
        head = _HEADING_RE.match(line)
        if head:
            flush()
            current_section = head.group(2).strip()
        buffer.append(line)
    flush()
    return chunks


def ingest_document(path: str | Path, tags: list[str] | None = None, store: KnowledgeStore | None = None, title: str | None = None) -> dict[str, Any]:
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(str(p))
    text, _pages = extract_text(p)
    quality = validate_extracted_text(text)
    if not quality["ok"]:
        reason = quality.pop("reason")
        raise ValueError(f"document text quality gate failed: {reason}; metrics={quality}")
    chunks = chunk_text(text)
    if not chunks:
        raise ValueError("document has no extractable text")

    kb_store = store or KnowledgeStore()
    meta = kb_store.add_document(p, title or p.stem, text, chunks, tags=tags)
    try:
        from .vector_index import rebuild_vector_index

        vector_result = rebuild_vector_index(kb_store)
        if not vector_result.get("ok"):
            raise RuntimeError(f"vector rebuild failed: {vector_result}")
    except Exception:
        kb_store.remove_by_doc_id(str(meta.get("doc_id") or ""))
        raise
    meta["vector_index"] = vector_result
    return meta
