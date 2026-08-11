"""Tempfile-only unit tests for observe_storage_diff (never touches production DATA)."""
from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from G4W.tools.observe_storage_diff import (
    build_report,
    evaluate_metrics,
    load_hybrid_counts,
    parse_transcript_mids,
    main as observe_main,
)


def _write_agg(path: Path, blocks: list[tuple[str, str, str, str]]) -> None:
    """blocks: (stamp, role, body, mid|None)"""
    parts = []
    for stamp, role, body, mid in blocks:
        chunk = f"[{stamp}] {role}:\n{body}\n"
        if mid:
            chunk += f"<!-- G4W:message_id={mid} -->\n"
        parts.append(chunk + "\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(parts), encoding="utf-8")


def _write_mirror(
    root: Path,
    mid: str,
    sender: str,
    content: str,
    role: str = "User",
    ts: str = "2026-07-21T12:00:00.000000Z",
) -> None:
    import hashlib

    identity = hashlib.sha256(f"{sender}|{mid}".encode()).hexdigest()
    stable = identity[:32]
    aa, bb = stable[:2], stable[2:4]
    rec = {
        "schema": "G4W.short_path_mirror.v1",
        "kind": "conversation",
        "stable_id": stable,
        "identity_sha256": identity,
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "content": content,
        "source": {
            "message_id": mid,
            "role": role,
            "sender_id": sender,
            "timestamp": ts,
        },
        "legacy_paths": [],
        "written_at": ts,
    }
    p = root / "c" / aa / bb / f"{stable}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")


def _init_hybrid(hybrid: Path, n: int = 3) -> None:
    hybrid.mkdir(parents=True, exist_ok=True)
    cas = hybrid / "cas"
    cas.mkdir(exist_ok=True)
    meta = hybrid / "meta.sqlite"
    conn = sqlite3.connect(str(meta))
    conn.execute(
        "CREATE TABLE chunks (chunk_id TEXT PRIMARY KEY, cas_hash TEXT, source_path TEXT)"
    )
    conn.execute("CREATE TABLE cas_index (cas_hash TEXT PRIMARY KEY, size_bytes INTEGER)")
    for i in range(n):
        h = f"{'ab' * 16}{i:02d}"[:64] if False else f"h{i:064d}"[-64:]
        # fixed-length-ish hashes
        h = f"{i:064x}"
        conn.execute(
            "INSERT INTO chunks(chunk_id, cas_hash, source_path) VALUES (?,?,?)",
            (f"c{i}", h, f"p{i}.md"),
        )
        conn.execute(
            "INSERT INTO cas_index(cas_hash, size_bytes) VALUES (?,?)", (h, 10)
        )
        cp = cas / h[:2] / h[2:4] / h
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_bytes(b"x")
    conn.commit()
    conn.close()


