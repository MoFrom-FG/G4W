import json
import tempfile
import unittest
from pathlib import Path

from G4W.core.capabilities import CapabilityRegistry
from G4W.core.storage import EventStore
from G4W.agents.worker_runner import extract_result
from G4W.agents.workers import WorkerManager


def test_registry(root: Path):
    package = Path(__file__).resolve().parents[1]
    return CapabilityRegistry.from_sop_root(package / "memory" / "sop", root / "compiled-capabilities.json")


class WorkerContractTests(unittest.TestCase):
    def test_structured_result_is_parsed(self):
        result = extract_result('<worker_result>{"status":"completed","summary":"ok"}</worker_result>')
        self.assertEqual(result, {"status": "completed", "summary": "ok"})

    def test_unstructured_output_is_wrapped_not_forwarded(self):
        result = extract_result("查询结果是晴天")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["summary"], "查询结果是晴天")

    def test_input_request_wins(self):
        result = extract_result("ignored", "需要城市")
        self.assertEqual(result["status"], "needs_input")
        self.assertEqual(result["question"], "需要城市")

    def test_worker_contract_forbids_user_identity(self):
        contract = (Path(__file__).resolve().parents[1] / "templates" / "agents" / "worker-contract.md").read_text(encoding="utf-8")
        self.assertIn("不得自称 G4W", contract)
        self.assertIn("不得直接向最终用户说话", contract)

    def test_running_worker_accepts_intervention_without_new_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manager = WorkerManager(root / "workers", test_registry(root), EventStore(root / "events.json"))
            worker_dir = root / "workers" / "worker-test"
            worker_dir.mkdir(parents=True)
            item = {"id": "worker-test", "bindingKey": "a:b", "senderId": "sender", "capabilityId": "worker.research", "lifecycle": "ephemeral", "status": "running", "runIndex": 1, "dir": str(worker_dir), "updatedAt": 0}
            manager.state.update(lambda state: state.setdefault("workers", {}).update({"worker-test": item}))
            result = manager.send("worker-test", "优先核实来源")
            self.assertEqual(result["control"], "intervene_injected")
            self.assertEqual(manager.get("worker-test")["runIndex"], 1)
            self.assertIn("优先核实来源", (worker_dir / "_intervene").read_text(encoding="utf-8"))

    def test_ephemeral_worker_cannot_rerun_before_review(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manager = WorkerManager(root / "workers", test_registry(root), EventStore(root / "events.json"))
            worker_dir = root / "workers" / "worker-test"
            worker_dir.mkdir(parents=True)
            item = {"id": "worker-test", "bindingKey": "a:b", "senderId": "sender", "capabilityId": "worker.research", "lifecycle": "ephemeral", "status": "completed", "runIndex": 1, "dir": str(worker_dir), "review": {"runIndex": 1, "state": "pending"}, "updatedAt": 0}
            manager.state.update(lambda state: state.setdefault("workers", {}).update({"worker-test": item}))
            with self.assertRaises(RuntimeError):
                manager.send("worker-test", "再查一次")


if __name__ == "__main__":
    unittest.main()
