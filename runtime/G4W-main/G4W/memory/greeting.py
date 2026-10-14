"""看板首页问候语：时段计算 + conductor 产出的缓存 + 时段兜底。

问候语由 checkin 维护轮里的 conductor 写入（`greeting.write` 原生SOP），
看板只读缓存；跨时段或超过 MAX_AGE 就退回纯拼接的时段问候。
"""
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from ..core.storage import JsonStore

MAX_AGE_SECONDS = 8 * 3600

# (period, 起始小时, 结束小时, 兜底问候前缀)；23–05 跨午夜
PERIODS: tuple[tuple[str, int, int, str], ...] = (
    ("morning", 5, 11, "上午好"),
    ("noon", 11, 13, "中午好"),
    ("afternoon", 13, 18, "下午好"),
    ("evening", 18, 23, "晚上好"),
    ("night", 23, 5, "夜深了"),
)
PERIOD_LABELS = {period: label for period, _, _, label in PERIODS}
PERIOD_RANGES = {
    "morning": "05–11",
    "noon": "11–13",
    "afternoon": "13–18",
    "evening": "18–23",
    "night": "23–05",
}


def period_for(now: datetime | float | None = None) -> str:
    moment = now if isinstance(now, datetime) else datetime.fromtimestamp(float(now if now is not None else time.time()))
    hour = moment.hour
    for period, start, end, _label in PERIODS:
        if start < end:
            if start <= hour < end:
                return period
        elif hour >= start or hour < end:
            return period
    return "morning"


def fallback_text(period: str, address: str) -> str:
    label = PERIOD_LABELS.get(period, "你好")
    name = str(address or "").strip()
    return f"{label}，{name}。" if name else f"{label}。"


class GreetingStore:
    """`<state_dir>/memory/dashboard-greeting.json`：{text, period, generatedAt, senderId}。"""

    def __init__(self, path: Path):
        self.store = JsonStore(Path(path), {"version": 1, "greeting": {}})

    def read(self) -> dict:
        return dict(self.store.read().get("greeting") or {})

    def write(self, text: str, period: str = "", sender_id: str = "", at: float | None = None) -> dict:
        body = " ".join(str(text or "").split())
        if not body:
            raise ValueError("greeting text is empty")
        value = {
            "text": body,
            "period": str(period or period_for(at)),
            "generatedAt": float(at if at is not None else time.time()),
            "senderId": str(sender_id or ""),
        }

        def update(state):
            state["greeting"] = value
            return dict(value)

        return self.store.update(update)

    def render(self, address: str, now: Callable[[], float] | float | None = None) -> dict:
        """看板读取用：新鲜就回 conductor 原文，否则回时段兜底文案。"""
        stamp = now() if callable(now) else (float(now) if now is not None else time.time())
        current = period_for(stamp)
        data = self.read()
        text = " ".join(str(data.get("text") or "").split())
        generated_at = float(data.get("generatedAt") or 0)
        stale = (
            not text
            or str(data.get("period") or "") != current
            or stamp - generated_at > MAX_AGE_SECONDS
        )
        if stale:
            return {
                "text": fallback_text(current, address),
                "source": "fallback",
                "period": current,
                "periodLabel": PERIOD_LABELS.get(current, ""),
                "stale": True,
                "generatedAt": generated_at,
            }
        return {
            "text": text,
            "source": "conductor",
            "period": current,
            "periodLabel": PERIOD_LABELS.get(current, ""),
            "stale": False,
            "generatedAt": generated_at,
        }
