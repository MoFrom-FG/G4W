from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone


SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
CATEGORY_COLORS = {
    name: f"var(--cat-{name})" for name in (
        "life", "work", "study", "exercise", "entertainment",
        "health", "social", "care", "travel", "rest",
    )
}
ZH_LABELS = {
    "life": "生活", "life.meal": "吃饭", "life.hygiene": "洗漱", "life.chores": "家务",
    "life.shopping": "购物", "life.errand": "办事", "life.other": "其他生活",
    "work": "工作", "work.coding": "编码", "work.meeting": "会议", "work.writing": "写作",
    "work.communication": "沟通", "work.other": "其他工作",
    "study": "学习", "study.reading": "阅读", "study.course": "课程", "study.practice": "练习",
    "study.review": "复盘", "study.other": "其他学习",
    "exercise": "运动", "exercise.walk": "散步", "exercise.workout": "锻炼",
    "exercise.stretch": "拉伸", "exercise.other": "其他运动",
    "entertainment": "娱乐", "entertainment.video": "视频", "entertainment.game": "游戏",
    "entertainment.social_media": "社交媒体", "entertainment.music": "音乐",
    "entertainment.other": "其他娱乐",
    "health": "健康", "health.rest": "恢复休息", "health.medication": "用药",
    "health.pain": "症状处理", "health.hospital": "就医", "health.other": "其他健康",
    "social": "社交", "social.chat": "聊天", "social.call": "通话", "social.family": "家人相处",
    "social.other": "其他社交",
    "care": "照料", "care.pet": "宠物照料", "care.household": "家庭照料",
    "care.self": "自我照料", "care.other": "其他照料",
    "travel": "出行", "travel.commute": "通勤", "travel.transit": "路程",
    "travel.other": "其他出行",
    "rest": "休息", "rest.sleep": "睡眠", "rest.nap": "小睡", "rest.idle": "发呆",
    "rest.other": "其他休息",
}
DEFAULT_CHILDREN = {
    "life": ("meal", "hygiene", "chores", "shopping", "errand", "other"),
    "work": ("coding", "meeting", "writing", "communication", "other"),
    "study": ("reading", "course", "practice", "review", "other"),
    "exercise": ("walk", "workout", "stretch", "other"),
    "entertainment": ("video", "game", "social_media", "music", "other"),
    "health": ("rest", "medication", "pain", "hospital", "other"),
    "social": ("chat", "call", "family", "other"),
    "care": ("pet", "household", "self", "other"),
    "travel": ("commute", "transit", "other"),
    "rest": ("sleep", "nap", "idle", "other"),
}


def default_taxonomy() -> dict:
    return {
        "categories": [
            {
                "id": category_id,
                "label": ZH_LABELS[category_id],
                "color": CATEGORY_COLORS[category_id],
                "children": [
                    {"id": f"{category_id}.{child}", "label": ZH_LABELS[f"{category_id}.{child}"]}
                    for child in children
                ],
            }
            for category_id, children in DEFAULT_CHILDREN.items()
        ],
        "eventNodes": [],
    }


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _duration(start_at: str, end_at: str) -> int:
    return max(0, int(((_parse(end_at) - _parse(start_at)).total_seconds() / 60.0) + 0.5))


def _local(value: str) -> datetime:
    return _parse(value).astimezone(SHANGHAI)


def _clock(value: str) -> str:
    return _local(value).strftime("%H:%M")


def _event_date(value: str) -> str:
    return _local(value).date().isoformat()


def _compact(minutes: int) -> str:
    hours, remaining = divmod(max(0, int(minutes)), 60)
    if not hours:
        return f"{remaining}m"
    return f"{hours}h" if not remaining else f"{hours}h{remaining}m"


def _full_duration(minutes: int) -> str:
    hours, remaining = divmod(max(0, int(minutes)), 60)
    if not hours:
        return f"{remaining}分钟"
    return f"{hours}小时" if not remaining else f"{hours}小时{remaining}分钟"


