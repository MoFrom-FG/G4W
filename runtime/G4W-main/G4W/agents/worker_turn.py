import time
from pathlib import Path

from ..core.storage import JsonStore


class WorkerTurnStore:
    """Persistent per-conversation worker progress reporting preference.

    enabled=True: Conductor reports intermediate worker progress.
    enabled=False (default): Conductor only reports when the worker completes (or fails).
    """

    def __init__(self, path: Path):
        self.store = JsonStore(path, {"version": 1, "bindings": {}})

    def get(self, binding_key: str) -> bool:
        entry = self.store.read().get("bindings", {}).get(binding_key, {})
        return bool(entry.get("enabled", False))

    def set(self, binding_key: str, enabled: bool) -> dict:
        value = {
            "enabled": bool(enabled),
            "mode": "worker_progress_reports",
            "updatedAt": time.time(),
        }

        def update(state):
            state.setdefault("bindings", {})[binding_key] = value
            return dict(value)

        return self.store.update(update)

    def status_text(self, binding_key: str) -> str:
        if self.get(binding_key):
            return "Worker进度汇报 已开启：worker运行中会汇报中间进度，完成后汇报最终结果。"
        return "Worker进度汇报 已关闭：worker运行中不汇报中间进度，只在完成/失败时汇报结果。"
