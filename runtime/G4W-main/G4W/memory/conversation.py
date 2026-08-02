import hashlib
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..core.storage import JsonStore, safe_segment


ASSISTANT_REPLY_MARKER = "G4W:assistant_reply_path="
MESSAGE_SUBTYPE_MARKER = "G4W:message_subtype="
MESSAGE_ID_MARKER = "G4W:message_id="
PARENT_ROUND_MARKER = "G4W:parent_round_id="
SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
LEGACY_ARCHIVE_ENVELOPE = re.compile(
    r"^\s*长回复已归档[：:]\s*(?P<path>[^\r\n]+)"
    r"(?:\r?\n摘要[：:]\s*(?P<summary>[\s\S]*?))?\s*$",
    flags=re.I,
)


def _strip_protocol_text(text: str) -> str:
    value = str(text or "").replace("\r\n", "\n")
    value = re.sub(r"(?im)^\s*(?:\*\*)?(?:LLM Running \(Turn \d+\)|Turn \d+) \.\.\.(?:\*\*)?\s*$", "", value)
    value = re.sub(r"(?im)^\s*\[ROUND END\]\s*$", "", value)
    value = re.sub(r"<silent\s*/?>", "", value, flags=re.I)
    return re.sub(r"\n{3,}", "\n\n", value).strip()


