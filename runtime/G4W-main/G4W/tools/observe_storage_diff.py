"""Read-only multi-source storage observer for dual-write / hybrid gates.

Never mutates G4W-data. Reports go to --out-json / --out-md only
(BBS_CWD evidence or tempfile). Exit: 0=package pass, 1=fail, 2=tool error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import traceback
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

MESSAGE_ID_MARKER = "G4W:message_id="
HEADER_RE = re.compile(r"^\[([^\]]+)\] (User|Assistant):\n", re.MULTILINE)
MID_RE = re.compile(
    rf"\n?<!--\s*{re.escape(MESSAGE_ID_MARKER)}(.*?)\s*-->", re.I
)
PROBE_SENDERS = {
    "s6-observation-probe",
    "m4-migration-verification",
}
DEFAULT_PACKAGE = "observe_green"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _parse_ts(raw: str) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    # ISO / Z
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    # transcript stamp: 2026-07-18 02:25:11 Asia/Shanghai
    m = re.match(
        r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.(\d+))?(?:\s+(.+))?",
        text,
    )
    if not m:
        return None
    date_s, time_s, frac, zone = m.groups()
    base = f"{date_s}T{time_s}"
    if frac:
        base += f".{frac[:6]}"
    try:
        dt = datetime.fromisoformat(base)
    except ValueError:
        return None
    if zone and "Shanghai" in zone:
        dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def sender_to_fs(sender_id: str) -> str:
    return str(sender_id or "").replace("@", "_")


def parse_transcript_mids(path: Path) -> Dict[str, Dict[str, Any]]:
    """Extract message_id -> {role, ts, body_sha256, stamp} from transcript.md."""
    out: Dict[str, Dict[str, Any]] = {}
    if not path.is_file():
        return out
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    headers = list(HEADER_RE.finditer(text))
    for i, match in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        body = text[match.end() : end].strip()
        stamp, role = match.group(1), match.group(2)
        mid_m = MID_RE.search(body)
        if not mid_m:
            continue
        mid = mid_m.group(1).strip()
        if not mid:
            continue
        body_clean = MID_RE.sub("", body).strip()
        # strip other HTML comments best-effort
        body_clean = re.sub(r"\n?<!--.*?-->", "", body_clean, flags=re.S).strip()
        out[mid] = {
            "message_id": mid,
            "role": role,
            "stamp": stamp,
            "ts": _parse_ts(stamp),
            "body_sha256": _sha256_text(body_clean),
            "source": "aggregate",
        }
    return out


def load_mirror_c(
    mirror_root: Path,
    sender_id: str,
    include_probes: bool = False,
) -> Dict[str, Dict[str, Any]]:
    """Load short-path-mirror conversation JSON keyed by message_id."""
    out: Dict[str, Dict[str, Any]] = {}
    root = Path(mirror_root) / "c"
    if not root.is_dir():
        return out
    for path in root.rglob("*.json"):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if rec.get("kind") not in (None, "conversation"):
            # still accept missing kind if source looks like conversation
            if rec.get("kind") and rec.get("kind") != "conversation":
                continue
        src = rec.get("source") or {}
        sid = str(src.get("sender_id") or "").strip()
        mid = str(src.get("message_id") or "").strip()
        if not mid:
            continue
        if sid and sid != sender_id:
            # allow probe only if include_probes
            if not include_probes and any(p in sid for p in PROBE_SENDERS):
                continue
            if sid != sender_id:
                continue
        if not include_probes and any(p in mid for p in PROBE_SENDERS):
            continue
        content = rec.get("content")
        if content is None:
            content = ""
        body_hash = rec.get("content_sha256") or _sha256_text(str(content))
        ts_raw = src.get("timestamp") or rec.get("written_at")
        out[mid] = {
            "message_id": mid,
            "role": src.get("role") or "",
            "ts": _parse_ts(str(ts_raw or "")),
            "body_sha256": body_hash,
            "written_at": rec.get("written_at"),
            "path": str(path),
            "source": "mirror_c",
        }
    return out


def load_hybrid_counts(hybrid_root: Path) -> Dict[str, Any]:
    meta = Path(hybrid_root) / "meta.sqlite"
    cas = Path(hybrid_root) / "cas"
    result: Dict[str, Any] = {
        "meta_exists": meta.is_file(),
        "cas_exists": cas.is_dir(),
        "chunks": None,
        "cas_index": None,
        "cas_files": None,
        "source_paths": [],
        "error": None,
    }
    if not meta.is_file():
        result["error"] = "meta.sqlite missing"
        return result
    try:
        uri = meta.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            result["chunks"] = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            result["cas_index"] = conn.execute("SELECT COUNT(*) FROM cas_index").fetchone()[0]
            rows = conn.execute(
                "SELECT DISTINCT source_path FROM chunks WHERE source_path IS NOT NULL LIMIT 5000"
            ).fetchall()
            result["source_paths"] = [r[0] for r in rows if r[0]]
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"sqlite: {exc}"
        return result
    n_files = 0
    if cas.is_dir():
        for p in cas.rglob("*"):
            if p.is_file() and not p.name.startswith("."):
                n_files += 1
    result["cas_files"] = n_files
    return result


def load_dual_state(data_root: Path) -> Dict[str, Any]:
    path = Path(data_root) / "observation" / "post_migration_dual_write_12h_state.json"
    if not path.is_file():
        return {"exists": False}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["exists"] = True
        data["_path"] = str(path)
        return data
    except (OSError, json.JSONDecodeError) as exc:
        return {"exists": True, "error": str(exc)}


def resolve_window(
    window_mode: str,
    dual: Dict[str, Any],
    mirror: Dict[str, Dict[str, Any]],
    since_utc: Optional[str],
) -> Tuple[Optional[datetime], str]:
    """Return (window_start_utc, description). None start = no lower bound (not recommended)."""
    if window_mode == "hours12":
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=12)
        return start, "rolling_12h"
    if window_mode == "since_utc" and since_utc:
        dt = _parse_ts(since_utc)
        return dt, f"since_utc={since_utc}"
    if window_mode == "mirror_active":
        # min written_at / ts among mirror entries for sender
        times = [m["ts"] for m in mirror.values() if m.get("ts")]
        if dual.get("window_start_utc"):
            dt = _parse_ts(str(dual["window_start_utc"]))
            if dt:
                return dt, "dual.window_start_utc"
        if times:
            return min(times), "min_mirror_ts"
        return None, "mirror_active_empty"
    # default: dual window if present else mirror_active
    if dual.get("window_start_utc"):
        dt = _parse_ts(str(dual["window_start_utc"]))
        if dt:
            return dt, "dual.window_start_utc"
    times = [m["ts"] for m in mirror.values() if m.get("ts")]
    if times:
        return min(times), "min_mirror_ts"
    return None, "unbounded"


def in_window(ts: Optional[datetime], start: Optional[datetime]) -> bool:
    if start is None:
        return True
    if ts is None:
        return False
    return ts >= start


def evaluate_metrics(
    agg: Dict[str, Dict[str, Any]],
    mir: Dict[str, Dict[str, Any]],
    hybrid: Dict[str, Any],
    dual: Dict[str, Any],
    window_start: Optional[datetime],
    min_chunks: Optional[int],
    sample_hash: int,
) -> Dict[str, Any]:
    mid_a = set(agg)
    mid_c = set(mir)
    # window filter for aggregate: mid in window by ts OR mid in mirror set
    mid_a_win = {
        m
        for m, rec in agg.items()
        if in_window(rec.get("ts"), window_start) or m in mid_c
    }
    # for coverage: prefer window; if empty fall back to mid_c join set
    if not mid_a_win and mid_c:
        mid_a_win = mid_a & mid_c  # type: ignore[assignment]
        # still keep only those with mids in mirror for "mirror_active" spirit
    both = mid_a & mid_c
    both_win = mid_a_win & mid_c
    only_a = mid_a_win - mid_c
    only_c = mid_c - mid_a
    # hash mismatches
    mismatches = []
    for mid in sorted(both):
        ha = agg[mid].get("body_sha256")
        hc = mir[mid].get("body_sha256")
        if ha and hc and ha != hc:
            mismatches.append(mid)
    # sample extra checks
    sample_ids = sorted(both)[: max(0, sample_hash)]
    sample_ok = sum(
        1
        for mid in sample_ids
        if agg[mid].get("body_sha256") == mir[mid].get("body_sha256")
    )

    count_a_win = len(mid_a_win)
    count_both = len(both)
    count_both_win = len(both_win)
    coverage = (count_both_win / count_a_win) if count_a_win else (1.0 if not mid_c else 0.0)
    # alternate coverage: both / max(window A that have mid potential)
    # Prefer: coverage = count_both / max(count of mids in A that are "dual-era")
    # When A-window is huge historical, use mid_c as denominator for M1_mirror_coverage
    m1_join = (len(both) / len(mid_c)) if mid_c else 1.0
    m1 = coverage if count_a_win else m1_join
    # Use max of join coverage vs window: for observe_green we use
    # M1 = count_both / max(count_C, 1) when window is mirror_active (mirror is dual-write set)
    if window_start is not None and mid_c:
        m1 = len(both) / max(len(mid_c), 1)

    only_mirror_rate = (len(only_c) / max(len(mid_c), 1)) if mid_c else 0.0
    hash_mismatch_rate = (len(mismatches) / max(len(both), 1)) if both else 0.0

    last_ts_a = max((r["ts"] for r in agg.values() if r.get("ts")), default=None)
    last_ts_c = max((r["ts"] for r in mir.values() if r.get("ts")), default=None)
    last_ts_delta = None
    if last_ts_a and last_ts_c:
        last_ts_delta = abs((last_ts_a - last_ts_c).total_seconds())

    chunks = hybrid.get("chunks")
    cas_index = hybrid.get("cas_index")
    cas_files = hybrid.get("cas_files")
    h1 = (
        chunks is not None
        and cas_index is not None
        and cas_files is not None
        and chunks == cas_index == cas_files
        and chunks >= 1
    )
    h2 = True
    if min_chunks is not None and chunks is not None:
        h2 = chunks >= min_chunks

    o1_ok = dual.get("auto_switch_allowed") is False or dual.get("auto_switch_allowed") is None
    # when dual missing, O1 still "not true"
    if dual.get("exists") and dual.get("auto_switch_allowed") is True:
        o1_ok = False
    o3_ok = True
    if dual.get("earliest_evaluation_utc"):
        earliest = _parse_ts(str(dual["earliest_evaluation_utc"]))
        if earliest and datetime.now(timezone.utc) < earliest:
            o3_ok = False
    # also require duration if window_start present
    if dual.get("window_start_utc") and dual.get("observation_duration_hours"):
        ws = _parse_ts(str(dual["window_start_utc"]))
        hours = float(dual["observation_duration_hours"])
        if ws and datetime.now(timezone.utc) < ws + timedelta(hours=hours):
            o3_ok = False

    metrics = {
        "M1_coverage": round(m1, 6),
        "M1_both_over_mirror": round(len(both) / max(len(mid_c), 1), 6) if mid_c else 1.0,
        "M1_both_over_agg_window": round(coverage, 6),
        "M2_only_mirror_rate": round(only_mirror_rate, 6),
        "M3_hash_mismatch_rate": round(hash_mismatch_rate, 6),
        "M3_hash_mismatch_count": len(mismatches),
        "M5_last_ts_delta_sec": last_ts_delta,
        "H1_hybrid_counts_equal": h1,
        "H1_detail": {
            "chunks": chunks,
            "cas_index": cas_index,
            "cas_files": cas_files,
            "error": hybrid.get("error"),
        },
        "H2_min_chunks": h2,
        "H2_min_chunks_req": min_chunks,
        "O1_auto_switch_false": o1_ok,
        "O2_verdict": dual.get("current_verdict"),
        "O3_observation_elapsed": o3_ok,
        "counts": {
            "agg_mids": len(mid_a),
            "agg_mids_window": count_a_win,
            "mirror_c": len(mid_c),
            "both": count_both,
            "both_window": count_both_win,
            "only_agg_window": len(only_a),
            "only_mirror": len(only_c),
        },
        "samples": {
            "hash_checked": len(sample_ids),
            "hash_ok": sample_ok,
            "mismatch_ids": mismatches[:20],
            "only_mirror_ids": sorted(only_c)[:20],
            "only_agg_window_ids": sorted(only_a)[:20],
        },
        "last_ts": {
            "aggregate": last_ts_a.isoformat().replace("+00:00", "Z") if last_ts_a else None,
            "mirror": last_ts_c.isoformat().replace("+00:00", "Z") if last_ts_c else None,
        },
    }

    # PASS thresholds
    m1_pass_loose = metrics["M1_coverage"] >= 0.95
    m1_pass_strict = metrics["M1_coverage"] >= 0.99
    m2_pass = metrics["M2_only_mirror_rate"] <= 0.01
    m3_pass = metrics["M3_hash_mismatch_count"] == 0
    m5_pass = last_ts_delta is None or last_ts_delta <= 300

    package_observe_green = bool(
        h1 and h2 and m3_pass and m1_pass_loose and o1_ok and o3_ok
    )
    package_cutover_ready = bool(
        package_observe_green
        and m1_pass_strict
        and m2_pass
        and m5_pass
        # H3/H4 not fully computed here — require explicit later; stay False without auth
        and False  # never auto cutover-ready without auth flag
    )

    metrics["pass"] = {
        "M1_loose": m1_pass_loose,
        "M1_strict": m1_pass_strict,
        "M2": m2_pass,
        "M3": m3_pass,
        "M5": m5_pass,
        "H1": h1,
        "H2": h2,
        "O1": o1_ok,
        "O3": o3_ok,
        "PACKAGE_OBSERVE_GREEN": package_observe_green,
        "PACKAGE_CUTOVER_READY": package_cutover_ready,
    }
    return metrics


def build_report(
    data_root: Path,
    sender_id: str,
    window_mode: str,
    since_utc: Optional[str],
    min_chunks: Optional[int],
    sample_hash: int,
    include_probes: bool,
    package: str,
) -> Dict[str, Any]:
    data_root = Path(data_root)
    fs = sender_to_fs(sender_id)
    agg_path = data_root / "memory" / "conversations" / fs / "transcript.md"
    mirror_root = data_root / "short-path-mirror"
    hybrid_root = data_root / "hybrid"

    dual = load_dual_state(data_root)
    if dual.get("mirror_root"):
        # allow dual to point mirror elsewhere (RO)
        alt = Path(str(dual["mirror_root"]))
        if alt.is_dir():
            mirror_root = alt

    agg = parse_transcript_mids(agg_path)
    mir = load_mirror_c(mirror_root, sender_id, include_probes=include_probes)
    hybrid = load_hybrid_counts(hybrid_root)
    window_start, window_desc = resolve_window(window_mode, dual, mir, since_utc)
    metrics = evaluate_metrics(
        agg, mir, hybrid, dual, window_start, min_chunks, sample_hash
    )

    pkg_key = "PACKAGE_OBSERVE_GREEN"
    if package == "cutover_ready":
        pkg_key = "PACKAGE_CUTOVER_READY"
    pkg_pass = metrics["pass"].get(pkg_key, False)

    report = {
        "schema": "G4W.observe_storage_diff.v1",
        "generated_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "data_root": str(data_root),
        "sender_id": sender_id,
        "paths": {
            "aggregate": str(agg_path),
            "mirror_root": str(mirror_root),
            "hybrid_root": str(hybrid_root),
            "dual_state": dual.get("_path"),
        },
        "window": {
            "mode": window_mode,
            "description": window_desc,
            "start_utc": window_start.isoformat().replace("+00:00", "Z")
            if window_start
            else None,
        },
        "dual_state_summary": {
            "exists": dual.get("exists"),
            "current_verdict": dual.get("current_verdict"),
            "auto_switch_allowed": dual.get("auto_switch_allowed"),
            "observation_duration_hours": dual.get("observation_duration_hours"),
            "window_start_utc": dual.get("window_start_utc"),
            "earliest_evaluation_utc": dual.get("earliest_evaluation_utc"),
        },
        "metrics": metrics,
        "package": package,
        "package_pass": pkg_pass,
        "ro_assert": True,
        "notes": [
            "Reports never write to DATA; observation state is read-only.",
            "PACKAGE_CUTOVER_READY stays false without explicit auth (tool hard-codes).",
            "HYBRID_MAIN_READ env is not cutover authorization.",
        ],
    }
    return report


def report_to_md(report: Dict[str, Any]) -> str:
    m = report.get("metrics") or {}
    p = m.get("pass") or {}
    c = m.get("counts") or {}
    lines = [
        f"# observe_storage_diff report",
        "",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- sender: `{report.get('sender_id')}`",
        f"- data_root: `{report.get('data_root')}`",
        f"- window: `{report.get('window')}`",
        f"- package: `{report.get('package')}` → **{'PASS' if report.get('package_pass') else 'FAIL'}**",
        "",
        "## Counts",
        f"- agg_mids: {c.get('agg_mids')}",
        f"- mirror_c: {c.get('mirror_c')}",
        f"- both: {c.get('both')}",
        f"- only_mirror: {c.get('only_mirror')}",
        f"- only_agg_window: {c.get('only_agg_window')}",
        "",
        "## Metrics",
        f"- M1 coverage: {m.get('M1_coverage')} (loose={p.get('M1_loose')}, strict={p.get('M1_strict')})",
        f"- M2 only_mirror_rate: {m.get('M2_only_mirror_rate')} pass={p.get('M2')}",
        f"- M3 hash_mismatch: {m.get('M3_hash_mismatch_count')} pass={p.get('M3')}",
        f"- H1 hybrid equal: {m.get('H1_detail')} pass={p.get('H1')}",
        f"- H2 min_chunks: pass={p.get('H2')} req={m.get('H2_min_chunks_req')}",
        f"- O1 auto_switch false: {p.get('O1')}",
        f"- O2 verdict: {m.get('O2_verdict')}",
        f"- O3 elapsed: {p.get('O3')}",
        "",
        "## Packages",
        f"- PACKAGE_OBSERVE_GREEN: **{p.get('PACKAGE_OBSERVE_GREEN')}**",
        f"- PACKAGE_CUTOVER_READY: **{p.get('PACKAGE_CUTOVER_READY')}** (never auto without auth)",
        "",
        "## Dual state",
        f"```json",
        json.dumps(report.get("dual_state_summary"), ensure_ascii=False, indent=2),
        "```",
        "",
        "## Samples (truncated)",
        f"```json",
        json.dumps(m.get("samples"), ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="RO observe_storage_diff")
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--sender", required=True)
    parser.add_argument(
        "--window-mode",
        default="mirror_active",
        choices=["mirror_active", "since_utc", "hours12", "dual"],
    )
    parser.add_argument("--since-utc", default=None)
    parser.add_argument("--min-chunks", type=int, default=None)
    parser.add_argument("--sample-hash", type=int, default=50)
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-md", default=None)
    parser.add_argument("--include-probes", action="store_true")
    parser.add_argument(
        "--package",
        default=DEFAULT_PACKAGE,
        choices=["observe_green", "cutover_ready"],
    )
    parser.add_argument("--ro-assert", action="store_true", default=True)
    args = parser.parse_args(argv)

    try:
        # window mode dual -> treat as mirror_active with dual preference
        wmode = args.window_mode
        if wmode == "dual":
            wmode = "mirror_active"
        report = build_report(
            data_root=Path(args.data_root),
            sender_id=args.sender,
            window_mode=wmode,
            since_utc=args.since_utc,
            min_chunks=args.min_chunks,
            sample_hash=args.sample_hash,
            include_probes=args.include_probes,
            package=args.package,
        )
    except Exception:  # noqa: BLE001
        err = traceback.format_exc()
        sys.stderr.write(err)
        if args.out_json:
            try:
                Path(args.out_json).write_text(
                    json.dumps({"error": err, "schema": "G4W.observe_storage_diff.v1"}, indent=2),
                    encoding="utf-8",
                )
            except OSError:
                pass
        return 2

    if args.out_json:
        Path(args.out_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out_json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    if args.out_md:
        Path(args.out_md).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out_md).write_text(report_to_md(report), encoding="utf-8")

    # print brief to stdout
    print(
        json.dumps(
            {
                "package": report["package"],
                "package_pass": report["package_pass"],
                "pass": report["metrics"]["pass"],
                "counts": report["metrics"]["counts"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if report.get("package_pass") else 1


if __name__ == "__main__":
    sys.exit(main())
