import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from G4W.core.config import Config
from G4W.core.service import G4WService
from G4W.wechat.weixin_delivery import format_deferred_reply_batch


class OutboxResilienceTests(unittest.TestCase):
    def test_expired_checkin_delivery_is_deferred_before_cancel(self):
        class FailingChannel:
            def send_text(self, *_args, **_kwargs):
                raise AssertionError("expired checkin should not be sent live")

        with tempfile.TemporaryDirectory() as td:
            service = G4WService(
                Config(state_dir=Path(td)),
                channel=FailingChannel(),
                session_factory=lambda *_args: None,
            )
            now = time.time()
            message = {
                "id": "msg-1",
                "bindingKey": "bot:user",
                "senderId": "user",
                "status": "sending",
                "kind": "text",
                "text": "hello proactive",
                "deferredKind": "checkin",
                "cancelOnUserActivity": True,
                "createdAt": now - 700,
                "expiresAt": now - 100,
            }
            service.outbox.store.write({"messages": [message]})

            delivered = service._deliver_claimed(dict(message))

            outbox_message = service.outbox.store.read()["messages"][0]
            deferred = service.deferred.store.read()["items"].get("user", [])
            self.assertTrue(delivered)
            self.assertEqual(outbox_message.get("status"), "cancelled")
            self.assertEqual(outbox_message.get("error"), "proactive delivery expired before send")
            self.assertEqual(len(deferred), 1)
            self.assertEqual(deferred[0].get("kind"), "checkin")
            self.assertEqual(deferred[0].get("text"), "hello proactive")

    def test_weixin_cap_tail_is_deferred_until_next_user_message(self):
        class CappedChannel:
            def send_text(self, *_args, **_kwargs):
                return {
                    "deliveredText": "已发送的前十条",
                    "deferredText": "第十一条以后需要补发的内容",
                    "deliveredCount": 10,
                }

        with tempfile.TemporaryDirectory() as td:
            service = G4WService(
                Config(state_dir=Path(td), checkin_enabled=False),
                channel=CappedChannel(),
                session_factory=lambda *_args: None,
            )
            service.outbox.prepare(
                "bot:user", "user", "old-token", "很长的最终回复", "dedupe-cap",
                round_id="round-cap", round_final=True, deferred_kind="plain_reply",
            )
            message = service.outbox.claim_next_pending()

            delivered = service._deliver_claimed(message)

            self.assertTrue(delivered)
            deferred = service.deferred.store.read()["items"].get("user", [])
            self.assertEqual(len(deferred), 1)
            self.assertEqual(deferred[0].get("text"), "第十一条以后需要补发的内容")

            service._enqueue_inbound({
                "accountId": "bot",
                "senderId": "user",
                "contextToken": "fresh-token",
                "text": "继续",
                "messageId": "incoming-1",
                "receivedAt": "2026-07-27T00:00:00Z",
            })

            self.assertEqual(service.deferred.count("user"), 0)
            events = service.events.store.read()["events"]
            user_events = [event for event in events if event.get("type") == "wechat.user_message"]
            self.assertEqual(len(user_events), 1)
            popped = user_events[0]["payload"].get("deferredReplies", [])
            self.assertEqual(len(popped), 1)
            self.assertEqual(popped[0].get("text"), "第十一条以后需要补发的内容")
            self.assertIn("context_token 的限制", format_deferred_reply_batch(popped))

    def test_duplicate_inbound_does_not_consume_deferred_reply(self):
        with tempfile.TemporaryDirectory() as td:
            service = G4WService(
                Config(state_dir=Path(td), checkin_enabled=False),
                channel=mock.Mock(),
                session_factory=lambda *_args: None,
            )
            service.events.enqueue(
                "wechat.user_message",
                "bot:user",
                {"text": "继续", "deferredReplies": []},
                dedupe_key="wechat.user_message:bot:incoming-1",
            )
            service.deferred.add("bot:user", "user", "之前未发送成功的内容", kind="plain_reply")

            event = service._enqueue_inbound({
                "accountId": "bot",
                "senderId": "user",
                "contextToken": "fresh-token",
                "text": "继续",
                "messageId": "incoming-1",
                "receivedAt": "2026-07-27T00:00:00Z",
            })

            self.assertEqual(event.get("dedupeKey"), "wechat.user_message:bot:incoming-1")
            self.assertEqual(service.deferred.count("user"), 1)
            events = service.events.store.read()["events"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["payload"].get("deferredReplies"), [])

    def test_outbox_loop_retries_after_transient_exception(self):
        service = object.__new__(G4WService)
        service.outbox = mock.Mock()
        service.outbox.recover_stale_sending.return_value = 0
        service._outbox_last_heartbeat = 0.0
        service._outbox_last_error = ""
        service._outbox_last_error_at = 0.0
        service._outbox_error_count = 0
        service._outbox_next_recovery = 0.0
        stop = threading.Event()
        calls = {"count": 0}

        def deliver(*_args, **_kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise PermissionError("temporary outbox lock")
            stop.set()

        service.deliver_outbox = deliver
        with mock.patch("G4W.core.service.traceback.print_exc"), mock.patch("builtins.print"):
            service._outbox_loop(stop)

        self.assertEqual(calls["count"], 2)
        self.assertEqual(service._outbox_error_count, 1)
        self.assertIn("temporary outbox lock", service._outbox_last_error)
        self.assertGreater(service._outbox_last_heartbeat, 0)


if __name__ == "__main__":
    unittest.main()