class ObserveStorageDiffTests(unittest.TestCase):
    def test_parse_transcript_mids(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "transcript.md"
            _write_agg(
                p,
                [
                    ("2026-07-21 12:00:00 Asia/Shanghai", "User", "hello", "mid-1"),
                    ("2026-07-21 12:00:01 Asia/Shanghai", "Assistant", "hi", None),
                    ("2026-07-21 12:00:02 Asia/Shanghai", "User", "again", "mid-2"),
                ],
            )
            mids = parse_transcript_mids(p)
            self.assertEqual(set(mids), {"mid-1", "mid-2"})
            self.assertEqual(mids["mid-1"]["role"], "User")

    def test_hybrid_counts_equal(self):
        with tempfile.TemporaryDirectory() as td:
            hybrid = Path(td) / "hybrid"
            _init_hybrid(hybrid, 3)
            c = load_hybrid_counts(hybrid)
            self.assertEqual(c["chunks"], 3)
            self.assertEqual(c["cas_index"], 3)
            self.assertEqual(c["cas_files"], 3)

    def test_package_observe_green_on_fixture(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sender = "user@im.wechat"
            fs = "user_im.wechat"
            agg = root / "memory" / "conversations" / fs / "transcript.md"
            body = "hello dual"
            _write_agg(
                agg,
                [
                    ("2026-07-21 12:00:00 Asia/Shanghai", "User", body, "mid-a"),
                    ("2026-07-21 12:00:05 Asia/Shanghai", "Assistant", "reply", "mid-b"),
                ],
            )
            mir_root = root / "short-path-mirror"
            _write_mirror(mir_root, "mid-a", sender, body, "User", "2026-07-21T04:00:00Z")
            _write_mirror(
                mir_root, "mid-b", sender, "reply", "Assistant", "2026-07-21T04:00:05Z"
            )
            _init_hybrid(root / "hybrid", 2)
            obs = root / "observation"
            obs.mkdir()
            (obs / "post_migration_dual_write_12h_state.json").write_text(
                json.dumps(
                    {
                        "auto_switch_allowed": False,
                        "current_verdict": "NOT_YET_EVALUATED",
                        "observation_duration_hours": 12,
                        "window_start_utc": "2026-07-20T00:00:00Z",
                        "earliest_evaluation_utc": "2026-07-20T12:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            report = build_report(
                data_root=root,
                sender_id=sender,
                window_mode="mirror_active",
                since_utc=None,
                min_chunks=2,
                sample_hash=10,
                include_probes=False,
                package="observe_green",
            )
            self.assertTrue(report["metrics"]["pass"]["H1"])
            self.assertTrue(report["metrics"]["pass"]["M3"])
            self.assertTrue(report["metrics"]["pass"]["M1_loose"])
            self.assertTrue(report["package_pass"])

    def test_hash_mismatch_fails_m3(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sender = "user@im.wechat"
            fs = "user_im.wechat"
            agg = root / "memory" / "conversations" / fs / "transcript.md"
            _write_agg(
                agg,
                [("2026-07-21 12:00:00 Asia/Shanghai", "User", "hello", "mid-a")],
            )
            _write_mirror(
                root / "short-path-mirror",
                "mid-a",
                sender,
                "DIFFERENT",
                "User",
                "2026-07-21T04:00:00Z",
            )
            _init_hybrid(root / "hybrid", 1)
            report = build_report(
                data_root=root,
                sender_id=sender,
                window_mode="mirror_active",
                since_utc=None,
                min_chunks=None,
                sample_hash=5,
                include_probes=False,
                package="observe_green",
            )
            self.assertFalse(report["metrics"]["pass"]["M3"])
            self.assertFalse(report["package_pass"])

    def test_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sender = "user@im.wechat"
            fs = "user_im.wechat"
            body = "x"
            _write_agg(
                root / "memory" / "conversations" / fs / "transcript.md",
                [("2026-07-21 12:00:00 Asia/Shanghai", "User", body, "m1")],
            )
            _write_mirror(
                root / "short-path-mirror", "m1", sender, body, "User", "2026-07-21T04:00:00Z"
            )
            _init_hybrid(root / "hybrid", 1)
            out_j = str(Path(td) / "r.json")
            out_m = str(Path(td) / "r.md")
            code = observe_main(
                [
                    "--data-root",
                    str(root),
                    "--sender",
                    sender,
                    "--out-json",
                    out_j,
                    "--out-md",
                    out_m,
                    "--min-chunks",
                    "1",
                    "--package",
                    "observe_green",
                ]
            )
            self.assertEqual(code, 0)
            self.assertTrue(Path(out_j).is_file())
            self.assertTrue(Path(out_m).is_file())

    def test_cli_cutover_ready_hard_fail(self):
        # No docstring: -v must keep "name ... ok" same-line for Master present check
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sender = "user@im.wechat"
            fs = "user_im.wechat"
            body = "x"
            _write_agg(
                root / "memory" / "conversations" / fs / "transcript.md",
                [("2026-07-21 12:00:00 Asia/Shanghai", "User", body, "m1")],
            )
            _write_mirror(
                root / "short-path-mirror", "m1", sender, body, "User", "2026-07-21T04:00:00Z"
            )
            _init_hybrid(root / "hybrid", 1)
            out_j = str(Path(td) / "cutover.json")
            out_m = str(Path(td) / "cutover.md")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = observe_main(
                    [
                        "--data-root",
                        str(root),
                        "--sender",
                        sender,
                        "--out-json",
                        out_j,
                        "--out-md",
                        out_m,
                        "--min-chunks",
                        "1",
                        "--package",
                        "cutover_ready",
                    ]
                )
            self.assertNotEqual(code, 0)
            self.assertEqual(code, 1)
            self.assertTrue(Path(out_j).is_file())
            data = json.loads(Path(out_j).read_text(encoding="utf-8"))
            self.assertFalse(data.get("package_pass"))
            self.assertEqual(data.get("package"), "cutover_ready")
            packages = (data.get("metrics") or {}).get("pass") or {}
            self.assertFalse(packages.get("PACKAGE_CUTOVER_READY", True))
            # observe_green may still be true on same fixture — green ≠ cutover
            self.assertTrue(packages.get("PACKAGE_OBSERVE_GREEN"))

    def test_package_cutover_ready_always_false_on_fixture(self):
        # No docstring: keep unittest -v "name ... ok" on one line
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sender = "user@im.wechat"
            fs = "user_im.wechat"
            body = "hello dual"
            agg = root / "memory" / "conversations" / fs / "transcript.md"
            _write_agg(
                agg,
                [
                    ("2026-07-21 12:00:00 Asia/Shanghai", "User", body, "mid-a"),
                    ("2026-07-21 12:00:05 Asia/Shanghai", "Assistant", "reply", "mid-b"),
                ],
            )
            mir_root = root / "short-path-mirror"
            _write_mirror(mir_root, "mid-a", sender, body, "User", "2026-07-21T04:00:00Z")
            _write_mirror(
                mir_root, "mid-b", sender, "reply", "Assistant", "2026-07-21T04:00:05Z"
            )
            _init_hybrid(root / "hybrid", 2)
            green = build_report(
                data_root=root,
                sender_id=sender,
                window_mode="mirror_active",
                since_utc=None,
                min_chunks=1,
                sample_hash=5,
                include_probes=False,
                package="observe_green",
            )
            cut = build_report(
                data_root=root,
                sender_id=sender,
                window_mode="mirror_active",
                since_utc=None,
                min_chunks=1,
                sample_hash=5,
                include_probes=False,
                package="cutover_ready",
            )
            self.assertTrue(green["package_pass"])
            self.assertFalse(cut["package_pass"])
            self.assertFalse(cut["metrics"]["pass"]["PACKAGE_CUTOVER_READY"])
            self.assertTrue(cut["metrics"]["pass"]["PACKAGE_OBSERVE_GREEN"])


if __name__ == "__main__":
    unittest.main()
