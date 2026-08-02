import time
from pathlib import Path

from ..core.storage import JsonStore


class TurnProgressStore:
    """Persistent per-conversation intermediate reply preference."""

    def __init__(self, path: Path):
        self.store = JsonStore(path, {"version": 1, "bindings": {}})

    def get(self, binding_key: str) -> bool:
        entry = self.store.read().get("bindings", {}).get(binding_key, {})
        return bool(entry.get("enabled", True))

    def set(self, binding_key: str, enabled: bool) -> dict:
        value = {
            "enabled": bool(enabled),
            "mode": "intermediate_replies",
            "updatedAt": time.time(),
        }

        def update(state):
            state.setdefault("bindings", {})[binding_key] = value
            return dict(value)

        return self.store.update(update)

    def status_text(self, binding_key: str) -> str:
        if self.get(binding_key):
            return "长任务 turn 显示 已开启：微信会像以前一样显示Conductor中间回复和最终回复。"
        return "长任务 turn 显示 已关闭：微信只显示最终回复，不显示中间回复。"
