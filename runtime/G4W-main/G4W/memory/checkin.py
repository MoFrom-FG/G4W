import random
import time
from pathlib import Path

from ..core.storage import JsonStore


class CheckinService:
    def __init__(self, path: Path):
        self.store = JsonStore(path, {"bindings": {}})

    def configure(self, binding_key: str, sender_id: str, minimum_minutes: int, maximum_minutes: int, enabled: bool = True) -> dict:
        """Upsert interval/enabled for a binding without wiping fire history.

        Preserves fireIndex / lastFiredAt / lastConversationRoundEndedAt so that
        restart or env re-sync cannot re-walk system.checkin:{binding}:{fi}
        dedupe keys that already exist as terminal (done/failed) events.
        """
        minimum = max(1, int(minimum_minutes))
        maximum = max(minimum, int(maximum_minutes))
        now = time.time()

        def update(state):
            bindings = state.setdefault("bindings", {})
            prev = dict(bindings.get(binding_key) or {})
            entry = {
                "bindingKey": binding_key,
                "senderId": sender_id,
                "minimumMinutes": minimum,
                "maximumMinutes": maximum,
                "enabled": bool(enabled),
                "nextAt": self._next(now, minimum, maximum),
                "updatedAt": now,
                # Never reset: empty-spin root cause was fireIndex forced back to 0.
                "fireIndex": int(prev.get("fireIndex", 0) or 0),
            }
            if prev.get("lastFiredAt") is not None:
                entry["lastFiredAt"] = prev["lastFiredAt"]
            if prev.get("lastConversationRoundEndedAt") is not None:
                entry["lastConversationRoundEndedAt"] = prev["lastConversationRoundEndedAt"]
            bindings[binding_key] = entry
            return dict(entry)

        return self.store.update(update)

    def status(self, binding_key: str) -> dict:
        return dict(self.store.read().get("bindings", {}).get(binding_key, {}))

    def disable(self, binding_key: str) -> dict:
        def update(state):
            entry = state.setdefault("bindings", {}).setdefault(binding_key, {"bindingKey": binding_key})
            entry.update({"enabled": False, "updatedAt": time.time()})
            return dict(entry)
        return self.store.update(update)

    def touch_after_round(self, binding_key: str, ended_at: float | None = None) -> dict:
        """Restart idle check-in timing after a user-facing round really ends."""
        target = str(binding_key or "")
        now = float(ended_at if ended_at is not None else time.time())
        def update(state):
            entry = state.setdefault("bindings", {}).get(target)
            if not entry:
                return {}
            entry["lastConversationRoundEndedAt"] = now
            entry["updatedAt"] = now
            if entry.get("enabled"):
                entry["nextAt"] = self._next(now, entry["minimumMinutes"], entry["maximumMinutes"])
            return dict(entry)
        return self.store.update(update) or {}

    def emit_due(self, event_store, l4_service=None, maintenance_service=None, user_name: str = "User", limit: int = 20) -> int:
        now = time.time()
        state = self.store.read()
        due = [entry for entry in state.get("bindings", {}).values() if entry.get("enabled") and float(entry.get("nextAt", 0)) <= now]
        emitted = 0
        state_changed = False
        for entry in sorted(due, key=lambda item: item.get("nextAt", 0))[:limit]:
            pending_system = any(
                item.get("bindingKey") == entry["bindingKey"] and item.get("status") == "pending"
                and item.get("type") != "wechat.user_message"
                for item in event_store.store.read().get("events", [])
            )
            if pending_system:
                # Do not consume a due check-in while another system event owns the binding.
                # Retry shortly without claiming a fire or restarting the full random interval.
                entry["nextAt"] = now + 60
                entry["updatedAt"] = now
                state_changed = True
                continue

            l4_result = l4_service.auto_check(entry["bindingKey"], entry["senderId"]) if l4_service is not None else None
            if (l4_result or {}).get("l4Started"):
                # L4 claimed this due slot; still advance schedule so we do not tight-loop.
                entry["lastFiredAt"] = now
                entry["nextAt"] = self._next(now, entry["minimumMinutes"], entry["maximumMinutes"])
                entry["updatedAt"] = now
                state_changed = True
                emitted += 1
                continue

            checkin = maintenance_service.build_checkin(entry["senderId"], user_name=user_name, now=now) if maintenance_service else {
                "mode": "companion", "text": f"{user_name or 'User'} comes to mind again."
            }

            # Walk fireIndex until enqueue actually yields a pending event.
            # EventStore.enqueue returns an existing row (including terminal done/failed)
            # when dedupeKey collides — advancing nextAt without a real pending checkin
            # is the empty-spin bug after configure() reset fireIndex to 0.
            base_fi = int(entry.get("fireIndex", 0) or 0)
            enqueued = None
            chosen_fi = base_fi
            for step in range(1, 65):
                fi = base_fi + step
                payload = {
                    "fireIndex": fi,
                    "minimumMinutes": entry["minimumMinutes"],
                    "maximumMinutes": entry["maximumMinutes"],
                    "l4Check": l4_result or {},
                    **checkin,
                }
                dedupe_key = f"system.checkin:{entry['bindingKey']}:{fi}"
                event = event_store.enqueue("system.checkin", entry["bindingKey"], payload, dedupe_key=dedupe_key)
                if event.get("status") == "pending":
                    enqueued = event
                    chosen_fi = fi
                    break

            entry["fireIndex"] = chosen_fi
            entry["updatedAt"] = now
            state_changed = True
            if not enqueued:
                # Exhausted free fireIndex window — retry soon, do not pretend we fired.
                entry["nextAt"] = now + 60
                continue

            if maintenance_service:
                maintenance_service.mark_maintenance_queued(entry["senderId"], checkin.get("mode", ""), now)
            entry["lastFiredAt"] = now
            entry["nextAt"] = self._next(now, entry["minimumMinutes"], entry["maximumMinutes"])
            emitted += 1
        if state_changed:
            self.store.write(state)
        return emitted

    @staticmethod
    def _next(now: float, minimum: int, maximum: int) -> float:
        return now + random.uniform(minimum * 60, maximum * 60)
