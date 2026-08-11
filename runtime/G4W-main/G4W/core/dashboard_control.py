from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any


class DashboardControlMailbox:
    """Tiny cross-process request mailbox used by the local dashboard.

    Each request and result is an independent file, so the dashboard and the
    long-running G4W process never rewrite the same JSON document.
    """

    def __init__(self, state_dir: str | Path):
        self.root = Path(state_dir) / "dashboard-control"
        self.requests_dir = self.root / "requests"
        self.results_dir = self.root / "results"
        self.lock = threading.RLock()
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    def submit(self, action: str, payload: dict[str, Any] | None = None) -> str:
        request_id = uuid.uuid4().hex
        self._write_json(self.requests_dir / f"{request_id}.json", {
            "id": request_id,
            "action": str(action or "").strip(),
            "payload": payload or {},
            "createdAt": time.time(),
        })
        return request_id

    def pending(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = []
        for path in sorted(self.requests_dir.glob("*.json"), key=lambda item: item.stat().st_mtime):
            request = self._read_json(path)
            if request:
                request["_path"] = str(path)
                rows.append(request)
            if len(rows) >= max(1, int(limit)):
                break
        return rows

    def complete(self, request: dict[str, Any], result: dict[str, Any]) -> None:
        request_id = str(request.get("id") or "").strip()
        if not request_id:
            return
        self._write_json(self.results_dir / f"{request_id}.json", {
            "id": request_id,
            "completedAt": time.time(),
            **result,
        })
        path = Path(str(request.get("_path") or self.requests_dir / f"{request_id}.json"))
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def wait(self, request_id: str, timeout: float = 4.0) -> dict[str, Any] | None:
        result_path = self.results_dir / f"{request_id}.json"
        deadline = time.monotonic() + max(0.1, float(timeout))
        while time.monotonic() < deadline:
            if result_path.is_file():
                result = self._read_json(result_path)
                try:
                    result_path.unlink(missing_ok=True)
                except OSError:
                    pass
                return result
            time.sleep(0.05)
        return None

