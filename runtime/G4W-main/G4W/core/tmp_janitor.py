"""Unified tmp orphan scan / plan / apply for G4W state roots.

Merges T1 p0_tmp_scan_tool + p1_orphan_tmp_cleanup_dryrun into one library API.
Default is dry-run: apply_deletes refuses unless confirm == CONFIRM_DELETE_TMP.
Never intended to walk hybrid/cas product trees as delete candidates.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

# JsonStore atomic leftover: .{name}.{pid}.{tid}.{32-hex}.tmp
JSONSTORE_TMP = re.compile(r"^\..+\.\d+\.\d+\.[0-9a-f]{32}\.tmp$", re.I)
NAME_TMP = re.compile(r"(\.tmp$|\.temp$|\.partial$|\.swp$)", re.I)

CONFIRM_DELETE_TMP = "DELETE-TMP"

# Path parts that must never be auto-deleted (product / user capture / CAS / backups)
DEFAULT_WHITELIST: frozenset[str] = frozenset({
    "edge_headless_capture",
    ".backups",
    "cas",
})

# Relative prefixes skipped entirely for orphan tmp (do not deep-scan as delete targets)
DEFAULT_SKIP_PREFIXES: tuple[str, ...] = (
    "hybrid/cas",
    "hybrid\\cas",
)

DEFAULT_PATTERNS: tuple[str, ...] = (
    "jsonstore_atomic_tmp",
    "name_pattern_tmp",
)


@dataclass
class TmpHit:
    path: str
    rel: str
    size: int
    mtime: float
    age_sec: int
    kind: str
    would_delete: bool
    blocked_by_whitelist: bool
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScanReport:
    root: str
    scan_time: str
    min_age_sec: float
    hits: list[TmpHit] = field(default_factory=list)
    temp_inventory: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "scan_time": self.scan_time,
            "min_age_sec": self.min_age_sec,
            "tmp_hit_count": len(self.hits),
            "would_delete_count": sum(1 for h in self.hits if h.would_delete),
            "would_delete_bytes": sum(h.size for h in self.hits if h.would_delete),
            "hits": [h.to_dict() for h in self.hits],
            "temp_inventory": self.temp_inventory,
            "notes": self.notes,
        }


@dataclass
class DeletePlan:
    root: str
    candidates: list[dict[str, Any]] = field(default_factory=list)
    dry_run: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "dry_run": self.dry_run,
            "candidate_count": len(self.candidates),
            "candidates": self.candidates,
        }


@dataclass
class ApplyResult:
    ok: bool
    mode: str
    deleted: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    refused_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _norm_parts(path: Path) -> set[str]:
    return {p.lower() for p in path.parts}


def _blocked(path: Path, whitelist: Iterable[str]) -> bool:
    wl = {w.lower() for w in whitelist}
    parts = _norm_parts(path)
    if parts & wl:
        return True
    # also match substring path segments like hybrid/cas
    s = str(path).replace("/", "\\").lower()
    for w in wl:
        token = w.replace("/", "\\").lower()
        if token and token in s:
            return True
    return False


def _should_skip_walk_rel(rel: str, skip_prefixes: Sequence[str]) -> bool:
    r = rel.replace("/", "\\").lower()
    for pref in skip_prefixes:
        p = pref.replace("/", "\\").lower().rstrip("\\")
        if r == p or r.startswith(p + "\\"):
            return True
    return False


def _classify_name(name: str, patterns: Sequence[str]) -> str | None:
    if "jsonstore_atomic_tmp" in patterns and JSONSTORE_TMP.match(name):
        return "jsonstore_atomic_tmp"
    if "name_pattern_tmp" in patterns and (NAME_TMP.search(name) or name.endswith(".tmp")):
        # avoid double-count: jsonstore already matched above
        if JSONSTORE_TMP.match(name):
            return "jsonstore_atomic_tmp"
        return "name_pattern_tmp"
    return None


def _temp_inventory(root: Path) -> list[dict[str, Any]]:
    temp = root / "temp"
    plan: list[dict[str, Any]] = []
    if not temp.is_dir():
        return plan
    now = time.time()
    for child in temp.iterdir():
        try:
            if child.is_file():
                st = child.stat()
                plan.append({
                    "path": str(child.relative_to(root)).replace("\\", "/"),
                    "kind": "file",
                    "files": 1,
                    "bytes": st.st_size,
                    "age_h": round((now - st.st_mtime) / 3600, 2),
                    "action": "REVIEW",
                    "name": child.name,
                })
            elif child.is_dir():
                n, b = 0, 0
                newest = oldest = None
                for f in child.rglob("*"):
                    if not f.is_file():
                        continue
                    n += 1
                    try:
                        st = f.stat()
                        b += st.st_size
                        newest = st.st_mtime if newest is None else max(newest, st.st_mtime)
                        oldest = st.st_mtime if oldest is None else min(oldest, st.st_mtime)
                    except OSError:
                        pass
                name = child.name
                action = "KEEP_REVIEW"
                if name in ("收藏",) or name.endswith(".hap"):
                    action = "KEEP_USER"
                if name.lower() == "edge_headless_capture" or "edge_headless_capture" in name.lower():
                    action = "KEEP_WHITELIST"
                plan.append({
                    "path": str(child.relative_to(root)).replace("\\", "/"),
                    "kind": "dir",
                    "files": n,
                    "bytes": b,
                    "age_newest_h": round((now - newest) / 3600, 2) if newest else None,
                    "age_oldest_h": round((now - oldest) / 3600, 2) if oldest else None,
                    "action": action,
                    "name": name,
                })
        except OSError:
            continue
    return plan


def scan_orphan_tmp(
    root: str | Path,
    patterns: Sequence[str] = DEFAULT_PATTERNS,
    whitelist: Iterable[str] = DEFAULT_WHITELIST,
    min_age_sec: float = 0.0,
    skip_prefixes: Sequence[str] = DEFAULT_SKIP_PREFIXES,
) -> ScanReport:
    """Scan root for orphan-like tmp files. Read-only. Does not delete.

    Covers JsonStore `.*.tmp` atomic sidecars and common name patterns.
    Paths under whitelist parts (incl. edge_headless_capture) never would_delete.
    hybrid/cas prefixes are skipped for walk classification of orphans.
    """
    root_p = Path(root).resolve()
    now = time.time()
    hits: list[TmpHit] = []
    notes = [
        "dry-run scan only; use plan_deletes + apply_deletes(confirm=DELETE-TMP) to mutate",
        "whitelist always includes edge_headless_capture by DEFAULT_WHITELIST",
        "skip_prefixes default excludes hybrid/cas deep trees",
    ]

    if not root_p.is_dir():
        notes.append(f"root missing or not dir: {root_p}")
        return ScanReport(
            root=str(root_p),
            scan_time=time.strftime("%Y-%m-%d %H:%M:%S"),
            min_age_sec=min_age_sec,
            hits=[],
            temp_inventory=[],
            notes=notes,
        )

    for dirpath, dirnames, filenames in os.walk(root_p):
        rel_dir = os.path.relpath(dirpath, root_p)
        if rel_dir == ".":
            rel_dir = ""
        # prune hybrid/cas and other skip prefixes
        pruned = []
        for d in list(dirnames):
            child_rel = f"{rel_dir}/{d}" if rel_dir else d
            if _should_skip_walk_rel(child_rel, skip_prefixes):
                pruned.append(d)
        for d in pruned:
            dirnames.remove(d)

        for fn in filenames:
            path = Path(dirpath) / fn
            kind = _classify_name(fn, patterns)
            if kind is None:
                continue
            rel = os.path.relpath(path, root_p)
            if _should_skip_walk_rel(rel, skip_prefixes):
                continue
            try:
                st = path.stat()
            except OSError as e:
                hits.append(TmpHit(
                    path=str(path), rel=rel.replace("\\", "/"), size=0, mtime=0.0,
                    age_sec=0, kind=kind, would_delete=False,
                    blocked_by_whitelist=False, error=str(e),
                ))
                continue
            age = now - st.st_mtime
            blocked = _blocked(path, whitelist)
            would = (not blocked) and (age >= min_age_sec)
            hits.append(TmpHit(
                path=str(path),
                rel=rel.replace("\\", "/"),
                size=int(st.st_size),
                mtime=float(st.st_mtime),
                age_sec=int(age),
                kind=kind,
                would_delete=bool(would),
                blocked_by_whitelist=bool(blocked),
            ))

    inv = _temp_inventory(root_p)
    return ScanReport(
        root=str(root_p),
        scan_time=time.strftime("%Y-%m-%d %H:%M:%S"),
        min_age_sec=min_age_sec,
        hits=hits,
        temp_inventory=inv,
        notes=notes,
    )


def plan_deletes(report: ScanReport | dict[str, Any]) -> DeletePlan:
    """Build delete plan from a ScanReport (or its to_dict()). Dry-run flag stays True."""
    if isinstance(report, ScanReport):
        root = report.root
        hits = report.hits
        cands = [
            {
                "path": h.path,
                "rel": h.rel,
                "size": h.size,
                "kind": h.kind,
                "age_sec": h.age_sec,
            }
            for h in hits if h.would_delete
        ]
    else:
        root = str(report.get("root", ""))
        cands = []
        for h in report.get("hits") or []:
            if h.get("would_delete"):
                cands.append({
                    "path": h.get("path", ""),
                    "rel": h.get("rel", ""),
                    "size": h.get("size", 0),
                    "kind": h.get("kind", ""),
                    "age_sec": h.get("age_sec", 0),
                })
    return DeletePlan(root=root, candidates=cands, dry_run=True)


def apply_deletes(
    plan: DeletePlan | dict[str, Any],
    confirm: str = "",
    *,
    force_confirm: str = CONFIRM_DELETE_TMP,
) -> ApplyResult:
    """Delete plan candidates. Refuses unless confirm == DELETE-TMP (default token).

    No implicit apply: empty confirm always refuses. Does not touch non-candidates.
    """
    if isinstance(plan, dict):
        candidates = list(plan.get("candidates") or [])
        root = str(plan.get("root", ""))
    else:
        candidates = list(plan.candidates)
        root = plan.root

    if confirm != force_confirm:
        return ApplyResult(
            ok=False,
            mode="REFUSED",
            deleted=[],
            errors=[],
            refused_reason=f"need confirm={force_confirm!r}, got {confirm!r}",
        )

    deleted: list[str] = []
    errors: list[dict[str, str]] = []
    for c in candidates:
        p = Path(c.get("path") or "")
        rel = c.get("rel") or str(p)
        try:
            if p.is_file():
                p.unlink()
                deleted.append(rel)
            elif not p.exists():
                # already gone — not an error
                deleted.append(rel)
            else:
                errors.append({"rel": rel, "error": "not a file"})
        except OSError as e:
            errors.append({"rel": rel, "error": str(e)})

    return ApplyResult(
        ok=len(errors) == 0,
        mode="APPLY",
        deleted=deleted,
        errors=errors,
        refused_reason="",
    )


# Back-compat aliases matching design freeze names
scan = scan_orphan_tmp
plan = plan_deletes
apply = apply_deletes

__all__ = [
    "CONFIRM_DELETE_TMP",
    "DEFAULT_WHITELIST",
    "DEFAULT_SKIP_PREFIXES",
    "DEFAULT_PATTERNS",
    "TmpHit",
    "ScanReport",
    "DeletePlan",
    "ApplyResult",
    "scan_orphan_tmp",
    "plan_deletes",
    "apply_deletes",
]
