import copy
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..core.storage import JsonStore, safe_segment


SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")


class InputCaptureStore:
    """Optional per-binding snapshots of every Conductor LLM call."""

    def __init__(self, path: Path, conversations_root: Path):
        self.store = JsonStore(path, {"version": 1, "bindings": {}})
        self.conversations_root = Path(conversations_root)
        self.lock = threading.RLock()

    def get(self, binding_key: str) -> bool:
        entry = self.store.read().get("bindings", {}).get(str(binding_key or ""), {})
        return bool(entry.get("enabled", False))

    def set(self, binding_key: str, enabled: bool) -> dict:
        value = {"enabled": bool(enabled), "mode": "llm_input_snapshots", "updatedAt": time.time()}

        def update(state):
            state.setdefault("bindings", {})[str(binding_key or "")] = value
            return dict(value)

        return self.store.update(update)

    def status_text(self, binding_key: str) -> str:
        if self.get(binding_key):
            return "完整Input快照 已开启：每次Conductor LLM调用都会按Round/Turn保存模型真实提交的system、history、tools和current messages。"
        return (
            "完整Input快照 已关闭：不会新增conductor/rounds/.../inputs/turnNN.json。\n"
            "output.txt、metadata.json和model-responses.txt属于Turn终端与审计日志，仍会正常生成。"
        )

    def save(self, sender_id: str, payload: dict) -> str:
        context = copy.deepcopy(payload.get("roundContext") or {})
        binding_key = str(context.get("bindingKey") or "")
        if not binding_key or not self.get(binding_key):
            return ""
        now = datetime.now(SHANGHAI)
        round_id = safe_segment(str(context.get("roundId") or f"round-{time.time_ns()}"))
        started = str(context.get("startedAtLocal") or now.strftime("%H%M%S"))
        started = re.sub(r"[^0-9]", "", started)[-6:] or now.strftime("%H%M%S")
        turn = max(1, int(payload.get("turn", 1) or 1))
        folder = (
            self.conversations_root
            / safe_segment(sender_id)
            / "conductor"
            / "rounds"
            / now.strftime("%Y")
            / now.strftime("%m")
            / now.strftime("%d")
            / round_id
            / "inputs"
        )
        target = folder / f"turn{turn:02d}.json"
        current_messages = copy.deepcopy(payload.get("submittedCurrentMessages") or [])
        if not current_messages:
            current_messages = [
                item for item in copy.deepcopy(payload.get("messages") or [])
                if str(item.get("role") or "").lower() != "system"
            ]
        snapshot = {
            "schemaVersion": 3,
            "metadata": {
                "generatedAt": now.isoformat(),
                "senderId": sender_id,
                "bindingKey": binding_key,
                "roundId": context.get("roundId", ""),
                "gaTurn": turn,
                "source": context.get("source", ""),
                "receivedAt": context.get("receivedAt", ""),
                "deliveryKind": context.get("deliveryKind", ""),
                "model": payload.get("model", ""),
                "systemFingerprint": payload.get("systemFingerprint", ""),
                "controllerSystemFingerprint": payload.get("controllerSystemFingerprint", ""),
                "cachePhase": "round_first" if turn == 1 else "tool_turn",
                "cleanHistory": copy.deepcopy(context.get("cleanHistory") or {}),
            },
            "submittedRequest": {
                "system": payload.get("systemPrompt", ""),
                "tools": copy.deepcopy(payload.get("tools") or []),
                "history": copy.deepcopy(payload.get("historyBeforeCall") or []),
                "currentMessages": current_messages,
            },
            "requestLayout": {
                "stableComponents": ["system", "tools", "history的既有前缀"],
                "conversationComponents": ["history的新增可见消息", "currentMessages", "同一Round工具结果"],
                "note": "tools是独立API字段，每个Turn均完整提交；JSON字段显示位置不代表模型先后阅读顺序。跨Round history只保留微信实际可见的纯净消息，同一Round内才临时追加工具调用与结果。",
            },
            "contextSources": {
                "isUserMessage": bool(context.get("isUserMessage")),
                "pureUserMessage": context.get("pureUserMessage", ""),
                "note": "submittedRequest is the normalized model input. Diagnostic duplicate renderings are intentionally omitted.",
            },
        }
        with self.lock:
            folder.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
            temporary.replace(target)
        return str(target)
