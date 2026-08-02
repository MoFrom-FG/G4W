import json
import os
import re
import threading
from pathlib import Path


def account_file_name(account_id: str) -> str:
    normalized = re.sub(r"[^a-z0-9._-]+", "-", str(account_id or "").strip().lower())
    normalized = re.sub(r"-+", "-", normalized).strip("-")
    return (normalized or "unknown") + ".json"


class WeixinAccountStore:
    """Per-account credential files with migration from the early aggregate file."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.legacy_file = self.root / "accounts.json"
        self.lock = threading.RLock()
        self._migrate_legacy()

    def _migrate_legacy(self) -> None:
        try:
            state = json.loads(self.legacy_file.read_text(encoding="utf-8"))
        except Exception:
            return
        for key, value in (state.get("accounts") or {}).items():
            if not isinstance(value, dict):
                continue
            account = dict(value)
            account.setdefault("accountId", str(key))
            target = self.root / account_file_name(account["accountId"])
            if not target.is_file():
                self._write_account(target, account)

    @staticmethod
    def _write_account(path: Path, account: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(account, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def read(self) -> dict:
        with self.lock:
            accounts = {}
            for path in sorted(self.root.glob("*.json")):
                if path.name == self.legacy_file.name or path.name.endswith(".context-tokens.json"):
                    continue
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not isinstance(value, dict):
                    continue
                account_id = str(value.get("accountId") or "").strip()
                if account_id:
                    accounts[account_id] = value
            return {"accounts": accounts}

    def write(self, state: dict) -> None:
        with self.lock:
            for key, value in (state.get("accounts") or {}).items():
                if not isinstance(value, dict):
                    continue
                account = dict(value)
                account.setdefault("accountId", str(key))
                self._write_account(self.root / account_file_name(account["accountId"]), account)

    def update(self, mutator):
        with self.lock:
            state = self.read()
            result = mutator(state)
            self.write(state)
            return result

