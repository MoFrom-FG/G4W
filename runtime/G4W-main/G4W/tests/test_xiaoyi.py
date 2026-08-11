import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from G4W.core.storage import EventStore
from G4W.features.xiaoyi import XiaoyiService


class Response:
    def __init__(self, value): self.value = value
    def read(self): return json.dumps(self.value, ensure_ascii=False).encode()
    def __enter__(self): return self
    def __exit__(self, *args): return False


class XiaoyiTests(unittest.TestCase):
    def test_submit_persists_and_completion_wakes_same_binding(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            events = EventStore(root / "events.json")
            service = XiaoyiService(root / "xiaoyi", "http://127.0.0.1:21991", events)
            replies = [Response({"ok": True, "jobId": "xy-1", "status": "pending"}), Response({"ok": True, "status": "completed", "finalText": "日程已创建"})]
            with mock.patch("urllib.request.urlopen", side_effect=replies):
                submitted = service.submit("account:sender", "sender", "创建明天下午三点日程")
                self.assertEqual(submitted["jobId"], "xy-1")
                self.assertEqual(service.poll(interval_seconds=0), 1)
            event = events.next_pending()
            self.assertEqual(event["bindingKey"], "account:sender")
            self.assertEqual(event["type"], "xiaoyi.completed")
            self.assertEqual(event["payload"]["finalText"], "日程已创建")
            self.assertEqual(service.get("account:sender", "xy-1")["status"], "completed")

    def test_pending_job_survives_service_reopen(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            events = EventStore(root / "events.json")
            first = XiaoyiService(root / "xiaoyi", "http://127.0.0.1:21991", events)
            with mock.patch("urllib.request.urlopen", return_value=Response({"ok": True, "jobId": "xy-2"})):
                first.submit("account:sender", "sender", "测试")
            reopened = XiaoyiService(root / "xiaoyi", "http://127.0.0.1:21991", events)
            self.assertEqual(reopened.list_for("account:sender")[0]["jobId"], "xy-2")


if __name__ == "__main__":
    unittest.main()
