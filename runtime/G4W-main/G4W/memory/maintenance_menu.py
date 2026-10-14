"""维护动作菜单：看板可配置「允许哪些维护动作」，checkin 维护轮把菜单注入提示词。

Conductor 在允许集里自主多选（可以一个都不做）；`greeting` 一项产出看板首页问候语。
"""
import time
from pathlib import Path

from ..core.storage import JsonStore
from .greeting import PERIOD_LABELS, PERIOD_RANGES, period_for

ACTION_CATALOG: tuple[dict, ...] = (
    {"id": "timeline", "label": "维护时间轴", "desc": "有新的生活时间块时写时间线（timeline.write，先读再 upsert）"},
    {"id": "diary", "label": "写日记", "desc": "只有情绪/决定/关系意义时才写（diary.append）"},
    {"id": "greeting", "label": "写看板问候语", "desc": "给看板首页写一句带时段问候和称呼的话（看板读取，不发微信）"},
    {"id": "signals", "label": "感知用户事件", "desc": "结合最近聊天判断要不要主动联系；要联系就用 <notify/> 破例"},
    {"id": "todo", "label": "待办督办", "desc": "检查 OPEN_TODOS 里到期或长期没动的事项"},
)
DEFAULT_ACTIONS: tuple[str, ...] = tuple(item["id"] for item in ACTION_CATALOG)
ACTION_IDS = frozenset(DEFAULT_ACTIONS)


class MaintenanceMenuStore:
    """`<state_dir>/maintenance-menu-config.json`：按 binding 记录允许的维护动作。"""

    def __init__(self, path: Path):
        self.store = JsonStore(Path(path), {"version": 1, "bindings": {}})

    def allowed(self, binding_key: str) -> list[str]:
        entry = self.store.read().get("bindings", {}).get(str(binding_key or ""), {})
        if "actions" not in entry:
            return list(DEFAULT_ACTIONS)
        return [item for item in entry.get("actions") or [] if item in ACTION_IDS]

    def set_allowed(self, binding_key: str, actions) -> list[str]:
        value = [str(item) for item in (actions or []) if str(item) in ACTION_IDS]
        entry = {"actions": value, "updatedAt": time.time()}

        def update(state):
            state.setdefault("bindings", {})[str(binding_key or "")] = entry
            return dict(entry)

        self.store.update(update)
        return value

    def render_checkin(self, binding_key: str, address: str = "", now: float | None = None) -> str:
        """注入 checkin 维护轮的菜单块；没有允许项时返回空串。"""
        allowed = self.allowed(binding_key)
        if not allowed:
            return ""
        stamp = float(now if now is not None else time.time())
        period = period_for(stamp)
        name = str(address or "").strip() or "你"
        lines = [
            "MAINTENANCE_MENU",
            "这是内部维护轮：以下几点由你自主选做，可以只做一件，也可以都不做；做完直接收尾。",
            f"当前时段：{PERIOD_LABELS.get(period, '')}（{PERIOD_RANGES.get(period, '')}）；你对用户的称呼是「{name}」。",
        ]
        for item in ACTION_CATALOG:
            if item["id"] not in allowed:
                continue
            desc = item["desc"]
            if item["id"] == "greeting":
                desc += (
                    f"；调用方式是 GA code_run 里执行 "
                    f"print_result(\"greeting\", \"write\", {{\"text\": \"{PERIOD_LABELS.get(period, '')}，{name}，…\"}})；"
                    "正文一句话、控制在 30 字以内、不要换行（看板 hero 只有一行位置），"
                    "也不要出现系统/机制/英文内部词"
                )
            lines.append(f"  [{item['id']}] {item['label']} — {desc}")
        lines.append("维护轮默认静默：收尾文本不会发给用户，也不要写「静默收尾」这类说明；确实要联系用户时把正文写在 <notify/> 之后。")
        return "\n".join(lines)
