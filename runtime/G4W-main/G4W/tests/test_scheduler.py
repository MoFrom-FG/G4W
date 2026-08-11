import tempfile
import time
import unittest
from pathlib import Path

from G4W.core.scheduler import ScheduledStore
from G4W.core.storage import EventStore


class SchedulerTests(unittest.TestCase):
    def test_due_reminder_emits_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            schedules = ScheduledStore(root / "schedules.json")
            events = EventStore(root / "events.json")
            job = schedules.create("a:b", "sender", "喝水", time.time() - 1)
            self.assertEqual(schedules.emit_due(events), 1)
            event = events.next_pending()
            self.assertEqual(event["payload"]["jobId"], job["id"])
            self.assertEqual(schedules.emit_due(events), 0)

    def test_cancelled_reminder_does_not_fire(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            schedules = ScheduledStore(root / "schedules.json")
            events = EventStore(root / "events.json")
            job = schedules.create("a:b", "sender", "喝水", time.time() - 1)
            schedules.cancel("a:b", job["id"])
            self.assertEqual(schedules.emit_due(events), 0)


if __name__ == "__main__":
    unittest.main()
