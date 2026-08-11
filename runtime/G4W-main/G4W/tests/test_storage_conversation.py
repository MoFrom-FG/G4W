import tempfile
import time
import unittest
import json
from pathlib import Path
from unittest import mock

from G4W.memory.conversation import ConversationStore
from G4W.core.storage import DeferredReplyStore, EventStore, OutboxStore


class StorageConversationTests(unittest.TestCase):
    def test_recent_context_keeps_all_visible_bot_messages_inside_user_round(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = ConversationStore(root / "conversations", root / "memory", recent_pairs=20)
            store.append("sender", "User", "第一条用户消息", "2026-07-16T04:00:00Z")
            store.append("sender", "Assistant", "第一条正常回复", "2026-07-16T04:00:01Z")
            store.append("sender", "Assistant", "主动check-in", "2026-07-16T04:05:00Z", subtype="checkin")
            store.append("sender", "Assistant", "Worker进度反馈", "2026-07-16T04:06:00Z", subtype="worker-progress")
            store.append("sender", "User", "第二条用户消息", "2026-07-16T04:10:00Z")
            store.append("sender", "Assistant", "第二条正常回复", "2026-07-16T04:10:01Z")

            recent = store.recent("sender")

            self.assertEqual(recent.count("[user]"), 2)
            self.assertEqual(recent.count("[assistant"), 4)
            self.assertIn("[12:05:00][assistant/checkin]", recent)
            self.assertIn("[12:06:00][assistant/worker-progress]", recent)
            self.assertLess(recent.index("主动check-in"), recent.index("第二条用户消息"))
            self.assertIn("Worker进度反馈", recent)

    def test_unanswered_worker_progress_collapses_but_replied_progress_is_kept(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = ConversationStore(root / "conversations", root / "memory", recent_pairs=20)
            store.append("sender", "Assistant", "进度1", "2026-07-16T04:00:00Z", subtype="worker-progress")
            store.append("sender", "Assistant", "进度2", "2026-07-16T04:01:00Z", subtype="worker-progress")
            store.append("sender", "User", "我知道了，继续", "2026-07-16T04:02:00Z")
            store.append("sender", "Assistant", "普通回复", "2026-07-16T04:02:01Z")
            store.append("sender", "Assistant", "进度3", "2026-07-16T04:03:00Z", subtype="worker-progress")
            store.append("sender", "Assistant", "最终完成", "2026-07-16T04:04:00Z", subtype="worker-final")

            recent = store.recent("sender")

            self.assertNotIn("进度1", recent)
            self.assertIn("进度2", recent)
            self.assertIn("我知道了，继续", recent)
            self.assertNotIn("进度3", recent)
            self.assertIn("[12:04:00][assistant/worker-final]", recent)
            self.assertIn("最终完成", recent)

    def test_event_store_survives_reopen(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "events.json"
            first = EventStore(path)
            event = first.enqueue("worker.completed", "a:b", {"workerId": "w1"})
            second = EventStore(path)
            self.assertEqual(second.next_pending()["id"], event["id"])
            second.mark(event["id"], "done")
            self.assertIsNone(EventStore(path).next_pending())

    def test_event_store_deduplicates_worker_run(self):
        with tempfile.TemporaryDirectory() as td:
            store = EventStore(Path(td) / "events.json")
            first = store.enqueue("worker.completed", "a:b", {"workerId": "w1"}, dedupe_key="worker.completed:w1:2")
            second = store.enqueue("worker.completed", "a:b", {"workerId": "w1"}, dedupe_key="worker.completed:w1:2")
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(len(store.store.read()["events"]), 1)

    def test_user_activity_cancels_only_pending_checkin_for_binding(self):
        with tempfile.TemporaryDirectory() as td:
            store = EventStore(Path(td) / "events.json")
            checkin = store.enqueue("system.checkin", "a:b", {})
            worker = store.enqueue("worker.completed", "a:b", {})
            other = store.enqueue("system.checkin", "x:y", {})
            self.assertEqual(store.cancel_pending("a:b", "system.checkin", "user active"), 1)
            state = {item["id"]: item for item in store.store.read()["events"]}
            self.assertEqual(state[checkin["id"]]["status"], "cancelled")
            self.assertEqual(state[worker["id"]]["status"], "pending")
            self.assertEqual(state[other["id"]]["status"], "pending")

    def test_outbox_is_durable_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "outbox.json"
            first_store = OutboxStore(path)
            first = first_store.prepare("a:b", "sender", "ctx", "hello", "reply:e1")
            second = first_store.prepare("a:b", "sender", "ctx", "hello", "reply:e1")
            self.assertEqual(first["id"], second["id"])
            reopened = OutboxStore(path)
            self.assertEqual(reopened.next_pending()["id"], first["id"])
            reopened.mark_sent(first["id"])
            self.assertIsNone(reopened.next_pending())

    def test_idle_claim_does_not_rewrite_outbox_file(self):
        with tempfile.TemporaryDirectory() as td:
            store = OutboxStore(Path(td) / "outbox.json")
            message = store.prepare("a:b", "sender", "ctx", "hello", "reply:e1")
            store.mark_sent(message["id"])
            with mock.patch.object(store.store, "write", wraps=store.store.write) as write:
                self.assertIsNone(store.claim_next_pending())
                write.assert_not_called()

    def test_json_store_retries_transient_windows_replace_error(self):
        with tempfile.TemporaryDirectory() as td:
            store = OutboxStore(Path(td) / "outbox.json")
            real_replace = __import__("os").replace
            attempts = {"count": 0}
            def flaky_replace(source, target):
                attempts["count"] += 1
                if attempts["count"] == 1:
                    raise PermissionError("temporary scanner lock")
                return real_replace(source, target)
            with mock.patch("G4W.core.storage.os.replace", side_effect=flaky_replace):
                store.prepare("a:b", "sender", "ctx", "hello", "reply:e1")
            self.assertEqual(attempts["count"], 2)
            self.assertEqual(store.store.read()["messages"][0]["text"], "hello")

    def test_deferred_reply_survives_until_next_context(self):
        with tempfile.TemporaryDirectory() as td:
            store = DeferredReplyStore(Path(td) / "deferred.json")
            store.add("a:b", "sender", "tail", kind="proactive_report")
            self.assertEqual(store.count("sender"), 1)
            item = store.pop_all("sender")[0]
            self.assertEqual(item["text"], "tail")
            self.assertEqual(item["kind"], "proactive_report")
            self.assertEqual(store.count("sender"), 0)

    def test_outbox_retry_blocks_later_messages_for_same_binding(self):
        with tempfile.TemporaryDirectory() as td:
            store = OutboxStore(Path(td) / "outbox.json")
            first = store.prepare("a:b", "sender", "ctx", "intermediate", "m1", round_final=False)
            second = store.prepare("a:b", "sender", "ctx", "final", "m2", round_final=True)
            store.mark_retry(first["id"], "temporary")
            self.assertIsNone(store.next_pending())
            def make_due(state):
                for item in state["messages"]:
                    if item["id"] == first["id"]:
                        item["nextAttemptAt"] = time.time() - 1
            store.store.update(make_due)
            self.assertEqual(store.next_pending()["id"], first["id"])
            store.mark_sent(first["id"])
            self.assertEqual(store.next_pending()["id"], second["id"])

    def test_held_file_is_released_after_turn_text_before_final(self):
        with tempfile.TemporaryDirectory() as td:
            store = OutboxStore(Path(td) / "outbox.json")
            held = store.prepare_file(
                "a:b", "sender", "ctx", "timeline.png", "file-1",
                round_id="round-1", turn=1, round_final=False,
                source="conductor-tool", held=True,
            )
            intermediate = store.prepare(
                "a:b", "sender", "ctx", "Turn 1", "intermediate-1",
                round_id="round-1", turn=1, round_final=False,
                source="conductor-intermediate",
            )
            self.assertIsNone(store.next_pending())
            self.assertEqual(store.release_held_files("round-1", through_turn=1), 1)
            final = store.prepare(
                "a:b", "sender", "ctx", "Turn 2", "final-1",
                round_id="round-1", turn=2, round_final=True,
            )

            self.assertEqual(store.next_pending()["id"], intermediate["id"])
            store.mark_sent(intermediate["id"])
            self.assertEqual(store.next_pending()["id"], held["id"])
            store.mark_sent(held["id"])
            self.assertEqual(store.next_pending()["id"], final["id"])

    def test_sending_file_blocks_same_binding_but_not_other_binding(self):
        with tempfile.TemporaryDirectory() as td:
            store = OutboxStore(Path(td) / "outbox.json")
            first = store.prepare_file("a:b", "sender", "ctx", "timeline.png", "file-1")
            store.prepare("a:b", "sender", "ctx", "same binding final", "final-a")
            other = store.prepare("x:y", "other", "ctx2", "other binding", "final-b")

            claimed = store.claim_next_pending()

            self.assertEqual(claimed["id"], first["id"])
            self.assertEqual(store.next_pending()["id"], other["id"])
            store.mark_sent(first["id"])

    def test_delivery_audit_keeps_retry_error_and_latency_fields(self):
        with tempfile.TemporaryDirectory() as td:
            store = OutboxStore(Path(td) / "outbox.json")
            message = store.prepare("a:b", "sender", "ctx", "hello", "m1")
            claimed = store.claim_next_pending()
            self.assertEqual(claimed["id"], message["id"])
            store.mark_retry(message["id"], "upload timeout")
            def make_due(state):
                state["messages"][0]["nextAttemptAt"] = time.time() - 1
            store.store.update(make_due)
            store.claim_next_pending()
            store.mark_sent(message["id"], {"ok": True})

            saved = store.store.read()["messages"][0]
            self.assertEqual(saved["status"], "sent")
            self.assertEqual(saved["lastError"], "upload timeout")
            self.assertGreaterEqual(saved["queueDelayMs"], 0)
            self.assertGreaterEqual(saved["attemptDurationMs"], 0)
            self.assertGreaterEqual(saved["totalDeliveryMs"], 0)

    def test_new_context_defers_pending_text_and_unblocks_new_final(self):
        with tempfile.TemporaryDirectory() as td:
            store = OutboxStore(Path(td) / "outbox.json")
            first = store.prepare("a:b", "sender", "old-ctx", "old intermediate", "m1", round_final=False)
            second = store.prepare("a:b", "sender", "old-ctx", "old final", "m2", round_final=True)
            store.mark_retry(first["id"], "expired context token")

            deferred = store.defer_pending_for_binding("a:b")
            self.assertEqual([item["id"] for item in deferred], [first["id"], second["id"]])
            self.assertIsNone(store.next_pending())

            fresh = store.prepare("a:b", "sender", "new-ctx", "new final", "m3", round_final=True)
            self.assertEqual(store.next_pending()["id"], fresh["id"])

    def test_new_context_does_not_convert_pending_files_to_text_deferred(self):
        with tempfile.TemporaryDirectory() as td:
            store = OutboxStore(Path(td) / "outbox.json")
            pending_file = store.prepare_file("a:b", "sender", "old-ctx", "report.txt", "file-1")

            self.assertEqual(store.defer_pending_for_binding("a:b"), [])
            self.assertEqual(store.next_pending()["id"], pending_file["id"])

    def test_recent_transcript_is_limited_to_pairs(self):
        with tempfile.TemporaryDirectory() as td:
            store = ConversationStore(Path(td) / "conversations", Path(td) / "memory", recent_pairs=2)
            for index in range(4):
                store.append("sender", "User", f"u{index}")
                store.append("sender", "Assistant", f"a{index}")
            recent = store.recent("sender")
            self.assertNotIn("u0", recent)
            self.assertNotIn("u1", recent)
            self.assertIn("u2", recent)
            self.assertIn("a3", recent)

    def test_long_assistant_reply_is_archived_and_recent_uses_path(self):
        with tempfile.TemporaryDirectory() as td:
            store = ConversationStore(
                Path(td) / "conversations", Path(td) / "memory",
                long_assistant_reply_chars=10,
            )
            store.append("sender", "User", "纯用户消息")
            store.append("sender", "Assistant", "这是一条明显超过十个字符的完整助手回复")
            files = list((Path(td) / "conversations" / "sender" / "assistant-replies").rglob("*.md"))
            self.assertEqual(len(files), 1)
            self.assertIn("完整助手回复", files[0].read_text(encoding="utf-8"))
            recent = store.recent("sender")
            self.assertIn("[user]", recent)
            self.assertIn("[assistant]", recent)
            self.assertIn(str(files[0]), recent)
            self.assertNotIn("这是一条明显超过十个字符的完整助手回复", recent)

    def test_user_prompt_archive_contains_only_original_user_text(self):
        with tempfile.TemporaryDirectory() as td:
            store = ConversationStore(Path(td) / "conversations", Path(td) / "memory", long_user_prompt_chars=5)
            path = store.save_user_prompt("sender", "用户纯净原文", force=True)
            self.assertEqual(path.read_text(encoding="utf-8"), "用户纯净原文\n")
            self.assertNotIn("Assistant", path.read_text(encoding="utf-8"))

    def test_memory_is_separate_from_transcript(self):
        with tempfile.TemporaryDirectory() as td:
            store = ConversationStore(Path(td) / "conversations", Path(td) / "memory")
            store.remember("sender", "user", "喜欢简短回复")
            self.assertIn("喜欢简短回复", store.read_memory("sender"))
            self.assertFalse(store.transcript_path("sender").exists())

    def test_clean_history_grows_twenty_to_forty_then_compacts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = ConversationStore(root / "conversations", root / "memory")
            for index in range(25):
                store.append("sender", "User", f"u{index}", f"2026-07-16T{index % 24:02d}:00:00+08:00")
                store.append("sender", "Assistant", f"a{index}", f"2026-07-16T{index % 24:02d}:00:01+08:00")

            initial = store.sync_clean_history("sender")
            self.assertEqual(initial["userRounds"], 20)
            self.assertEqual(initial["messageCount"], 40)
            initial_prefix = initial["messages"]

            for index in range(25, 45):
                store.append("sender", "User", f"u{index}")
                store.append("sender", "Assistant", f"a{index}")
                grown = store.sync_clean_history("sender")
            self.assertEqual(grown["userRounds"], 40)
            self.assertEqual(grown["messages"][:len(initial_prefix)], initial_prefix)

            store.append("sender", "User", "u45")
            store.append("sender", "Assistant", "a45")
            compacted = store.sync_clean_history("sender")
            rendered = json.dumps(compacted["messages"], ensure_ascii=False)
            self.assertTrue(compacted["compacted"])
            self.assertEqual(compacted["userRounds"], 20)
            self.assertNotIn("u25", rendered)
            self.assertIn("u45", rendered)

    def test_clean_history_keeps_visible_events_but_not_unanswered_progress(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = ConversationStore(root / "conversations", root / "memory")
            store.append("sender", "User", "你好")
            store.append("sender", "Assistant", "正常回复")
            store.append("sender", "Assistant", "主动问候", subtype="checkin")
            store.append("sender", "Assistant", "进度一", subtype="worker-progress")
            first = json.dumps(store.sync_clean_history("sender")["messages"], ensure_ascii=False)
            self.assertIn("主动问候", first)
            self.assertNotIn("进度一", first)

            store.append("sender", "User", "知道了，继续")
            replied = json.dumps(store.sync_clean_history("sender", exclude_open_user=True)["messages"], ensure_ascii=False)
            self.assertIn("进度一", replied)
            self.assertNotIn("知道了，继续", replied)
            store.append("sender", "Assistant", "最终完成", subtype="worker-final")
            final = json.dumps(store.sync_clean_history("sender")["messages"], ensure_ascii=False)
            self.assertIn("最终完成", final)

    def test_clean_history_archives_long_content_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = ConversationStore(
                root / "conversations", root / "memory",
                long_assistant_reply_chars=10, long_user_prompt_chars=10,
            )
            long_user = "用户长消息" * 10
            long_assistant = "助手长回复" * 10
            store.save_user_prompt("sender", long_user, force=True)
            store.append("sender", "User", long_user)
            store.append("sender", "Assistant", long_assistant)
            first = store.sync_clean_history("sender")
            rendered = json.dumps(first["messages"], ensure_ascii=False)
            self.assertIn("长消息原文", rendered)
            self.assertIn("assistant/history-archive", rendered)
            self.assertIn("internal: 这是此前微信可见长回复的历史压缩引用", rendered)
            self.assertNotIn(long_user, rendered)
            self.assertIn("preview:", rendered)

            reopened = ConversationStore(
                root / "conversations", root / "memory",
                long_assistant_reply_chars=10, long_user_prompt_chars=10,
            )
            second = reopened.sync_clean_history("sender")
            self.assertEqual(first["messages"], second["messages"])

    def test_clean_history_relabels_previously_delivered_archive_envelope(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = ConversationStore(root / "conversations", root / "memory", long_assistant_reply_chars=0)
            store.append("sender", "User", "给我真实路径")
            store.append(
                "sender", "Assistant",
                "长回复已归档：D:\\missing\\assistant-replies\\fake.md\n摘要：内部预览",
            )
            rendered = json.dumps(store.sync_clean_history("sender")["messages"], ensure_ascii=False)
            self.assertIn("assistant/history-archive", rendered)
            self.assertIn("preview: 内部预览", rendered)
            self.assertNotIn("长回复已归档：", rendered)


if __name__ == "__main__":
    unittest.main()
