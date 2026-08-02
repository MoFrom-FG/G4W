import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..core.storage import JsonStore


SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
TIMELINE_INTERVAL_SECONDS = 2 * 60 * 60
DIARY_INTERVAL_SECONDS = 6 * 60 * 60

TIMELINE_SIGNAL_PATTERNS = (
    ("life", re.compile(r"吃|饭|早餐|早饭|午饭|中饭|晚饭|夜宵|kfc|肯德基|洗澡|洗头|洗漱|家务|收拾|购物|买了|办事", re.I)),
    ("work", re.compile(r"工作|代码|开发|调试|仓库|实现|修复|开会|会议|写作|沟通|项目", re.I)),
    ("study", re.compile(r"学习|看书|阅读|课程|上课|练习|复盘|考试|作业", re.I)),
    ("exercise", re.compile(r"运动|散步|走走|锻炼|健身|拉伸|跑步", re.I)),
    ("entertainment", re.compile(r"娱乐|游戏|王者|视频|看剧|电影|音乐|刷手机|刷视频|抖音|b站|bilibili", re.I)),
    ("health", re.compile(r"健康|吃药|服药|药|头痛|头疼|疼|不舒服|医院|看病|门诊|adhd", re.I)),
    ("social", re.compile(r"社交|聊天|通话|打电话|家人|朋友|消息", re.I)),
    ("care", re.compile(r"照料|照顾|宠物|猫|家庭|自己|自我照顾", re.I)),
    ("travel", re.compile(r"出门|回家|到家|离家|在路上|通勤|开车|地铁|公交|打车|出行", re.I)),
    ("rest", re.compile(r"睡|睡觉|午睡|小睡|醒了|起床|躺|休息|放空|困", re.I)),
)
DIARY_SIGNAL_PATTERN = re.compile(r"累|困|开心|难过|焦虑|担心|生气|崩|重要|决定|确认|想法|状态|喜欢|讨厌|压力|舒服|不舒服|陪|夸|记得|记住", re.I)
SLEEP_OR_WAKE_PATTERN = re.compile(r"睡|睡觉|午睡|小睡|醒了|起床|晚安|准备睡", re.I)
DIARY_CONTEXT_PATTERN = re.compile(r"一整天|今天|下午|晚上|上午|凌晨|一直|很|太|终于|刚刚", re.I)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value="") -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except Exception:
            parsed = _now_utc()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value="") -> str:
    return _parse_time(value).isoformat().replace("+00:00", "Z")


def _unique(values) -> list[str]:
    return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def detect_maintenance_signals(text: str) -> dict:
    normalized = str(text or "").strip()
    signals = [name for name, pattern in TIMELINE_SIGNAL_PATTERNS if pattern.search(normalized)]
    timeline_dirty = bool(signals)
    force = bool(SLEEP_OR_WAKE_PATTERN.search(normalized))
    diary_dirty = bool(
        DIARY_SIGNAL_PATTERN.search(normalized)
        or force
        or (timeline_dirty and DIARY_CONTEXT_PATTERN.search(normalized))
    )
    return {
        "timelineDirty": timeline_dirty,
        "diaryDirty": diary_dirty,
        "forceMaintenance": force,
        "signals": _unique(signals),
    }


