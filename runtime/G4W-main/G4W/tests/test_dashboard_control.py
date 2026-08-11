import tempfile
import unittest
from pathlib import Path

from G4W.core.dashboard_control import DashboardControlMailbox
from G4W.core.service import G4WService


class _Backend:
    def __init__(self, model, name):
        self.model = model
        self.name = name


class _Client:
    def __init__(self, model, name):
        self.backend = _Backend(model, name)


class _Agent:
    def __init__(self):
        self.llmclients = [_Client("flash-model", "flash"), _Client("pro-model", "pro")]

    def list_llms(self):
        return [(0, "Native/flash", True), (1, "Native/pro", False)]


class _Session:
    agent = _Agent()


class _Controller:
    def session(self, sender_id):
        return _Session()

    def set_model(self, sender_id, query):
        return {"modelNo": int(query), "model": "pro-model", "name": "pro"}


class _Workers:
    default_model = "flash-model"
    pro_model = "pro-model"


class DashboardControlTests(unittest.TestCase):
    def test_mailbox_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            mailbox = DashboardControlMailbox(Path(directory))
            request_id = mailbox.submit("list_models", {"senderId": "sender"})
            request = mailbox.pending()[0]
            mailbox.complete(request, {"ok": True, "models": []})
            result = mailbox.wait(request_id, timeout=0.2)
            self.assertTrue(result["ok"])
            self.assertEqual(mailbox.pending(), [])

    def test_service_processes_model_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            service = G4WService.__new__(G4WService)
            service.dashboard_control = DashboardControlMailbox(Path(directory))
            service.controller = _Controller()
            service.workers = _Workers()
            request_id = service.dashboard_control.submit("set_model", {"senderId": "sender", "query": "1"})
            self.assertEqual(service.process_dashboard_requests(), 1)
            result = service.dashboard_control.wait(request_id, timeout=0.2)
            self.assertTrue(result["ok"])
            self.assertEqual(result["selected"]["modelNo"], 1)
            self.assertEqual(len(result["models"]), 2)


if __name__ == "__main__":
    unittest.main()
