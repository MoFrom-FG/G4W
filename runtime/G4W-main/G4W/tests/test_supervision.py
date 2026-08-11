import tempfile
import time
import unittest
from pathlib import Path

from G4W.core.storage import EventStore
from G4W.features.supervision import DidaCli, SelfControlService


class FakeDida:
    def __init__(self): self.completed = []
    def available(self): return True
    def list_open_tasks(self, limit=8): return [{"id": "t1", "title": "完成迁移", "projectId": "p1", "priority": 5, "status": 0, "dueDate": ""}]
    def complete_task(self, task): self.completed.append(task["id"])
    def create_focus(self, task, started_at, ended_at, minutes): self.completed.append((task["id"], minutes))


class SupervisionTests(unittest.TestCase):
    def test_blank_dida_placeholder_does_not_enable_an_implicit_cli(self):
        cli = DidaCli("")
        self.assertFalse(cli.available())
        with self.assertRaisesRegex(RuntimeError, "not configured"):
            cli.run(["project", "list"])

    def test_ctdp_reservation_and_execution_chains(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            service = SelfControlService(root / "self-control.json", EventStore(root / "events.json"), FakeDida(), default_focus_minutes=1)
            opened = service.act("account:sender", "sender", "open", {})
            self.assertEqual(opened["session"]["state"], "awaiting_selection")
            service.act("account:sender", "sender", "select", {"task_id": "t1"})
            started = service.act("account:sender", "sender", "start", {"focus_minutes": 1})
            self.assertEqual(started["chains"]["reservation"]["count"], 1)
            completed = service.act("account:sender", "sender", "complete", {})
            self.assertEqual(completed["session"]["state"], "completed")
            self.assertEqual(completed["chains"]["execution"]["count"], 1)
            self.assertTrue(completed["session"]["didaSynced"])

    def test_due_focus_wakes_same_conversation_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            events = EventStore(root / "events.json")
            service = SelfControlService(root / "self-control.json", events, FakeDida(), default_focus_minutes=1)
            service.act("account:sender", "sender", "open", {"tasks": [{"id": "t1", "title": "任务"}]})
            service.act("account:sender", "sender", "select", {"task_id": "t1"})
            service.act("account:sender", "sender", "start", {"focus_minutes": 1})
            state = service.store.read(); state["sessions"]["sender"]["focusEndsAt"] = time.time() - 1; service.store.write(state)
            self.assertEqual(service.process_due(), 1)
            self.assertEqual(service.process_due(), 0)
            event = events.next_pending()
            self.assertEqual(event["type"], "supervision.due")
            self.assertEqual(event["bindingKey"], "account:sender")

    def test_interrupt_resets_execution_chain(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            service = SelfControlService(root / "self-control.json", EventStore(root / "events.json"), FakeDida())
            service.act("account:sender", "sender", "open", {"tasks": [{"id": "t1", "title": "任务"}]})
            service.act("account:sender", "sender", "select", {"task_id": "t1"})
            service.act("account:sender", "sender", "start", {})
            result = service.act("account:sender", "sender", "interrupt", {"reason": "临时来电"})
            self.assertEqual(result["session"]["state"], "interrupted")
            self.assertEqual(result["chains"]["execution"]["failures"], 1)


if __name__ == "__main__":
    unittest.main()
