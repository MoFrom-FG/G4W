import hashlib
import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = "G4W.short_path_mirror.v1"
_DOMAIN = "G4W-mirror-v1\0"
_KIND_DIR = {"conversation": "c", "round": "r"}


def _canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _utc_timestamp(value) -> str:
    if isinstance(value, datetime):
        stamp = value
    else:
        try:
            stamp = datetime.fromtimestamp(float(value), timezone.utc)
        except (TypeError, ValueError, OSError):
            text = str(value or "").strip()
            if text:
                try:
                    stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
                except ValueError:
                    return text
            else:
                stamp = datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class ShortPathMirror:
    """Best-effort, write-only mirror. Legacy stores remain the sole read source."""

    def __init__(self, root: Path, enabled: bool = False):
        self.root = Path(root)
        self.enabled = bool(enabled)
        self.lock = threading.RLock()

    def _error(self, operation: str, error: Exception, **context) -> None:
        event = {
            "event": "short_path_mirror_error",
            "operation": operation,
            "error_type": type(error).__name__,
            "error": str(error),
            **context,
        }
        print(json.dumps(event, ensure_ascii=False, sort_keys=True, default=str), file=sys.stderr)

    def _identity(self, kind: str, identity: dict) -> tuple[str, str]:
        digest = _sha256_text(_DOMAIN + kind + "\0" + _canonical(identity))
        return digest[:32], digest

    def _target(self, kind: str, stable_id: str) -> Path:
        return self.root / _KIND_DIR[kind] / stable_id[:2] / stable_id[2:4] / f"{stable_id}.json"

    def _write(self, kind: str, identity: dict, content: str, source: dict, legacy_paths: list[str]) -> Path | None:
        if not self.enabled:
            return None
        stable_id, identity_digest = self._identity(kind, identity)
        target = self._target(kind, stable_id)
        record = {
            "schema": SCHEMA,
            "kind": kind,
            "stable_id": stable_id,
            "identity_sha256": identity_digest,
            "content_sha256": _sha256_text(content),
            "content": content,
            "source": source,
            "legacy_paths": [str(path) for path in legacy_paths],
            "written_at": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        }
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n"
        temp_name = ""
        with self.lock:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    existing = json.loads(target.read_text(encoding="utf-8"))
                    if existing.get("kind") != kind or existing.get("identity_sha256") != identity_digest:
                        raise RuntimeError("stable ID collision")
                    if existing.get("content_sha256") == record["content_sha256"]:
                        return target
                    if kind != "round":
                        raise RuntimeError("stable identity reused with different content")
                fd, temp_name = tempfile.mkstemp(prefix=".mirror-", suffix=".tmp", dir=target.parent)
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_name, target)
                temp_name = ""
                return target
            except Exception as error:
                self._error("write", error, kind=kind, stable_id=stable_id, target=str(target))
                return None
            finally:
                if temp_name:
                    try:
                        Path(temp_name).unlink(missing_ok=True)
                    except OSError:
                        pass

    def write_conversation(self, sender_id: str, role: str, content: str, timestamp="", subtype: str = "", message_id: str = "", parent_round_id: str = "", legacy_paths=()) -> Path | None:
        content = str(content or "")
        normalized_timestamp = _utc_timestamp(timestamp)
        if str(message_id or ""):
            identity = {"source_id": str(message_id)}
        else:
            identity = {"fallback": {
                "sender_id": str(sender_id),
                "role": str(role),
                "timestamp": normalized_timestamp,
                "subtype": str(subtype or ""),
                "parent_round_id": str(parent_round_id or ""),
                "content_sha256": _sha256_text(content),
            }}
        source = {
            "sender_id": str(sender_id), "role": str(role), "timestamp": normalized_timestamp,
            "subtype": str(subtype or ""), "message_id": str(message_id or ""),
            "parent_round_id": str(parent_round_id or ""),
        }
        return self._write("conversation", identity, content, source, list(legacy_paths))

    def write_round(self, sender_id: str, round_id: str, content: str, cancelled: bool = False, legacy_path: str = "") -> Path | None:
        identity = ({"sender_id": str(sender_id), "round_id": str(round_id)} if sender_id and round_id
                    else {"legacy_path": str(legacy_path).replace("\\", "/")})
        source = {"sender_id": str(sender_id), "round_id": str(round_id), "cancelled": bool(cancelled)}
        return self._write("round", identity, str(content or ""), source, [str(legacy_path)])
