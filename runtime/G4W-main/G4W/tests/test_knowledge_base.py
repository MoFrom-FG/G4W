import json
from pathlib import Path

import pytest

from G4W.knowledge.commands import handle_kb_command
from G4W.knowledge.ingest import ingest_document
from G4W.knowledge.search import search_knowledge
from G4W.knowledge.store import KnowledgeStore


class _HandlerParent:
    tool_ledger = None


class _TestHandler:
    parent = _HandlerParent()
    G4W_controller = None
    G4W_sender_id = "test-sender"


from G4W.agents.handlers import ConductorHandler


def _handler():
    return ConductorHandler(_TestHandler())


def _payload(outcome):
    return outcome.data if hasattr(outcome, "data") else outcome


def test_knowledge_ingest_search_remove(tmp_path):
    doc = tmp_path / "manual.md"
    doc.write_text("# Install\nG4W knowledge base keeps documents separate from memory.", encoding="utf-8")
    store = KnowledgeStore(tmp_path / "kb")

    meta = ingest_document(doc, tags=["manual"], store=store)
    assert meta["doc_id"]
    assert "memory" not in str(store.root).lower()

    result = search_knowledge("separate memory", store=store)
    assert result["mode"] == "vector"
    assert result["hits"]
    hit = result["hits"][0]
    assert hit["title"] == "manual"
    assert hit["quote"]
    assert "source" in hit and "section" in hit and "page" in hit

    docs = store.list_documents()
    assert len(docs) == 1
    assert store.resolve_number("1") == meta["doc_id"]
    assert store.remove_by_doc_id(meta["doc_id"])
    assert store.list_documents() == []


def test_knowledge_search_tool_returns_structured_action_with_next_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("G4W_KNOWLEDGE_DATA_DIR", str(tmp_path / "knowledge"))
    doc = tmp_path / "manual.md"
    doc.write_text("# Base\nKnowledge answers must cite source quotes.", encoding="utf-8")
    ingest_document(doc, store=KnowledgeStore())

    outcome = _handler().do_G4W_knowledge_search({"query": "source quotes"}, None)
    payload = _payload(outcome)

    assert payload["ok"] is True
    assert payload["hit_count"] >= 1
    assert payload["recommended_action"] == "read_relevant_knowledge_chunk_then_answer"
    assert outcome.next_prompt
    assert "G4W_knowledge_read" in outcome.next_prompt


def test_knowledge_ingest_rejects_garbled_text(tmp_path):
    doc = tmp_path / "garbled.md"
    doc.write_text("Ã« Ôó ¶« Ñ¡ ¼¯ µÚ Ò» ¾í " * 20, encoding="utf-8")
    store = KnowledgeStore(tmp_path / "kb")

    with pytest.raises(ValueError, match="quality gate failed"):
        ingest_document(doc, store=store)

    assert store.list_documents() == []


def test_kb_command_list_rebuild_with_env(tmp_path, monkeypatch):
    monkeypatch.setenv("G4W_KNOWLEDGE_DATA_DIR", str(tmp_path / "knowledge"))
    assert "暂无文档" in handle_kb_command("list")
    assert "关键词索引已重建" in handle_kb_command("rebuild")
    assert "可用命令" in handle_kb_command("help")


