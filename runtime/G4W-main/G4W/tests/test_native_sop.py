import json
import tempfile
import unittest
from pathlib import Path

from G4W.core.storage import OutboxStore
from G4W.features.native_sop import execute


class NativeSopTests(unittest.TestCase):
    def context(self, root: Path, turn: int = 2) -> Path:
        state = root / "state"
        workspace = root / "workspace"
        state.mkdir(parents=True)
        workspace.mkdir(parents=True)
        path = root / "G4W-context.json"
        path.write_text(json.dumps({
            "stateDir": str(state),
            "workspaceRoot": str(workspace),
            "sharedMemoryRoot": str(root / "sop"),
            "senderId": "sender",
            "bindingKey": "account:sender",
            "contextToken": "ctx",
            "roundId": "round-1",
            "turn": turn,
        }), encoding="utf-8")
        return path

    def test_file_send_sop_uses_durable_held_outbox(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            context = self.context(root, turn=3)
            target = root / "workspace" / "report.md"
            target.write_text("report", encoding="utf-8")

            result = execute("file-send", "send", {"path": str(target)}, context)

            self.assertTrue(result["queued"])
            item = OutboxStore(root / "state" / "outbox.json").store.read()["messages"][0]
            self.assertEqual(item["bindingKey"], "account:sender")
            self.assertEqual(item["roundId"], "round-1")
            self.assertEqual(item["turn"], 3)
            self.assertEqual(item["status"], "held")

    def test_timeline_sop_writes_and_reads_deterministic_store(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            context = self.context(root)
            event = {
                "title": "编程",
                "startAt": "2026-07-17T10:00:00+08:00",
                "endAt": "2026-07-17T11:00:00+08:00",
                "categoryId": "work",
                "subcategoryId": "work.coding",
            }

            execute("timeline", "write", {"date": "2026-07-17", "events": [event]}, context)
            result = execute("timeline", "read", {"date": "2026-07-17"}, context)

            self.assertEqual(result["date"], "2026-07-17")
            self.assertEqual(result["events"][0]["subcategoryId"], "work.coding")


if __name__ == "__main__":
    unittest.main()