class ConversationStore:
    def __init__(
        self,
        root: Path,
        memory_root: Path,
        recent_pairs: int = 20,
        recent_max_chars: int = 12000,
        long_assistant_reply_chars: int = 300,
        long_user_prompt_chars: int = 1500,
        short_path_mirror=None,
        f1_read_path: str = "legacy",
        stop_aggregate_write: bool = False,
    ):
        self.root = Path(root)
        self.memory_root = Path(memory_root)
        self.recent_pairs = recent_pairs
        self.recent_max_chars = recent_max_chars
        self.long_assistant_reply_chars = long_assistant_reply_chars
        self.long_user_prompt_chars = long_user_prompt_chars
        self.short_path_mirror = short_path_mirror
        self.stop_aggregate_write = bool(stop_aggregate_write)
        # F1: "legacy" reads aggregate transcript.md; "daily_primary" concatenates
        # daily files under transcripts/; "mirror_primary" is stub → legacy.
        # Default legacy keeps bit-identical behaviour.
        mode = str(f1_read_path or "legacy").strip().lower()
        if mode in ("daily_primary", "daily", "daily-primary"):
            self.f1_read_path = "daily_primary"
        elif mode in ("mirror_primary", "mirror", "mirror-primary"):
            self.f1_read_path = "mirror_primary"
        else:
            self.f1_read_path = "legacy"
        self.bindings = JsonStore(self.root / "bindings.json", {"bindings": {}})
        self.transcript_lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)
        self.memory_root.mkdir(parents=True, exist_ok=True)

    def binding_key(self, account_id: str, sender_id: str) -> str:
        return f"{account_id}:{sender_id}"

    def bind(self, account_id: str, sender_id: str, context_token: str = "") -> dict:
        key = self.binding_key(account_id, sender_id)
        now = time.time()
        def apply(state):
            entry = state.setdefault("bindings", {}).setdefault(key, {})
            entry.update({
                "accountId": account_id,
                "senderId": sender_id,
                "contextToken": context_token or entry.get("contextToken", ""),
                "lastInboundAt": now,
                "updatedAt": now,
            })
            return dict(entry)
        return self.bindings.update(apply)

    def get_binding(self, binding_key: str) -> dict | None:
        return self.bindings.read().get("bindings", {}).get(binding_key)

    def binding_keys_for_sender(self, sender_id: str) -> list[str]:
        target = str(sender_id or "")
        return [
            key for key, value in self.bindings.read().get("bindings", {}).items()
            if str(value.get("senderId") or "") == target
        ]

    def latest_inbound_at(self, sender_id: str) -> float:
        target = str(sender_id or "")
        values = [
            float(value.get("lastInboundAt", 0) or 0)
            for value in self.bindings.read().get("bindings", {}).values()
            if str(value.get("senderId") or "") == target
        ]
        return max(values, default=0.0)

    def update_binding(self, binding_key: str, **values) -> dict:
        def update(state):
            entry = state.setdefault("bindings", {}).get(binding_key)
            if not entry:
                raise KeyError(f"binding not found: {binding_key}")
            entry.update({key: value for key, value in values.items() if value is not None})
            entry["updatedAt"] = time.time()
            return dict(entry)
        return self.bindings.update(update)

    def conversation_dir(self, sender_id: str) -> Path:
        return self.root / safe_segment(sender_id)

    def transcript_path(self, sender_id: str) -> Path:
        """Compatibility aggregate used by L4 and existing diagnostics."""
        return self.conversation_dir(sender_id) / "transcript.md"

    def daily_transcript_path(self, sender_id: str, stamp: time.struct_time | None = None) -> Path:
        stamp = stamp or time.localtime()
        return self.conversation_dir(sender_id) / "transcripts" / time.strftime("%Y/%m/%Y-%m-%d.md", stamp)

    def list_daily_transcript_paths(self, sender_id: str) -> list[Path]:
        """Return sorted daily transcript files under transcripts/ (read-only list)."""
        root = self.conversation_dir(sender_id) / "transcripts"
        if not root.is_dir():
            return []
        paths = [p for p in root.rglob("*.md") if p.is_file()]
        paths.sort(key=lambda p: str(p).replace("\\", "/"))
        return paths

    def _read_legacy_transcript(self, sender_id: str) -> str:
        try:
            return self.transcript_path(sender_id).read_text(encoding="utf-8")
        except Exception:
            return ""

    def _read_daily_primary_transcript(self, sender_id: str) -> str:
        """Concatenate daily transcript files in path-sorted order (shadow/primary candidate)."""
        chunks: list[str] = []
        for path in self.list_daily_transcript_paths(sender_id):
            try:
                chunks.append(path.read_text(encoding="utf-8"))
            except Exception:
                continue
        return "".join(chunks)

    def read_transcript(self, sender_id: str, mode: str | None = None) -> str:
        """Read conversation transcript text according to F1 read path.

        mode=None uses store.f1_read_path (default ``legacy`` → aggregate transcript.md).
        ``daily_primary`` concatenates daily files under transcripts/ (RO; no write).
        ``mirror_primary`` is not implemented → fail-soft fall back to legacy.
        """
        chosen = str(mode if mode is not None else self.f1_read_path or "legacy").strip().lower()
        if chosen in ("daily_primary", "daily", "daily-primary"):
            return self._read_daily_primary_transcript(sender_id)
        # mirror_primary and unknown → legacy (no raise; shadow-only contract)
        return self._read_legacy_transcript(sender_id)

    def shadow_compare_transcripts(self, sender_id: str) -> dict:
        """RO dual-read: legacy aggregate vs daily_primary concat. No writes, no cutover.

        Task-aligned keys: aggregate_bytes, daily_bytes, aggregate_hash, daily_hash,
        mid_hint_or_counts, mode. Extra probe fields retained for observe evidence.
        """
        legacy_text = self._read_legacy_transcript(sender_id)
        daily_text = self._read_daily_primary_transcript(sender_id)
        daily_paths = [str(p) for p in self.list_daily_transcript_paths(sender_id)]
        aggregate = self.transcript_path(sender_id)
        legacy_bytes = len(legacy_text.encode("utf-8"))
        daily_bytes = len(daily_text.encode("utf-8"))
        aggregate_hash = hashlib.sha256(legacy_text.encode("utf-8")).hexdigest() if legacy_text else ""
        daily_hash = hashlib.sha256(daily_text.encode("utf-8")).hexdigest() if daily_text else ""
        return {
            # TASK_T1_W1 required keys
            "aggregate_bytes": legacy_bytes,
            "daily_bytes": daily_bytes,
            "aggregate_hash": aggregate_hash,
            "daily_hash": daily_hash,
            "mid_hint_or_counts": {
                "legacy_chars": len(legacy_text),
                "daily_chars": len(daily_text),
                "daily_file_count": len(daily_paths),
                "equal_text": legacy_text == daily_text,
            },
            "mode": self.f1_read_path,
            # Extra probe fields
            "sender_id": sender_id,
            "f1_read_path_default": self.f1_read_path,
            "legacy_path": str(aggregate),
            "legacy_exists": aggregate.is_file(),
            "legacy_chars": len(legacy_text),
            "daily_paths": daily_paths,
            "daily_file_count": len(daily_paths),
            "daily_chars": len(daily_text),
            "equal_text": legacy_text == daily_text,
            "legacy_sha256_10": aggregate_hash[:10] if aggregate_hash else "",
            "daily_sha256_10": daily_hash[:10] if daily_hash else "",
        }

    # Back-compat alias used by early T1-W1 smoke / evidence
    def shadow_compare_transcript(self, sender_id: str) -> dict:
        return self.shadow_compare_transcripts(sender_id)

    def assistant_replies_dir(self, sender_id: str, stamp: time.struct_time | None = None) -> Path:
        stamp = stamp or time.localtime()
        return self.conversation_dir(sender_id) / "assistant-replies" / time.strftime("%Y/%m", stamp)

    def user_prompts_dir(self, sender_id: str, stamp: time.struct_time | None = None) -> Path:
        stamp = stamp or time.localtime()
        return self.conversation_dir(sender_id) / "user-prompts" / time.strftime("%Y/%m", stamp)

    @staticmethod
    def _short_hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]

    def save_user_prompt(self, sender_id: str, text: str, force: bool = False) -> Path | None:
        """Archive only the user's original text; never system/history/assistant context."""
        body = str(text or "").strip()
        if not body or (not force and len(body) <= self.long_user_prompt_chars):
            return None
        stamp = time.localtime()
        path = self.user_prompts_dir(sender_id, stamp) / f"{time.strftime('%Y%m%d-%H%M%S', stamp)}-{self._short_hash(body)}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(body + "\n", encoding="utf-8")
        return path

    def _save_assistant_reply(self, sender_id: str, text: str, transcript_path: Path, stamp: time.struct_time) -> Path | None:
        if not self.long_assistant_reply_chars or len(text) <= self.long_assistant_reply_chars:
            return None
        path = self.assistant_replies_dir(sender_id, stamp) / f"{time.strftime('%Y%m%d-%H%M%S', stamp)}-{self._short_hash(text)}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            body = "\n".join([
                "# Assistant Reply", "", f"sender: {sender_id}",
                f"time: {time.strftime('%Y-%m-%d %H:%M:%S', stamp)} Asia/Shanghai",
                f"transcript: {transcript_path}", "", "## Reply", "", text, "",
            ])
            path.write_text(body, encoding="utf-8")
        return path

    @staticmethod
    def _marker_value(value: str) -> str:
        return str(value or "").replace("-->", "-- >").strip()

    def append(
        self,
        sender_id: str,
        role: str,
        text: str,
        timestamp="",
        subtype: str = "",
        message_id: str = "",
        parent_round_id: str = "",
    ) -> bool:
        body = str(text or "").strip()
        if role not in ("User", "Assistant") or not body:
            return False
        stamp = self._local_stamp(timestamp)
        aggregate = self.transcript_path(sender_id)
        daily = self.daily_transcript_path(sender_id, stamp)
        safe_message_id = self._marker_value(message_id)
        message_marker = f"<!-- {MESSAGE_ID_MARKER}{safe_message_id} -->" if safe_message_id else ""
        with self.transcript_lock:
            if message_marker:
                for probe in (daily, aggregate):  # daily-first dedup (required when aggregate frozen)
                    if not probe.exists():
                        continue
                    try:
                        if message_marker in probe.read_text(encoding="utf-8", errors="replace"):
                            return False
                    except OSError:
                        pass
            assistant_path = self._save_assistant_reply(sender_id, body, daily, stamp) if role == "Assistant" else None
            markers = []
            if assistant_path:
                markers.append(f"<!-- {ASSISTANT_REPLY_MARKER}{assistant_path} -->")
            if str(subtype or "").strip():
                markers.append(f"<!-- {MESSAGE_SUBTYPE_MARKER}{self._marker_value(subtype)} -->")
            if message_marker:
                markers.append(message_marker)
            if str(parent_round_id or "").strip():
                markers.append(f"<!-- {PARENT_ROUND_MARKER}{self._marker_value(parent_round_id)} -->")
            marker_text = "" if not markers else "\n" + "\n".join(markers)
            block = f"[{time.strftime('%Y-%m-%d %H:%M:%S', stamp)} Asia/Shanghai] {role}:\n{body}{marker_text}\n\n"
            write_pairs = [
                (daily, f"# G4W WeChat Transcript\nsender: {sender_id}\ndate: {time.strftime('%Y-%m-%d', stamp)}\n\n"),
            ]
            if not self.stop_aggregate_write:
                write_pairs.insert(
                    0,
                    (aggregate, f"# G4W Transcript\nsender: {sender_id}\n\n"),
                )
            for path, header in write_pairs:
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    path.write_text(header, encoding="utf-8")
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(block)
            if self.short_path_mirror is not None:
                self.short_path_mirror.write_conversation(
                    sender_id, role, body, timestamp, subtype, message_id, parent_round_id,
                    legacy_paths=[str(aggregate.relative_to(self.root)), str(daily.relative_to(self.root))],
                )
            return True

    @staticmethod
    def _local_stamp(value="") -> time.struct_time:
        if isinstance(value, (int, float)):
            parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
        else:
            try:
                parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
            except Exception:
                parsed = datetime.now(timezone.utc)
        return parsed.astimezone(SHANGHAI).timetuple()

    def _blocks(self, sender_id: str) -> list[tuple[str, str, str, str, str]]:
        # Route through read_transcript so F1 flag is respected; default legacy
        # is bit-identical to the previous direct transcript.md read.
        try:
            text = self.read_transcript(sender_id)
        except Exception:
            return []
        if not text:
            return []
        pattern = re.compile(r"^\[([^\]]+)\] (User|Assistant):\n", re.MULTILINE)
        headers = list(pattern.finditer(text))
        blocks = []
        marker_re = re.compile(rf"\n?<!--\s*{re.escape(ASSISTANT_REPLY_MARKER)}(.*?)\s*-->", re.I)
        subtype_re = re.compile(rf"\n?<!--\s*{re.escape(MESSAGE_SUBTYPE_MARKER)}(.*?)\s*-->", re.I)
        message_id_re = re.compile(rf"\n?<!--\s*{re.escape(MESSAGE_ID_MARKER)}(.*?)\s*-->", re.I)
        parent_round_re = re.compile(rf"\n?<!--\s*{re.escape(PARENT_ROUND_MARKER)}(.*?)\s*-->", re.I)
        for index, match in enumerate(headers):
            end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
            body = text[match.end():end].strip()
            marker = marker_re.search(body)
            reply_path = marker.group(1).strip() if marker else ""
            subtype_marker = subtype_re.search(body)
            subtype = subtype_marker.group(1).strip() if subtype_marker else ""
            body = marker_re.sub("", body).strip()
            body = subtype_re.sub("", body).strip()
            body = message_id_re.sub("", body).strip()
            body = parent_round_re.sub("", body).strip()
            if match.group(2) == "Assistant":
                body = _strip_protocol_text(body)
            blocks.append((match.group(2), body, match.group(1), reply_path, subtype))
        return blocks

    def _visible_blocks(self, sender_id: str, include_unanswered_progress: bool = True) -> list[tuple[str, str, str, str, str]]:
        """Return the actual visible conversation projection.

        Unanswered Worker progress is collapsed to the latest milestone, while
        a progress message that was followed by a user reply remains part of
        the real conversation. A Worker final replaces any still-pending
        progress message.
        """
        blocks = []
        pending_progress = None
        for block in self._blocks(sender_id):
            role, body, stamp, reply_path, subtype = block
            if role == "Assistant" and subtype == "worker-progress":
                pending_progress = block
                continue
            if role == "User":
                if pending_progress:
                    blocks.append(pending_progress)
                pending_progress = None
                blocks.append(block)
                continue
            if role == "Assistant" and subtype == "worker-final":
                pending_progress = None
                blocks.append(block)
                continue
            pending_progress = None
            blocks.append(block)
        if pending_progress and include_unanswered_progress:
            blocks.append(pending_progress)
        return blocks

    def _conversation_groups(self, sender_id: str, exclude_open_user: bool = False) -> list[dict]:
        groups = []
        current = None
        for role, body, stamp, reply_path, subtype in self._visible_blocks(sender_id):
            if role == "User":
                if current:
                    groups.append(current)
                current = {"user": (body, stamp), "assistants": []}
            elif current and current.get("user"):
                current["assistants"].append((body, stamp, reply_path, subtype))
            else:
                groups.append({"user": None, "assistants": [(body, stamp, reply_path, subtype)]})
        if current:
            groups.append(current)
        if exclude_open_user and groups and groups[-1].get("user") and not groups[-1]["assistants"]:
            groups.pop()
        return groups

    def _find_user_prompt_reference(self, sender_id: str, body: str) -> Path | None:
        if len(str(body or "")) <= self.long_user_prompt_chars:
            return None
        suffix = f"-{self._short_hash(str(body or '').strip())}.md"
        root = self.conversation_dir(sender_id) / "user-prompts"
        try:
            return max((path for path in root.rglob(f"*{suffix}") if path.is_file()), key=lambda path: path.stat().st_mtime)
        except (ValueError, OSError):
            return None

    @staticmethod
    def _compact_summary(text: str, limit: int = 240) -> str:
        value = re.sub(r"\s+", " ", _strip_protocol_text(text)).strip()
        return value if len(value) <= limit else value[:limit].rstrip() + "…"

    @staticmethod
    def _compact_timestamp(stamp: str) -> str:
        value = str(stamp or "").strip()
        match = re.match(r"\d{4}-(\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})", value)
        return f"{match.group(1)} {match.group(2)}" if match else value

    def _clean_message_content(self, sender_id: str, role: str, body: str, stamp: str, reply_path: str = "", subtype: str = "") -> str:
        legacy_archive = LEGACY_ARCHIVE_ENVELOPE.fullmatch(str(body or "").strip()) if role == "Assistant" else None
        if role == "User":
            label = "user"
        elif reply_path or legacy_archive:
            label = "assistant/history-archive"
        else:
            label = f"assistant-{subtype}" if subtype else "assistant"
        header = f"[{self._compact_timestamp(stamp)}][{label}]"
        visible = str(body or "").strip()
        if role == "User":
            reference = self._find_user_prompt_reference(sender_id, visible)
            if reference:
                visible = "\n".join([
                    f"长消息原文：{reference}",
                    "这是用户当时发送的完整原文路径；需要细节时按需读取。",
                ])
        elif reply_path or legacy_archive:
            summary = self._compact_summary(visible)
            archive_path = reply_path
            if legacy_archive:
                archive_path = str(legacy_archive.group("path") or "").strip()
                summary = str(legacy_archive.group("summary") or "").strip() or summary
            visible = "\n".join([
                f"archive: {archive_path}",
                "internal: 这是此前微信可见长回复的历史压缩引用；禁止把archive路径或本块原样回复给用户。",
            ])
            if summary:
                visible += f"\npreview: {summary}"
        return f"{header}\n{visible}".strip()

    def format_current_user(self, sender_id: str, body: str, timestamp: str = "") -> str:
        text = str(body or "").strip()
        stamp = self._local_stamp(timestamp)
        stamp_text = time.strftime("%Y-%m-%d %H:%M:%S Asia/Shanghai", stamp)
        reference = self.save_user_prompt(sender_id, text, force=True) if len(text) > self.long_user_prompt_chars else None
        if reference:
            return "\n".join([
                f"[{self._compact_timestamp(stamp_text)}][user/current]",
                f"长消息原文：{reference}",
                "这是用户当前消息的完整原文路径；需要细节时按需读取。",
            ])
        return f"[{self._compact_timestamp(stamp_text)}][user/current]\n{text}".strip()

    @staticmethod
    def _tail_user_rounds(groups: list[dict], count: int) -> list[dict]:
        selected = []
        user_rounds = 0
        for group in reversed(groups):
            selected.append(group)
            if group.get("userRound"):
                user_rounds += 1
                if user_rounds >= max(1, int(count or 1)):
                    break
        return list(reversed(selected))

    def _clean_history_groups(self, sender_id: str, exclude_open_user: bool = False) -> list[dict]:
        """Build immutable visible-message records for the API history.

        Each transcript block is its own record.  Grouping a user and all
        later Assistant messages under one hash would make the hash change
        whenever a delayed check-in or Worker report arrived, forcing a full
        history rebuild and defeating prefix caching.
        """
        result = []
        blocks = self._visible_blocks(sender_id, include_unanswered_progress=False)
        if exclude_open_user and blocks and blocks[-1][0] == "User":
            blocks = blocks[:-1]
        for role, body, stamp, reply_path, subtype in blocks:
            content = self._clean_message_content(sender_id, role, body, stamp, reply_path, subtype)
            identity = (role, stamp, body, reply_path, subtype)
            key = hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()[:20]
            result.append({
                "key": key,
                "userRound": role == "User",
                "messages": [{
                    "role": "user" if role == "User" else "assistant",
                    "content": [{"type": "text", "text": content}],
                }],
            })
        return result

    def clean_history_path(self, sender_id: str) -> Path:
        return self.conversation_dir(sender_id) / "conductor" / "history" / "clean-window.json"

    def reset_clean_history(self, sender_id: str) -> dict:
        available = self._clean_history_groups(sender_id, exclude_open_user=False)
        boundary = str(available[-1].get("key") or "") if available else ""
        state = {
            "version": 4,
            "groups": [],
            "compactions": 0,
            "sessionBoundaryKey": boundary,
            "minimumUserRounds": 20,
            "maximumUserRounds": 40,
            "userRounds": 0,
            "messageCount": 0,
            "lastGroupKey": "",
            "updatedAt": time.time(),
        }
        JsonStore(self.clean_history_path(sender_id), state).write(state)
        return state

    def sync_clean_history(self, sender_id: str, exclude_open_user: bool = False, minimum_user_rounds: int = 20, maximum_user_rounds: int = 40) -> dict:
        """Maintain an append-only clean API history with hysteresis.

        The window starts with the latest 20 user rounds, grows append-only to
        40 and only then compacts back to 20. This keeps the DeepSeek request
        prefix stable for many rounds instead of invalidating a rolling window
        on every new message.
        """
        minimum = max(1, int(minimum_user_rounds or 20))
        maximum = max(minimum + 1, int(maximum_user_rounds or 40))
        available = self._clean_history_groups(sender_id, exclude_open_user=exclude_open_user)
        path = self.clean_history_path(sender_id)
        store = JsonStore(path, {"version": 4, "groups": [], "compactions": 0})
        state = store.read()
        boundary = str(state.get("sessionBoundaryKey") or "")
        if boundary:
            available_keys_before_boundary = [group["key"] for group in available]
            if boundary in available_keys_before_boundary:
                available = available[available_keys_before_boundary.index(boundary) + 1:]
        existing = list(state.get("groups") or []) if int(state.get("version", 0) or 0) == 4 else []
        available_keys = [group["key"] for group in available]
        mode = "unchanged"

        if not existing:
            selected = self._tail_user_rounds(available, minimum)
            mode = "initialized"
        else:
            last_key = str(existing[-1].get("key") or "")
            if last_key and last_key in available_keys:
                selected = existing + available[available_keys.index(last_key) + 1:]
                if len(selected) != len(existing):
                    mode = "appended"
            else:
                selected = self._tail_user_rounds(available, minimum)
                mode = "rebuilt"

        deduplicated = []
        seen = set()
        for group in selected:
            key = str(group.get("key") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            deduplicated.append(group)
        selected = deduplicated
        user_rounds = sum(1 for group in selected if group.get("userRound"))
        compacted = False
        if user_rounds > maximum:
            selected = self._tail_user_rounds(selected, minimum)
            user_rounds = sum(1 for group in selected if group.get("userRound"))
            compacted = True
            mode = "compacted"

        changed = selected != existing or int(state.get("minimumUserRounds", 0) or 0) != minimum or int(state.get("maximumUserRounds", 0) or 0) != maximum
        if changed:
            state.update({
                "version": 4,
                "groups": selected,
                "minimumUserRounds": minimum,
                "maximumUserRounds": maximum,
                "userRounds": user_rounds,
                "messageCount": sum(len(group.get("messages") or []) for group in selected),
                "lastGroupKey": str(selected[-1].get("key") or "") if selected else "",
                "updatedAt": time.time(),
            })
            if compacted:
                state["compactions"] = int(state.get("compactions", 0) or 0) + 1
                state["lastCompactedAt"] = time.time()
            store.write(state)

        messages = [message for group in selected for message in (group.get("messages") or [])]
        return {
            "messages": messages,
            "groups": len(selected),
            "userRounds": user_rounds,
            "messageCount": len(messages),
            "mode": mode,
            "compacted": compacted,
            "path": str(path),
        }

    def recent(self, sender_id: str, exclude_open_user: bool = False) -> str:
        groups = self._conversation_groups(sender_id, exclude_open_user=exclude_open_user)
        selected_groups = []
        user_rounds = 0
        for group in reversed(groups):
            if group.get("user"):
                if user_rounds >= self.recent_pairs:
                    break
                user_rounds += 1
            selected_groups.append(group)
        rendered_groups = []
        for group in reversed(selected_groups):
            messages = []
            if group.get("user"):
                user_body, user_stamp = group["user"]
                messages.append(("user", user_body, user_stamp))
            for assistant_body, assistant_stamp, reply_path, subtype in group["assistants"]:
                visible = reply_path if reply_path else assistant_body
                label = f"assistant/{subtype}" if subtype else "assistant"
                messages.append((label, visible, assistant_stamp))
            rendered_groups.append(messages)
        selected = []
        used = 0
        for messages in reversed(rendered_groups):
            group_text = self._render_recent_messages(messages, include_header=False)
            addition = len(group_text) + (1 if selected else 0)
            if selected and used + addition > self.recent_max_chars:
                break
            selected.append(messages)
            used += addition
        if not selected:
            return ""
        flattened = [message for group in reversed(selected) for message in group]
        return self._render_recent_messages(flattened, include_header=True)

    @staticmethod
    def _render_recent_messages(messages: list[tuple[str, str, str]], include_header: bool = True) -> str:
        lines = ["# 最近微信记录", "时区：Asia/Shanghai"] if include_header else []
        current_date = ""
        for role, body, stamp in messages:
            date = str(stamp or "")[:10]
            clock = str(stamp or "")[11:19]
            if date and date != current_date:
                lines.extend(([""] if lines else []) + [f"## {date}"])
                current_date = date
            label_time = clock or str(stamp or "")
            lines.extend([f"[{label_time}][{role}]", str(body or "").strip(), ""])
        return "\n".join(lines).strip()

    def user_memory_path(self, sender_id: str) -> Path:
        return self.memory_root / "users" / f"{safe_segment(sender_id)}.md"

    def operational_memory_path(self, sender_id: str) -> Path:
        return self.memory_root / "operational" / f"{safe_segment(sender_id)}.md"

    def memory_index_path(self, sender_id: str) -> Path:
        return self.conversation_dir(sender_id) / "indexes" / "memory-index.md"

    def build_memory_index(self, sender_id: str) -> str:
        conversation = self.conversation_dir(sender_id)
        index = self.memory_index_path(sender_id)
        lines = [
            "# G4W Memory Index",
            "",
            "这是L1导航层，只保存定位信息；具体事实必须按需读取L2或原始证据。",
            "",
            "## L0 确定性事实",
            f"- 用户事实：{self.user_memory_path(sender_id)}",
            f"- 操作偏好：{self.operational_memory_path(sender_id)}",
            "",
            "## L2 语义与日记",
            f"- 语义摘要：{conversation / 'summaries' / 'history_insight' / 'memory_brief.md'}",
            f"- 活跃知识：{conversation / 'summaries' / 'history_insight' / 'active_knowledge.json'}",
            f"- 情绪事件：{conversation / 'summaries' / 'history_insight' / 'emotion_events.json'}",
            f"- 日记：{conversation / 'summaries' / 'diary'}",
            "",
            "## 原始证据",
            f"- 每日微信记录：{conversation / 'transcripts'}",
            f"- 长助手回复：{conversation / 'assistant-replies'}",
            f"- L4运行证据：{conversation / 'summaries' / 'history_insight' / 'runs'}",
            "",
            "检索顺序：先查语义摘要或日记，再依据source path读取对应transcript核实精确日期与原文。",
        ]
        text = "\n".join(lines).rstrip() + "\n"
        index.parent.mkdir(parents=True, exist_ok=True)
        if not index.exists() or index.read_text(encoding="utf-8", errors="replace") != text:
            index.write_text(text, encoding="utf-8")
        return text

    def read_memory(self, sender_id: str, query: str | None = None) -> str:
        """Assemble stable long-term memory for the system prompt.

        ``query`` is accepted for compatibility only. Dynamic retrieval hits are
        exposed by ``retrieval_context`` so the system prompt stays stable.
        """
        sections = []
        for title, path in (("User Memory", self.user_memory_path(sender_id)), ("Operational Memory", self.operational_memory_path(sender_id))):
            try:
                value = path.read_text(encoding="utf-8").strip()
            except Exception:
                value = ""
            if value:
                sections.append(f"## {title}\n{value}")
        sections.append("## L1 Memory Index\n" + self.build_memory_index(sender_id).strip())
        return "\n\n".join(sections)

    def retrieval_context(self, sender_id: str, query: str | None = None) -> str:
        """Assemble this-round retrieval hits outside the system prompt."""
        inject_query = str(query or "").strip() or None
        base_sections = [self.read_memory(sender_id)]
        sections = []
        # P: production hybrid retrieval context (feature-flagged, fail-soft, read-only)
        try:
            from .hybrid_reader import hybrid_section_for

            hybrid_text = hybrid_section_for(
                self.memory_root, sender_id, base_sections, query=inject_query
            )
            if hybrid_text:
                sections.append(hybrid_text)
        except Exception:
            pass
        # R1: optional vector retrieval context (product gate + legacy flag).
        # Import/config failure -> fail-closed (treat addon off; never inject).
        try:
            # Product total switch first: installed and enabled; off -> no embed/HNSW inject.
            try:
                from .vector.vector_config import vector_enabled as _addon_vector_enabled

                addon_on = bool(_addon_vector_enabled())
            except Exception:
                addon_on = False

            if addon_on:
                from .vector import vector_retrieval_enabled
                from .vector.prod_inject import vector_section_for

                if vector_retrieval_enabled():
                    vtext = vector_section_for(
                        self.memory_root, sender_id, base_sections, query=inject_query
                    )
                    if vtext:
                        sections.append(vtext)
        except Exception:
            # any inject-path failure -> omit vector section only
            pass
        return "\n\n".join(sections)

    def remember(self, sender_id: str, kind: str, content: str) -> dict:
        path = self.operational_memory_path(sender_id) if kind == "operational" else self.user_memory_path(sender_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = str(content or "").strip()
        if not line:
            raise ValueError("memory content is empty")
        # User memory must not store structural/log pollution; operational stays freer.
        if str(kind or "").lower() != "operational":
            try:
                from ..agents.reply_gates import gate_remember_content

                ok_mem, reason = gate_remember_content(line)
                if not ok_mem:
                    return {
                        "ok": False,
                        "error": "memory_gate_blocked",
                        "reason": reason,
                        "path": str(path),
                    }
            except Exception:
                pass
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if line not in existing:
            with path.open("a", encoding="utf-8") as handle:
                if not existing:
                    handle.write(f"# {kind.title()} Memory\n")
                handle.write(f"\n- {line}\n")
        return {"ok": True, "path": str(path)}
