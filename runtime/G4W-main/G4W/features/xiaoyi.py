import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from ..core.storage import JsonStore, safe_segment


TERMINAL = {"completed", "failed", "cancelled"}


class XiaoyiService:
    def __init__(self, root: Path, bridge_url: str, event_store):
        self.root = Path(root)
        self.bridge_url = str(bridge_url or "http://127.0.0.1:21991").rstrip("/")
        self.events = event_store
        self.pending_dir = self.root / "pending"
        self.done_dir = self.root / "done"
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.done_dir.mkdir(parents=True, exist_ok=True)
        self.runtime = JsonStore(self.root / "runtime.json", {"lastPollAt": 0, "errors": {}})

    def submit(self, binding_key: str, sender_id: str, prompt: str, options: dict | None = None) -> dict:
        text = str(prompt or "").strip()
        if not text:
            raise ValueError("xiaoyi prompt is required")
        options = options or {}
        body = {"prompt": text, "mode": "xiaoyi_prompt"}
        for source, target in (
            ("job_id", "jobId"), ("push_id", "pushId"), ("task_summary", "taskSummary"),
            ("notify_title", "notifyTitle"), ("notify_text", "notifyText"), ("confirm_text", "confirmText"),
        ):
            value = str(options.get(source) or options.get(target) or "").strip()
            if value:
                body[target] = value
        if "requires_user_action" in options or "requiresUserAction" in options or "confirm" in options:
            body["requiresUserAction"] = bool(options.get("requires_user_action", options.get("requiresUserAction", options.get("confirm"))))
        result = self._request("POST", "/prompt", body)
        if not result.get("ok", True) or not result.get("jobId"):
            raise RuntimeError(str(result.get("error") or "xiaoyi submit failed"))
        job_id = str(result["jobId"])
        record = {
            "jobId": job_id,
            "bindingKey": binding_key,
            "senderId": sender_id,
            "prompt": text,
            "options": options,
            "status": str(result.get("status") or "pending"),
            "pushDataId": str(result.get("pushDataId") or ""),
            "createdAt": time.time(),
            "updatedAt": time.time(),
        }
        self._write(self.pending_dir / f"{safe_segment(job_id)}.json", record)
        return self.public(record)

    def poll(self, interval_seconds: float = 1.0, limit: int = 20) -> int:
        now = time.time()
        runtime = self.runtime.read()
        if now - float(runtime.get("lastPollAt", 0)) < interval_seconds:
            return 0
        runtime["lastPollAt"] = now
        self.runtime.write(runtime)
        completed = 0
        for path in sorted(self.pending_dir.glob("*.json"), key=lambda item: item.stat().st_mtime)[:limit]:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                result = self._request("GET", "/prompt-results/" + urllib.parse.quote(str(record["jobId"]), safe=""))
                status = str(result.get("status") or record.get("status") or "pending")
                if not result.get("ok", True) and "not found" in str(result.get("error", "")).lower():
                    continue
                record.update({
                    "status": status,
                    "finalText": str(result.get("finalText") or ""),
                    "error": str(result.get("error") or ""),
                    "updatedAt": time.time(),
                })
                if status not in TERMINAL:
                    self._write(path, record)
                    continue
                record["completedAt"] = time.time()
                done = self.done_dir / path.name
                self._write(done, record)
                path.unlink(missing_ok=True)
                self.events.enqueue(
                    "xiaoyi.completed",
                    record["bindingKey"],
                    {"jobId": record["jobId"], "status": status, "finalText": record["finalText"], "error": record["error"], "prompt": record["prompt"]},
                    dedupe_key=f"xiaoyi.completed:{record['jobId']}:{status}",
                )
                completed += 1
            except Exception as error:
                def save(state, name=path.name, message=str(error)):
                    state.setdefault("errors", {})[name] = {"error": message[:500], "at": time.time()}
                self.runtime.update(save)
        return completed

    def list_for(self, binding_key: str) -> list[dict]:
        records = []
        for directory in (self.pending_dir, self.done_dir):
            for path in directory.glob("*.json"):
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                    if record.get("bindingKey") == binding_key:
                        records.append(self.public(record))
                except Exception:
                    pass
        return sorted(records, key=lambda item: item.get("createdAt", 0), reverse=True)

    def get(self, binding_key: str, job_id: str) -> dict:
        name = safe_segment(job_id) + ".json"
        for directory in (self.pending_dir, self.done_dir):
            path = directory / name
            if path.is_file():
                record = json.loads(path.read_text(encoding="utf-8"))
                if record.get("bindingKey") != binding_key:
                    raise PermissionError("xiaoyi job does not belong to this conversation")
                return self.public(record)
        raise KeyError(f"xiaoyi job not found: {job_id}")

    def health(self) -> dict:
        return self._request("GET", "/health")

    def _request(self, method: str, endpoint: str, body: dict | None = None) -> dict:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.bridge_url + endpoint, data=data, headers={"Content-Type": "application/json"}, method=method)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                return json.loads(error.read().decode("utf-8"))
            except Exception:
                raise RuntimeError(f"xiaoyi bridge HTTP {error.code}") from error

    @staticmethod
    def _write(path: Path, value: dict) -> None:
        JsonStore(path, {}).write(value)

    @staticmethod
    def public(record: dict) -> dict:
        return {key: record.get(key) for key in ("jobId", "prompt", "status", "finalText", "error", "pushDataId", "createdAt", "updatedAt", "completedAt")}
