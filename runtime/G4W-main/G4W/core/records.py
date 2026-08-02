import copy
import json
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .storage import JsonStore, safe_segment


SHANGHAI = timezone(timedelta(hours=8))
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


TIMELINE_CATEGORY_RULES = (
    (("睡眠", "睡觉", "入睡", "起床"), "rest", "rest.sleep"),
    (("午睡", "小睡"), "rest", "rest.nap"),
    (("安静时段", "安静时间", "发呆", "未回复", "休息"), "rest", "rest.other"),
    (("早餐", "午餐", "晚餐", "吃饭", "用餐", "面包", "夜宵"), "life", "life.meal"),
    (("洗漱", "洗澡", "刷牙"), "life", "life.hygiene"),
    (("散步", "走路"), "exercise", "exercise.walk"),
    (("健身", "锻炼", "训练", "拉伸", "运动"), "exercise", "exercise.workout"),
    (("医院", "就医", "看病"), "health", "health.hospital"),
    (("吃药", "用药"), "health", "health.medication"),
    (("疼", "不舒服", "症状"), "health", "health.pain"),
    (("bug", "修复", "重构", "测试", "调试", "代码", "编程", "开发", "系统", "worker", "g4w", "G4W", "sop", "目录分析", "记忆维护", "忙工作"), "work", "work.coding"),
    (("会议", "开会"), "work", "work.meeting"),
    (("写作", "文档", "报告"), "work", "work.writing"),
    (("研究生", "入学", "课程", "上课"), "study", "study.course"),
    (("pdf", "阅读", "看书", "资料"), "study", "study.reading"),
    (("学习", "练习", "复盘", "调研"), "study", "study.review"),
    (("聊天", "互动", "陪伴", "通话"), "social", "social.chat"),
    (("电影", "视频", "追剧"), "entertainment", "entertainment.video"),
    (("游戏",), "entertainment", "entertainment.game"),
    (("音乐", "听歌"), "entertainment", "entertainment.music"),
    (("通勤", "公交", "地铁"), "travel", "travel.commute"),
    (("出门", "路上", "出行"), "travel", "travel.transit"),
)


def infer_timeline_classification(title: str, note: str = "", tags=None) -> tuple[str, str]:
    text = " ".join([str(title or ""), str(note or ""), *[str(tag) for tag in (tags or [])]]).lower()
    for keywords, category_id, subcategory_id in TIMELINE_CATEGORY_RULES:
        if any(keyword.lower() in text for keyword in keywords):
            return category_id, subcategory_id
    return "life", "life.other"


def shanghai_now() -> datetime:
    return datetime.now(SHANGHAI)


def normalize_date(value: str = "") -> str:
    text = str(value or "").strip() or shanghai_now().date().isoformat()
    if not DATE_RE.fullmatch(text):
        raise ValueError("date must use YYYY-MM-DD")
    datetime.strptime(text, "%Y-%m-%d")
    return text


def normalize_time(value: str = "") -> str:
    text = str(value or "").strip() or shanghai_now().strftime("%H:%M")
    datetime.strptime(text, "%H:%M")
    return text


