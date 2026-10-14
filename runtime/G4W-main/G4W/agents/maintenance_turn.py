import time
from pathlib import Path

from ..core.storage import JsonStore


class MaintenanceTurnStore:
    """Persistent per-conversation maintenance-round reply preference.

    enabled=True: 维护轮的收尾文本照常发出，由 Conductor 自己用 <silent/> 决定闭嘴。
    enabled=False (default): 维护轮默认静默——收尾文本不发给用户，只有 Conductor
    写出 <notify/> 时才放行正文（见 templates/agents/conductor-policy.md 第 10 条）。

    随机 check-in(companion)属于主动联系，不在这个开关的范围内。
    """

    def __init__(self, path: Path):
        self.store = JsonStore(path, {"version": 1, "bindings": {}})

    def get(self, binding_key: str) -> bool:
        entry = self.store.read().get("bindings", {}).get(binding_key, {})
        return bool(entry.get("enabled", False))

    def set(self, binding_key: str, enabled: bool) -> dict:
        value = {
            "enabled": bool(enabled),
            "mode": "maintenance_replies",
            "updatedAt": time.time(),
        }

        def update(state):
            state.setdefault("bindings", {})[binding_key] = value
            return dict(value)

        return self.store.update(update)

    def status_text(self, binding_key: str) -> str:
        if self.get(binding_key):
            return "维护轮回复 已开启：维护轮(时间线/日记/内部事件)的收尾文本会发给用户，除非 Conductor 输出 <silent/>。"
        return "维护轮回复 已关闭：维护轮默认静默，只有 Conductor 输出 <notify/> 时正文才会发给用户。"
