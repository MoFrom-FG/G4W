import json
import tempfile
import unittest
from pathlib import Path

from G4W.core.config import Config
from G4W.core.records import DiaryStore, TimelineStore
from G4W.features.timeline_publish import TimelinePublisher
from G4W.core.service import G4WService


class RecordStoreTests(unittest.TestCase):
    def test_diary_reads_legacy_and_appends_active_entry(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            legacy = root / "legacy"
            legacy.mkdir()
            (legacy / "2026-07-14.md").write_text("## 08:00 旧记录\n\n早起。", encoding="utf-8")
            store = DiaryStore(root / "diary", legacy)
            store.append("完成了迁移。", title="开发", date="2026-07-14", at_time="20:30")
            content = store.read("2026-07-14")["content"]
            self.assertIn("旧记录", content)
            self.assertIn("## 20:30 开发", content)

    def test_timeline_copies_legacy_day_on_first_write(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            legacy = root / "legacy.json"
            legacy.write_text(json.dumps({"facts": {"2026-07-14": {"status": "draft", "events": [{"id": "old", "startAt": "2026-07-14T08:00:00+08:00", "endAt": "2026-07-14T09:00:00+08:00", "title": "旧事件"}]}}}, ensure_ascii=False), encoding="utf-8")
            store = TimelineStore(root / "timeline.json", legacy)
            result = store.write("2026-07-14", [{"id": "new", "title": "新事件", "startAt": "2026-07-14T10:00:00+08:00", "endAt": "2026-07-14T11:00:00+08:00"}])
            self.assertEqual(result["eventCount"], 2)
            self.assertEqual([item["id"] for item in store.read("2026-07-14")["events"]], ["old", "new"])

    def test_timeline_rejects_cross_day_event(self):
        with tempfile.TemporaryDirectory() as td:
            store = TimelineStore(Path(td) / "timeline.json")
            with self.assertRaises(ValueError):
                store.write("2026-07-14", [{"title": "跨日", "startAt": "2026-07-14T23:00:00+08:00", "endAt": "2026-07-15T01:00:00+08:00"}])

    def test_timeline_infers_and_repairs_category_colors(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "timeline.json"
            store = TimelineStore(path)
            written = store.write("2026-07-14", [{
                "title": "重构测试与bug修复",
                "startAt": "2026-07-14T20:00:00+08:00",
                "endAt": "2026-07-14T21:00:00+08:00",
            }])
            self.assertEqual(written["events"][0]["categoryId"], "work")
            self.assertEqual(written["events"][0]["subcategoryId"], "work.coding")

            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["facts"]["2026-07-14"]["events"].append({
                "id": "legacy-sleep", "title": "睡眠", "note": "", "categoryId": "life", "subcategoryId": "",
                "startAt": "2026-07-14T01:00:00+08:00", "endAt": "2026-07-14T08:00:00+08:00",
            })
            path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            repaired = store.repair_categories()
            self.assertEqual(repaired["repaired"], 1)
            events = {item["id"]: item for item in store.read("2026-07-14")["events"]}
            self.assertEqual(events["legacy-sleep"]["categoryId"], "rest")
            self.assertEqual(events["legacy-sleep"]["subcategoryId"], "rest.sleep")

    def test_service_exposes_records_only_as_direct_capabilities(self):
        with tempfile.TemporaryDirectory() as td:
            service = G4WService(Config(state_dir=Path(td)), channel=object(), session_factory=lambda *_: None)
            service.conversations.bind("account", "sender", "ctx")
            diary = service.controller.execute_direct("sender", "diary.manage", {"action": "append", "date": "2026-07-14", "time": "20:00", "content": "测试"})
            timeline = service.controller.execute_direct("sender", "timeline.manage", {"action": "write", "date": "2026-07-14", "events": [{"title": "测试", "startAt": "2026-07-14T20:00:00+08:00", "endAt": "2026-07-14T20:30:00+08:00"}]})
            self.assertTrue(diary["ok"])
            self.assertTrue(Path(diary["filePath"]).is_file())
            self.assertEqual(
                Path(diary["filePath"]).relative_to(service.config.conversations_dir).as_posix(),
                "sender/summaries/diary/2026/07/2026-07-14.md",
            )
            self.assertTrue(timeline["ok"])
            self.assertEqual(service.capabilities.get("diary.manage")["route"], "direct")
            self.assertEqual(service.capabilities.get("timeline.manage")["route"], "direct")

    def test_timeline_static_site_builds_without_node(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = TimelineStore(root / "timeline-facts.json")
            store.write("2026-07-14", [{"title": "完成迁移", "startAt": "2026-07-14T20:00:00+08:00", "endAt": "2026-07-14T21:00:00+08:00", "note": "纯Python"}])
            result = TimelinePublisher(store, root).build()
            page = Path(result["indexFile"])
            self.assertTrue(page.is_file())
            self.assertIn("assets/dashboard.js", page.read_text(encoding="utf-8"))
            data = json.loads(Path(result["dataFile"]).read_text(encoding="utf-8"))
            self.assertIn("完成迁移", json.dumps(data, ensure_ascii=False))
            self.assertTrue((root / "site" / "assets" / "dashboard.js").is_file())
            self.assertEqual(set(data), {"meta", "taxonomy", "timelines", "ranges"})

    def test_timeline_default_and_neko_themes_use_distinct_bundled_assets(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = TimelineStore(root / "timeline-facts.json")
            default_result = TimelinePublisher(store, root, locale="zh-CN", theme="default").build()
            default_hash = default_result["assets"]["sha256"]
            neko_result = TimelinePublisher(store, root, locale="zh-CN", theme="neko").build()
            self.assertEqual(default_result["theme"], "default")
            self.assertEqual(neko_result["theme"], "neko")
            self.assertNotEqual(default_hash, neko_result["assets"]["sha256"])
            data = json.loads(Path(neko_result["dataFile"]).read_text(encoding="utf-8"))
            self.assertEqual(data["meta"]["locale"], "zh-CN")


if __name__ == "__main__":
    unittest.main()