def _localized_taxonomy(raw: dict | None) -> dict:
    source = raw if isinstance(raw, dict) and raw.get("categories") else default_taxonomy()
    categories = []
    for category in source.get("categories") or []:
        category_id = str(category.get("id") or "")
        categories.append({
            **category,
            "label": ZH_LABELS.get(category_id, category.get("label") or category_id),
            "children": [
                {**child, "label": ZH_LABELS.get(str(child.get("id") or ""), child.get("label") or child.get("id"))}
                for child in (category.get("children") or [])
            ],
        })
    return {"categories": categories, "eventNodes": [dict(node) for node in (source.get("eventNodes") or [])]}


def _category_map(taxonomy: dict) -> dict:
    result = {}
    for category in taxonomy.get("categories") or []:
        category_id = str(category.get("id") or "")
        color = CATEGORY_COLORS.get(category_id, category.get("color") or "var(--cat-life)")
        result[category_id] = {"categoryId": category_id, "label": category.get("label") or category_id, "color": color}
        for child in category.get("children") or []:
            child_id = str(child.get("id") or "")
            result[child_id] = {"categoryId": category_id, "label": child.get("label") or child_id, "color": color}
    return result


def _item_style(color: str) -> str:
    return f"background:{color};border-color:{color};color:var(--text);"


def _day_timeline(day: str, value: dict, category_map: dict) -> dict:
    items = []
    for event in value.get("events") or []:
        category = category_map.get(event.get("subcategoryId")) or category_map.get(event.get("categoryId")) or {}
        color = category.get("color") or "#4E79A7"
        minutes = _duration(event.get("startAt"), event.get("endAt"))
        items.append({
            "id": event.get("id"), "start": event.get("startAt"), "end": event.get("endAt"),
            "content": f"{event.get('title', '')} | {_compact(minutes)}",
            "style": _item_style(color),
            "tooltip": {
                "title": event.get("title", ""), "note": event.get("note", ""), "color": color,
                "durationText": _full_duration(minutes),
                "timeText": f"{_clock(event.get('startAt'))} - {_clock(event.get('endAt'))}",
            },
            "className": f"cat-{event.get('categoryId', '')}",
        })
    return {
        "date": day, "start": f"{day}T00:00:00.000+08:00", "end": f"{day}T23:59:59.999+08:00",
        "groups": [], "items": items,
    }


def _week_start(day: str) -> str:
    parsed = date.fromisoformat(day)
    return (parsed - timedelta(days=parsed.weekday())).isoformat()


def _week_ranges(dates: list[str]) -> list[dict]:
    starts = sorted({_week_start(day) for day in dates})
    result = []
    for start in starts:
        start_date = date.fromisoformat(start)
        days = [(start_date + timedelta(days=index)).isoformat() for index in range(7)]
        result.append({"key": start, "label": f"{start} 当周", "dates": days})
    return result


def _week_timeline(week: dict, facts: dict, category_map: dict) -> dict:
    weekday_labels = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
    groups = [{"id": day, "content": weekday_labels[index]} for index, day in enumerate(week["dates"])]
    items = []
    anchor = date(2000, 1, 1)
    for day in week["dates"]:
        for event in (facts.get(day) or {}).get("events") or []:
            category = category_map.get(event.get("subcategoryId")) or category_map.get(event.get("categoryId")) or {}
            color = category.get("color") or "#4E79A7"
            start_clock, end_clock = _clock(event.get("startAt")), _clock(event.get("endAt"))
            start_at = f"{anchor.isoformat()}T{start_clock}:00+08:00"
            end_day = anchor + (timedelta(days=1) if _parse(event.get("endAt")) <= _parse(event.get("startAt")) else timedelta())
            end_at = f"{end_day.isoformat()}T{end_clock}:00+08:00"
            minutes = _duration(event.get("startAt"), event.get("endAt"))
            items.append({
                "id": f"{day}:{event.get('id')}", "group": day, "start": start_at, "end": end_at,
                "content": f"{event.get('title', '')} | {_compact(minutes)}", "style": _item_style(color),
                "tooltip": {
                    "title": event.get("title", ""), "note": event.get("note", ""), "color": color,
                    "durationText": _full_duration(minutes), "timeText": f"{start_clock} - {end_clock}", "dateText": day,
                },
                "className": f"cat-{event.get('categoryId', '')}",
            })
    return {
        "key": week["key"], "label": week["label"],
        "start": "2000-01-01T00:00:00.000+08:00", "end": "2000-01-01T23:59:59.999+08:00",
        "groups": groups, "items": items,
    }


