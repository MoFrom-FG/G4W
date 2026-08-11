import json
import tempfile
import unittest
from pathlib import Path

from G4W.agents.round_log import ConductorRoundLog, latest_output
from G4W.agents.worker_runner import build_worker_job_context, render_report
from G4W.agents.workers import WorkerManager
from G4W.core.capabilities import CapabilityRegistry
from G4W.core.storage import EventStore
from G4W.memory.sop_catalog import SopCatalog


class NewArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.package = Path(__file__).resolve().parents[1]

    def registry(self, root: Path):
        return CapabilityRegistry.from_sop_root(self.package / "memory" / "sop", root / "capabilities.json")

    def test_sop_catalog_compiles_and_searches(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            compiled = root / "runtime" / "cache" / "capabilities.json"
            catalog = SopCatalog(self.package / "memory" / "sop", compiled)
            document = catalog.compile_capabilities()
            self.assertGreaterEqual(len(document["capabilities"]), 15)
            self.assertTrue(compiled.is_file())
            result = catalog.search("模型 Pro", role="worker")
            self.assertTrue(any(item["relativePath"].endswith("model_routing_sop.md") for item in result["items"]))
            read = catalog.read("worker/model-routing/model_routing_sop.md", role="worker")
            self.assertIn("Flash", read["content"])

    def test_packaging_bucket_has_no_runtime_role_distinction(self):
        catalog = SopCatalog(self.package / "memory" / "sop")

        extension = catalog.read("wechat-media", role="conductor")
        self.assertTrue(extension["ok"])
        self.assertEqual(extension["visibility"], "shared")

        missing = catalog.read("definitely-missing-sop", role="worker")
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["status"], "not_found")
        self.assertIn("candidates", missing)

    def test_ordinary_sop_needs_no_metadata_or_capability(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "sop"
            root.mkdir(parents=True)
            (root / "global_mem_insight.txt").write_text(
                "# Index\n\n- `demo` → `demo_sop.md`（演示、别名） — 普通知识SOP\n",
                encoding="utf-8",
            )
            (root / "demo_sop.md").write_text("# Demo\n\n无需sop.json和capability。\n", encoding="utf-8")
            catalog = SopCatalog(root, Path(td) / "capabilities.json")

            self.assertTrue(catalog.read("demo_sop.md")["ok"])
            self.assertTrue(catalog.read("demo")["ok"])
            self.assertEqual(catalog.compile_capabilities()["capabilities"], [])

    def test_public_sop_export_excludes_private_files_and_private_index_entries(self):
        with tempfile.TemporaryDirectory() as td:
            destination = Path(td) / "public-sop"
            catalog = SopCatalog(self.package / "memory" / "sop")
            result = catalog.export_public(destination)

            self.assertTrue(result["ok"])
            self.assertFalse((destination / "private").exists())
            public_index = (destination / "global_mem_insight.txt").read_text(encoding="utf-8")
            self.assertNotIn("private/", public_index)
            self.assertTrue((destination / "native" / "timeline" / "timeline_sop.md").is_file())
            self.assertTrue((destination / "native" / "dida" / "dida_sop.md").is_file())
            self.assertFalse((destination / "wechat" / "xiaoyi").exists())
            self.assertFalse((destination / "wechat" / "location").exists())
            self.assertFalse((destination / "demo").exists())
            self.assertNotIn("ticktick", public_index.lower())
            self.assertEqual(result["demoExcluded"], True)
            template_root = self.package / "templates" / "memory" / "sop"
            self.assertEqual(
                (destination / "global_mem_insight.txt").read_text(encoding="utf-8"),
                (template_root / "global_mem_insight.txt").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                (destination / "global_mem.txt").read_text(encoding="utf-8"),
                (template_root / "global_mem.txt").read_text(encoding="utf-8"),
            )

    def test_conductor_rounds_are_conversation_scoped(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "conversations"
            log = ConductorRoundLog(root)
            output = log.begin("sender@wechat", "round-abc", {"source": "test"})
            log.finish(output, "Turn 1 ...\nhello")
            self.assertIn("sender_wechat", str(output))
            self.assertEqual(output.parent.name, "round-abc")
            self.assertTrue((output.parent / "metadata.json").is_file())
            self.assertTrue(latest_output(root).samefile(output))

    def test_worker_topic_layout_and_archive(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manager = WorkerManager(
                root / "legacy-workers", self.registry(root), EventStore(root / "events.json"),
                conversations_root=root / "memory" / "conversations",
                state_path=root / "memory" / "worker-registry.json",
                ga_memory_root=root / "ga-worker-memory",
            )
            manager._start = lambda item, task: None
            public = manager.spawn("a:s", "sender", "worker.weather", "查询西安未来七天天气")
            item = manager.get(public["id"])
            self.assertIn("查询西安未来七天天气", item["dir"])
            self.assertIn(str(Path("memory") / "conversations" / "sender" / "workers"), item["dir"])
            worker_dir = Path(item["dir"])
            (worker_dir / "report.md").write_text("report", encoding="utf-8")
            manager.state.update(lambda state: state["workers"][public["id"]].update({
                "status": "completed", "runIndex": 1, "result": {"status": "completed", "summary": "晴"},
                "review": {"runIndex": 1, "state": "pending"},
            }))
            manager.review(public["id"], 1, "accept", "ok")
            archived = manager.get(public["id"])
            self.assertEqual(archived["status"], "archived")
            self.assertTrue(Path(archived["archivePath"]).is_file())
            self.assertFalse(worker_dir.exists())

    def test_worker_markdown_report_contains_conductor_summary(self):
        report = render_report(
            {"topic": "目录分析", "id": "worker-x", "runIndex": 1, "capabilityId": "worker.general", "lifecycle": "ephemeral", "task": "分析目录"},
            {"status": "completed", "summary": "分析完成", "model": "deepseek-v4-pro", "data": {"files": 10}},
            {"turn": 6},
            [{"from": "deepseek-v4-flash", "to": "deepseek-v4-pro", "reason": "复杂分析"}],
        )
        self.assertIn("# Worker任务报告：目录分析", report)
        self.assertIn("分析完成", report)
        self.assertIn("deepseek-v4-flash → deepseek-v4-pro", report)

    def test_worker_system_receives_shared_l1_sop_index(self):
        catalog = SopCatalog(self.package / "memory" / "sop")
        context = build_worker_job_context(
            {
                "id": "worker-test",
                "capabilityId": "worker.general",
                "task": "读取时间线SOP并完成任务",
                "gaMemoryRoot": "",
            },
            "WORKER CONTRACT",
            catalog,
        )
        self.assertIn("[Memory A] GA Native Memory", context)
        self.assertIn("[Memory B] G4W Shared SOP Index", context)
        self.assertIn(str(catalog.index_path), context)
        self.assertIn("native/timeline/timeline_sop.md", context)
        self.assertIn("worker/model-routing/model_routing_sop.md", context)
        self.assertLess(context.index("[Memory B] G4W Shared SOP Index"), context.index("G4W WORKER JOB"))

        timeline = catalog.read("native/timeline/timeline_sop.md", role="worker")
        self.assertTrue(timeline["ok"])
        routing = catalog.read("worker/model-routing/model_routing_sop.md", role="worker")
        self.assertIn("agent.next_llm", routing["content"])
        self.assertIn("G4W_worker_switch_model", routing["content"])

    def test_flat_persistent_worker_runs_are_migrated(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            conversations = root / "memory" / "conversations"
            worker_dir = conversations / "sender" / "workers" / "2026" / "07" / "L4语义记忆整理--abc"
            worker_dir.mkdir(parents=True)
            (worker_dir / "history.json").write_text("[]", encoding="utf-8")
            (worker_dir / "job-1.json").write_text(json.dumps({
                "id": "worker-abc", "runIndex": 1, "task": "L4语义记忆整理",
                "capabilityId": "worker.l4", "lifecycle": "persistent", "topic": "L4语义记忆整理",
            }, ensure_ascii=False), encoding="utf-8")
            (worker_dir / "result-1.json").write_text(json.dumps({
                "status": "completed", "summary": "历史语义整理完成", "model": "deepseek-v4-flash",
            }, ensure_ascii=False), encoding="utf-8")
            (worker_dir / "progress.json").write_text(json.dumps({"summary": "完成", "turn": 8}), encoding="utf-8")
            raw = worker_dir / "runtime" / "model_responses"
            raw.mkdir(parents=True)
            (raw / "old.txt").write_text("Turn 1 ...", encoding="utf-8")
            state_path = root / "memory" / "worker-registry.json"
            state_path.write_text(json.dumps({"workers": {"worker-abc": {
                "id": "worker-abc", "senderId": "sender", "bindingKey": "a:sender",
                "capabilityId": "worker.l4", "lifecycle": "persistent", "status": "sleeping",
                "task": "L4语义记忆整理", "topic": "L4语义记忆整理", "runIndex": 1,
                "dir": str(worker_dir), "result": {"status": "completed", "summary": "历史语义整理完成"},
            }}}, ensure_ascii=False), encoding="utf-8")

            manager = WorkerManager(
                root / "legacy-workers", self.registry(root), EventStore(root / "events.json"),
                conversations_root=conversations, state_path=state_path,
            )
            item = manager.get("worker-abc")
            run = Path(item["currentRunDir"])
            self.assertTrue((run / "job.json").is_file())
            self.assertTrue((run / "result.json").is_file())
            self.assertTrue((run / "progress.json").is_file())
            self.assertTrue((run / "report.md").is_file())
            self.assertTrue((run / "model-responses" / "model-responses.txt").is_file())
            self.assertFalse((worker_dir / "job-1.json").exists())
            self.assertTrue((worker_dir / "history.json").is_file())

    def test_model_switch_event_is_forwarded_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            events = EventStore(root / "events.json")
            manager = WorkerManager(root / "workers", self.registry(root), events)
            worker = root / "workers" / "worker-x"
            run = worker / "runs" / "2026" / "07" / "run-0001"
            run.mkdir(parents=True)
            (run / "model-events.jsonl").write_text(json.dumps({"from": "flash", "to": "pro", "reason": "complex"}) + "\n", encoding="utf-8")
            manager.state.update(lambda state: state["workers"].update({"worker-x": {
                "id": "worker-x", "bindingKey": "a:s", "senderId": "s", "status": "running",
                "runIndex": 1, "dir": str(worker), "currentRunDir": str(run), "modelEventLines": 0,
            }}))
            self.assertEqual(len(manager.scan_model_switches()), 1)
            self.assertEqual(manager.scan_model_switches(), [])
            pending = [item for item in events.store.read()["events"] if item["type"] == "worker.model_switched"]
            self.assertEqual(len(pending), 1)


if __name__ == "__main__":
    unittest.main()
