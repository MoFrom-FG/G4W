"""L4 summaries/.backups rotation — plan/apply separated; dry-run default.

Does NOT touch hybrid CAS / objects. Apply only MOVEs candidates into
quarantine (never plain unlink) and requires explicit confirm token.
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

NAME_RE = re.compile(r"l4compress-(\d{8})-(\d{6})")
CONFIRM_TOKEN = "ROTATE-BACKUPS"
DEFAULT_KEEP_LAST = 5
DEFAULT_MAX_AGE_DAYS = 14.0


def parse_name_ts(name: str) -> Optional[datetime]:
    m = NAME_RE.match(name)
    if not m:
        return None
    return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(
        tzinfo=timezone.utc
    )


def dir_bytes(p: Path) -> tuple[int, int]:
    n, b = 0, 0
    if not p.is_dir():
        return 0, 0
    for f in p.rglob("*"):
        if f.is_file():
            n += 1
            try:
                b += f.stat().st_size
            except OSError:
                pass
    return n, b


@dataclass
class BackupItem:
    name: str
    path: str
    name_ts: Optional[str]
    age_from_name_days: Optional[float]
    age_days: Optional[float]
    rank_from_newest: int
    files: int
    bytes: int
    action: str  # KEEP | DELETE_CANDIDATE

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RotationPlan:
    backups_root: str
    keep_last: int
    max_age_days: float
    now_utc: str
    items: list[BackupItem] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    keep: list[str] = field(default_factory=list)
    total_candidate_bytes: int = 0
    applied: bool = False
    moved: list[dict[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["items"] = [i if isinstance(i, dict) else i.to_dict() for i in self.items]
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


def plan_rotation(
    backups_root: Path | str,
    keep_last: int = DEFAULT_KEEP_LAST,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    now: Optional[datetime] = None,
) -> RotationPlan:
    """Build a dry-run rotation plan for l4compress-* dirs under backups_root.

    KEEP if rank_from_newest <= keep_last OR age_from_name_days <= max_age_days.
    Unknown-name dirs sort last and are still subject to keep_last only via rank.
    """
    root = Path(backups_root)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    plan = RotationPlan(
        backups_root=str(root),
        keep_last=int(keep_last),
        max_age_days=float(max_age_days),
        now_utc=now.isoformat(),
    )
    if not root.is_dir():
        plan.notes.append("backups_root missing or not a directory")
        return plan

    dirs = [d for d in root.iterdir() if d.is_dir() and d.name.startswith("l4compress-")]
    parsed: list[tuple[Path, Optional[datetime]]] = []
    for d in dirs:
        parsed.append((d, parse_name_ts(d.name)))

    # newest first by name_ts; unknowns last
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    parsed.sort(key=lambda x: x[1] or epoch, reverse=True)

    for rank, (d, ts) in enumerate(parsed, 1):
        n, b = dir_bytes(d)
        age_name = (now - ts).total_seconds() / 86400 if ts else None
        try:
            mtime = datetime.fromtimestamp(d.stat().st_mtime, tz=timezone.utc)
            age_mtime = (now - mtime).total_seconds() / 86400
        except OSError:
            mtime, age_mtime = None, None
        keep = rank <= keep_last or (
            age_name is not None and age_name <= max_age_days
        )
        item = BackupItem(
            name=d.name,
            path=str(d),
            name_ts=ts.strftime("%Y%m%d-%H%M%S") if ts else None,
            age_from_name_days=round(age_name, 2) if age_name is not None else None,
            age_days=round(age_mtime, 2) if age_mtime is not None else None,
            rank_from_newest=rank,
            files=n,
            bytes=b,
            action="KEEP" if keep else "DELETE_CANDIDATE",
        )
        plan.items.append(item)
        if keep:
            plan.keep.append(d.name)
        else:
            plan.candidates.append(d.name)
            plan.total_candidate_bytes += b

    plan.notes.append(
        f"scanned={len(plan.items)} keep={len(plan.keep)} "
        f"candidates={len(plan.candidates)} (dry-run; no mutate)"
    )
    return plan


def apply_rotation(
    plan: RotationPlan,
    *,
    quarantine_root: Path | str,
    confirm: str = "",
    dry_run: bool = False,
) -> RotationPlan:
    """MOVE DELETE_CANDIDATE dirs into quarantine_root.

    Dual gate:
      1) confirm must equal CONFIRM_TOKEN ("ROTATE-BACKUPS")
      2) dry_run=False required to actually move

    Never unlinks; only shutil.move into quarantine. Hybrid/CAS paths are
    out of scope (caller must pass summaries/.backups only).
    """
    if dry_run:
        plan.notes.append("apply_rotation dry_run=True — no moves")
        return plan
    if confirm != CONFIRM_TOKEN:
        plan.errors.append(
            f"apply refused: confirm must be {CONFIRM_TOKEN!r} (got {confirm!r})"
        )
        return plan
    if not plan.candidates:
        plan.notes.append("no candidates to move")
        plan.applied = True
        return plan

    qroot = Path(quarantine_root)
    try:
        qroot.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        plan.errors.append(f"cannot create quarantine: {e}")
        return plan

    root = Path(plan.backups_root)
    moved: list[dict[str, str]] = []
    for name in list(plan.candidates):
        src = root / name
        if not src.is_dir():
            plan.errors.append(f"missing candidate dir: {src}")
            continue
        # namespace under quarantine by original name (+ counter if collision)
        dst = qroot / name
        if dst.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            dst = qroot / f"{name}__{stamp}"
        try:
            shutil.move(str(src), str(dst))
            moved.append({"from": str(src), "to": str(dst)})
        except OSError as e:
            plan.errors.append(f"move failed {src} -> {dst}: {e}")

    plan.moved = moved
    plan.applied = len(moved) > 0 and not plan.errors
    if moved and not plan.errors:
        plan.notes.append(f"moved {len(moved)} dirs to quarantine")
    elif moved and plan.errors:
        plan.notes.append(f"partial move: {len(moved)} ok, errors={len(plan.errors)}")
        plan.applied = False
    return plan


def find_sender_backups(
    data_root: Path | str,
    sender: str,
) -> Path:
    """Resolve memory/conversations/{sender}/summaries/.backups under data_root."""
    return (
        Path(data_root)
        / "memory"
        / "conversations"
        / sender
        / "summaries"
        / ".backups"
    )


def plan_for_sender(
    data_root: Path | str,
    sender: str,
    keep_last: int = DEFAULT_KEEP_LAST,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    now: Optional[datetime] = None,
) -> RotationPlan:
    return plan_rotation(
        find_sender_backups(data_root, sender),
        keep_last=keep_last,
        max_age_days=max_age_days,
        now=now,
    )


def plan_to_report(plan: RotationPlan) -> dict[str, Any]:
    """JSON-serializable report aligned with T1 dry-run script shape."""
    return plan.to_dict()


__all__ = [
    "CONFIRM_TOKEN",
    "DEFAULT_KEEP_LAST",
    "DEFAULT_MAX_AGE_DAYS",
    "BackupItem",
    "RotationPlan",
    "parse_name_ts",
    "dir_bytes",
    "plan_rotation",
    "apply_rotation",
    "find_sender_backups",
    "plan_for_sender",
    "plan_to_report",
]
