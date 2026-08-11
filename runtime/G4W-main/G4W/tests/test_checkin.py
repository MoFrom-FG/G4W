import tempfile
import time
import unittest
from pathlib import Path

from G4W.memory.checkin import CheckinService
from G4W.core.storage import EventStore


class CheckinTests(unittest.TestCase):
    def test_random_checkin_emits_and_reschedules(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            service = CheckinService(root / "checkin.json")
            events = EventStore(root / "events.json")
            service.configure("account:sender", "sender", 10, 20)
            state = service.store.read(); state["bindings"]["account:sender"]["nextAt"] = time.time() - 1; service.store.write(state)
            self.assertEqual(service.emit_due(events), 1)
            event = events.next_pending()
            self.assertEqual(event["type"], "system.checkin")
            self.assertEqual(event["bindingKey"], "account:sender")
            self.assertGreater(service.status("account:sender")["nextAt"], time.time())

    def test_pending_system_event_defers_without_consuming_checkin(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            service = CheckinService(root / "checkin.json")
            events = EventStore(root / "events.json")
            service.configure("account:sender", "sender", 10, 20)
            state = service.store.read()
            state["bindings"]["account:sender"]["nextAt"] = time.time() - 1
            service.store.write(state)
            events.enqueue("worker.report", "account:sender", {"status": "busy"})

            before = time.time()
            self.assertEqual(service.emit_due(events), 0)
            after = service.status("account:sender")
            self.assertEqual(after["fireIndex"], 0)
            self.assertNotIn("lastFiredAt", after)
            self.assertGreaterEqual(after["nextAt"], before + 59)
            self.assertLessEqual(after["nextAt"], time.time() + 61)
            pending_types = [item["type"] for item in events.store.read()["events"] if item["status"] == "pending"]
            self.assertEqual(pending_types, ["worker.report"])

    def test_user_round_end_restarts_idle_timer(self):
        with tempfile.TemporaryDirectory() as td:
            service = CheckinService(Path(td) / "checkin.json")
            service.configure("account:sender", "sender", 3, 3)
            ended_at = time.time() + 10
            updated = service.touch_after_round("account:sender", ended_at)
            self.assertEqual(updated["lastConversationRoundEndedAt"], ended_at)
            self.assertAlmostEqual(updated["nextAt"], ended_at + 180, places=3)

    def test_configure_preserves_fire_index(self):
        with tempfile.TemporaryDirectory() as td:
            service = CheckinService(Path(td) / "checkin.json")
            service.configure("account:sender", "sender", 10, 20)
            state = service.store.read()
            state["bindings"]["account:sender"]["fireIndex"] = 418
            state["bindings"]["account:sender"]["lastFiredAt"] = 12345.0
            service.store.write(state)

            again = service.configure("account:sender", "sender", 5, 15, True)
            self.assertEqual(again["fireIndex"], 418)
            self.assertEqual(again["lastFiredAt"], 12345.0)
            self.assertEqual(again["minimumMinutes"], 5)
            self.assertEqual(again["maximumMinutes"], 15)
            self.assertTrue(again["enabled"])

    def test_emit_skips_terminal_dedupe_and_still_enqueues(self):
        """Historical done events for fireIndex 1..N must not empty-spin after reset."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            service = CheckinService(root / "checkin.json")
            events = EventStore(root / "events.json")
            service.configure("account:sender", "sender", 10, 20)

            # Simulate years of done checkins for fi=1..5, then fireIndex wiped to 0.
            for fi in range(1, 6):
                ev = events.enqueue(
                    "system.checkin",
                    "account:sender",
                    {"fireIndex": fi, "mode": "companion"},
                    dedupe_key=f"system.checkin:account:sender:{fi}",
                )
                events.mark(ev["id"], "done")

            state = service.store.read()
            state["bindings"]["account:sender"]["fireIndex"] = 0
            state["bindings"]["account:sender"]["nextAt"] = time.time() - 1
            service.store.write(state)

            self.assertEqual(service.emit_due(events), 1)
            pending = [e for e in events.store.read()["events"] if e["status"] == "pending"]
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["type"], "system.checkin")
            self.assertEqual(pending[0]["payload"]["fireIndex"], 6)
            self.assertEqual(service.status("account:sender")["fireIndex"], 6)


if __name__ == "__main__":
    unittest.main()
