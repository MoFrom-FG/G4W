import re
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..core.storage import safe_segment


ROUND_END = "[ROUND END]"
SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")


def output_index(path: Path) -> int:
    match = re.fullmatch(r"output(\d*)\.txt", path.name, flags=re.I)
    if not match:
        return -1
    return int(match.group(1) or 0)


def output_name(index: int) -> str:
    return "output.txt" if index <= 0 else f"output{index}.txt"


class ConductorRoundLog:
    """Persist the real GA stream for every Conductor invocation.

    This is deliberately independent from the WeChat outbox.  Silent rounds,
    tool-only turns and failed deliveries must still remain observable.
    """

    def __init__(self, root: Path, short_path_mirror=None):
        self.root = Path(root).resolve()
        self.short_path_mirror = short_path_mirror
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.day_key = ""
        self.next_index = 0

    def _day_dir(self, sender_id: str = "") -> Path:
        now = datetime.now(SHANGHAI)
        self.day_key = now.strftime("%Y/%m/%d")
        base = self.root / safe_segment(sender_id) / "conductor" / "rounds" if sender_id else self.root
        return base / now.strftime("%Y") / now.strftime("%m") / now.strftime("%d")

    @staticmethod
    def _scan_next_index(day_dir: Path) -> int:
        indexes = [output_index(path) for path in day_dir.glob("output*.txt")]
        return max([-1, *indexes]) + 1

    def begin(self, sender_id: str = "", round_id: str = "", metadata: dict | None = None) -> Path:
        with self.lock:
            day_dir = self._day_dir(sender_id)
            day_dir.mkdir(parents=True, exist_ok=True)
            if sender_id and round_id:
                round_dir = day_dir / safe_segment(round_id)
                if round_dir.exists():
                    round_dir = day_dir / f"{safe_segment(round_id)}-{datetime.now(SHANGHAI).strftime('%H%M%S%f')}"
                round_dir.mkdir(parents=True, exist_ok=True)
                path = round_dir / "output.txt"
                path.write_text("", encoding="utf-8")
                record = {"senderId": sender_id, "roundId": round_id, "createdAt": datetime.now(SHANGHAI).isoformat(), **(metadata or {})}
                (round_dir / "metadata.json").write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
                return path
            if self.next_index <= 0 or not (day_dir / output_name(max(0, self.next_index - 1))).exists():
                self.next_index = self._scan_next_index(day_dir)
            path = day_dir / output_name(self.next_index)
            self.next_index += 1
            path.write_text("", encoding="utf-8")
            return path

    def append(self, path: Path, text: str) -> None:
        value = str(text or "")
        if not value:
            return
        with self.lock:
            with Path(path).open("a", encoding="utf-8") as stream:
                stream.write(value)
                stream.flush()

    def finish(self, path: Path, full_text: str, cancelled: bool = False) -> None:
        final = str(full_text or "")
        if cancelled and not final.strip():
            final = "[G4W] Conductor round cancelled."
        final = final.rstrip()
        rendered = (final + "\n\n" if final else "") + ROUND_END + "\n"
        with self.lock:
            target = Path(path)
            try:
                current = target.read_text(encoding="utf-8")
            except Exception:
                current = ""
            if rendered.startswith(current):
                with target.open("a", encoding="utf-8") as stream:
                    stream.write(rendered[len(current):])
                    stream.flush()
            else:
                target.write_text(rendered, encoding="utf-8")
            if self.short_path_mirror is not None:
                metadata = {}
                try:
                    metadata = json.loads((target.parent / "metadata.json").read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    pass
                try:
                    legacy_path = str(target.relative_to(self.root))
                except ValueError:
                    legacy_path = str(target)
                self.short_path_mirror.write_round(
                    str(metadata.get("senderId") or ""), str(metadata.get("roundId") or ""),
                    rendered, cancelled=cancelled, legacy_path=legacy_path,
                )


def latest_output(root: Path) -> Path | None:
    root = Path(root)
    paths = list(root.rglob("output*.txt")) if root.exists() else []
    candidates = [path for path in paths if path.name.lower() == "output.txt" or output_index(path) >= 0]
    return max(candidates, key=lambda path: (path.stat().st_mtime_ns, output_index(path)), default=None)