def test_knowledge_root_ignores_conversation_runtime(tmp_path, monkeypatch):
    from G4W.knowledge import paths

    workspace = tmp_path / "G4W"
    runtime_dir = workspace / "runtime" / "G4W-data" / "memory" / "conversations" / "c1" / "runtime"
    runtime_dir.mkdir(parents=True)
    (workspace / "start_G4W_ga.bat").write_text("", encoding="utf-8")

    monkeypatch.delenv("G4W_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("G4W_DATA_DIR", raising=False)
    monkeypatch.delenv("G4W_STATE_DIR", raising=False)
    monkeypatch.delenv("G4W_KNOWLEDGE_DATA_DIR", raising=False)
    monkeypatch.setenv("G4W_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("BBS_CWD", str(workspace))

    expected = workspace / "runtime" / "G4W-data" / "knowledge"
    assert paths.data_root() == expected.resolve()


def test_knowledge_read_tool_reads_chunk_window(tmp_path, monkeypatch):
    monkeypatch.setenv("G4W_KNOWLEDGE_DATA_DIR", str(tmp_path / "knowledge"))
    store = KnowledgeStore()
    doc = store.add_document(
        tmp_path / "source.md",
        "Window Doc",
        "alpha beta gamma",
        [
            {"text": "first chunk", "page": 1, "section": "A"},
            {"text": "target chunk contains the answer", "page": 2, "section": "B"},
            {"text": "third chunk", "page": 3, "section": "C"},
        ],
    )

    outcome = _handler().do_G4W_knowledge_read(
        {"chunk_id": f"{doc['doc_id']}:0001", "window": 1, "max_chars": 1000}, None
    )
    payload = _payload(outcome)

    assert payload["ok"] is True
    assert payload["chunk_count"] == 3
    assert [chunk["text"] for chunk in payload["chunks"]] == [
        "first chunk",
        "target chunk contains the answer",
        "third chunk",
    ]
    assert "G4W_knowledge_read" in outcome.next_prompt


def test_knowledge_read_tool_reads_page(tmp_path, monkeypatch):
    monkeypatch.setenv("G4W_KNOWLEDGE_DATA_DIR", str(tmp_path / "knowledge"))
    store = KnowledgeStore()
    doc = store.add_document(
        tmp_path / "source.md",
        "Page Doc",
        "page content",
        [
            {"text": "page one", "page": 1},
            {"text": "page two target", "page": 2},
        ],
    )

    outcome = _handler().do_G4W_knowledge_read({"doc_id": doc["doc_id"], "page": 2}, None)
    payload = _payload(outcome)

    assert payload["ok"] is True
    assert payload["chunk_count"] == 1
    assert payload["chunks"][0]["text"] == "page two target"


def test_knowledge_read_tool_is_registered_after_search():
    tools_path = Path(__file__).resolve().parents[1] / "agents" / "conductor_tools.json"
    tools = json.loads(tools_path.read_text(encoding="utf-8"))
    names = [tool["function"]["name"] for tool in tools]

    assert "G4W_knowledge_read" in names
    assert names.index("G4W_knowledge_search") < names.index("G4W_knowledge_read")


def test_knowledge_ingest_tool_returns_required_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("G4W_KNOWLEDGE_DATA_DIR", str(tmp_path / "knowledge"))
    doc = tmp_path / "tool.md"
    doc.write_text("# Tool Import\nFormal KB ingest returns a document id.", encoding="utf-8")

    outcome = _handler().do_G4W_knowledge_ingest(
        {"path": str(doc), "title": "Tool Doc", "tags": ["tool", "kb"]}, None
    )
    payload = _payload(outcome)

    assert payload["ok"] is True
    assert payload["doc_id"]
    assert payload["title"] == "Tool Doc"
    assert payload["chunk_count"] >= 1
    assert payload["source_path"] == str(doc)
    assert payload["stored_path"]
    assert payload["tags"] == ["tool", "kb"]
    assert "导入成功" in outcome.next_prompt


def test_knowledge_ingest_tool_failure_does_not_claim_ingested(tmp_path, monkeypatch):
    monkeypatch.setenv("G4W_KNOWLEDGE_DATA_DIR", str(tmp_path / "knowledge"))

    outcome = _handler().do_G4W_knowledge_ingest({"path": str(tmp_path / "missing.md")}, None)
    payload = _payload(outcome)

    assert payload["ok"] is False
    assert "不得回复已收进知识库" in outcome.next_prompt


def test_knowledge_ingest_tool_rejects_garbled_text(tmp_path, monkeypatch):
    monkeypatch.setenv("G4W_KNOWLEDGE_DATA_DIR", str(tmp_path / "knowledge"))
    doc = tmp_path / "garbled.md"
    doc.write_text("Ã« Ôó ¶« Ñ¡ ¼¯ µÚ Ò» ¾í " * 20, encoding="utf-8")

    outcome = _handler().do_G4W_knowledge_ingest({"path": str(doc)}, None)
    payload = _payload(outcome)

    assert payload["ok"] is False
    assert "quality gate failed" in payload["error"]
    assert "不得回复已收进知识库" in outcome.next_prompt


def test_knowledge_remove_tool_accepts_number(tmp_path, monkeypatch):
    monkeypatch.setenv("G4W_KNOWLEDGE_DATA_DIR", str(tmp_path / "knowledge"))
    doc = tmp_path / "numbered.md"
    doc.write_text("Numbered delete target", encoding="utf-8")
    meta = ingest_document(doc, store=KnowledgeStore())
    KnowledgeStore().list_documents()

    outcome = _handler().do_G4W_knowledge_remove({"number": "1"}, None)
    payload = _payload(outcome)

    assert payload["ok"] is True
    assert payload["doc_id"] == meta["doc_id"]
    assert payload["title"] == meta["title"]
    assert KnowledgeStore().list_documents() == []
    assert "删除成功" in outcome.next_prompt


def test_knowledge_remove_tool_not_found_does_not_claim_removed(tmp_path, monkeypatch):
    monkeypatch.setenv("G4W_KNOWLEDGE_DATA_DIR", str(tmp_path / "knowledge"))

    outcome = _handler().do_G4W_knowledge_remove({"doc_id": "missing"}, None)
    payload = _payload(outcome)

    assert payload["ok"] is False
    assert "不得回复已移除" in outcome.next_prompt
