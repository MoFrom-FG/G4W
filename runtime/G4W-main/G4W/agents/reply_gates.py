"""Outbound / memory assertion gates for G4W Conductor.

Design (structure + evidence binding + speech-act intent):
- No business-keyword sole conditions (e.g. not ``if "找到了" in text``).
- archive / text_preview / search hits are clue_only until file_read verifies User lines.
- Structural leak uses transcript *shape*, not a ban-list of topic words.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable


# --- Speech-act / structure patterns (role & protocol morphology, not topic keywords) ---

_ROLE_TOKEN = r"(?:user|assistant|User|Assistant|主人|助手|用户|人类|Human|System|system)"
# Timestamp + role on same line, several bracket / bare styles.
_TS_ROLE_LINE = re.compile(
    rf"(?m)^[ \t]*[【\[]?"
    rf"(?:"
    rf"\d{{1,2}}[-/月]\d{{1,2}}(?:\s+\d{{1,2}}[:：]\d{{2}})?|"
    rf"20\d{{2}}[-/]\d{{1,2}}[-/]\d{{1,2}}(?:[ T]\d{{1,2}}[:：]\d{{2}}(?::\d{{2}})?)?"
    rf")"
    rf"[\]】]?\s*[\[【(（]?\s*{_ROLE_TOKEN}\s*[\]】)）]?"
)
_BARE_ROLE_LINE = re.compile(rf"(?m)^[ \t]*{_ROLE_TOKEN}\s*[:：]")
_INTERNAL_PROTOCOL = re.compile(
    r"(?im)(?:LLM Running\s*\(Turn|"
    r"\[ROUND END\]|"
    r"^\s*🛠️|"
    r"长回复已归档|"
    r"G4W_final_reply)"
)
_ARCHIVE_PATH_SHAPE = re.compile(
    r"(?i)(?:"
    r"assistant[/\\-]replies|"
    r"history-archive|"
    r"runtime[/\\]tool-results|"
    r"[A-Za-z]:\\[^\s\n]*assistant[^\s\n]*|"
    r"/users?/[^ \n]+/(?:assistant|transcripts|runtime)"
    r")"
)
_ABSOLUTE_WIN_PATH = re.compile(r"(?i)[A-Za-z]:\\[^\s\n]{8,}")
# Quoted spans: 「…」 “…” '…' "…" （short）
_QUOTE_SPAN = re.compile(
    r"「([^」]{2,240})」|"
    r"“([^”]{2,240})”|"
    r"'([^']{4,240})'|"
    r'"([^"]{4,240})"'
)
# Memory-assertion speech act: structural claim about past retrieval, not bare keywords alone.
_MEMORY_ASSERT_CUES = re.compile(
    r"(?is)(?:"
    r"(?:找到了|查到了|翻到了|定位到了|检索到了|记起来了|回忆起).{0,40}(?:"
    r"你|您|当时|那次|原话|说过|提到|记录|transcript|对话)"
    r"|"
    r"(?:你|您).{0,12}(?:当时|那次|曾经).{0,30}(?:说|讲|提|写).{0,20}[「\"“]"
    r"|"
    r"(?:原文|原话|记录里).{0,20}(?:是|写的是|如下)"
    r"|"
    r"(?:根据|依据).{0,16}(?:历史|聊天|对话|transcript|记忆).{0,20}(?:记录|原文)"
    r")"
)
_COMMIT_PROMISE_CUES = re.compile(
    r"(?is)(?:"
    r"(?:已经|已).{0,6}(?:记住|记下|写入|存好|记到).{0,20}(?:记忆|长期|L2|日记|时间线)?"
    r"|"
    r"(?:帮你|为你).{0,8}(?:记住|记下了|写进)"
    r"|"
    r"(?:记住了|记下了|写进记忆了|已存档)"
    r")"
)
_META_FORMAT_TEACH = re.compile(
    r"(?is)(?:系统内部|内部格式|日志格式|turn\s*分隔|示例格式|格式长这样|不是\s*turn)"
)

_USER_LINE_IN_TRANSCRIPT = re.compile(
    rf"(?im)^[ \t]*[【\[]?"
    rf"(?:"
    rf"\d{{1,2}}[-/月]\d{{1,2}}(?:\s+\d{{1,2}}[:：]\d{{2}})?|"
    rf"20\d{{2}}[-/]\d{{1,2}}[-/]\d{{1,2}}(?:[ T]\d{{1,2}}[:：]\d{{2}}(?::\d{{2}})?)?"
    rf")?"
    rf"[\]】]?\s*[\[【(（]?\s*(?:user|User|主人|用户|人类|Human)\s*[\]】)）]?\s*[:：]?\s*(.*)$"
)
_USER_CONTENT_LOOSE = re.compile(
    r"(?im)^(?:user|User|主人|用户)\s*[:：]\s*(.+)$"
)

_TRANSCRIPT_PATH_HINT = re.compile(
    r"(?i)(?:transcripts?[/\\]|history[/\\].*\.md$|users[/\\][^/\\]+[/\\].*\.md$|\.md$)"
)


def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


def _extract_quotes(text: str) -> list[str]:
    out: list[str] = []
    for m in _QUOTE_SPAN.finditer(text or ""):
        for g in m.groups():
            if g and g.strip():
                out.append(g.strip())
    return out


def extract_user_lines_from_tool_text(text: str) -> list[str]:
    """Pull User-side lines from file_read / tool body (structure, not topic)."""
    lines: list[str] = []
    raw = str(text or "")
    for m in _USER_LINE_IN_TRANSCRIPT.finditer(raw):
        body = (m.group(1) or "").strip()
        if body:
            lines.append(body)
    for m in _USER_CONTENT_LOOSE.finditer(raw):
        body = (m.group(1) or "").strip()
        if body and body not in lines:
            lines.append(body)
    # Also accept plain bullet / quoted user content blocks after a User header line.
    role_only = re.compile(rf"(?im)^[ \t]*[【\[]?.*{_ROLE_TOKEN}.*[\]】]?\s*$")
    current_user = False
    buf: list[str] = []
    for line in raw.splitlines():
        if re.search(r"(?i)user|主人|用户|Human", line) and (
            _TS_ROLE_LINE.search(line) or _BARE_ROLE_LINE.search(line) or role_only.match(line)
        ):
            if re.search(r"(?i)assistant|助手|system", line) and not re.search(
                r"(?i)user|主人|用户|Human", line
            ):
                current_user = False
                continue
            current_user = True
            # inline body after role on same line already handled; keep following lines
            continue
        if current_user:
            if _TS_ROLE_LINE.search(line) or _BARE_ROLE_LINE.search(line):
                if re.search(r"(?i)assistant|助手|system", line):
                    current_user = False
                continue
            s = line.strip()
            if s:
                buf.append(s)
                lines.append(s)
    return lines


def path_looks_like_document(path: str) -> bool:
    p = str(path or "").replace("\\", "/").lower()
    if not p:
        return False
    return bool(
        "/knowledge/" in p
        or p.startswith("knowledge/")
        or "/documents/" in p
        or p.startswith("documents/")
        or "/docs/" in p
        or p.startswith("docs/")
    )


def path_looks_like_transcript(path: str) -> bool:
    p = str(path or "").replace("\\", "/").lower()
    if not p:
        return False
    if path_looks_like_document(p):
        return False
    if "assistant-replies" in p or "history-archive" in p or "tool-results" in p:
        return False
    if "transcript" in p:
        return True
    if "/users/" in p and p.endswith(".md"):
        return True
    if re.search(r"20\d{2}[-_/]\d{1,2}[-_/]\d{1,2}", p) and p.endswith(".md"):
        return True
    return bool(_TRANSCRIPT_PATH_HINT.search(p) and "sop" not in p)


@dataclass
class EvidenceRecord:
    tool: str
    path: str = ""
    tier: str = "clue_only"  # clue_only | verified_user
    user_lines: list[str] = field(default_factory=list)
    raw_excerpt: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvidenceLedger:
    """Per-round evidence book: search/archive = clue; verified User lines from file_read."""

    round_id: str = ""
    records: list[EvidenceRecord] = field(default_factory=list)
    writes: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, int] = field(default_factory=dict)

    def _bump(self, key: str, n: int = 1) -> None:
        self.metrics[key] = int(self.metrics.get(key, 0) or 0) + n

    def reset(self, round_id: str = "") -> None:
        self.round_id = round_id or self.round_id
        self.records.clear()
        self.writes.clear()

    def add_clue(self, tool: str, text: str = "", path: str = "", **meta: Any) -> None:
        self.records.append(
            EvidenceRecord(
                tool=tool,
                path=path,
                tier="clue_only",
                raw_excerpt=(text or "")[:2000],
                meta=dict(meta),
            )
        )
        self._bump("clue_adds")

    def add_file_read(self, path: str, text: str, archived: bool = False) -> EvidenceRecord:
        """file_read: verified only when body yields User lines and is not pure archive meta."""
        path = str(path or "")
        body = str(text or "")
        user_lines = extract_user_lines_from_tool_text(body)
        looks_tx = path_looks_like_transcript(path)
        pl = path.replace("\\", "/").lower()
        archive_like = bool(
            archived
            or "assistant-replies" in pl
            or "history-archive" in pl
            or "tool-results" in pl
            or "/archives/" in pl
        )
        # Overflow / assistant-reply archives are never quote-verification sources.
        if archive_like:
            rec = EvidenceRecord(
                tool="file_read",
                path=path,
                tier="clue_only",
                user_lines=[],
                raw_excerpt=body[:2000],
                meta={"archived": True, "archive_like": True},
            )
            self.records.append(rec)
            self._bump("file_read_clue")
            return rec
        if user_lines and looks_tx:
            tier = "verified_user"
            self._bump("file_read_verified")
        elif user_lines and not looks_tx:
            # Non-transcript file that still has User: lines (e.g. notes) verifies document text,
            # not chat-memory provenance.
            tier = "verified_document"
            self._bump("file_read_document_verified")
        elif looks_tx and body.strip():
            # Transcript-shaped file without clear role markup: weak verified pool from lines.
            tier = "verified_user"
            user_lines = [ln.strip() for ln in body.splitlines() if ln.strip()][:80]
            self._bump("file_read_verified_loose")
        elif body.strip():
            tier = "verified_document"
            self._bump("file_read_document_verified")
        else:
            tier = "clue_only"
            self._bump("file_read_clue")
        rec = EvidenceRecord(
            tool="file_read",
            path=path,
            tier=tier,
            user_lines=user_lines,
            raw_excerpt=body[:4000],
            meta={"archived": archived},
        )
        self.records.append(rec)
        return rec

    def add_memory_search(self, payload: dict | str) -> None:
        """G4W_memory_search hits are always clue_only (preview/text not fact)."""
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, default=str)
        paths: list[str] = []
        if isinstance(payload, dict):
            for section in ("hybrid", "vector"):
                block = payload.get(section) or {}
                hits = block.get("hits") if isinstance(block, dict) else None
                if not hits:
                    continue
                for h in hits:
                    if isinstance(h, dict):
                        paths.append(str(h.get("source_path") or h.get("path") or h.get("item_id") or ""))
        self.records.append(
            EvidenceRecord(
                tool="G4W_memory_search",
                path=";".join(p for p in paths if p)[:500],
                tier="clue_only",
                raw_excerpt=text[:2000],
                meta={"paths": paths[:20]},
            )
        )
        self._bump("search_clue")

    def add_knowledge_search(self, payload: dict | str) -> None:
        """Knowledge hits verify document quotes, but never verify user chat memory."""
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, default=str)
        quotes: list[str] = []
        sources: list[str] = []
        if isinstance(payload, dict):
            hits = payload.get("hits") or []
            if isinstance(hits, list):
                for h in hits:
                    if not isinstance(h, dict):
                        continue
                    quote = str(h.get("quote") or "").strip()
                    if quote:
                        quotes.append(quote)
                    source = str(h.get("source") or h.get("title") or h.get("doc_id") or "").strip()
                    if source:
                        sources.append(source)
        self.records.append(
            EvidenceRecord(
                tool="G4W_knowledge_search",
                path=";".join(sources)[:500],
                tier="verified_knowledge" if quotes else "clue_only",
                user_lines=[],
                raw_excerpt="\n".join(quotes)[:4000] or text[:2000],
                meta={"sources": sources[:20], "quote_count": len(quotes)},
            )
        )
        self._bump("knowledge_verified" if quotes else "knowledge_clue")

    def note_write(self, kind: str, content: str, ok: bool, path: str = "") -> None:
        self.writes.append(
            {"kind": kind, "content": (content or "")[:500], "ok": bool(ok), "path": path}
        )
        self._bump("writes_ok" if ok else "writes_fail")

    def verified_user_corpus(self) -> str:
        parts: list[str] = []
        for rec in self.records:
            if rec.tier != "verified_user":
                continue
            parts.extend(rec.user_lines)
            if rec.raw_excerpt and not rec.user_lines:
                parts.append(rec.raw_excerpt)
        return "\n".join(parts)

    def verified_document_corpus(self) -> str:
        parts: list[str] = []
        for rec in self.records:
            if rec.tier not in {"verified_document", "verified_knowledge"}:
                continue
            if rec.raw_excerpt:
                parts.append(rec.raw_excerpt)
        return "\n".join(parts)

    def has_verified_user(self) -> bool:
        return any(r.tier == "verified_user" and (r.user_lines or r.raw_excerpt) for r in self.records)

    def has_verified_document(self) -> bool:
        return any(r.tier in {"verified_document", "verified_knowledge"} and r.raw_excerpt for r in self.records)

    def has_any_clue(self) -> bool:
        return bool(self.records)

    def successful_write_for(self, snippet: str = "") -> bool:
        if not self.writes:
            return False
        ok_writes = [w for w in self.writes if w.get("ok")]
        if not ok_writes:
            return False
        if not snippet:
            return True
        n = _norm_ws(snippet)
        for w in ok_writes:
            if n and _norm_ws(str(w.get("content") or "")) and (
                n in _norm_ws(str(w.get("content") or ""))
                or _norm_ws(str(w.get("content") or "")) in n
            ):
                return True
        # Promise without binding to specific content: any successful write this round counts.
        return True


@dataclass
class SpeechActResult:
    memory_assertion: bool = False
    commit_promise: bool = False
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)


def tag_speech_acts(text: str) -> SpeechActResult:
    """Weak structural speech-act tagger (features, not sole keyword equality)."""
    t = str(text or "")
    reasons: list[str] = []
    mem = bool(_MEMORY_ASSERT_CUES.search(t))
    # Extra structure: has quote + past-reference morphology nearby
    quotes = _extract_quotes(t)
    if quotes and re.search(r"(?is)(?:当时|那次|之前|原话|记录|说过|提到)", t):
        mem = True
        reasons.append("quote+past_ref")
    if mem:
        reasons.append("memory_assert_cue")
    commit = bool(_COMMIT_PROMISE_CUES.search(t))
    if commit:
        reasons.append("commit_promise_cue")
    conf = 0.0
    if mem:
        conf += 0.55
    if commit:
        conf += 0.45
    if quotes and mem:
        conf = min(1.0, conf + 0.15)
    return SpeechActResult(
        memory_assertion=mem,
        commit_promise=commit,
        confidence=conf,
        reasons=reasons,
    )


@dataclass
class StructuralLeakResult:
    score: float
    action: str  # allow | strip | reject
    reasons: list[str] = field(default_factory=list)
    stripped_text: str = ""


def detect_structural_leak(text: str, user_asked_format: bool = False) -> StructuralLeakResult:
    """Score fake multi-turn / internal archive shapes in outbound text."""
    raw = str(text or "")
    if not raw.strip():
        return StructuralLeakResult(0.0, "allow", [], raw)

    score = 0.0
    reasons: list[str] = []

    ts_hits = list(_TS_ROLE_LINE.finditer(raw))
    bare_hits = list(_BARE_ROLE_LINE.finditer(raw))
    role_block_count = len(ts_hits) + len(bare_hits)
    if role_block_count >= 2:
        score += 0.55
        reasons.append(f"multi_role_blocks:{role_block_count}")
    elif role_block_count == 1:
        score += 0.15
        reasons.append("single_role_block")

    proto = list(_INTERNAL_PROTOCOL.finditer(raw))
    if proto:
        score += 0.35 * min(2, len(proto))
        reasons.append(f"internal_protocol:{len(proto)}")

    arch = list(_ARCHIVE_PATH_SHAPE.finditer(raw))
    if arch:
        score += 0.4
        reasons.append(f"archive_path_shape:{len(arch)}")

    abs_paths = list(_ABSOLUTE_WIN_PATH.finditer(raw))
    # Only penalize when combined with archive-ish or multi path dump
    if len(abs_paths) >= 2:
        score += 0.25
        reasons.append("multi_abs_paths")
    elif abs_paths and arch:
        score += 0.15
        reasons.append("abs+archive")

    # Teaching / single example demotion
    if user_asked_format or _META_FORMAT_TEACH.search(raw):
        if role_block_count <= 1 and not proto:
            score = max(0.0, score - 0.45)
            reasons.append("format_teach_demote")
        else:
            score = max(0.0, score - 0.15)
            reasons.append("format_teach_partial")

    # Strip pass: remove high-risk lines, keep natural language.
    stripped_lines: list[str] = []
    for line in raw.splitlines():
        drop = bool(
            _TS_ROLE_LINE.search(line)
            or _INTERNAL_PROTOCOL.search(line)
            or _ARCHIVE_PATH_SHAPE.search(line)
        )
        if drop and role_block_count >= 2:
            continue
        if _ARCHIVE_PATH_SHAPE.search(line) or (
            _ABSOLUTE_WIN_PATH.search(line) and re.search(r"(?i)assistant|transcript|runtime|archive", line)
        ):
            # Drop internal coordinate lines always from strip candidate
            continue
        if _INTERNAL_PROTOCOL.search(line):
            continue
        stripped_lines.append(line)
    stripped = "\n".join(stripped_lines).strip()
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)

    if score >= 0.75:
        action = "reject"
    elif score >= 0.35:
        action = "strip"
    else:
        action = "allow"
        stripped = raw

    if action == "strip" and (not stripped or len(stripped) < 8):
        action = "reject"

    return StructuralLeakResult(score=score, action=action, reasons=reasons, stripped_text=stripped)


@dataclass
class QuoteGateResult:
    ok: bool
    missing: list[str] = field(default_factory=list)
    checked: int = 0


def quote_inclusion_check(text: str, ledger: EvidenceLedger, *, corpus: str | None = None) -> QuoteGateResult:
    """Every explicit quote in an assertion must be present in the chosen verified corpus."""
    quotes = _extract_quotes(text)
    if not quotes:
        return QuoteGateResult(ok=True, missing=[], checked=0)
    source_corpus = ledger.verified_user_corpus() if corpus is None else corpus
    norm_corpus = _norm_ws(source_corpus)
    missing: list[str] = []
    for q in quotes:
        nq = _norm_ws(q)
        if len(nq) < 4:
            continue
        if not norm_corpus or nq not in norm_corpus:
            missing.append(q)
    return QuoteGateResult(ok=not missing, missing=missing, checked=len(quotes))


@dataclass
class FinalGateResult:
    ok: bool
    text: str
    action: str  # deliver | remediate | block_commit_language
    remediate_prompt: str = ""
    reasons: list[str] = field(default_factory=list)
    speech: SpeechActResult | None = None
    leak: StructuralLeakResult | None = None


_REMEDIATE_STRUCT = (
    "你的上一条草稿含伪造多 turn 或内部 archive/协议形态，禁止照抄日志。"
    "只用对用户可见的自然语言重答；证据只来自本轮 tool_result 中已核实的 User 原文。"
    "禁止自写时间戳角色行、LLM Running、ROUND END、assistant-replies 路径。"
)

_REMEDIATE_QUOTE = (
    "你在做记忆/原话断言，但引号内句子无法在本轮已 file_read 核实的 User 行中逐字找到。"
    "禁止编造原话。若只有 G4W_memory_search 的 preview/hit：先 file_read 对应 source_path，"
    "再按模板回复：日期 + 路径/锚点 + 原文引用 + 一句解释；缺证据则坦白未核实。"
)

_REMEDIATE_COMMIT = (
    "你承诺「已记住/已写入」，但本轮没有成功的记忆写入副作用。"
    "删掉已落盘承诺，改为：将在用户确认后写入 / 或先调用写入工具；不要假装已记住。"
)

_REMEDIATE_ASSERT_NO_EVIDENCE = (
    "你在断言已找到历史原话/记录，但本轮 EvidenceLedger 没有 verified_user（需 file_read transcript 类文件）。"
    "search hit / text_preview / archive 指针只是线索。先 file_read 再答，或明确说尚未核实。"
)

_DOCUMENT_ASSERT_CUES = re.compile(
    r"(?is)(?:知识库|文档|资料|书中|文件|file_read|knowledge|KB|原文如下|原文是|原文：|通过知识库|通过 file_read)"
)
_USER_HISTORY_ASSERT_CUES = re.compile(
    r"(?is)(?:你|您|主人|用户).{0,24}(?:当时|那次|曾经|说过|提到|原话|记录)|(?:历史记录|聊天记录|transcript|对话).{0,24}(?:你|您|用户|User)"
)


def _assertion_evidence_kind(text: str) -> str:
    if _USER_HISTORY_ASSERT_CUES.search(text or ""):
        return "user"
    if _DOCUMENT_ASSERT_CUES.search(text or ""):
        return "document"
    return "user"


def gate_final_reply(
    text: str,
    ledger: EvidenceLedger | None = None,
    *,
    user_message: str = "",
    allow_remediate: bool = True,
) -> FinalGateResult:
    """Compose structural + speech-act + evidence gates for user-visible final text."""
    ledger = ledger or EvidenceLedger()
    original = str(text or "")
    reasons: list[str] = []

    user_asked_format = bool(
        re.search(r"(?is)格式|archive|归档|transcript|turn\s*分隔|日志", user_message or "")
    )
    leak = detect_structural_leak(original, user_asked_format=user_asked_format)
    text = original
    if leak.action == "strip":
        text = leak.stripped_text
        reasons.append("struct_strip:" + ",".join(leak.reasons))
        ledger._bump("struct_strip")
    elif leak.action == "reject":
        ledger._bump("struct_reject")
        return FinalGateResult(
            ok=False,
            text="",
            action="remediate" if allow_remediate else "deliver",
            remediate_prompt=_REMEDIATE_STRUCT,
            reasons=leak.reasons + ["struct_reject"],
            leak=leak,
        )

    speech = tag_speech_acts(text)

    if speech.commit_promise and not ledger.successful_write_for():
        ledger._bump("commit_block")
        # Soft strip commit sentences if we can still deliver other content
        cleaned = _COMMIT_PROMISE_CUES.sub("", text)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        if speech.memory_assertion or len(cleaned) < 8:
            return FinalGateResult(
                ok=False,
                text=cleaned,
                action="remediate" if allow_remediate else "block_commit_language",
                remediate_prompt=_REMEDIATE_COMMIT,
                reasons=reasons + ["commit_without_write"],
                speech=speech,
                leak=leak,
            )
        text = cleaned
        reasons.append("commit_language_stripped")

    if speech.memory_assertion:
        evidence_kind = _assertion_evidence_kind(text)
        if evidence_kind == "document":
            if not ledger.has_verified_document():
                ledger._bump("assert_no_verified_document")
                return FinalGateResult(
                    ok=False,
                    text=text,
                    action="remediate" if allow_remediate else "deliver",
                    remediate_prompt="你在断言知识库/文档原文，但本轮 EvidenceLedger 没有 verified_document/verified_knowledge。先使用知识库检索或 file_read 核实文档原文，再引用。",
                    reasons=reasons + ["document_assert_without_verified_document"],
                    speech=speech,
                    leak=leak,
                )
            q = quote_inclusion_check(text, ledger, corpus=ledger.verified_document_corpus())
        else:
            if not ledger.has_verified_user():
                ledger._bump("assert_no_verified")
                return FinalGateResult(
                    ok=False,
                    text=text,
                    action="remediate" if allow_remediate else "deliver",
                    remediate_prompt=_REMEDIATE_ASSERT_NO_EVIDENCE,
                    reasons=reasons + ["memory_assert_without_verified_user"],
                    speech=speech,
                    leak=leak,
                )
            q = quote_inclusion_check(text, ledger)
        if not q.ok:
            ledger._bump("quote_fail")
            return FinalGateResult(
                ok=False,
                text=text,
                action="remediate" if allow_remediate else "deliver",
                remediate_prompt=_REMEDIATE_QUOTE
                + (f" 缺失引文样例：{q.missing[0][:80]}" if q.missing else ""),
                reasons=reasons + [f"quote_not_in_verified:{len(q.missing)}"],
                speech=speech,
                leak=leak,
            )
        ledger._bump("quote_ok")

    return FinalGateResult(
        ok=True,
        text=text,
        action="deliver",
        reasons=reasons or ["pass"],
        speech=speech,
        leak=leak,
    )


def sanitize_outbound_reply(
    text: str,
    ledger: EvidenceLedger | None = None,
    *,
    user_message: str = "",
) -> str:
    """Best-effort outbound sanitize used at controller delivery (no remediate loop)."""
    result = gate_final_reply(
        text,
        ledger,
        user_message=user_message,
        allow_remediate=False,
    )
    if result.action == "deliver" and result.text.strip():
        return result.text
    if result.leak and result.leak.action == "strip" and result.leak.stripped_text.strip():
        return result.leak.stripped_text
    # Last resort: structural strip only
    leak = detect_structural_leak(str(text or ""), user_asked_format=False)
    if leak.stripped_text.strip():
        return leak.stripped_text
    # Fail-closed short notice for high structural pollution
    if result.leak and result.leak.score >= 0.75:
        return "这轮回复草稿含内部日志形态，已拦截。我重新用自然语言说明结论。"
    return result.text or str(text or "")


def gate_remember_content(content: str) -> tuple[bool, str]:
    """Block operational pollution written as user memory (structure shapes)."""
    raw = str(content or "").strip()
    if not raw:
        return False, "memory content is empty"
    if _INTERNAL_PROTOCOL.search(raw) or _TS_ROLE_LINE.search(raw):
        return False, "remember blocked: content looks like transcript/protocol dump, not a memory fact"
    if _BARE_ROLE_LINE.search(raw) and len(raw.splitlines()) >= 2:
        return False, "remember blocked: multi-role dump is not a memory fact"
    leak = detect_structural_leak(raw)
    if leak.score >= 0.4:
        return False, "remember blocked: content looks like transcript/protocol dump, not a memory fact"
    if _ARCHIVE_PATH_SHAPE.search(raw) and len(raw) > 80:
        return False, "remember blocked: internal archive coordinates are not user memory"
    return True, ""


# Public helpers for handlers wiring
def ensure_ledger(handler: Any) -> EvidenceLedger:
    ledger = getattr(handler, "_evidence_ledger", None)
    if not isinstance(ledger, EvidenceLedger):
        ledger = EvidenceLedger()
        handler._evidence_ledger = ledger
    round_id = ""
    try:
        round_id = str(handler._current_round_id())
    except Exception:
        round_id = str(getattr(handler, "_fallback_round_id", "") or "")
    if round_id and ledger.round_id != round_id:
        ledger.reset(round_id)
        ledger.round_id = round_id
    return ledger
