"""Unit tests for G4W.core.tmp_janitor — tempfile fixtures only."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from G4W.core import tmp_janitor as tj


class TmpJanitorTests(unittest.TestCase):
    def _touch(self, path: Path, age_sec: float = 0.0, content: bytes = b"x") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        if age_sec:
            ts = time.time() - age_sec
            os.utime(path, (ts, ts))
        return path

    def test_jsonstore_pattern_and_plan_apply_confirm(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # JsonStore-like sidecar
            name = f".state.json.{os.getpid()}.1.{uuid.uuid4().hex}.tmp"
            orphan = self._touch(root / "accounts" / name, age_sec=120, content=b'{"a":1}')
            # young keep
            young = self._touch(root / "young.tmp", age_sec=1, content=b"y")
            # whitelist edge_headless_capture
            edge = self._touch(
                root / "temp" / "edge_headless_capture" / "shot.tmp",
                age_sec=9999,
                content=b"edge",
            )
            # hybrid/cas skip
            cas = self._touch(root / "hybrid" / "cas" / "obj.tmp", age_sec=9999, content=b"cas")

            report = tj.scan_orphan_tmp(root, min_age_sec=60)
            d = report.to_dict()
            self.assertGreaterEqual(d["tmp_hit_count"], 1)
            # edge blocked
            edge_hits = [h for h in report.hits if "edge_headless_capture" in h.rel.replace("\\", "/")]
            self.assertTrue(edge_hits)
            self.assertTrue(all(h.blocked_by_whitelist for h in edge_hits))
            self.assertTrue(all(not h.would_delete for h in edge_hits))
            # cas pruned/skipped — should not appear as would_delete
            cas_hits = [h for h in report.hits if "hybrid/cas" in h.rel.replace("\\", "/") or h.rel.replace("\\", "/").startswith("hybrid\\cas".replace("\\","/"))]
            # after walk prune, ideally zero
            self.assertEqual(len([h for h in report.hits if "cas" in Path(h.rel).parts]), 0)

            plan = tj.plan_deletes(report)
            rels = {c["rel"].replace("\\", "/") for c in plan.candidates}
            self.assertIn(orphan.relative_to(root).as_posix(), rels)
            # young below min_age
            self.assertNotIn("young.tmp", rels)
            # edge not candidate
            self.assertFalse(any("edge_headless_capture" in r for r in rels))

            refused = tj.apply_deletes(plan, confirm="")
            self.assertFalse(refused.ok)
            self.assertEqual(refused.mode, "REFUSED")
            self.assertTrue(orphan.exists())

            refused2 = tj.apply_deletes(plan, confirm="WRONG")
            self.assertFalse(refused2.ok)

            ok = tj.apply_deletes(plan, confirm=tj.CONFIRM_DELETE_TMP)
            self.assertEqual(ok.mode, "APPLY")
            self.assertFalse(orphan.exists())
            self.assertTrue(edge.exists(), "whitelist path must survive")
            self.assertTrue(cas.exists(), "cas path must survive")
            self.assertTrue(young.exists())

    def test_default_whitelist_contains_edge(self):
        self.assertIn("edge_headless_capture", tj.DEFAULT_WHITELIST)

    def test_name_pattern_tmp(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._touch(root / "foo.partial", age_sec=100)
            self._touch(root / "bar.temp", age_sec=100)
            report = tj.scan_orphan_tmp(root, min_age_sec=10)
            kinds = {h.kind for h in report.hits}
            self.assertIn("name_pattern_tmp", kinds)
            plan = tj.plan_deletes(report)
            self.assertEqual(len(plan.candidates), 2)

    def test_temp_inventory_marks_edge_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = root / "temp" / "edge_headless_capture"
            d.mkdir(parents=True)
            (d / "x.bin").write_bytes(b"1")
            report = tj.scan_orphan_tmp(root)
            names = {row.get("name") for row in report.temp_inventory}
            self.assertIn("edge_headless_capture", names)
            edge_rows = [r for r in report.temp_inventory if r.get("name") == "edge_headless_capture"]
            self.assertEqual(edge_rows[0]["action"], "KEEP_WHITELIST")


if __name__ == "__main__":
    unittest.main()
