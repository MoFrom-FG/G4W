import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any


def safe_segment(value: str, fallback: str = "unknown") -> str:
    normalized = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(value or "").strip())
    return normalized.strip("_")[:160] or fallback


class JsonStore:
    def __init__(self, path: Path, default: Any):
        self.path = Path(path)
        self.default = default
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def read(self) -> Any:
        with self.lock:
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                return json.loads(json.dumps(self.default, ensure_ascii=False))

    def write(self, value: Any) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
            last_error = None
            for attempt in range(6):
                tmp = self.path.with_name(
                    f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
                )
                try:
                    tmp.write_text(payload, encoding="utf-8")
                    os.replace(tmp, self.path)
                    return
                except OSError as error:
                    last_error = error
                    try:
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
                    if attempt >= 5:
                        raise
                    time.sleep(min(0.32, 0.01 * (2 ** attempt)))
            if last_error is not None:
                raise last_error

    def update(self, mutator):
        with self.lock:
            value = self.read()
            result = mutator(value)
            self.write(value)
            return result


class EventStore:
    def __init__(self, path: Path):
        self.store = JsonStore(path, {"events": []})

    def enqueue(self, event_type: str, binding_key: str, payload: dict | None = None, dedupe_key: str = "") -> dict:
        event = {
            "id": uuid.uuid4().hex,
            "type": event_type,
            "bindingKey": binding_key,
            "payload": payload or {},
            "status": "pending",
            "createdAt": time.time(),
            "attempts": 0,
            "dedupeKey": dedupe_key,
        }
        def add(state):
            if dedupe_key:
                for existing in state.setdefault("events", []):
                    if existing.get("dedupeKey") == dedupe_key:
                        return existing
            state.setdefault("events", []).append(event)
            return event
        return self.store.update(add)

    def find_by_dedupe_key(self, dedupe_key: str) -> dict | None:
        if not dedupe_key:
            return None
        for item in self.store.read().get("events", []):
            if item.get("dedupeKey") == dedupe_key:
                return dict(item)
        return None

    def next_pending(self) -> dict | None:
        state = self.store.read()
        pending = [item for item in state.get("events", []) if item.get("status") == "pending"]
        return min(pending, key=lambda item: item.get("createdAt", 0), default=None)

    def pending(self, limit: int = 200) -> list[dict]:
        items = [item for item in self.store.read().get("events", []) if item.get("status") == "pending"]
        return sorted(items, key=lambda item: item.get("createdAt", 0))[:max(1, int(limit))]

    def pending_for_binding(self, binding_key: str, limit: int = 20) -> list[dict]:
        pending = [
            item for item in self.store.read().get("events", [])
            if item.get("status") == "pending" and item.get("bindingKey") == binding_key
        ]
        return sorted(pending, key=lambda item: item.get("createdAt", 0))[:max(1, limit)]

    def mark(self, event_id: str, status: str, error: str = "") -> None:
        def apply(state):
            for item in state.get("events", []):
                if item.get("id") == event_id:
                    item["status"] = status
                    item["error"] = error[:500]
                    item["attempts"] = int(item.get("attempts", 0)) + 1
                    item["updatedAt"] = time.time()
                    break
            if len(state.get("events", [])) > 2000:
                state["events"] = state["events"][-1500:]
        self.store.update(apply)

    def patch(self, event_id: str, *, payload: dict | None = None, **values) -> dict | None:
        """Atomically update one event without changing its queue position."""
        def apply(state):
            for item in state.get("events", []):
                if item.get("id") != event_id:
                    continue
                if payload:
                    item.setdefault("payload", {}).update(payload)
                item.update({key: value for key, value in values.items() if value is not None})
                item["updatedAt"] = time.time()
                return dict(item)
            return None
        return self.store.update(apply)

    def claim_intervention(self, event_id: str, round_id: str, target_turn: int) -> dict | None:
        """Move a pending user event out of the normal dispatcher while GA consumes it."""
        now = time.time()
        def apply(state):
            for item in state.get("events", []):
                if item.get("id") != event_id or item.get("status") != "pending":
                    continue
                item.update({
                    "status": "intervening",
                    "interventionRoundId": str(round_id or ""),
                    "interventionTargetTurn": max(1, int(target_turn or 1)),
                    "interventionClaimedAt": now,
                    "updatedAt": now,
                })
                return dict(item)
            return None
        return self.store.update(apply)

    def resolve_intervention(self, event_ids: list[str], *, consumed: bool, reason: str = "") -> int:
        targets = {str(value or "") for value in event_ids if str(value or "")}
        if not targets:
            return 0
        now = time.time()
        def apply(state):
            count = 0
            for item in state.get("events", []):
                if item.get("id") not in targets or item.get("status") != "intervening":
                    continue
                item["status"] = "done" if consumed else "pending"
                item["updatedAt"] = now
                item["interventionResolvedAt"] = now
                item["interventionOutcome"] = "consumed" if consumed else "requeued"
                item["error"] = "" if consumed else str(reason or "intervention returned to queue")[:500]
                count += 1
            return count
        return int(self.store.update(apply) or 0)

    def recover_intervening(self, reason: str = "service restarted before intervention completed") -> int:
        """A live Conductor never survives a process restart, so its claims cannot remain hidden."""
        now = time.time()
        def apply(state):
            count = 0
            for item in state.get("events", []):
                if item.get("status") != "intervening":
                    continue
                item.update({
                    "status": "pending",
                    "updatedAt": now,
                    "interventionResolvedAt": now,
                    "interventionOutcome": "recovered",
                    "error": str(reason)[:500],
                })
                count += 1
            return count
        return int(self.store.update(apply) or 0)

    def cancel_pending(self, binding_key: str, event_type: str, reason: str = "superseded") -> int:
        target_binding = str(binding_key or "")
        target_type = str(event_type or "")
        if not target_binding or not target_type:
            return 0
        def apply(state):
            count = 0
            now = time.time()
            for item in state.get("events", []):
                if (
                    item.get("status") == "pending"
                    and item.get("bindingKey") == target_binding
                    and item.get("type") == target_type
                ):
                    item.update({"status": "cancelled", "error": str(reason)[:500], "updatedAt": now})
                    count += 1
            return count
        return int(self.store.update(apply) or 0)


