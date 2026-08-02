import time
from pathlib import Path

from .storage import JsonStore


class CacheMetricsStore:
    def __init__(self, path: Path):
        self.store = JsonStore(path, {"senders": {}})

    def record(self, sender_id: str, value: dict) -> None:
        item = {**value, "recordedAt": time.time()}
        def update(state):
            entry = state.setdefault("senders", {}).setdefault(sender_id, {"samples": []})
            samples = entry.setdefault("samples", [])
            previous = samples[-1] if samples else {}
            item["invalidationReason"] = (
                "system_prompt_changed"
                if previous.get("systemFingerprint") and previous.get("systemFingerprint") != item.get("systemFingerprint")
                else ("model_changed" if previous.get("model") and previous.get("model") != item.get("model")
                      else ("clean_history_compacted" if item.get("cleanHistoryCompacted") else ""))
            )
            samples.append(item)
            entry["samples"] = samples[-100:]
            entry["updatedAt"] = time.time()
        self.store.update(update)

    def status(self, sender_id: str) -> dict:
        samples = self.store.read().get("senders", {}).get(sender_id, {}).get("samples", [])
        recent = samples[-1] if samples else {}
        rolling = samples[-20:]
        def weighted(items):
            total_input = sum(int(item.get("inputTokens", 0) or 0) for item in items)
            total_cached = sum(int(item.get("cachedTokens", 0) or 0) for item in items)
            return {
                "ratio": (total_cached / total_input) if total_input else 0.0,
                "samples": len(items),
                "inputTokens": total_input,
                "cachedTokens": total_cached,
            }

        overall = weighted(rolling)
        first_turn = weighted([item for item in rolling if item.get("cachePhase") == "round_first"])
        tool_turn = weighted([item for item in rolling if item.get("cachePhase") == "tool_turn"])
        user_message = weighted([item for item in rolling if item.get("source") == "wechat.user_message"])
        internal_event = weighted([item for item in rolling if item.get("source") != "wechat.user_message"])
        return {
            "lastRatio": float(recent.get("ratio", 0.0) or 0.0),
            "rollingRatio": overall["ratio"],
            "rollingSamples": len(rolling),
            "rolling": overall,
            "firstTurn": first_turn,
            "toolTurn": tool_turn,
            "userMessage": user_message,
            "internalEvent": internal_event,
            "samples": len(samples),
            "model": recent.get("model", ""),
            "systemFingerprint": recent.get("systemFingerprint", ""),
            "invalidationReason": recent.get("invalidationReason", ""),
            "cleanHistoryMode": recent.get("cleanHistoryMode", ""),
            "cleanHistoryUserRounds": int(recent.get("cleanHistoryUserRounds", 0) or 0),
            "cleanHistoryMessageCount": int(recent.get("cleanHistoryMessageCount", 0) or 0),
        }