class DiaryStore:
    def __init__(self, root: Path, legacy_root: Path | None = None, conversation_root: Path | None = None):
        self.root = Path(root)
        self.legacy_root = Path(legacy_root) if legacy_root else None
        self.conversation_root = Path(conversation_root) if conversation_root else None
        if self.conversation_root is None:
            self.root.mkdir(parents=True, exist_ok=True)

    def _active_root(self, sender_id: str = "") -> Path:
        if self.conversation_root is None:
            return self.root
        sender = safe_segment(sender_id)
        active = self.conversation_root / sender / "summaries" / "diary"
        self._migrate_root_diary(active, sender)
        return active

    def _migrate_root_diary(self, active: Path, sender: str) -> None:
        marker = active / ".layout-version.json"
        if marker.is_file():
            return
        active.mkdir(parents=True, exist_ok=True)
        moved = []
        if self.root.is_dir():
            for source in sorted(self.root.glob("????-??-??.md")):
                try:
                    day = normalize_date(source.stem)
                except ValueError:
                    continue
                target = active / day[:4] / day[5:7] / f"{day}.md"
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    continue
                shutil.move(str(source), str(target))
                moved.append(str(target))
        marker.write_text(json.dumps({
            "version": 1,
            "sender": sender,
            "migratedFrom": str(self.root),
            "moved": moved,
            "completedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _active_path(self, day: str, sender_id: str = "") -> Path:
        root = self._active_root(sender_id)
        if self.conversation_root is None:
            return root / f"{day}.md"
        return root / day[:4] / day[5:7] / f"{day}.md"

    def append(self, content: str, title: str = "", date: str = "", at_time: str = "", sender_id: str = "") -> dict:
        body = str(content or "").strip()
        if not body:
            raise ValueError("diary content is empty")
        day = normalize_date(date)
        clock = normalize_time(at_time)
        path = self._active_path(day, sender_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        heading = f"## {clock}" + (f" {str(title).strip()}" if str(title).strip() else "")
        prefix = "\n\n" if path.exists() and path.stat().st_size else ""
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{prefix}{heading}\n\n{body}")
        return {"ok": True, "date": day, "time": clock, "title": str(title).strip(), "content": body, "filePath": str(path)}

    def read(self, date: str = "", sender_id: str = "") -> dict:
        day = normalize_date(date)
        active = self._active_path(day, sender_id)
        former = self.root / f"{day}.md" if self.conversation_root is not None else None
        legacy = self.legacy_root / f"{day}.md" if self.legacy_root else None
        sections = []
        seen = set()
        for path in (legacy, former, active):
            if path and path.is_file():
                value = path.read_text(encoding="utf-8", errors="replace").strip()
                if value and value not in seen:
                    seen.add(value)
                    sections.append(value)
        return {"date": day, "content": "\n\n".join(item for item in sections if item)}

    def list_dates(self, limit: int = 30, sender_id: str = "") -> dict:
        dates = set()
        active = self._active_root(sender_id)
        for root in (self.legacy_root, self.root, active):
            if root and root.is_dir():
                dates.update(path.stem for path in root.rglob("????-??-??.md"))
        return {"dates": sorted(dates, reverse=True)[:max(1, min(int(limit or 30), 365))]}


class TimelineStore:
    def __init__(self, path: Path, legacy_path: Path | None = None):
        self.path = Path(path)
        self.legacy_path = Path(legacy_path) if legacy_path else None
        self.store = JsonStore(self.path, {"version": 1, "timezone": "Asia/Shanghai", "facts": {}})

    def _legacy(self) -> dict:
        if not self.legacy_path or not self.legacy_path.is_file():
            return {"facts": {}}
        return JsonStore(self.legacy_path, {"facts": {}}).read()

    @staticmethod
    def _read_json(path: Path) -> dict:
        try:
            return __import__("json").loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def merged_state(self) -> dict:
        self.repair_categories()
        legacy = self._legacy()
        active = self.store.read()
        facts = copy.deepcopy(legacy.get("facts") or {})
        facts.update(copy.deepcopy(active.get("facts") or {}))
        active_taxonomy = self.path.with_name("timeline-taxonomy.json")
        legacy_taxonomy = self.legacy_path.with_name("timeline-taxonomy.json") if self.legacy_path else None
        taxonomy_source = self._read_json(active_taxonomy)
        if not taxonomy_source and legacy_taxonomy:
            taxonomy_source = self._read_json(legacy_taxonomy)
        taxonomy = taxonomy_source.get("taxonomy") or active.get("taxonomy") or legacy.get("taxonomy") or {}
        return {
            "version": 1,
            "timezone": active.get("timezone") or legacy.get("timezone") or "Asia/Shanghai",
            "taxonomy": taxonomy,
            "facts": facts,
        }

    def _day(self, date: str) -> dict | None:
        active = self.store.read().get("facts", {}).get(date)
        if active is not None:
            return active
        return self._legacy().get("facts", {}).get(date)

    def read(self, date: str = "") -> dict:
        day = normalize_date(date)
        value = self._day(day)
        return {"date": day, "status": (value or {}).get("status", "empty"), "events": copy.deepcopy((value or {}).get("events", []))}

    def list_dates(self, limit: int = 30) -> dict:
        dates = set(self._legacy().get("facts", {})) | set(self.store.read().get("facts", {}))
        return {"dates": sorted(dates, reverse=True)[:max(1, min(int(limit or 30), 3650))]}

    def write(self, date: str, events: list[dict], mode: str = "append", finalize: bool = False) -> dict:
        day = normalize_date(date)
        if not isinstance(events, list) or not events:
            raise ValueError("timeline events must be a non-empty list")
        normalized = [self._normalize_event(day, event) for event in events]

        def update(state):
            facts = state.setdefault("facts", {})
            current = facts.get(day)
            if current is None:
                current = copy.deepcopy(self._legacy().get("facts", {}).get(day) or {"events": []})
                facts[day] = current
            existing = list(current.get("events", []))
            if mode == "replace":
                merged = normalized
            elif mode == "upsert":
                by_id = {str(item.get("id")): item for item in existing if item.get("id")}
                for item in normalized:
                    by_id[item["id"]] = item
                merged = list(by_id.values())
            elif mode == "append":
                known = {str(item.get("id")) for item in existing}
                merged = existing + [item for item in normalized if item["id"] not in known]
            else:
                raise ValueError("timeline mode must be append, upsert, or replace")
            merged.sort(key=lambda item: (item.get("startAt", ""), item.get("endAt", ""), item.get("id", "")))
            current.update({
                "status": "final" if finalize else "draft",
                "updatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "source": "G4W-python-conductor",
                "events": merged,
            })
            return copy.deepcopy(current)

        result = self.store.update(update)
        return {"ok": True, "date": day, "status": result["status"], "eventCount": len(result["events"]), "events": normalized}

    def delete(self, date: str, event_id: str) -> dict:
        day = normalize_date(date)
        target = str(event_id or "").strip()
        if not target:
            raise ValueError("event_id is required")

        def update(state):
            facts = state.setdefault("facts", {})
            current = facts.get(day)
            if current is None:
                current = copy.deepcopy(self._legacy().get("facts", {}).get(day) or {"events": []})
                facts[day] = current
            before = len(current.get("events", []))
            current["events"] = [item for item in current.get("events", []) if str(item.get("id")) != target]
            current["updatedAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            current["source"] = "G4W-python-conductor"
            return before != len(current["events"]), len(current["events"])

        removed, count = self.store.update(update)
        return {"ok": removed, "date": day, "eventId": target, "eventCount": count}

    def repair_categories(self) -> dict:
        """Repair legacy/default-life events that never received taxonomy ids."""
        snapshot = self.store.read()
        needs_repair = any(
            not str(event.get("subcategoryId") or "").strip()
            for value in (snapshot.get("facts") or {}).values()
            for event in (value or {}).get("events") or []
        )
        if not needs_repair:
            return {"ok": True, "repaired": 0, "events": []}
        repaired = []

        def update(state):
            for day, value in (state.get("facts") or {}).items():
                for event in (value or {}).get("events") or []:
                    current_category = str(event.get("categoryId") or "").strip()
                    current_subcategory = str(event.get("subcategoryId") or "").strip()
                    if current_subcategory:
                        continue
                    inferred_category, inferred_subcategory = infer_timeline_classification(
                        event.get("title", ""), event.get("note", ""), event.get("tags") or []
                    )
                    if current_category and current_category != "life":
                        inferred_category = current_category
                        inferred_subcategory = f"{current_category}.other"
                    event["categoryId"] = inferred_category
                    event["subcategoryId"] = inferred_subcategory
                    repaired.append({"date": day, "id": event.get("id", ""), "categoryId": inferred_category, "subcategoryId": inferred_subcategory})
            return repaired

        result = self.store.update(update)
        return {"ok": True, "repaired": len(result), "events": result}

    @staticmethod
    def _normalize_event(day: str, event: dict) -> dict:
        if not isinstance(event, dict):
            raise ValueError("each timeline event must be an object")
        title = str(event.get("title") or "").strip()
        start_at = str(event.get("startAt") or event.get("start_at") or "").strip()
        end_at = str(event.get("endAt") or event.get("end_at") or "").strip()
        if not title or not start_at or not end_at:
            raise ValueError("timeline event requires title, startAt and endAt")
        start = datetime.fromisoformat(start_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_at.replace("Z", "+00:00"))
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("timeline timestamps require an explicit timezone offset")
        if end <= start:
            raise ValueError("timeline endAt must be after startAt")
        if start.astimezone(SHANGHAI).date().isoformat() != day or (end - timedelta(microseconds=1)).astimezone(SHANGHAI).date().isoformat() != day:
            raise ValueError("timeline event must stay inside the target Asia/Shanghai date")
        event_id = str(event.get("id") or f"evt_{uuid.uuid4().hex[:16]}")
        requested_category = str(event.get("categoryId") or event.get("category_id") or "").strip()
        requested_subcategory = str(event.get("subcategoryId") or event.get("subcategory_id") or "").strip()
        inferred_category, inferred_subcategory = infer_timeline_classification(title, event.get("note", ""), event.get("tags") or [])
        if requested_subcategory:
            category_id = requested_category or requested_subcategory.split(".", 1)[0]
            subcategory_id = requested_subcategory
        elif requested_category and requested_category != "life":
            category_id = requested_category
            subcategory_id = f"{requested_category}.other"
        else:
            category_id = inferred_category
            subcategory_id = inferred_subcategory
        return {
            "id": event_id,
            "startAt": start.isoformat(),
            "endAt": end.isoformat(),
            "title": title,
            "note": str(event.get("note") or "").strip(),
            "categoryId": category_id,
            "subcategoryId": subcategory_id,
            "eventNodeId": str(event.get("eventNodeId") or event.get("event_node_id") or "").strip(),
            "tags": [str(tag).strip() for tag in (event.get("tags") or []) if str(tag).strip()],
            "confidence": max(0.0, min(float(event.get("confidence", 1.0)), 1.0)),
            "sourceMessageIds": [str(item) for item in (event.get("sourceMessageIds") or []) if str(item)],
        }