class WechatMaintenanceService:
    """Python port of the original G4W timeline/diary check-in gate."""

    def __init__(
        self,
        path: Path,
        timeline_interval_seconds: int = TIMELINE_INTERVAL_SECONDS,
        diary_interval_seconds: int = DIARY_INTERVAL_SECONDS,
    ):
        self.store = JsonStore(Path(path), {"senders": {}})
        self.timeline_interval_seconds = max(1, int(timeline_interval_seconds))
        self.diary_interval_seconds = max(1, int(diary_interval_seconds))

    def _entry(self, state: dict, sender_id: str) -> dict:
        return state.setdefault("senders", {}).setdefault(sender_id, {
            "dirtySince": "",
            "lastTimelineAt": "",
            "lastDiaryAt": "",
            "lastTranscriptAt": "",
            "lastMaintenanceCheckinAt": "",
            "timelineDirty": False,
            "diaryDirty": False,
            "forceMaintenance": False,
            "signals": [],
            "lastUserText": "",
        })

    def mark_user_message(self, sender_id: str, text: str, received_at="") -> dict | None:
        sender = str(sender_id or "").strip()
        body = str(text or "").strip()
        if not sender or not body:
            return None
        signals = detect_maintenance_signals(body)
        if not signals["timelineDirty"] and not signals["diaryDirty"]:
            return None
        at = _iso(received_at)

        def update(state):
            entry = self._entry(state, sender)
            entry["lastTranscriptAt"] = at
            entry["lastUserText"] = body[:239] + "…" if len(body) > 240 else body
            entry["dirtySince"] = entry.get("dirtySince") or at
            entry["timelineDirty"] = bool(entry.get("timelineDirty") or signals["timelineDirty"])
            entry["diaryDirty"] = bool(entry.get("diaryDirty") or signals["diaryDirty"])
            entry["forceMaintenance"] = bool(entry.get("forceMaintenance") or signals["forceMaintenance"])
            entry["signals"] = _unique([*(entry.get("signals") or []), *signals["signals"]])[-20:]
            return {"senderId": sender, **entry}

        return self.store.update(update)

    def mark_timeline_written(self, sender_id: str, at="") -> dict:
        return self._mark_written(sender_id, "timeline", at)

    def mark_diary_written(self, sender_id: str, at="") -> dict:
        return self._mark_written(sender_id, "diary", at)

    def _mark_written(self, sender_id: str, field: str, at="") -> dict:
        sender = str(sender_id or "").strip()
        written_at = _iso(at)

        def update(state):
            entry = self._entry(state, sender)
            if field == "timeline":
                entry["lastTimelineAt"] = written_at
                entry["timelineDirty"] = False
            elif field == "diary":
                entry["lastDiaryAt"] = written_at
                entry["diaryDirty"] = False
            if not entry.get("timelineDirty") and not entry.get("diaryDirty"):
                entry.update({"dirtySince": "", "forceMaintenance": False, "signals": [], "lastUserText": ""})
            return {"senderId": sender, **entry}

        return self.store.update(update)

    def build_checkin(self, sender_id: str, user_name: str = "User", now="") -> dict:
        sender = str(sender_id or "").strip()
        current = _parse_time(now)
        if not sender:
            return {"mode": "companion", "text": f"{user_name or 'User'} comes to mind again."}
        entry = dict(self.store.read().get("senders", {}).get(sender, {}))
        cross_day = self._cross_day(entry.get("lastMaintenanceCheckinAt") or entry.get("lastTranscriptAt"), current)
        timeline_due = bool(entry.get("timelineDirty")) and (
            entry.get("forceMaintenance") or cross_day or self._elapsed(entry.get("lastTimelineAt"), current, self.timeline_interval_seconds)
        )
        diary_due = bool(entry.get("diaryDirty")) and (
            entry.get("forceMaintenance") or cross_day or self._elapsed(entry.get("lastDiaryAt"), current, self.diary_interval_seconds)
        )
        if not timeline_due and not diary_due:
            return {"mode": "companion", "text": f"{user_name or 'User'} comes to mind again."}
        reasons = []
        if timeline_due:
            reasons.append("timeline")
        if diary_due:
            reasons.append("diary")
        if cross_day:
            reasons.append("cross-day")
        if entry.get("forceMaintenance"):
            reasons.append("sleep-or-wake")
        local = current.astimezone(SHANGHAI)
        lines = [
            "WECHAT_MAINTENANCE_CHECKIN",
            f"reason: {', '.join(_unique(reasons))}",
            f"senderId: {sender}",
            f"now: {local.strftime('%Y-%m-%d %H:%M')} Asia/Shanghai; UTC {_iso(current)}",
            f"dirtySince: {entry.get('dirtySince') or '(unknown)'}",
            f"life_event_signal: {', '.join(entry.get('signals') or []) or '(unspecified)'}",
        ]
        if entry.get("lastUserText"):
            lines.append(f"latest user signal: {entry['lastUserText']}")
        return {
            "mode": "maintenance",
            "text": "\n".join(lines),
            "dueTimeline": timeline_due,
            "dueDiary": diary_due,
            "signals": list(entry.get("signals") or []),
        }

    def mark_maintenance_queued(self, sender_id: str, mode: str, at="") -> dict | None:
        if str(mode or "").strip() != "maintenance":
            return None
        sender = str(sender_id or "").strip()

        def update(state):
            entry = self._entry(state, sender)
            entry["lastMaintenanceCheckinAt"] = _iso(at)
            return {"senderId": sender, **entry}

        return self.store.update(update)

    @staticmethod
    def _elapsed(value: str, now: datetime, seconds: int) -> bool:
        if not value:
            return True
        return (now - _parse_time(value)).total_seconds() >= seconds

    @staticmethod
    def _cross_day(value: str, now: datetime) -> bool:
        if not value:
            return False
        return _parse_time(value).astimezone(SHANGHAI).date() != now.astimezone(SHANGHAI).date()