def _event_blocks(events: list[dict], include_date: bool) -> list[dict]:
    result = []
    for event in sorted(events, key=lambda item: item.get("startAt", "")):
        day_label = _event_date(event.get("startAt"))[5:] if include_date else ""
        time_label = f"{_clock(event.get('startAt'))} - {_clock(event.get('endAt'))}"
        minutes = _duration(event.get("startAt"), event.get("endAt"))
        result.append({
            "eventNodeId": event.get("id"), "label": event.get("title", ""), "dateLabel": day_label,
            "timeLabel": time_label, "compactDuration": _compact(minutes),
            "fullLabel": f"{day_label + ' ' if include_date else ''}{time_label} {event.get('title', '')}",
            "note": event.get("note", ""), "status": "official" if event.get("eventNodeId") else "derived",
            "categoryId": event.get("categoryId", ""), "subcategoryId": event.get("subcategoryId", ""),
            "subcategoryLabel": event.get("subcategoryId", ""), "minutes": minutes,
        })
    return result


def _hourly(events: list[dict]) -> list[dict]:
    buckets = [{"key": str(hour), "label": f"{hour:02d}:00", "minutes": 0} for hour in range(24)]
    for event in events:
        start, end = _parse(event.get("startAt")), _parse(event.get("endAt"))
        event_day = _local(event.get("startAt")).date()
        for hour in range(24):
            hour_start = datetime.combine(event_day, datetime.min.time(), SHANGHAI) + timedelta(hours=hour)
            hour_end = hour_start + timedelta(hours=1)
            overlap = max(0.0, (min(end, hour_end) - max(start, hour_start)).total_seconds())
            buckets[hour]["minutes"] += int(overlap / 60.0 + 0.5)
    return buckets


def _aggregate(key: str, label: str, unit: str, events: list[dict], category_map: dict, all_dates: list[str]) -> dict:
    total = sum(_duration(event.get("startAt"), event.get("endAt")) for event in events)
    category_minutes = defaultdict(int)
    subcategory_minutes = defaultdict(int)
    category_trends = {day: defaultdict(int) for day in all_dates}
    subcategory_trends = {day: defaultdict(int) for day in all_dates}
    for event in events:
        minutes = _duration(event.get("startAt"), event.get("endAt"))
        category_id = event.get("categoryId") or "life"
        subcategory_id = event.get("subcategoryId") or ""
        day = _event_date(event.get("startAt"))
        category_minutes[category_id] += minutes
        category_trends.setdefault(day, defaultdict(int))[category_id] += minutes
        if subcategory_id:
            subcategory_minutes[subcategory_id] += minutes
            subcategory_trends.setdefault(day, defaultdict(int))[subcategory_id] += minutes
    categories = []
    for category_id, minutes in sorted(category_minutes.items(), key=lambda item: item[1], reverse=True):
        meta = category_map.get(category_id) or {}
        categories.append({
            "categoryId": category_id, "label": meta.get("label") or category_id,
            "color": meta.get("color") or CATEGORY_COLORS.get(category_id, "var(--cat-life)"),
            "minutes": minutes, "percent": round(minutes / total, 4) if total else 0,
        })
    category_details = {}
    for category in categories:
        category_id = category["categoryId"]
        children = []
        for subcategory_id, minutes in sorted(subcategory_minutes.items(), key=lambda item: item[1], reverse=True):
            meta = category_map.get(subcategory_id) or {}
            if meta.get("categoryId") != category_id and not subcategory_id.startswith(category_id + "."):
                continue
            children.append({
                "subcategoryId": subcategory_id, "categoryId": category_id,
                "label": meta.get("label") or subcategory_id, "color": category["color"],
                "minutes": minutes, "percent": round(minutes / category["minutes"], 4) if category["minutes"] else 0,
            })
        related = [event for event in events if event.get("categoryId") == category_id]
        category_details[category_id] = {
            "categoryId": category_id, "label": category["label"], "color": category["color"],
            "trend": [{"key": day, "label": day[5:], "minutes": category_trends.get(day, {}).get(category_id, 0)} for day in all_dates],
            "subcategories": children, "events": _event_blocks(related, len(all_dates) > 1),
        }
    subcategory_details = {}
    for subcategory_id, minutes in subcategory_minutes.items():
        meta = category_map.get(subcategory_id) or {}
        category_id = meta.get("categoryId") or subcategory_id.split(".", 1)[0]
        related = [event for event in events if event.get("subcategoryId") == subcategory_id]
        subcategory_details[subcategory_id] = {
            "subcategoryId": subcategory_id, "categoryId": category_id,
            "label": meta.get("label") or subcategory_id,
            "color": meta.get("color") or CATEGORY_COLORS.get(category_id, "var(--cat-life)"),
            "trend": [{"key": day, "label": day[5:], "minutes": subcategory_trends.get(day, {}).get(subcategory_id, 0)} for day in all_dates],
            "events": _event_blocks(related, len(all_dates) > 1),
        }
    return {
        "key": key, "label": label, "unit": unit, "totalMinutes": total,
        "categories": categories, "categoryDetails": category_details, "subcategoryDetails": subcategory_details,
    }


