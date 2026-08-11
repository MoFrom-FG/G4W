import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from G4W.memory import l4_safe
from G4W.core.config import Config
from G4W.memory.conversation import ConversationStore
from G4W.memory.maintenance import L4MaintenanceService
from G4W.core.storage import EventStore
from G4W.memory.wechat_maintenance import WechatMaintenanceService, detect_maintenance_signals


class FakeWorkers:
    def __init__(self):
        self.items = []

    def spawn(self, binding_key, sender_id, capability_id, task, lifecycle, model_tier=""):
        worker_id = f"w{len(self.items) + 1}"
        self.items.append({
            "id": worker_id, "bindingKey": binding_key, "senderId": sender_id,
            "capabilityId": capability_id, "task": task, "lifecycle": lifecycle,
            "modelTier": model_tier,
        })
        return {"id": worker_id, "status": "running"}


def write_candidates(root: Path, sender_id: str, run_id: str):
    output = l4_safe.subagent_output_dir(root, sender_id, run_id)
    output.mkdir(parents=True, exist_ok=True)
    values = {
        "active_knowledge.candidate.json": {"_meta": {}},
        "emotion_events.candidate.json": {"events": []},
        "incremental_markers.candidate.json": {},
    }
    for name in l4_safe.REQUIRED_CANDIDATE_FILES:
        path = output / name
        if name in values:
            path.write_text(json.dumps(values[name], ensure_ascii=False), encoding="utf-8")
        else:
            path.write_text(f"# {name}\n", encoding="utf-8")


class MaintenanceTests(unittest.TestCase):
    def setup_l4(self, root: Path):
        config = Config(state_dir=root / "G4W-data")
        config.ensure_dirs()
        conversations = ConversationStore(config.conversations_dir, config.memory_dir)
        conversations.bind("account", "sender", "ctx")
        events = EventStore(config.state_dir / "events.json")
        workers = FakeWorkers()
        service = L4MaintenanceService(config.state_dir, 30, 2, 0, 0.0)
        service.attach_workers(workers)
        service.attach_events(events)
        return config, conversations, service, workers

    def bootstrap_and_finalize(self, config, conversations, service, workers):
        conversations.append("sender", "User", "基线事实", timestamp="2026-07-13T10:00:00+08:00")
        prepared = service.command("account:sender", "sender", "bootstrap --from 2026-07-13 --to 2026-07-13")
        self.assertTrue(prepared["l4Started"])
        self.assertEqual(workers.items[-1]["modelTier"], "flash")
        missing = service.finalize_worker(prepared["workerId"])
        self.assertEqual(missing["status"], "validation_failed")
        write_candidates(config.state_dir.parent, "sender", prepared["run_id"])
        finalized = service.finalize_worker(prepared["workerId"])
        self.assertEqual(finalized["status"], "finalized")
        self.assertEqual(service.finalize_worker(prepared["workerId"])["status"], "not_l4_worker")
        self.assertEqual(l4_safe.validate_finalize(config.state_dir.parent, "sender", prepared["run_id"])["status"], "already_finalized")
        return prepared

    def test_status_dryrun_missing_marker_and_bootstrap_pipeline(self):
        with tempfile.TemporaryDirectory() as td:
            config, conversations, service, workers = self.setup_l4(Path(td))
            conversations.append("sender", "User", "第一条", timestamp="2026-07-13T09:00:00+08:00")
            status = service.status("sender")
            self.assertFalse(status["marker_exists"])
            dryrun = service.command("account:sender", "sender", "dryrun")
            self.assertEqual(dryrun["status"], "needs_bootstrap")
            prepared = service.command("account:sender", "sender", "bootstrap --from 2026-07-13 --to 2026-07-13")
            self.assertEqual(prepared["status"], "prepared")
            self.assertEqual(prepared["messages"], 1)
            self.assertEqual(workers.items[0]["capabilityId"], "worker.l4")

    def test_finalize_validation_and_idempotency(self):
        with tempfile.TemporaryDirectory() as td:
            config, conversations, service, workers = self.setup_l4(Path(td))
            self.bootstrap_and_finalize(config, conversations, service, workers)
            marker = l4_safe.load_markers(config.state_dir.parent, "sender")
            self.assertEqual(marker["last_processed_timestamp"], "2026-07-13 10:00:00 Asia/Shanghai")
            self.assertTrue((config.conversations_dir / "sender" / "summaries" / "history_insight" / "memory_brief.md").is_file())

    def test_auto_threshold_sample_miss_and_boundary(self):
        with tempfile.TemporaryDirectory() as td:
            config, conversations, service, workers = self.setup_l4(Path(td))
            self.bootstrap_and_finalize(config, conversations, service, workers)
            boundary = l4_safe.load_markers(config.state_dir.parent, "sender")["last_processed_timestamp"]
            for index in range(29):
                conversations.append("sender", "User", f"新增 {index}", timestamp=f"2026-07-14T10:{index:02d}:00+08:00")
            skipped = service.auto_check("account:sender", "sender")
            self.assertIn("new user messages 29", skipped["reason"])
            conversations.append("sender", "User", "新增 29", timestamp="2026-07-14T10:29:30+08:00")
            sampled = service.auto_check("account:sender", "sender")
            self.assertIn("sample miss", sampled["reason"])
            marker = l4_safe.load_markers(config.state_dir.parent, "sender")
            self.assertEqual(marker["last_processed_timestamp"], boundary)
            self.assertTrue(marker["last_poll_at"])

    def test_auto_two_transcript_files_and_cooldown(self):
        with tempfile.TemporaryDirectory() as td:
            config, conversations, service, workers = self.setup_l4(Path(td))
            self.bootstrap_and_finalize(config, conversations, service, workers)
            conversations.append("sender", "User", "第二天", timestamp="2026-07-14T10:00:00+08:00")
            conversations.append("sender", "User", "第三天", timestamp="2026-07-15T10:00:00+08:00")
            service.min_new_user_messages = 99
            service.cooldown_hours = 0
            result = service.auto_check("account:sender", "sender")
            self.assertIn("sample miss", result["reason"])
            service.sample_rate = 1.0
            service.cooldown_hours = 4
            cooldown = service.auto_check("account:sender", "sender")
            self.assertIn("cooldown 4", cooldown["reason"])

    def test_original_timeline_diary_maintenance_gate(self):
        with tempfile.TemporaryDirectory() as td:
            service = WechatMaintenanceService(Path(td) / "maintenance.json", 7200, 21600)
            signals = detect_maintenance_signals("今天写代码写得很累，准备睡了")
            self.assertTrue(signals["timelineDirty"])
            self.assertTrue(signals["diaryDirty"])
            self.assertTrue(signals["forceMaintenance"])
            service.mark_user_message("sender", "今天写代码写得很累，准备睡了", "2026-07-14T22:00:00+08:00")
            checkin = service.build_checkin("sender", "Edmond", "2026-07-14T22:01:00+08:00")
            self.assertEqual(checkin["mode"], "maintenance")
            self.assertTrue(checkin["dueTimeline"])
            self.assertTrue(checkin["dueDiary"])
            service.mark_timeline_written("sender", "2026-07-14T22:02:00+08:00")
            service.mark_diary_written("sender", "2026-07-14T22:03:00+08:00")
            self.assertEqual(service.build_checkin("sender", "Edmond", "2026-07-14T22:04:00+08:00")["mode"], "companion")


if __name__ == "__main__":
    unittest.main()