class OutboxStore:
    def __init__(self, path: Path):
        self.store = JsonStore(path, {"messages": []})
        self.recover_stale_sending()

    def prepare(
        self,
        binding_key: str,
        sender_id: str,
        context_token: str,
        text: str,
        dedupe_key: str,
        *,
        round_id: str = "",
        turn: int = 1,
        round_final: bool = True,
        source: str = "conductor",
        deferred_kind: str = "plain_reply",
        deferred_prefix: str = "",
        cancel_on_user_activity: bool = False,
        expires_after_seconds: float = 0,
        message_subtype: str = "",
    ) -> dict:
        now = time.time()
        ttl = max(0.0, float(expires_after_seconds or 0))
        message = {
            "id": uuid.uuid4().hex,
            "bindingKey": binding_key,
            "senderId": sender_id,
            "contextToken": context_token,
            "text": text,
            "kind": "text",
            "dedupeKey": dedupe_key,
            "status": "pending",
            "attempts": 0,
            "createdAt": now,
            "queuedAt": now,
            "nextAttemptAt": 0,
            "roundId": str(round_id or dedupe_key),
            "turn": max(1, int(turn or 1)),
            "roundFinal": bool(round_final),
            "source": str(source or "conductor"),
            "deferredKind": str(deferred_kind or "plain_reply"),
            "deferredPrefix": str(deferred_prefix or ""),
            "cancelOnUserActivity": bool(cancel_on_user_activity),
            "expiresAt": now + ttl if ttl else 0,
            "messageSubtype": str(message_subtype or ""),
        }
        def add(state):
            for existing in state.setdefault("messages", []):
                if existing.get("dedupeKey") == dedupe_key:
                    return existing
            state["messages"].append(message)
            return message
        return self.store.update(add)

    def prepare_file(self, binding_key: str, sender_id: str, context_token: str, file_path: str, dedupe_key: str, *, round_id: str = "", turn: int = 1, round_final: bool = True, source: str = "conductor", deferred_kind: str = "plain_reply", deferred_prefix: str = "", held: bool = False) -> dict:
        now = time.time()
        message = {
            "id": uuid.uuid4().hex,
            "bindingKey": binding_key,
            "senderId": sender_id,
            "contextToken": context_token,
            "filePath": str(file_path),
            "kind": "file",
            "dedupeKey": dedupe_key,
            "status": "held" if held else "pending",
            "attempts": 0,
            "createdAt": now,
            "queuedAt": now,
            "nextAttemptAt": 0,
            "roundId": str(round_id or dedupe_key),
            "turn": max(1, int(turn or 1)),
            "roundFinal": bool(round_final),
            "source": str(source or "conductor"),
            "deferredKind": str(deferred_kind or "plain_reply"),
            "deferredPrefix": str(deferred_prefix or ""),
        }
        def add(state):
            for existing in state.setdefault("messages", []):
                if existing.get("dedupeKey") == dedupe_key:
                    return existing
            state["messages"].append(message)
            return message
        return self.store.update(add)

    @staticmethod
    def _next_eligible(messages: list[dict], now: float) -> dict | None:
        # A held or actively sending item is still the head of its binding.
        # This prevents a later final reply from overtaking a file produced by
        # an earlier Turn while allowing other bindings to make progress.
        active = [item for item in messages if item.get("status") in ("pending", "held", "sending")]
        heads = {}
        for item in sorted(active, key=lambda value: value.get("createdAt", 0)):
            key = str(item.get("bindingKey") or item.get("senderId") or "global")
            heads.setdefault(key, item)
        eligible = [
            item for item in heads.values()
            if item.get("status") == "pending" and float(item.get("nextAttemptAt", 0)) <= now
        ]
        return min(eligible, key=lambda item: item.get("createdAt", 0), default=None)

    def next_pending(self) -> dict | None:
        now = time.time()
        return self._next_eligible(self.store.read().get("messages", []), now)

    def claim_next_pending(self) -> dict | None:
        now = time.time()
        with self.store.lock:
            state = self.store.read()
            selected = self._next_eligible(state.get("messages", []), now)
            if not selected:
                return None
            for item in state.get("messages", []):
                if item.get("id") != selected.get("id"):
                    continue
                item["status"] = "sending"
                item.setdefault("firstAttemptAt", now)
                item["attemptStartedAt"] = now
                item["updatedAt"] = now
                claimed = dict(item)
                self.store.write(state)
                return claimed
            return None

    def release_held_files(self, round_id: str, through_turn: int = 999999) -> int:
        target = str(round_id or "")
        if not target:
            return 0
        limit = max(1, int(through_turn or 1))
        def release(state):
            items = [
                item for item in state.get("messages", [])
                if item.get("status") == "held"
                and item.get("roundId") == target
                and int(item.get("turn", 1) or 1) <= limit
            ]
            items.sort(key=lambda item: (int(item.get("turn", 1) or 1), float(item.get("queuedAt", item.get("createdAt", 0)) or 0)))
            now = time.time()
            for index, item in enumerate(items):
                item.update({
                    "status": "pending",
                    "releasedAt": now,
                    "createdAt": now + index * 0.000001,
                    "nextAttemptAt": 0,
                    "updatedAt": now,
                })
            return len(items)
        return int(self.store.update(release) or 0)

    def recover_stale_sending(self, stale_after_seconds: float = 120.0) -> int:
        cutoff = time.time() - max(10.0, float(stale_after_seconds or 120.0))
        with self.store.lock:
            state = self.store.read()
            count = 0
            now = time.time()
            for item in state.get("messages", []):
                if item.get("status") != "sending":
                    continue
                if float(item.get("attemptStartedAt", 0) or 0) > cutoff:
                    continue
                item.update({
                    "status": "pending",
                    "nextAttemptAt": now,
                    "updatedAt": now,
                    "lastError": "Recovered stale in-flight delivery after service restart",
                })
                count += 1
            if count:
                self.store.write(state)
            return count

    def mark_sent(self, message_id: str, result: dict | None = None) -> None:
        def apply(state):
            for item in state.get("messages", []):
                if item.get("id") == message_id:
                    now = time.time()
                    created = float(item.get("queuedAt", item.get("createdAt", now)) or now)
                    first_attempt = float(item.get("firstAttemptAt", now) or now)
                    attempt_started = float(item.get("attemptStartedAt", first_attempt) or first_attempt)
                    item.update({
                        "status": "sent",
                        "sentAt": now,
                        "error": "",
                        "queueDelayMs": max(0, round((first_attempt - created) * 1000, 3)),
                        "attemptDurationMs": max(0, round((now - attempt_started) * 1000, 3)),
                        "totalDeliveryMs": max(0, round((now - created) * 1000, 3)),
                    })
                    if isinstance(result, dict):
                        item["delivery"] = json.loads(json.dumps(result, ensure_ascii=False, default=str))
                    break
            if len(state.get("messages", [])) > 2000:
                state["messages"] = state["messages"][-1500:]
        self.store.update(apply)

    def mark_retry(self, message_id: str, error: str) -> None:
        def apply(state):
            for item in state.get("messages", []):
                if item.get("id") == message_id:
                    now = time.time()
                    attempts = int(item.get("attempts", 0)) + 1
                    delay = min(300, 2 ** min(attempts, 8))
                    item.update({
                        "status": "pending",
                        "attempts": attempts,
                        "error": str(error)[:500],
                        "lastError": str(error)[:500],
                        "lastFailedAt": now,
                        "nextAttemptAt": now + delay,
                        "updatedAt": now,
                    })
                    break
        self.store.update(apply)

    def mark_cancelled(self, message_id: str, reason: str) -> bool:
        def apply(state):
            for item in state.get("messages", []):
                if item.get("id") != message_id or item.get("status") not in ("pending", "held", "sending"):
                    continue
                item.update({
                    "status": "cancelled",
                    "cancelledAt": time.time(),
                    "error": str(reason or "cancelled")[:500],
                })
                return True
            return False
        return bool(self.store.update(apply))

    def cancel_superseded_for_sender(self, sender_id: str, reason: str = "superseded by new user activity") -> list[dict]:
        target = str(sender_id or "")
        if not target:
            return []
        def apply(state):
            cancelled = []
            now = time.time()
            for item in state.get("messages", []):
                if item.get("senderId") != target or item.get("status") not in ("pending", "held"):
                    continue
                if not item.get("cancelOnUserActivity"):
                    continue
                item.update({"status": "cancelled", "cancelledAt": now, "error": str(reason)[:500]})
                cancelled.append(dict(item))
            return cancelled
        return self.store.update(apply) or []

    def defer_pending_for_binding(self, binding_key: str) -> list[dict]:
        """Atomically remove older unsent text from the live FIFO.

        A fresh inbound WeChat message supplies a new context_token.  Pending
        replies from the previous token must become the next reply's deferred
        prefix instead of blocking that new reply forever.
        """
        target = str(binding_key or "")
        if not target:
            return []
        def apply(state):
            deferred = []
            now = time.time()
            for item in state.get("messages", []):
                if item.get("bindingKey") != target or item.get("status") != "pending":
                    continue
                if item.get("kind", "text") != "text":
                    continue
                item.update({"status": "deferred", "deferredAt": now, "error": "moved to next context_token"})
                deferred.append(dict(item))
            return deferred
        return self.store.update(apply) or []

    def defer_pending_for_sender(self, sender_id: str, exclude_round_id: str = "") -> list[dict]:
        """Move every remaining unsent text reply for a logical WeChat user.

        A sender can survive a bot-account re-login and therefore have more
        than one bindingKey. Ordering is a conversation property, not an
        account-binding property.
        """
        target = str(sender_id or "")
        excluded = str(exclude_round_id or "")
        if not target:
            return []
        def apply(state):
            deferred = []
            now = time.time()
            for item in state.get("messages", []):
                if item.get("senderId") != target or item.get("status") != "pending":
                    continue
                if excluded and str(item.get("roundId") or "") == excluded:
                    continue
                if item.get("kind", "text") != "text":
                    continue
                item.update({"status": "deferred", "deferredAt": now, "error": "moved to next user turn"})
                deferred.append(dict(item))
            return deferred
        return self.store.update(apply) or []

    def retarget_round(self, sender_id: str, round_id: str, binding_key: str, context_token: str) -> int:
        """Keep unsent items from the active Round live after WeChat refreshes its context token."""
        target_sender = str(sender_id or "")
        target_round = str(round_id or "")
        if not target_sender or not target_round:
            return 0
        now = time.time()
        def apply(state):
            count = 0
            for item in state.get("messages", []):
                if item.get("senderId") != target_sender or str(item.get("roundId") or "") != target_round:
                    continue
                if item.get("status") not in ("pending", "held"):
                    continue
                item.update({
                    "bindingKey": str(binding_key or item.get("bindingKey") or ""),
                    "contextToken": str(context_token or item.get("contextToken") or ""),
                    "updatedAt": now,
                })
                count += 1
            return count
        return int(self.store.update(apply) or 0)

    def cancel_round(self, round_id: str) -> int:
        target = str(round_id or "")
        if not target:
            return 0
        def apply(state):
            count = 0
            for item in state.get("messages", []):
                if item.get("roundId") == target and item.get("status") in ("pending", "held") and str(item.get("source", "")).startswith("conductor"):
                    item.update({"status": "cancelled", "cancelledAt": time.time(), "error": "Conductor round cancelled"})
                    count += 1
            return count
        return int(self.store.update(apply) or 0)


class DeferredReplyStore:
    def __init__(self, path: Path):
        self.store = JsonStore(path, {"items": {}})

    def add(self, binding_key: str, sender_id: str, text: str, kind: str = "plain_reply") -> dict:
        item = {
            "id": uuid.uuid4().hex,
            "bindingKey": binding_key,
            "senderId": sender_id,
            "text": str(text or ""),
            "kind": str(kind or "plain_reply"),
            "createdAt": time.time(),
        }
        def update(state):
            state.setdefault("items", {}).setdefault(sender_id, []).append(item)
            return item
        return self.store.update(update)

    def pop_all(self, sender_id: str) -> list[dict]:
        def update(state):
            return state.setdefault("items", {}).pop(sender_id, [])
        return self.store.update(update) or []

    def count(self, sender_id: str = "") -> int:
        items = self.store.read().get("items", {})
        if sender_id:
            return len(items.get(sender_id, []))
        return sum(len(values) for values in items.values())
