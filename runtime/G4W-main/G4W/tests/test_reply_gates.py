"""Unit tests for evidence-first outbound / memory gates."""

from __future__ import annotations

import unittest

from G4W.agents.reply_gates import (
    EvidenceLedger,
    detect_structural_leak,
    extract_user_lines_from_tool_text,
    gate_final_reply,
    gate_remember_content,
    path_looks_like_transcript,
    quote_inclusion_check,
    sanitize_outbound_reply,
    tag_speech_acts,
)


class TestStructuralLeak(unittest.TestCase):
    def test_fake_multi_turn_log_rejected(self):
        text = (
            "喵～我找到了！\n"
            "[07-16 12:00][user] 定位一下\n"
            "[07-16 12:01][assistant] 好的\n"
            "[ROUND END]\n"
        )
        leak = detect_structural_leak(text, user_asked_format=False)
        self.assertIn(leak.action, ("strip", "reject"))
        self.assertGreaterEqual(leak.score, 0.5)

    def test_llm_running_protocol_stripped(self):
        text = "先说明结论。\nLLM Running (Turn 1)\n🛠️ G4W_memory_search({})\n"
        leak = detect_structural_leak(text, user_asked_format=False)
        self.assertTrue(leak.stripped_text)
        self.assertNotIn("LLM Running", leak.stripped_text)

    def test_natural_reply_passes(self):
        text = "我查过相关记录，但还没核对到你那句原话，不能假装找到了。"
        leak = detect_structural_leak(text, user_asked_format=False)
        self.assertEqual(leak.action, "allow")


class TestEvidenceLedger(unittest.TestCase):
    def test_search_is_clue_only(self):
        led = EvidenceLedger()
        led.add_memory_search(
            {
                "hit_count": 1,
                "hybrid": {"hits": [{"text_preview": "User: 你好", "source_path": "x.md"}]},
                "vector": {"hits": []},
            }
        )
        self.assertFalse(led.has_verified_user())
        self.assertTrue(any(r.tier == "clue_only" for r in led.records))

    def test_archive_path_not_verified(self):
        led = EvidenceLedger()
        rec = led.add_file_read(
            r"D:\users\u\assistant-replies\long.md",
            "[07-16 12:00][user] 秘密内容\n",
            archived=True,
        )
        self.assertEqual(rec.tier, "clue_only")
        self.assertFalse(led.has_verified_user())

    def test_transcript_file_read_verified(self):
        led = EvidenceLedger()
        body = "[2026-07-16 12:00][user] 不用提醒我开会\n[2026-07-16 12:01][assistant] 好\n"
        rec = led.add_file_read(
            r"D:\data\users\u\transcripts\2026-07-16.md",
            body,
            archived=False,
        )
        self.assertEqual(rec.tier, "verified_user")
        self.assertTrue(led.has_verified_user())
        self.assertTrue(any("不用提醒" in u for u in rec.user_lines))


class TestSpeechAndQuotes(unittest.TestCase):
    def test_memory_assertion_without_evidence_remediates(self):
        text = '找到了你当时的原话：「不用提醒我」。'
        result = gate_final_reply(text, EvidenceLedger(), allow_remediate=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.action, "remediate")
        self.assertTrue(any("memory_assert" in r for r in result.reasons))

    def test_memory_assertion_with_quote_ok(self):
        led = EvidenceLedger()
        led.add_file_read(
            "users/u/transcripts/2026-07-16.md",
            "User: 不用提醒我开会\nAssistant: 收到\n",
        )
        text = '2026-07-16 transcripts/… 「不用提醒我开会」——这是你当时说的免提醒偏好。'
        result = gate_final_reply(text, led, allow_remediate=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "deliver")

    def test_commit_without_write_blocked(self):
        text = "好的，已经帮你记住并写进长期记忆了。"
        result = gate_final_reply(text, EvidenceLedger(), allow_remediate=True)
        self.assertFalse(result.ok)
        self.assertTrue(any("commit" in r for r in result.reasons))

    def test_commit_with_write_ok(self):
        led = EvidenceLedger()
        led.note_write("file_write", "user prefers no reminders", ok=True, path="user.md")
        text = "已经帮你记下了：以后开会不主动提醒。"
        result = gate_final_reply(text, led, allow_remediate=True)
        self.assertTrue(result.ok)

    def test_quote_must_subset_user_lines(self):
        led = EvidenceLedger()
        led.add_file_read(
            "t.md",
            "User: 真实用户句\nAssistant: 助手说的不能当用户原话\n",
        )
        q = quote_inclusion_check('他说「助手说的不能当用户原话」', led)
        self.assertFalse(q.ok)

    def test_kb_assertion_with_verified_knowledge_quote_ok(self):
        led = EvidenceLedger()
        led.add_knowledge_search(
            {
                "ok": True,
                "hits": [
                    {
                        "doc_id": "mao-1",
                        "chunk_id": "c1",
                        "title": "毛选第一卷",
                        "text": "边界党要巩固井冈山和九陇山两个根据地。",
                    }
                ],
            }
        )
        text = "根据知识库文档《毛选第一卷》原文：『边界党要巩固井冈山和九陇山两个根据地。』"
        result = gate_final_reply(text, led, allow_remediate=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "deliver")

    def test_document_file_read_assertion_with_quote_ok(self):
        led = EvidenceLedger()
        led.add_file_read("knowledge/mao1.md", "边界党要巩固井冈山和九陇山两个根据地。")
        text = "文档原文是：『边界党要巩固井冈山和九陇山两个根据地。』"
        result = gate_final_reply(text, led, allow_remediate=True)
        self.assertTrue(result.ok)

    def test_user_history_assertion_rejects_document_quote(self):
        led = EvidenceLedger()
        led.add_file_read("knowledge/mao1.md", "边界党要巩固井冈山和九陇山两个根据地。")
        text = "找到了你当时的原话：『边界党要巩固井冈山和九陇山两个根据地。』"
        result = gate_final_reply(text, led, allow_remediate=True)
        self.assertFalse(result.ok)
        self.assertTrue(any("memory_assert_without_verified_user" in r for r in result.reasons))


class TestSanitizeAndRemember(unittest.TestCase):
    def test_sanitize_strips_protocol(self):
        dirty = "结论：还没核实。\nLLM Running (Turn 2)\n[ROUND END]\n"
        clean = sanitize_outbound_reply(dirty, EvidenceLedger())
        self.assertNotIn("LLM Running", clean)
        self.assertNotIn("[ROUND END]", clean)

    def test_remember_blocks_structural_pollution(self):
        ok, reason = gate_remember_content(
            "[07-16 12:00][assistant] 内部日志不该进用户记忆\nLLM Running (Turn 1)"
        )
        self.assertFalse(ok)
        self.assertTrue(reason)

    def test_remember_allows_normal_fact(self):
        ok, reason = gate_remember_content("用户偏好：开会不需要主动提醒")
        self.assertTrue(ok)

    def test_path_helpers(self):
        self.assertTrue(path_looks_like_transcript("users/x/transcripts/2026-07-16.md"))
        self.assertFalse(path_looks_like_transcript("users/x/assistant-replies/a.md"))
        lines = extract_user_lines_from_tool_text("User: hello world\nAssistant: hi\n")
        self.assertTrue(any("hello world" in x for x in lines))


class TestSpeechTag(unittest.TestCase):
    def test_tag_detects_acts(self):
        s = tag_speech_acts('根据历史记录，找到了你当时原话：「abc」')
        self.assertTrue(s.memory_assertion)


if __name__ == "__main__":
    unittest.main()
