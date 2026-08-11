"""Unit tests for G4W.memory.backups_rotation (tempfile only; no prod data)."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from G4W.memory.backups_rotation import (
    CONFIRM_TOKEN,
    apply_rotation,
    find_sender_backups,
    parse_name_ts,
    plan_for_sender,
    plan_rotation,
)


def _mk_backup(root: Path, name: str, payload: str = "x") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "snap.txt").write_text(payload, encoding="utf-8")
    return d


class TestParseNameTs(unittest.TestCase):
    def test_ok(self):
        ts = parse_name_ts("l4compress-20260701-120000")
        self.assertIsNotNone(ts)
        self.assertEqual(ts.year, 2026)
        self.assertEqual(ts.month, 7)
        self.assertEqual(ts.day, 1)

    def test_bad(self):
        self.assertIsNone(parse_name_ts("not-a-backup"))
        self.assertIsNone(parse_name_ts("l4compress-bad"))


class TestPlanRotation(unittest.TestCase):
    def test_missing_root(self):
        with tempfile.TemporaryDirectory() as td:
            plan = plan_rotation(Path(td) / "nope", keep_last=2, max_age_days=1)
            self.assertEqual(plan.items, [])
            self.assertTrue(any("missing" in n for n in plan.notes))

    def test_keep_last_and_age(self):
        # now fixed so ages are deterministic
        now = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".backups"
            root.mkdir()
            # 8 backups: newest first by name; keep_last=3, max_age_days=7
            # days 0,2,4,6,8,10,12,14 from now
            for i, day_ago in enumerate([0, 2, 4, 6, 8, 10, 12, 14]):
                dt = now - timedelta(days=day_ago)
                name = f"l4compress-{dt.strftime('%Y%m%d-%H%M%S')}"
                _mk_backup(root, name, payload=f"p{i}")

            plan = plan_rotation(root, keep_last=3, max_age_days=7.0, now=now)
            self.assertEqual(len(plan.items), 8)
            # rank 1-3 always KEEP; day_ago 0,2,4,6 also age-keep (≤7)
            # day_ago 8,10,12,14: rank 5-8 → DELETE if age>7 and rank>3
            keeps = {i.name for i in plan.items if i.action == "KEEP"}
            cands = {i.name for i in plan.items if i.action == "DELETE_CANDIDATE"}
            self.assertEqual(len(keeps) + len(cands), 8)
            # at least the 3 newest kept
            ranked = sorted(plan.items, key=lambda x: x.rank_from_newest)
            for item in ranked[:3]:
                self.assertEqual(item.action, "KEEP", item.name)
            # oldest should be candidate
            oldest = ranked[-1]
            self.assertEqual(oldest.action, "DELETE_CANDIDATE")
            self.assertGreater(plan.total_candidate_bytes, 0)
            self.assertFalse(plan.applied)

    def test_all_keep_when_small(self):
        now = datetime(2026, 7, 20, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for i in range(3):
                dt = now - timedelta(days=i)
                _mk_backup(root, f"l4compress-{dt.strftime('%Y%m%d-%H%M%S')}")
            plan = plan_rotation(root, keep_last=5, max_age_days=14, now=now)
            self.assertEqual(plan.candidates, [])
            self.assertEqual(len(plan.keep), 3)


class TestApplyRotation(unittest.TestCase):
    def test_refuse_without_confirm(self):
        now = datetime(2026, 7, 20, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "b"
            root.mkdir()
            for day_ago in [0, 30, 60, 90, 120]:
                dt = now - timedelta(days=day_ago)
                _mk_backup(root, f"l4compress-{dt.strftime('%Y%m%d-%H%M%S')}")
            plan = plan_rotation(root, keep_last=1, max_age_days=1, now=now)
            self.assertTrue(plan.candidates)
            q = Path(td) / "q"
            out = apply_rotation(plan, quarantine_root=q, confirm="wrong", dry_run=False)
            self.assertTrue(out.errors)
            self.assertFalse(out.applied)
            # sources still present
            for name in plan.candidates:
                self.assertTrue((root / name).is_dir())

    def test_dry_run_flag_no_move(self):
        now = datetime(2026, 7, 20, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "b"
            root.mkdir()
            for day_ago in [0, 40, 50]:
                dt = now - timedelta(days=day_ago)
                _mk_backup(root, f"l4compress-{dt.strftime('%Y%m%d-%H%M%S')}")
            plan = plan_rotation(root, keep_last=1, max_age_days=1, now=now)
            q = Path(td) / "q"
            out = apply_rotation(
                plan, quarantine_root=q, confirm=CONFIRM_TOKEN, dry_run=True
            )
            self.assertFalse(out.moved)
            for name in plan.candidates:
                self.assertTrue((root / name).is_dir())

    def test_apply_moves_to_quarantine(self):
        now = datetime(2026, 7, 20, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "b"
            root.mkdir()
            names = []
            for day_ago in [0, 40, 50, 60]:
                dt = now - timedelta(days=day_ago)
                n = f"l4compress-{dt.strftime('%Y%m%d-%H%M%S')}"
                _mk_backup(root, n)
                names.append(n)
            plan = plan_rotation(root, keep_last=1, max_age_days=1, now=now)
            self.assertGreaterEqual(len(plan.candidates), 2)
            q = Path(td) / "quarantine"
            out = apply_rotation(
                plan, quarantine_root=q, confirm=CONFIRM_TOKEN, dry_run=False
            )
            self.assertTrue(out.applied, out.errors)
            self.assertEqual(len(out.moved), len(plan.candidates))
            for name in plan.candidates:
                self.assertFalse((root / name).exists())
                self.assertTrue((q / name).is_dir())
            # newest kept
            kept = plan.keep[0]
            self.assertTrue((root / kept).is_dir())

    def test_plan_json_roundtrip_shape(self):
        now = datetime(2026, 7, 20, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _mk_backup(root, "l4compress-20260720-000000")
            plan = plan_rotation(root, now=now)
            raw = plan.to_json()
            data = json.loads(raw)
            self.assertIn("items", data)
            self.assertIn("candidates", data)
            self.assertEqual(data["applied"], False)


class TestSenderHelper(unittest.TestCase):
    def test_find_and_plan_for_sender(self):
        now = datetime(2026, 7, 20, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            data = Path(td)
            sender = "acct-demo"
            b = find_sender_backups(data, sender)
            b.mkdir(parents=True)
            _mk_backup(b, "l4compress-20260719-010203")
            plan = plan_for_sender(data, sender, keep_last=5, now=now)
            self.assertEqual(len(plan.items), 1)
            self.assertEqual(plan.items[0].action, "KEEP")


if __name__ == "__main__":
    unittest.main()
