import unittest

from G4W.wechat.weixin_delivery import apply_delivery_budget, build_effective_reply_text, chunk_reply_text_for_weixin, format_deferred_reply_batch, pack_final_burst, prepare_reply_chunks, take_live_delivery


class WeixinDeliveryTests(unittest.TestCase):
    def test_reply_splits_on_chinese_sentences(self):
        chunks = chunk_reply_text_for_weixin("第一句。第二句。第三句。", min_chunk=1)
        self.assertEqual(chunks, ["第一句。", "第二句。", "第三句。"])

    def test_short_chunks_merge(self):
        chunks = chunk_reply_text_for_weixin("一。二。三。", min_chunk=10)
        self.assertEqual(chunks, ["一。\n二。\n三。"])

    def test_delivery_budget_aggregates_tail_into_ninth_bubble(self):
        sent, deferred = apply_delivery_budget([str(index) for index in range(12)], max_messages=10)
        self.assertEqual(len(sent), 9)
        self.assertEqual(sent[:8], [str(index) for index in range(8)])
        self.assertEqual(sent[8], "8\n9\n10\n11")
        self.assertEqual(deferred, "")

    def test_delivery_budget_uses_tenth_only_when_tail_is_too_long(self):
        sent, deferred = apply_delivery_budget([str(index) for index in range(8)] + ["甲" * 3000, "乙" * 3000])
        self.assertEqual(len(sent), 10)
        self.assertEqual(len(sent[8]), 3800)
        self.assertTrue(sent[9])
        self.assertEqual(deferred, "")

    def test_delivery_budget_counts_intermediate_bubbles_in_same_round(self):
        sent, deferred = apply_delivery_budget([str(index) for index in range(12)], sent_count=1)
        self.assertEqual(len(sent), 8)
        self.assertEqual(sent[:7], [str(index) for index in range(7)])
        self.assertEqual(sent[-1], "7\n8\n9\n10\n11")
        self.assertEqual(deferred, "")

    def test_structural_markdown_is_preserved_and_not_sentence_split(self):
        text = "# 标题\n\n- 第一项。\n- 第二项。"
        chunks, preserve_markdown = prepare_reply_chunks(text, min_chunk=1)
        self.assertTrue(preserve_markdown)
        self.assertEqual(chunks, [text])

    def test_horizontal_rule_is_structural_markdown_and_stays_one_bubble(self):
        text = "前文\n\n---\n\n后文"
        chunks, preserve_markdown = prepare_reply_chunks(text, min_chunk=1)
        self.assertTrue(preserve_markdown)
        self.assertEqual(chunks, [text])

    def test_live_delivery_holds_after_eight_until_final_burst(self):
        live, held = take_live_delivery([str(index) for index in range(11)], sent_count=0)
        self.assertEqual(live, [str(index) for index in range(8)])
        self.assertEqual(held, ["8", "9", "10"])
        final, deferred = pack_final_burst(held, sent_count=8)
        self.assertEqual(final, ["8\n9\n10"])
        self.assertEqual(deferred, "")

    def test_final_reply_keeps_first_eight_chunks_as_separate_bubbles(self):
        final, deferred = pack_final_burst(["第一段", "第二段", "第三段"], sent_count=0)
        self.assertEqual(final, ["第一段", "第二段", "第三段"])
        self.assertEqual(deferred, "")

    def test_final_reply_aggregates_only_content_after_eighth_bubble(self):
        final, deferred = pack_final_burst([str(index) for index in range(12)], sent_count=0)
        self.assertEqual(final[:8], [str(index) for index in range(8)])
        self.assertEqual(final[8], "8\n9\n10\n11")
        self.assertEqual(deferred, "")

    def test_final_burst_defers_content_after_tenth_bubble_capacity(self):
        final, deferred = pack_final_burst(["a" * 3900, "b"], sent_count=8)
        self.assertEqual([len(item) for item in final], [3800, 100])
        self.assertEqual(deferred, "b")

    def test_deferred_batch_separates_worker_reports_from_other_messages(self):
        prefix = format_deferred_reply_batch([
            {"kind": "plain_reply", "text": "旧尾段"},
            {"kind": "system_reply", "text": "期间主动联系"},
            {"kind": "proactive_report", "text": "Worker到Turn 10"},
        ])
        combined = build_effective_reply_text(prefix, "本轮回复")
        self.assertIn("===== 上轮对话遗留内容 =====", combined)
        self.assertIn("===== 期间模型主动联系 =====", combined)
        self.assertIn("===== 期间模型主动汇报 =====", combined)
        self.assertIn("===== 本轮模型回复 =====", combined)


if __name__ == "__main__":
    unittest.main()