def build_timeline_views(state: dict, meta: dict | None = None, locale: str = "zh-CN") -> dict:
    meta = dict(meta or {})
    facts = state.get("facts") or {}
    dates = sorted(facts)
    taxonomy = _localized_taxonomy(state.get("taxonomy"))
    category_map = _category_map(taxonomy)
    weeks = _week_ranges(dates)
    day_timelines = {day: _day_timeline(day, facts.get(day) or {}, category_map) for day in dates}
    week_timelines = {week["key"]: _week_timeline(week, facts, category_map) for week in weeks}
    day_ranges = {}
    for day in dates:
        events = (facts.get(day) or {}).get("events") or []
        aggregate = _aggregate(day, day, "hour", events, category_map, [day])
        for category_id, detail in aggregate["categoryDetails"].items():
            detail["trend"] = _hourly([event for event in events if event.get("categoryId") == category_id])
        for subcategory_id, detail in aggregate["subcategoryDetails"].items():
            detail["trend"] = _hourly([event for event in events if event.get("subcategoryId") == subcategory_id])
        day_ranges[day] = aggregate
    week_ranges = {}
    for week in weeks:
        events = [event for day in week["dates"] for event in ((facts.get(day) or {}).get("events") or [])]
        week_ranges[week["key"]] = _aggregate(week["key"], week["label"], "day", events, category_map, week["dates"])
    month_dates = defaultdict(list)
    for day in dates:
        month_dates[day[:7]].append(day)
    month_ranges = {}
    for month, days in month_dates.items():
        events = [event for day in days for event in ((facts.get(day) or {}).get("events") or [])]
        month_ranges[month] = _aggregate(month, month, "day", events, category_map, days)
    generated = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "meta": {
            "generatedAt": generated, "updatedAt": meta.get("updatedAt", ""),
            "taxonomyUpdatedAt": meta.get("taxonomyUpdatedAt", ""), "factsUpdatedAt": meta.get("factsUpdatedAt", ""),
            "isDemoData": False, "timezone": state.get("timezone") or "Asia/Shanghai", "locale": locale or "zh-CN",
            "availableDates": dates, "latestDate": dates[-1] if dates else "",
        },
        "taxonomy": taxonomy,
        "timelines": {"day": day_timelines, "week": week_timelines},
        "ranges": {"day": day_ranges, "week": week_ranges, "month": month_ranges},
    }
