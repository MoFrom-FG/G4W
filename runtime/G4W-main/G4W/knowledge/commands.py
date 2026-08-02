from __future__ import annotations

import json
from typing import Any

from .store import KnowledgeStore


def _extract_group(title: str) -> str:
    """按标题第一个 '-' 之前的前缀作为系列组名；无 '-' 则整体自成一册。"""
    idx = title.find("-")
    if idx > 0:
        prefix = title[:idx].strip()
        if prefix:
            return prefix
    return title


def _format_docs(docs: list[dict[str, Any]], expand: bool = False) -> str:
    if not docs:
        return "知识库暂无文档。"
    if expand:
        lines = ["知识库文档（完整编号）："]
        for i, doc in enumerate(docs, start=1):
            tags = doc.get("tags") or []
            tag_text = f" tags={','.join(tags)}" if tags else ""
            lines.append(f"{i}. {doc.get('title') or doc.get('doc_id')} ({doc.get('chunk_count', 0)} chunks){tag_text}")
        return "\n".join(lines)
    # 精简视图：按系列分组折叠
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for doc in docs:
        title = doc.get("title") or doc.get("doc_id") or ""
        group = _extract_group(title)
        if group not in groups:
            groups[group] = []
            order.append(group)
        groups[group].append(doc)
    lines = ["知识库文档：", "（提示：/kb list all 查看完整编号）"]
    for i, group in enumerate(order, start=1):
        gdocs = groups[group]
        if len(gdocs) == 1:
            d = gdocs[0]
            lines.append(f"{i}. {d.get('title') or d.get('doc_id')} ({d.get('chunk_count', 0)} chunks)")
        else:
            total = sum(d.get("chunk_count", 0) for d in gdocs)
            lines.append(f"{i}. {group}（{len(gdocs)}个文档 / 共{total} chunks）")
    return "\n".join(lines)


def handle_kb_command(args: str = "") -> str:
    args = (args or "").strip()
    store = KnowledgeStore()
    if not args or args in {"help", "status"}:
        return "可用命令：/kb list、/kb remove 编号、/kb rebuild"
    parts = args.split()
    action = parts[0].lower()
    if action == "list":
        expand = len(parts) > 1 and parts[1].lower() in {"all", "全部"}
        return _format_docs(store.list_documents(), expand=expand)
    if action == "remove":
        if len(parts) < 2:
            return "请提供 /kb list 中的编号，例如：/kb remove 1"
        doc_id = store.resolve_number(parts[1])
        if not doc_id:
            return f"未找到编号 {parts[1]}。请先运行 /kb list 刷新编号。"
        ok = store.remove_by_doc_id(doc_id)
        return "已移除知识库文档。" if ok else "移除失败：文档不存在。"
    if action == "rebuild":
        result = {"keyword": store.rebuild_keyword_index()}
        try:
            from .vector_index import kb_vector_enabled, rebuild_vector_index
        except Exception as exc:
            result["vector"] = "unavailable"
            result["vector_error"] = str(exc)
        else:
            if kb_vector_enabled():
                try:
                    result["vector"] = rebuild_vector_index(store)
                except Exception as exc:
                    result["vector_error"] = str(exc)
            else:
                result["vector"] = "disabled"
        return "知识库关键词索引已重建：" + json.dumps(result, ensure_ascii=False)
    return "未知 /kb 指令。可用命令：/kb list、/kb remove 编号、/kb rebuild"
