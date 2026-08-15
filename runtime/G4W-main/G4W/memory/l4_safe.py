"""Safe G4W L4 memory maintenance.

The only write targets are:
- summaries/user_only
- summaries/chunks
- summaries/history_insight
- summaries/.backups

Raw transcripts, assistant-replies, users, and operational are read-only/denylisted.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import random
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


DEFAULT_USER_ID = "default"
DEFAULT_MIN_NEW_USER_MESSAGES = 30
DEFAULT_MIN_NEW_TRANSCRIPT_FILES = 2
DEFAULT_COOLDOWN_HOURS = 4.0
DEFAULT_SAMPLE_RATE = 0.2
MAX_MEMORY_BRIEF_CHARS = 600
REQUIRED_CANDIDATE_FILES = [
    "active_knowledge.candidate.json",
    "emotion_events.candidate.json",
    "incremental_markers.candidate.json",
    "README.candidate.md",
    "memory_brief.candidate.md",
    "proposed_updates.candidate.md",
    "user_profile.candidate.md",  # 可选:缺失/校验失败时跳过画像更新,不阻塞 finalize
    "subagent_report.md",
]

TRANSCRIPT_MESSAGE_RE = re.compile(
    r"^\[(?P<timestamp>[^\]]+)\]\s+(?P<speaker>User|Assistant):\s*\n(?P<body>.*?)(?=^\[[^\]]+\]\s+(?:User|Assistant):\s*\n|\Z)",
    re.MULTILINE | re.DOTALL,
)
DATE_FILE_RE = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})\.md$")

RULE_KEYWORDS = ("以后", "默认", "记住", "必须", "不要", "规则", "流程就这么来", "确认", "SOP", "sop")
PREFERENCE_KEYWORDS = ("喜欢", "偏好", "不喜欢", "讨厌", "叫我", "称呼")
PROJECT_KEYWORDS = ("项目", "比赛", "计划", "研究", "实现", "开发", "调试", "TODO", "todo")
EMOTION_KEYWORDS = {
    "积极/满意": ("开心", "高兴", "棒", "很好", "成功", "看到了", "期待"),
    "疲惫/睡眠": ("累", "困", "通宵", "睡", "醒", "晚安", "休息"),
    "焦虑/压力": ("焦虑", "担心", "压力", "烦", "崩", "不舒服"),
    "亲密/感激": ("抱抱", "想你", "谢谢", "爱你", "猫猫"),
}
ACTIVITY_KEYWORDS = {
    "饮食": ("吃", "饭", "早餐", "午饭", "晚饭", "夜宵", "外卖"),
    "睡眠": ("睡", "醒", "起床", "晚安", "通宵", "困"),
    "开发/调试": ("代码", "开发", "调试", "修改", "脚本", "仓库"),
    "项目/研究": ("项目", "研究", "计划", "比赛", "TODO", "todo"),
    "推送/通知": ("推送", "Meow", "meow", "通知"),
    "手机/小艺": ("小艺", "手机", "闹钟", "备忘录", "位置"),
}


@dataclass(frozen=True)
class UserMessage:
    timestamp: str
    body: str
    source_transcript: str
    date: str


def safe_segment(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "_", (value or "").strip())
    normalized = re.sub(r"^_+|_+$", "", normalized)
    return (normalized[:120] or value or "").strip()


def workspace_root(explicit: str = "") -> Path:
    for candidate in (explicit, os.environ.get("G4W_WORKSPACE_ROOT", ""), str(Path(__file__).resolve().parents[3])):
        if candidate:
            path = Path(candidate).resolve()
            if path.is_dir():
                return path
    raise RuntimeError("Cannot resolve workspace root")


def wechat_root(root: Path) -> Path:
    return root / "G4W-data" / "memory"


def conversation_root(root: Path, user_id: str) -> Path:
    return wechat_root(root) / "conversations" / safe_segment(user_id)


def summaries_root(root: Path, user_id: str) -> Path:
    return conversation_root(root, user_id) / "summaries"


def transcripts_root(root: Path, user_id: str) -> Path:
    return conversation_root(root, user_id) / "transcripts"


def history_root(root: Path, user_id: str) -> Path:
    return summaries_root(root, user_id) / "history_insight"


def user_only_path(root: Path, user_id: str, date_str: str) -> Path:
    year, month, _ = date_str.split("-")
    return summaries_root(root, user_id) / "user_only" / year / month / f"{date_str}.md"


def chunk_path(root: Path, user_id: str, date_str: str) -> Path:
    year, month, _ = date_str.split("-")
    return summaries_root(root, user_id) / "chunks" / year / month / f"{date_str}.md"


def active_knowledge_path(root: Path, user_id: str) -> Path:
    return history_root(root, user_id) / "active_knowledge.json"


def emotion_events_path(root: Path, user_id: str) -> Path:
    return history_root(root, user_id) / "emotion_events.json"


def markers_path(root: Path, user_id: str) -> Path:
    return history_root(root, user_id) / "incremental_markers.json"


def memory_brief_path(root: Path, user_id: str) -> Path:
    return history_root(root, user_id) / "memory_brief.md"


def user_profile_md_path(root: Path, user_id: str) -> Path:
    return history_root(root, user_id) / "user_profile.md"


def user_profile_draft_path(root: Path, user_id: str) -> Path:
    return history_root(root, user_id) / "user_profile.draft.md"


def proposed_updates_path(root: Path, user_id: str) -> Path:
    return history_root(root, user_id) / "proposed_updates.md"


def readme_path(root: Path, user_id: str) -> Path:
    return history_root(root, user_id) / "README.md"


def runs_root(root: Path, user_id: str) -> Path:
    return history_root(root, user_id) / "runs"


def run_root(root: Path, user_id: str, run_id: str) -> Path:
    safe_run_id = safe_segment(run_id)
    return runs_root(root, user_id) / safe_run_id


def run_manifest_path(root: Path, user_id: str, run_id: str) -> Path:
    return run_root(root, user_id, run_id) / "run_manifest.json"


def subagent_output_dir(root: Path, user_id: str, run_id: str) -> Path:
    return run_root(root, user_id, run_id) / "subagent_output"


def lock_path(root: Path, user_id: str) -> Path:
    return summaries_root(root, user_id) / ".l4compress.lock"


def rel(path: Path, base: Path) -> str:
    try:
        return str(path.resolve().relative_to(base.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def snippet_hash(text: str) -> str:
    return sha256_text(text.strip())[:16]


def ensure_allowed_write(path: Path) -> None:
    resolved = path.resolve()
    parts = set(resolved.parts)
    if "transcripts" in parts or "assistant-replies" in parts:
        raise RuntimeError(f"Refusing to write source directory: {resolved}")
    if "memory" in parts and ("users" in parts or "operational" in parts):
        raise RuntimeError(f"Refusing to write linked memory file: {resolved}")
    if resolved.name == ".l4compress.lock":
        return
    if not any(part in ("user_only", "chunks", "history_insight", ".backups") for part in resolved.parts):
        raise RuntimeError(f"Path is outside L4 allowlist: {resolved}")


def atomic_write_text(path: Path, content: str) -> None:
    ensure_allowed_write(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    json.loads(text)
    atomic_write_text(path, text + "\n")


def load_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    if not path.is_file():
        return json.loads(json.dumps(default, ensure_ascii=False))
    return json.loads(path.read_text(encoding="utf-8"))


def preflight(root: Path, user_id: str) -> None:
    if root.name == "GenericAgent-main" and root.parent.name == "GenericAgent-main":
        raise RuntimeError(f"Refusing nested workspace root: {root}")
    transcripts = transcripts_root(root, user_id).resolve()
    summaries = summaries_root(root, user_id).resolve()
    chunks = summaries / "chunks"
    if not transcripts.is_dir():
        raise RuntimeError(f"transcripts directory not found: {transcripts}")
    inner_bad = root / "GenericAgent-main" / "G4W-data" / "memory"
    if str(conversation_root(root, user_id).resolve()).lower().startswith(str(inner_bad.resolve()).lower()):
        raise RuntimeError(f"Refusing inner GenericAgent-main G4W-data path: {conversation_root(root, user_id)}")
    if not chunks.is_dir():
        raise RuntimeError(f"summaries/chunks directory not found: {chunks}")
    if summaries not in chunks.parents:
        raise RuntimeError(f"chunks is not under summaries: {chunks}")


@contextlib.contextmanager
def acquire_lock(root: Path, user_id: str, dry_run: bool = False):
    if dry_run:
        yield
        return
    path = lock_path(root, user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError(f"L4 maintenance already running: {path}")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(now_iso())
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def transcript_files(root: Path, user_id: str, start: str = "", end: str = "") -> List[Path]:
    files = []
    for path in transcripts_root(root, user_id).rglob("*.md"):
        match = DATE_FILE_RE.search(path.name)
        if not match:
            continue
        date_str = match.group("date")
        if start and date_str < start:
            continue
        if end and date_str > end:
            continue
        files.append(path)
    return sorted(files)


def parse_user_messages(path: Path, source_base: Path) -> List[UserMessage]:
    text = path.read_text(encoding="utf-8", errors="replace")
    match_date = DATE_FILE_RE.search(path.name)
    date_str = match_date.group("date") if match_date else ""
    messages = []
    for match in TRANSCRIPT_MESSAGE_RE.finditer(text):
        if match.group("speaker") != "User":
            continue
        body = re.sub(
            r"\n?<!--\s*G4W:(?:message_subtype|message_id|parent_round_id)\s*=.*?-->",
            "",
            match.group("body"),
            flags=re.I,
        ).strip()
        if body:
            messages.append(UserMessage(match.group("timestamp").strip(), body, rel(path, source_base), date_str))
    return messages


def collect_user_messages(root: Path, user_id: str, start: str = "", end: str = "") -> List[UserMessage]:
    messages: List[UserMessage] = []
    for path in transcript_files(root, user_id, start=start, end=end):
        messages.extend(parse_user_messages(path, root))
    return sorted(messages, key=lambda item: (item.timestamp, item.source_transcript))


def messages_after(messages: Iterable[UserMessage], last_processed: Optional[str]) -> List[UserMessage]:
    if not last_processed:
        return []
    return [message for message in messages if message.timestamp > last_processed]


def group_by_date(messages: Iterable[UserMessage]) -> Dict[str, List[UserMessage]]:
    grouped: Dict[str, List[UserMessage]] = {}
    for message in messages:
        grouped.setdefault(message.date, []).append(message)
    return dict(sorted(grouped.items()))


def load_markers(root: Path, user_id: str) -> Dict[str, Any]:
    return load_json(markers_path(root, user_id), {
        "_meta": {"created": now_iso(), "version": 2},
        "scan_window": {"start": "", "end": "", "processed_dates": []},
        "last_processed_timestamp": None,
        "processed_hashes": {},
        "activities": {},
        "pending_checks": [],
        "_next_scan_hint": "",
        "last_l2_at": None,
        "last_poll_at": None,
    })


def save_initial_markers(root: Path, user_id: str) -> Dict[str, Any]:
    markers = load_markers(root, user_id)
    markers.setdefault("_meta", {})["initialized_at"] = now_iso()
    atomic_write_json(markers_path(root, user_id), markers)
    return markers


def format_user_only(date_str: str, messages: List[UserMessage]) -> Tuple[str, str]:
    body = "\n\n".join(f"[{message.timestamp}] {message.body}" for message in messages).strip()
    if body:
        body += "\n"
    content_hash = sha256_text(body)
    header = [
        f"# User Only Transcript: {date_str}",
        f"source_transcript: {messages[0].source_transcript if messages else ''}",
        f"generated_at: {now_iso()}",
        f"message_count: {len(messages)}",
        f"content_hash: {content_hash}",
        "",
    ]
    return "\n".join(header) + body, content_hash


def write_user_only(root: Path, user_id: str, grouped: Dict[str, List[UserMessage]], dry_run: bool) -> Dict[str, str]:
    hashes: Dict[str, str] = {}
    for date_str, messages in grouped.items():
        content, content_hash = format_user_only(date_str, messages)
        hashes[date_str] = content_hash
        path = user_only_path(root, user_id, date_str)
        if path.is_file() and f"content_hash: {content_hash}" in path.read_text(encoding="utf-8", errors="replace")[:400]:
            continue
        if not dry_run:
            atomic_write_text(path, content)
    return hashes


def detect_activities(messages: List[UserMessage], root: Path, user_id: str) -> Dict[str, Dict[str, Any]]:
    activities: Dict[str, Dict[str, Any]] = {}
    for message in messages:
        for name, keywords in ACTIVITY_KEYWORDS.items():
            if any(keyword in message.body for keyword in keywords):
                user_only_source = rel(user_only_path(root, user_id, message.date), root)
                item = activities.setdefault(name, {
                    "type": "activity",
                    "speaker": "user",
                    "status": "active",
                    "first_seen": message.timestamp,
                    "last_seen": message.timestamp,
                    "timestamp": message.timestamp,
                    "dates": [],
                    "source_transcript": message.source_transcript,
                    "user_only_source": user_only_source,
                    "snippet": message.body[:180],
                    "confidence": 0.8,
                    "source_transcripts": [],
                    "user_only_sources": [],
                    "representative_snippet": message.body[:180],
                })
                item["last_seen"] = message.timestamp
                item["timestamp"] = message.timestamp
                if message.date not in item["dates"]:
                    item["dates"].append(message.date)
                if message.source_transcript not in item["source_transcripts"]:
                    item["source_transcripts"].append(message.source_transcript)
                if user_only_source not in item["user_only_sources"]:
                    item["user_only_sources"].append(user_only_source)
    return activities


def record(message: UserMessage, kind: str, snippet: str, confidence: float, root: Path, user_id: str) -> Dict[str, Any]:
    return {
        "type": kind,
        "timestamp": message.timestamp,
        "speaker": "user",
        "source_transcript": message.source_transcript,
        "user_only_source": rel(user_only_path(root, user_id, message.date), root),
        "snippet": snippet.strip(),
        "confidence": confidence,
    }


def dedupe_records(items: List[Dict[str, Any]], key_fields: Tuple[str, ...]) -> List[Dict[str, Any]]:
    seen = set()
    result = []
    for item in items:
        key = tuple(item.get(field, "") for field in key_fields)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def merge_unique(existing: List[Dict[str, Any]], incoming: List[Dict[str, Any]], key_fields: Tuple[str, ...]) -> List[Dict[str, Any]]:
    result = list(existing)
    seen = {tuple(item.get(field, "") for field in key_fields) for item in result}
    for item in incoming:
        key = tuple(item.get(field, "") for field in key_fields)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def extract_preferences(messages: List[UserMessage], root: Path, user_id: str) -> List[Dict[str, Any]]:
    return [
        record(message, "preference", message.body[:160], 0.85, root, user_id)
        for message in messages
        if any(keyword in message.body for keyword in PREFERENCE_KEYWORDS)
    ]


def extract_capability_rules(messages: List[UserMessage], root: Path, user_id: str) -> List[Dict[str, Any]]:
    results = []
    for message in messages:
        if any(keyword in message.body for keyword in RULE_KEYWORDS):
            confidence = 0.9 if any(keyword in message.body for keyword in ("以后", "默认", "必须", "不要", "流程就这么来", "记住")) else 0.8
            results.append(record(message, "rule", message.body[:220], confidence, root, user_id))
    return results


def infer_project_status(text: str) -> str:
    if any(keyword in text for keyword in ("完成", "搞定", "成功")):
        return "已完成/验证"
    if any(keyword in text for keyword in ("计划", "规划", "看看", "研究")):
        return "规划/研究"
    if any(keyword in text for keyword in ("调试", "修改", "实现", "开发")):
        return "进行中"
    return "未知"


def extract_projects(messages: List[UserMessage], root: Path, user_id: str) -> List[Dict[str, Any]]:
    items = []
    for message in messages:
        if any(keyword in message.body for keyword in PROJECT_KEYWORDS):
            name = message.body.replace("\n", " ")[:80]
            items.append({**record(message, "project", name, 0.82, root, user_id), "name": name, "status": infer_project_status(name)})
    return dedupe_records(items, ("name",))


def extract_emotions(messages: List[UserMessage], root: Path, user_id: str) -> List[Dict[str, Any]]:
    events = []
    for message in messages:
        for category, keywords in EMOTION_KEYWORDS.items():
            if any(keyword in message.body for keyword in keywords) and len(message.body) >= 4:
                events.append({
                    **record(message, "emotion", message.body[:180], 0.82, root, user_id),
                    "category": category,
                    "snippet_hash": snippet_hash(message.body),
                })
                break
    return dedupe_records(events, ("timestamp", "snippet_hash"))


def summarize_day(root: Path, user_id: str, date_str: str, messages: List[UserMessage], content_hash: str) -> str:
    activities = detect_activities(messages, root, user_id)
    points = []
    if activities:
        points.append(f"- 活动: {'、'.join(sorted(activities.keys()))}")
    rules = extract_capability_rules(messages, root, user_id)
    if rules:
        points.append(f"- 明确规则/流程: {len(rules)} 条")
    projects = extract_projects(messages, root, user_id)
    if projects:
        points.append(f"- 项目/研究: {'；'.join(item['name'] for item in projects[:3])}")
    emotions = extract_emotions(messages, root, user_id)
    if emotions:
        points.append(f"- 情绪/状态: {'、'.join(sorted({item['category'] for item in emotions}))}")
    if not points:
        points.append(f"- 要点: {(messages[0].body if messages else '无用户消息').replace(chr(10), ' ')[:80]}")
    timeline = [f"  - {message.timestamp}: {message.body.replace(chr(10), ' ')[:160]}" for message in messages[:12]]
    if len(messages) > 12:
        timeline.append(f"  - ... 另有 {len(messages) - 12} 条用户消息")
    return "\n".join([
        f"# {date_str} 用户侧对话摘要",
        "",
        f"source_user_only: {rel(user_only_path(root, user_id, date_str), root)}",
        f"content_hash: {content_hash}",
        "",
        "## 摘要",
        *points,
        "",
        "## 时间点",
        *timeline,
        "",
    ])


def write_chunks(root: Path, user_id: str, grouped: Dict[str, List[UserMessage]], hashes: Dict[str, str], dry_run: bool) -> int:
    written = 0
    for date_str, messages in grouped.items():
        content_hash = hashes[date_str]
        path = chunk_path(root, user_id, date_str)
        if path.is_file() and f"content_hash: {content_hash}" in path.read_text(encoding="utf-8", errors="replace")[:300]:
            continue
        if not dry_run:
            atomic_write_text(path, summarize_day(root, user_id, date_str, messages, content_hash))
        written += 1
    return written


def load_active(root: Path, user_id: str) -> Dict[str, Any]:
    return load_json(active_knowledge_path(root, user_id), {
        "_meta": {"generated": "", "source": "user_only transcripts", "version": 2},
        "user_profile": {"preferences": []},
        "ongoing_projects": [],
        "agent_capabilities_learned": [],
        "memory_lessons": [],
    })


def build_proposed_updates(preferences: List[Dict[str, Any]], rules: List[Dict[str, Any]]) -> str:
    lines = ["# Proposed Memory Updates", "", "These candidates are not auto-applied to users/ or operational/.", ""]
    if preferences:
        lines.append("## users candidates")
        lines.extend(f"- [{item['timestamp']}] {item['snippet']} ({item['source_transcript']})" for item in preferences)
        lines.append("")
    if rules:
        lines.append("## operational candidates")
        lines.extend(f"- [{item['timestamp']}] {item['snippet']} ({item['source_transcript']})" for item in rules)
        lines.append("")
    if len(lines) <= 4:
        lines.append("- No high-confidence proposed updates.")
    return "\n".join(lines).rstrip() + "\n"


def build_memory_brief(active: Dict[str, Any], emotion_data: Dict[str, Any]) -> str:
    lines = ["# 记忆摘要", f"更新: {datetime.now().strftime('%Y-%m-%d %H:%M')}", ""]
    prefs = active.get("user_profile", {}).get("preferences", [])[-3:]
    projects = active.get("ongoing_projects", [])[-3:]
    rules = active.get("agent_capabilities_learned", [])[-3:]
    emotions = emotion_data.get("events", [])[-2:]
    lines.extend(["## 关键事实", *(f"- {item.get('snippet', '')[:80]}" for item in prefs)] if prefs else ["## 关键事实", "- 暂无高置信偏好"])
    lines.extend(["", "## 活跃项目", *(f"- {item.get('name', '')[:60]} → {item.get('status', '')}" for item in projects)] if projects else ["", "## 活跃项目", "- 暂无活跃项目记录"])
    lines.extend(["", "## 最近情绪", *(f"- {item.get('category', '')}: {item.get('snippet', '')[:70]}" for item in emotions)] if emotions else ["", "## 最近情绪", "- 暂无情绪事件"])
    lines.extend(["", "## 操作规则", *(f"- {item.get('snippet', '')[:80]}" for item in rules)] if rules else ["", "## 操作规则", "- 暂无新规则"])
    text = "\n".join(lines)
    if len(text) > MAX_MEMORY_BRIEF_CHARS:
        text = text[:MAX_MEMORY_BRIEF_CHARS].rsplit("\n", 1)[0] + "\n\n<!-- truncated -->"
    return text.rstrip() + "\n"


def build_readme(user_id: str, messages: List[UserMessage], hashes: Dict[str, str], active: Dict[str, Any], emotion_data: Dict[str, Any]) -> str:
    dates = sorted(hashes.keys())
    return "\n".join([
        "# History Insight - G4W L4",
        "",
        f"generated: {now_iso()}",
        f"user: {user_id}",
        f"window: {(dates[0] if dates else '')} ~ {(dates[-1] if dates else '')}",
        f"user_messages_processed: {len(messages)}",
        "",
        "## Products",
        "- active_knowledge.json",
        "- emotion_events.json",
        "- incremental_markers.json",
        "- memory_brief.md",
        "- proposed_updates.md",
        "",
        "## Counts",
        f"- preferences: {len(active.get('user_profile', {}).get('preferences', []))}",
        f"- projects: {len(active.get('ongoing_projects', []))}",
        f"- rules: {len(active.get('agent_capabilities_learned', []))}",
        f"- emotion_events: {emotion_data.get('_meta', {}).get('total_events', 0)}",
        "",
    ])


def create_backup(root: Path, user_id: str, dry_run: bool) -> Optional[str]:
    if dry_run:
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = summaries_root(root, user_id) / ".backups" / f"l4compress-{stamp}"
    for path in (summaries_root(root, user_id) / "user_only", summaries_root(root, user_id) / "chunks", history_root(root, user_id)):
        if path.exists():
            target = dest / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(path, target, dirs_exist_ok=True) if path.is_dir() else shutil.copy2(path, target)
    return rel(dest, root)


def make_run_id(trigger_mode: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = hashlib.sha1(f"{trigger_mode}-{stamp}-{os.getpid()}".encode("utf-8")).hexdigest()[:8]
    return f"{trigger_mode}-{stamp}-{suffix}"


def build_run_manifest(
    root: Path,
    user_id: str,
    trigger_mode: str,
    run_id: str,
    messages: List[UserMessage],
    hashes: Dict[str, str],
    marker_before: Dict[str, Any],
    start: str = "",
    end: str = "",
) -> Dict[str, Any]:
    grouped = group_by_date(messages)
    source_transcripts = sorted({message.source_transcript for message in messages})
    dates = sorted(grouped.keys())
    output_dir = subagent_output_dir(root, user_id, run_id)
    manifest = {
        "run_id": run_id,
        "mode": trigger_mode,
        "created_at": now_iso(),
        "workspace_root": str(root),
        "safe_user_id": safe_segment(user_id),
        "user_id": user_id,
        "conversation_root": str(conversation_root(root, user_id)),
        "window": {
            "from": start or (dates[0] if dates else ""),
            "to": end or (dates[-1] if dates else ""),
            "dates": dates,
        },
        "last_marker_before": marker_before,
        "candidate_last_timestamp": max([message.timestamp for message in messages], default=marker_before.get("last_processed_timestamp")),
        "user_message_count": len(messages),
        "message_counts_by_date": {date_str: len(items) for date_str, items in grouped.items()},
        "content_hashes": hashes,
        "user_only_files": [rel(user_only_path(root, user_id, date_str), root) for date_str in dates],
        "chunk_files": [rel(chunk_path(root, user_id, date_str), root) for date_str in dates],
        "source_transcripts": source_transcripts,
        "allowed_output_dir": rel(output_dir, root),
        "required_output_files": REQUIRED_CANDIDATE_FILES,
        "official_outputs": {
            "active_knowledge": rel(active_knowledge_path(root, user_id), root),
            "emotion_events": rel(emotion_events_path(root, user_id), root),
            "incremental_markers": rel(markers_path(root, user_id), root),
            "memory_brief": rel(memory_brief_path(root, user_id), root),
            "proposed_updates": rel(proposed_updates_path(root, user_id), root),
            "README": rel(readme_path(root, user_id), root),
            "user_profile": rel(user_profile_md_path(root, user_id), root),
        },
        "rules": [
            "Subagent must only read files listed in this manifest.",
            "Subagent must write only the required files under allowed_output_dir.",
            "Every formal insight must include timestamp, speaker=user, source_transcript, user_only_source, snippet, confidence.",
            "Do not write transcripts, assistant-replies, memory/users, memory/operational, or all_histories.txt.",
        ],
        # 用户画像:worker 输出完整叙述文(固定小节)。init=首次(结合素材撰写),
        # incremental=基于现有画像改写(未变化段落一字不改)。素材引用供首次使用。
        "user_profile": {
            "mode": "incremental" if user_profile_md_path(root, user_id).is_file() else "init",
            "path": rel(user_profile_md_path(root, user_id), root),
            "source_refs": {
                "users": rel(wechat_root(root) / "users" / f"{safe_segment(user_id)}.md", root),
                "operational": rel(wechat_root(root) / "operational" / f"{safe_segment(user_id)}.md", root),
                "active_knowledge": rel(active_knowledge_path(root, user_id), root),
            },
        },
    }
    return manifest


def prepare_run(
    root: Path,
    user_id: str,
    trigger_mode: str = "deep",
    start: str = "",
    end: str = "",
    dry_run: bool = False,
) -> Dict[str, Any]:
    preflight(root, user_id)
    mode_for_selection = "bootstrap" if trigger_mode == "bootstrap" else "deep"
    messages, marker_exists = select_messages(root, user_id, mode_for_selection, start, end)
    if not marker_exists and trigger_mode != "bootstrap":
        if not dry_run:
            save_initial_markers(root, user_id)
        return {
            "status": "needs_bootstrap",
            "reason": "incremental_markers.json is missing; run /l4compress bootstrap --from YYYY-MM-DD --to YYYY-MM-DD",
            "data_root": str(conversation_root(root, user_id)),
        }
    if not messages:
        return {"status": "skipped", "reason": "no new user messages", "messages": 0}

    grouped = group_by_date(messages)
    marker_before = load_markers(root, user_id)
    planned_hashes = {date_str: format_user_only(date_str, date_messages)[1] for date_str, date_messages in grouped.items()}
    run_id = make_run_id(trigger_mode)
    manifest = build_run_manifest(root, user_id, trigger_mode, run_id, messages, planned_hashes, marker_before, start, end)

    planned_paths = []
    for date_str in grouped:
        planned_paths.extend([rel(user_only_path(root, user_id, date_str), root), rel(chunk_path(root, user_id, date_str), root)])
    planned_paths.extend([rel(run_manifest_path(root, user_id, run_id), root), rel(subagent_output_dir(root, user_id, run_id), root)])
    if dry_run:
        return {
            "status": "dryrun",
            "mode": trigger_mode,
            "messages": len(messages),
            "dates": sorted(grouped.keys()),
            "planned_writes": planned_paths,
            "candidate_last_timestamp": manifest["candidate_last_timestamp"],
        }

    with acquire_lock(root, user_id):
        backup = create_backup(root, user_id, dry_run=False)
        hashes = write_user_only(root, user_id, grouped, dry_run=False)
        chunks_written = write_chunks(root, user_id, grouped, hashes, dry_run=False)
        manifest = build_run_manifest(root, user_id, trigger_mode, run_id, messages, hashes, marker_before, start, end)
        output_dir = subagent_output_dir(root, user_id, run_id)
        output_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(run_manifest_path(root, user_id, run_id), manifest)

    return {
        "status": "prepared",
        "mode": trigger_mode,
        "run_id": run_id,
        "messages": len(messages),
        "dates": sorted(grouped.keys()),
        "candidate_last_timestamp": manifest["candidate_last_timestamp"],
        "run_manifest": str(run_manifest_path(root, user_id, run_id)),
        "allowed_output_dir": str(subagent_output_dir(root, user_id, run_id)),
        "required_output_files": REQUIRED_CANDIDATE_FILES,
        "user_only_files": len(hashes),
        "chunks_written": chunks_written,
        "backup": backup,
    }


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def load_manifest(root: Path, user_id: str, run_id: str) -> Dict[str, Any]:
    manifest_file = run_manifest_path(root, user_id, run_id)
    if not manifest_file.is_file():
        raise RuntimeError(f"run_manifest.json not found for run_id={run_id}: {manifest_file}")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    expected_output = subagent_output_dir(root, user_id, run_id).resolve()
    actual_output = (root / manifest.get("allowed_output_dir", "")).resolve()
    if actual_output != expected_output:
        raise RuntimeError(f"manifest allowed_output_dir mismatch: {actual_output} != {expected_output}")
    if not is_within(actual_output, history_root(root, user_id) / "runs"):
        raise RuntimeError(f"manifest output dir is outside runs: {actual_output}")
    return manifest


def candidate_path(root: Path, user_id: str, run_id: str, name: str) -> Path:
    path = subagent_output_dir(root, user_id, run_id) / name
    if not is_within(path, subagent_output_dir(root, user_id, run_id)):
        raise RuntimeError(f"candidate path escaped output dir: {path}")
    return path


def load_candidate_json(root: Path, user_id: str, run_id: str, name: str) -> Dict[str, Any]:
    path = candidate_path(root, user_id, run_id, name)
    if not path.is_file():
        raise RuntimeError(f"required candidate file missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_candidate_text(root: Path, user_id: str, run_id: str, name: str) -> str:
    path = candidate_path(root, user_id, run_id, name)
    if not path.is_file():
        raise RuntimeError(f"required candidate file missing: {path}")
    return path.read_text(encoding="utf-8", errors="replace")


def evidence_errors(item: Dict[str, Any], manifest: Dict[str, Any]) -> List[str]:
    errors = []
    for field in ("timestamp", "speaker", "source_transcript", "user_only_source", "snippet", "confidence"):
        if field not in item or item.get(field) in ("", None):
            errors.append(f"missing {field}")
    if str(item.get("speaker", "")).lower() != "user":
        errors.append("speaker must be user")
    try:
        confidence = float(item.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0
    if confidence < 0.8:
        errors.append("confidence below 0.8")
    source_transcripts = set(manifest.get("source_transcripts", []))
    user_only_files = set(manifest.get("user_only_files", []))
    if item.get("source_transcript") not in source_transcripts:
        errors.append("source_transcript not in manifest")
    if item.get("user_only_source") not in user_only_files:
        errors.append("user_only_source not in manifest")
    timestamp = str(item.get("timestamp", ""))
    candidate_last = str(manifest.get("candidate_last_timestamp", ""))
    marker_before = manifest.get("last_marker_before", {}) or {}
    last_before = str(marker_before.get("last_processed_timestamp") or "")
    if candidate_last and timestamp and timestamp > candidate_last:
        errors.append("timestamp after manifest boundary")
    if manifest.get("mode") != "bootstrap" and last_before and timestamp and timestamp <= last_before:
        errors.append("timestamp at or before previous marker")
    return errors


def extract_active_items(data: Any, path_name: str = "") -> Tuple[Any, List[str]]:
    """Filter active knowledge recursively.

    Dict entries inside lists are formal insights and must carry evidence. Invalid
    items are returned as proposed-update strings instead of entering official JSON.
    """
    rejected: List[str] = []
    if isinstance(data, dict):
        result: Dict[str, Any] = {}
        for key, value in data.items():
            if key == "_meta":
                result[key] = value if isinstance(value, dict) else {}
                continue
            cleaned, sub_rejected = extract_active_items(value, f"{path_name}.{key}" if path_name else key)
            rejected.extend(sub_rejected)
            if cleaned not in ({}, [], None, ""):
                result[key] = cleaned
        return result, rejected
    if isinstance(data, list):
        cleaned_items = []
        for item in data:
            if isinstance(item, dict):
                cleaned_items.append(item)
            else:
                rejected.append(f"- `{path_name}` scalar item needs evidence: {str(item)[:180]}")
        return cleaned_items, rejected
    if data in ("", None):
        return data, rejected
    rejected.append(f"- `{path_name}` scalar value needs evidence: {str(data)[:180]}")
    return None, rejected


def filter_active_candidate(active_candidate: Dict[str, Any], manifest: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    cleaned, rejected = extract_active_items(active_candidate)

    def walk(value: Any, path_name: str = "") -> Any:
        if isinstance(value, dict):
            if path_name and any(field in value for field in ("timestamp", "source_transcript", "snippet", "confidence")):
                errors = evidence_errors(value, manifest)
                if errors:
                    rejected.append(f"- `{path_name}` rejected: {', '.join(errors)} | {str(value.get('snippet', ''))[:160]}")
                    return None
                return value
            result = {}
            for key, sub_value in value.items():
                walked = walk(sub_value, f"{path_name}.{key}" if path_name else key)
                if walked not in ({}, [], None, ""):
                    result[key] = walked
            return result
        if isinstance(value, list):
            result = []
            for index, item in enumerate(value):
                walked = walk(item, f"{path_name}[{index}]")
                if walked not in ({}, [], None, ""):
                    result.append(walked)
            return result
        return value

    filtered = walk(cleaned)
    if not isinstance(filtered, dict):
        filtered = {}
    filtered["_meta"] = {
        **(filtered.get("_meta", {}) if isinstance(filtered.get("_meta"), dict) else {}),
        "generated": now_iso(),
        "source": "GA subagent semantic mining",
        "version": 3,
        "run_id": manifest.get("run_id"),
    }
    return filtered, rejected


def flatten_lists(data: Any, current_path: str = "") -> List[Tuple[str, List[Dict[str, Any]]]]:
    if isinstance(data, dict):
        result: List[Tuple[str, List[Dict[str, Any]]]] = []
        for key, value in data.items():
            path_name = f"{current_path}.{key}" if current_path else key
            if isinstance(value, list) and all(isinstance(item, dict) for item in value):
                result.append((path_name, value))
            else:
                result.extend(flatten_lists(value, path_name))
        return result
    return []


def set_path(data: Dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cursor = data
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = value


def stable_item_key(item: Dict[str, Any]) -> Tuple[str, str]:
    for key in ("stable_key", "key", "name", "capability", "title"):
        if item.get(key):
            return key, str(item.get(key))
    return "snippet", f"{item.get('timestamp', '')}:{snippet_hash(str(item.get('snippet', '')))}"


def merge_active(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(existing) if isinstance(existing, dict) else {}
    merged["_meta"] = incoming.get("_meta", {"generated": now_iso(), "source": "GA subagent semantic mining", "version": 3})
    for dotted, incoming_list in flatten_lists(incoming):
        existing_cursor: Any = merged
        for part in dotted.split("."):
            if not isinstance(existing_cursor, dict):
                existing_cursor = {}
                break
            existing_cursor = existing_cursor.get(part, [])
        existing_list = existing_cursor if isinstance(existing_cursor, list) else []
        by_key = {stable_item_key(item): item for item in existing_list if isinstance(item, dict)}
        for item in incoming_list:
            by_key[stable_item_key(item)] = {**by_key.get(stable_item_key(item), {}), **item}
        set_path(merged, dotted, list(by_key.values()))
    for key, value in incoming.items():
        if key == "_meta" or isinstance(value, list):
            continue
        if isinstance(value, dict):
            merged.setdefault(key, {})
            if isinstance(merged[key], dict):
                for sub_key, sub_value in value.items():
                    if not isinstance(sub_value, list):
                        merged[key][sub_key] = sub_value
    return merged


def normalize_emotion_candidate(candidate: Any, manifest: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    rejected: List[str] = []
    raw_events = candidate if isinstance(candidate, list) else candidate.get("events", []) if isinstance(candidate, dict) else []
    if not isinstance(raw_events, list):
        raw_events = []
    events = []
    for index, event in enumerate(raw_events):
        if not isinstance(event, dict):
            rejected.append(f"- emotion_events[{index}] is not an object")
            continue
        errors = evidence_errors(event, manifest)
        if errors:
            rejected.append(f"- emotion_events[{index}] rejected: {', '.join(errors)} | {str(event.get('snippet', ''))[:160]}")
            continue
        event = dict(event)
        event.setdefault("type", "emotion")
        event.setdefault("snippet_hash", snippet_hash(str(event.get("snippet", ""))))
        events.append(event)
    events = dedupe_records(events, ("timestamp", "snippet_hash"))
    return {
        "_meta": {
            "total_events": len(events),
            "categories": sorted({item.get("category", "") for item in events if item.get("category")}),
            "updated": now_iso(),
            "version": 3,
            "run_id": manifest.get("run_id"),
        },
        "events": events,
    }, rejected


def merge_markers(existing: Dict[str, Any], candidate: Dict[str, Any], manifest: Dict[str, Any]) -> Dict[str, Any]:
    next_markers = dict(existing) if isinstance(existing, dict) else {}
    processed_dates = sorted(set(next_markers.get("scan_window", {}).get("processed_dates", [])) | set(manifest.get("window", {}).get("dates", [])))
    next_markers["scan_window"] = {
        "start": min(processed_dates) if processed_dates else "",
        "end": max(processed_dates) if processed_dates else "",
        "processed_dates": processed_dates,
    }
    next_markers["last_processed_timestamp"] = manifest.get("candidate_last_timestamp")
    next_markers["processed_hashes"] = {
        **(next_markers.get("processed_hashes", {}) if isinstance(next_markers.get("processed_hashes"), dict) else {}),
        **(manifest.get("content_hashes", {}) if isinstance(manifest.get("content_hashes"), dict) else {}),
    }
    for key in ("activities", "gone_things", "pending_checks"):
        if key in candidate:
            next_markers[key] = candidate[key]
    next_markers["last_l2_at"] = now_iso()
    next_markers["last_run_id"] = manifest.get("run_id")
    next_markers["_next_scan_hint"] = f"从 {manifest.get('candidate_last_timestamp') or '(unknown)'} 后的新用户消息开始增量扫描"
    next_markers.setdefault("_meta", {})["version"] = 3
    return next_markers


def append_rejections_to_proposed(candidate_text: str, rejected: List[str], report_text: str, manifest: Dict[str, Any]) -> str:
    lines = [candidate_text.rstrip(), "", "## Validator Notes", f"- run_id: {manifest.get('run_id')}"]
    if rejected:
        lines.append("- Some candidate items were not merged into official memory:")
        lines.extend(rejected)
    else:
        lines.append("- All validated high-confidence candidate items were eligible for official merge.")
    if report_text.strip():
        lines.extend(["", "## Subagent Report Excerpt", report_text.strip()[:2000]])
    return "\n".join(lines).rstrip() + "\n"


# 用户画像素材草稿(worker 产出,报告体不带语气;最终画像由 conductor 复述维护)
PROFILE_DRAFT_MAX_CHARS = 12000


def validate_profile_candidate(text: str) -> bool:
    """确定性校验 worker 的画像素材草稿:非空 + 长度上限。

    草稿是"交接物"——内容质量与语气由 SOP 约束(报告体/证据全),
    最终画像由 conductor 在 worker-final round 复述落盘。
    """
    body = str(text or "").strip()
    if not body:
        return False
    if len(body) > PROFILE_DRAFT_MAX_CHARS:
        return False
    return True


def _upsert_with_embed_retry(fn, **kwargs) -> Dict[str, Any]:
    """Run an index-upsert callable; retry once after ensuring the embed server.

    When the failure reason is an embedding/remote failure (service down or
    half-dead), first call ``ensure_embed_running`` (which now requires a real
    inference probe) and re-run the upsert once. This prevents the 2026-08-13
    pattern — L4 finalized while the vector index silently stayed stale.
    Failures remain visible in the returned summary for worker reporting.
    """
    out: Dict[str, Any] = fn(**kwargs)
    reason = str(out.get("embedding_reason") or out.get("reason") or "")
    if out.get("status") == "error" and reason in (
        "remote_http_failed",
        "remote_not_ready",
        "embedding_failed",
    ):
        try:
            from .vector.embed_lifecycle import ensure_embed_running

            up = ensure_embed_running(timeout_s=45.0)
            if up.get("ok"):
                out = fn(**kwargs)
                out["retried_after_embed_start"] = True
            else:
                out["retry_embed_start"] = {
                    "ok": False,
                    "detail": up.get("detail") or up.get("status") or "start failed",
                }
        except Exception as exc:  # pragma: no cover - defensive
            out["retry_embed_start"] = {
                "ok": False,
                "detail": f"{type(exc).__name__}: {exc}",
            }
    return out


def validate_finalize(root: Path, user_id: str, run_id: str, dry_run: bool = False) -> Dict[str, Any]:
    preflight(root, user_id)
    manifest = load_manifest(root, user_id, run_id)
    output_dir = subagent_output_dir(root, user_id, run_id)
    # user_profile.candidate.md 为可选(旧版 worker / 中途升级不产出时跳过画像合并)
    OPTIONAL_CANDIDATES = {"user_profile.candidate.md"}
    missing = [
        name for name in REQUIRED_CANDIDATE_FILES
        if name not in OPTIONAL_CANDIDATES
        and not candidate_path(root, user_id, run_id, name).is_file()
    ]
    if missing:
        return {"status": "validation_failed", "reason": "missing candidate files", "missing": missing, "run_id": run_id}

    active_candidate = load_candidate_json(root, user_id, run_id, "active_knowledge.candidate.json")
    emotion_candidate = load_candidate_json(root, user_id, run_id, "emotion_events.candidate.json")
    marker_candidate = load_candidate_json(root, user_id, run_id, "incremental_markers.candidate.json")
    readme_candidate = load_candidate_text(root, user_id, run_id, "README.candidate.md")
    memory_brief_candidate = load_candidate_text(root, user_id, run_id, "memory_brief.candidate.md")
    proposed_candidate = load_candidate_text(root, user_id, run_id, "proposed_updates.candidate.md")
    report_text = load_candidate_text(root, user_id, run_id, "subagent_report.md")

    active_filtered, active_rejected = filter_active_candidate(active_candidate, manifest)
    emotion_filtered, emotion_rejected = normalize_emotion_candidate(emotion_candidate, manifest)
    rejected = active_rejected + emotion_rejected

    existing_markers = load_markers(root, user_id)
    if existing_markers.get("last_run_id") == run_id:
        return {"status": "already_finalized", "run_id": run_id, "last_processed_timestamp": existing_markers.get("last_processed_timestamp")}
    before_boundary = str((manifest.get("last_marker_before") or {}).get("last_processed_timestamp") or "")
    current_boundary = str(existing_markers.get("last_processed_timestamp") or "")
    if before_boundary and current_boundary and current_boundary != before_boundary:
        return {
            "status": "validation_failed",
            "reason": "marker boundary changed after prepare; refusing to finalize stale run",
            "run_id": run_id,
            "expected_boundary": before_boundary,
            "current_boundary": current_boundary,
        }

    active = merge_active(load_active(root, user_id), active_filtered)
    existing_emotion = load_json(emotion_events_path(root, user_id), {"_meta": {"total_events": 0, "categories": []}, "events": []})
    merged_emotion_events = merge_unique(existing_emotion.get("events", []), emotion_filtered.get("events", []), ("timestamp", "snippet_hash"))
    emotion_data = {
        "_meta": {
            "total_events": len(merged_emotion_events),
            "categories": sorted({item.get("category", "") for item in merged_emotion_events if item.get("category")}),
            "updated": now_iso(),
            "version": 3,
            "run_id": run_id,
        },
        "events": merged_emotion_events,
    }
    markers = merge_markers(existing_markers, marker_candidate, manifest)
    proposed_text = append_rejections_to_proposed(proposed_candidate, rejected, report_text, manifest)
    memory_brief = memory_brief_candidate.strip()
    if len(memory_brief) > 1000:
        memory_brief = memory_brief[:1000].rsplit("\n", 1)[0] + "\n\n<!-- truncated by validator -->"
    readme = readme_candidate.rstrip() + "\n\n---\nvalidator_finalized_at: " + now_iso() + "\n"

    # 用户画像素材草稿:worker 产出(报告体,不带语气),校验后落盘为
    # user_profile.draft.md 作为 conductor 复述的交接物。
    # 最终 user_profile.md 由 conductor 在 worker-final round 维护(读草稿+现有
    # 画像 → 助手（conductor）语气 → 更新/修改/保持不变)。candidate 缺失/校验失败不阻塞。
    profile_text = ""
    user_profile: Dict[str, Any] = {"updated": "", "written": False, "reason": "no_candidate"}
    try:
        profile_candidate = load_candidate_text(root, user_id, run_id, "user_profile.candidate.md")
    except Exception:
        profile_candidate = ""
    if str(profile_candidate or "").strip():
        if validate_profile_candidate(profile_candidate):
            profile_text = profile_candidate.strip()
            user_profile = {
                "updated": now_iso(),
                "written": True,
                "bytes": len(profile_text.encode("utf-8")),
                "path": str(user_profile_draft_path(root, user_id)),
            }
        else:
            user_profile = {
                "updated": "",
                "written": False,
                "reason": "candidate_validation_failed",
                "path": str(user_profile_draft_path(root, user_id)),
            }

    if dry_run:
        return {
            "status": "dryrun",
            "run_id": run_id,
            "active_candidate_sections": sorted(active_filtered.keys()),
            "emotion_events_to_merge": len(emotion_filtered.get("events", [])),
            "rejected_candidates": len(rejected),
            "output_dir": str(output_dir),
        }

    with acquire_lock(root, user_id):
        backup = create_backup(root, user_id, dry_run=False)
        atomic_write_json(active_knowledge_path(root, user_id), active)
        atomic_write_json(emotion_events_path(root, user_id), emotion_data)
        atomic_write_text(memory_brief_path(root, user_id), memory_brief.rstrip() + "\n")
        atomic_write_text(proposed_updates_path(root, user_id), proposed_text)
        atomic_write_text(readme_path(root, user_id), readme)
        atomic_write_json(markers_path(root, user_id), markers)
        if profile_text:
            atomic_write_text(user_profile_draft_path(root, user_id), profile_text.rstrip() + "\n")

    # TASK-E: L4 finalize → vector index chain (fail-soft; never blocks finalize).
    # Task2: L4 insight upsert; Task3: transcript window upsert (watermark →
    # latest transcript). When /vector is on and embedding is down, each task
    # retries once after ensure_embed_running; failures stay visible in the
    # returned summary and the worker SOP reports them to the user.
    # Do not add a second upsert hook in controller; this is the single write entry.
    vector_upsert: Dict[str, Any] = {"status": "skipped", "reason": "not_attempted"}
    transcript_upsert: Dict[str, Any] = {"status": "skipped", "reason": "not_attempted"}
    try:
        from .vector.l4_index_upsert import upsert_l4_insights_to_index

        vector_upsert = _upsert_with_embed_retry(
            upsert_l4_insights_to_index,
            active=active,
            emotion=emotion_data,
            user_id=user_id,
            run_id=run_id,
            dry_run=False,
        )
        from .vector.transcript_chunk_upsert import upsert_transcript_window

        transcript_upsert = _upsert_with_embed_retry(
            upsert_transcript_window,
            transcripts_root=transcripts_root(root, user_id),
            run_id=run_id,
        )
    except Exception as exc:  # pragma: no cover - defensive
        vector_upsert = {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }

    return {
        "status": "finalized",
        "run_id": run_id,
        "messages": manifest.get("user_message_count", 0),
        "dates": manifest.get("window", {}).get("dates", []),
        "last_processed_timestamp": markers.get("last_processed_timestamp"),
        "active_sections": sorted(active_filtered.keys()),
        "emotion_events_merged": len(emotion_filtered.get("events", [])),
        "rejected_candidates": len(rejected),
        "backup": backup,
        "readme": str(readme_path(root, user_id)),
        "memory_brief": str(memory_brief_path(root, user_id)),
        "user_profile": {
            "updated": str(user_profile.get("updated") or ""),
            "written": bool(user_profile.get("written")),
            "reason": user_profile.get("reason") or "",
            "bytes": int(user_profile.get("bytes") or 0),
            "path": str(user_profile_draft_path(root, user_id)),
        },
        "vector_upsert": vector_upsert,
        "transcript_upsert": transcript_upsert,
    }


def update_history_insight(root: Path, user_id: str, messages: List[UserMessage], hashes: Dict[str, str], dry_run: bool) -> Dict[str, Any]:
    preferences = [item for item in extract_preferences(messages, root, user_id) if item["confidence"] >= 0.8]
    rules = [item for item in extract_capability_rules(messages, root, user_id) if item["confidence"] >= 0.8]
    projects = [item for item in extract_projects(messages, root, user_id) if item["confidence"] >= 0.8]
    emotions = [item for item in extract_emotions(messages, root, user_id) if item["confidence"] >= 0.8]
    activities = detect_activities(messages, root, user_id)

    active = load_active(root, user_id)
    active["_meta"] = {"generated": now_iso(), "source": "user_only transcripts", "version": 2}
    active.setdefault("user_profile", {})["preferences"] = merge_unique(active.get("user_profile", {}).get("preferences", []), preferences, ("timestamp", "snippet"))
    active["ongoing_projects"] = merge_unique(active.get("ongoing_projects", []), projects, ("name",))
    active["agent_capabilities_learned"] = merge_unique(active.get("agent_capabilities_learned", []), rules, ("timestamp", "snippet"))
    active["memory_lessons"] = active.get("memory_lessons", [])

    emotion_data = load_json(emotion_events_path(root, user_id), {"_meta": {"total_events": 0, "categories": []}, "events": []})
    emotion_data["events"] = merge_unique(emotion_data.get("events", []), emotions, ("timestamp", "snippet_hash"))
    emotion_data["_meta"] = {
        "total_events": len(emotion_data["events"]),
        "categories": sorted({item.get("category", "") for item in emotion_data["events"] if item.get("category")}),
        "updated": now_iso(),
        "version": 2,
    }

    markers = load_markers(root, user_id)
    processed_dates = sorted(set(markers.get("scan_window", {}).get("processed_dates", [])) | set(hashes.keys()))
    last_ts = max([message.timestamp for message in messages], default=markers.get("last_processed_timestamp"))
    markers["scan_window"] = {
        "start": min(processed_dates) if processed_dates else "",
        "end": max(processed_dates) if processed_dates else "",
        "processed_dates": processed_dates,
    }
    markers["last_processed_timestamp"] = last_ts
    markers["processed_hashes"] = {**markers.get("processed_hashes", {}), **hashes}
    existing_activities = markers.get("activities", {})
    for name, item in activities.items():
        old = existing_activities.get(name, {})
        item["first_seen"] = old.get("first_seen") or item["first_seen"]
        item["dates"] = sorted(set(old.get("dates", [])) | set(item.get("dates", [])))
        item["source_transcripts"] = sorted(set(old.get("source_transcripts", [])) | set(item.get("source_transcripts", [])))
        item["user_only_sources"] = sorted(set(old.get("user_only_sources", [])) | set(item.get("user_only_sources", [])))
        existing_activities[name] = item
    markers["activities"] = existing_activities
    markers["last_l2_at"] = now_iso()
    markers["_next_scan_hint"] = f"从 {last_ts or '(unknown)'} 后的新用户消息开始增量扫描"

    if not dry_run:
        atomic_write_json(active_knowledge_path(root, user_id), active)
        atomic_write_json(emotion_events_path(root, user_id), emotion_data)
        atomic_write_json(markers_path(root, user_id), markers)
        atomic_write_text(memory_brief_path(root, user_id), build_memory_brief(active, emotion_data))
        atomic_write_text(proposed_updates_path(root, user_id), build_proposed_updates(preferences, rules))
        atomic_write_text(readme_path(root, user_id), build_readme(user_id, messages, hashes, active, emotion_data))

    return {
        "preferences": len(preferences),
        "rules": len(rules),
        "projects": len(projects),
        "emotions": len(emotions),
        "activities": len(activities),
        "last_processed_timestamp": last_ts,
    }


def select_messages(root: Path, user_id: str, mode: str, start: str = "", end: str = "") -> Tuple[List[UserMessage], bool]:
    marker_exists = markers_path(root, user_id).is_file()
    if mode == "bootstrap":
        if not start or not end:
            raise RuntimeError("bootstrap requires --from-date and --to-date")
        return collect_user_messages(root, user_id, start, end), marker_exists
    if not marker_exists:
        return [], False
    markers = load_markers(root, user_id)
    return messages_after(collect_user_messages(root, user_id), markers.get("last_processed_timestamp")), True


def run_l2(root: Path, user_id: str, mode: str = "deep", dry_run: bool = False, start: str = "", end: str = "", manual: bool = False) -> Dict[str, Any]:
    preflight(root, user_id)
    messages, marker_exists = select_messages(root, user_id, "bootstrap" if mode == "bootstrap" else "deep", start, end)
    if not marker_exists and mode != "bootstrap":
        if not dry_run:
            save_initial_markers(root, user_id)
        return {"status": "needs_bootstrap", "reason": "incremental_markers.json is missing; run bootstrap explicitly", "data_root": str(conversation_root(root, user_id))}
    if not messages:
        return {"status": "skipped", "reason": "no new user messages", "messages": 0}

    grouped = group_by_date(messages)
    planned_paths = []
    for date_str in grouped:
        planned_paths.extend([rel(user_only_path(root, user_id, date_str), root), rel(chunk_path(root, user_id, date_str), root)])
    planned_paths.extend([
        rel(active_knowledge_path(root, user_id), root),
        rel(emotion_events_path(root, user_id), root),
        rel(markers_path(root, user_id), root),
        rel(memory_brief_path(root, user_id), root),
        rel(proposed_updates_path(root, user_id), root),
        rel(readme_path(root, user_id), root),
    ])
    if dry_run:
        return {"status": "dryrun", "mode": mode, "messages": len(messages), "dates": sorted(grouped.keys()), "planned_writes": planned_paths, "last_processed_timestamp": max(item.timestamp for item in messages)}

    with acquire_lock(root, user_id):
        backup = create_backup(root, user_id, dry_run=False)
        hashes = write_user_only(root, user_id, grouped, dry_run=False)
        chunks_written = write_chunks(root, user_id, grouped, hashes, dry_run=False)
        insight = update_history_insight(root, user_id, messages, hashes, dry_run=False)
    return {
        "status": "done",
        "mode": mode,
        "manual": manual,
        "messages": len(messages),
        "dates": sorted(grouped.keys()),
        "user_only_files": len(hashes),
        "chunks_written": chunks_written,
        "history_insight": insight,
        "backup": backup,
        "productions": {
            "user_only": rel(summaries_root(root, user_id) / "user_only", root),
            "chunks": rel(summaries_root(root, user_id) / "chunks", root),
            "history_insight": rel(history_root(root, user_id), root),
        },
    }


def status(root: Path, user_id: str) -> Dict[str, Any]:
    preflight(root, user_id)
    marker_file = markers_path(root, user_id)
    markers = load_markers(root, user_id)
    all_messages = collect_user_messages(root, user_id)
    pending = messages_after(all_messages, markers.get("last_processed_timestamp")) if marker_file.is_file() else []
    return {
        "status": "ok",
        "user_id": user_id,
        "safe_user_id": safe_segment(user_id),
        "conversation_root": str(conversation_root(root, user_id)),
        "marker_exists": marker_file.is_file(),
        "last_processed_timestamp": markers.get("last_processed_timestamp"),
        "pending_user_messages": len(pending),
        "transcript_user_messages": len(all_messages),
        "history_insight": str(history_root(root, user_id)),
        "chunks": str(summaries_root(root, user_id) / "chunks"),
    }


def auto_check(
    root: Path,
    user_id: str,
    min_messages: int,
    cooldown_hours: float,
    sample_rate: float,
    min_transcript_files: int = DEFAULT_MIN_NEW_TRANSCRIPT_FILES,
) -> Dict[str, Any]:
    preflight(root, user_id)
    if not markers_path(root, user_id).is_file():
        return {"status": "skipped", "reason": "marker missing; auto never bootstraps"}
    markers = load_markers(root, user_id)
    pending = messages_after(collect_user_messages(root, user_id), markers.get("last_processed_timestamp"))
    pending_files = sorted({message.source_transcript for message in pending})
    min_files = max(1, int(min_transcript_files))
    if len(pending) < min_messages and len(pending_files) < min_files:
        return {
            "status": "skipped",
            "reason": f"new user messages {len(pending)} < {min_messages} and transcript files {len(pending_files)} < {min_files}",
            "pending_user_messages": len(pending),
            "pending_transcript_files": len(pending_files),
        }
    last_l2 = markers.get("last_l2_at")
    if last_l2:
        try:
            if datetime.now() - datetime.fromisoformat(last_l2) < timedelta(hours=cooldown_hours):
                return {"status": "skipped", "reason": f"cooldown {cooldown_hours}h", "pending_user_messages": len(pending)}
        except ValueError:
            pass
    if random.random() >= sample_rate:
        markers["last_poll_at"] = now_iso()
        atomic_write_json(markers_path(root, user_id), markers)
        return {"status": "skipped", "reason": f"sample miss rate={sample_rate}", "pending_user_messages": len(pending)}
    return prepare_run(root, user_id, trigger_mode="auto", dry_run=False)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safe G4W L4 memory maintenance")
    parser.add_argument("--user-id", default=DEFAULT_USER_ID)
    parser.add_argument("--workspace-root", default="")
    parser.add_argument("--mode", choices=["status", "dryrun", "deep", "bootstrap", "auto", "prepare", "validate-finalize"], default="status")
    parser.add_argument("--trigger-mode", choices=["deep", "bootstrap", "auto"], default="deep")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--from-date", dest="from_date", default="")
    parser.add_argument("--to-date", dest="to_date", default="")
    parser.add_argument("--min-new-user-messages", type=int, default=DEFAULT_MIN_NEW_USER_MESSAGES)
    parser.add_argument("--cooldown-hours", type=float, default=DEFAULT_COOLDOWN_HOURS)
    parser.add_argument("--sample-rate", type=float, default=DEFAULT_SAMPLE_RATE)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--force", action="store_true", help="Compatibility flag; does not enable full-history scans.")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    root = workspace_root(args.workspace_root)
    try:
        if args.mode == "status":
            result = status(root, args.user_id)
        elif args.mode == "dryrun":
            result = prepare_run(root, args.user_id, trigger_mode=args.trigger_mode, start=args.from_date, end=args.to_date, dry_run=True)
        elif args.mode == "prepare":
            result = prepare_run(root, args.user_id, trigger_mode=args.trigger_mode, start=args.from_date, end=args.to_date, dry_run=False)
        elif args.mode == "validate-finalize":
            if not args.run_id:
                raise RuntimeError("validate-finalize requires --run-id")
            result = validate_finalize(root, args.user_id, args.run_id, dry_run=False)
        elif args.mode == "bootstrap":
            result = prepare_run(root, args.user_id, trigger_mode="bootstrap", start=args.from_date, end=args.to_date, dry_run=False)
        elif args.mode == "auto":
            result = auto_check(root, args.user_id, args.min_new_user_messages, args.cooldown_hours, args.sample_rate)
        else:
            result = prepare_run(root, args.user_id, trigger_mode="deep", dry_run=False)
    except Exception as exc:
        result = {"status": "error", "error": str(exc)}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") != "error" else 1


if __name__ == "__main__":
    raise SystemExit(main())
