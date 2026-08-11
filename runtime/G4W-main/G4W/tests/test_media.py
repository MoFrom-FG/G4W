import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from G4W.wechat.aes import decrypt_block, decrypt_ecb_pkcs7, encrypt_block, encrypt_ecb_pkcs7
from G4W.core.config import Config
from G4W.wechat.media import AttachmentStore, extract_attachments, send_weixin_file
from G4W.core.service import G4WService, format_inbound_message
from G4W.wechat.weixin import WeixinChannel


class FakeResponse:
    status = 200
    def __init__(self, data: bytes, content_type="application/octet-stream", headers=None):
        self.data = data
        self.headers = mock.Mock()
        self.headers.get_content_type.return_value = content_type
        self.headers.get.side_effect = lambda key, default=None: (headers or {}).get(key, default)
    def read(self): return self.data
    def __enter__(self): return self
    def __exit__(self, *args): return False


class FakeFileChannel:
    def __init__(self): self.sent = []
    def get_min_chunk_chars(self): return 10
    def set_min_chunk_chars(self, value): return value
    def send_text(self, sender_id, text, context_token="", delivery_id="", **kwargs): return {"deferredText": ""}
    def send_file(self, sender_id, file_path, context_token="", delivery_id=""):
        self.sent.append((sender_id, Path(file_path), context_token)); return {"ok": True}


class FakeUploadChannel:
    def __init__(self):
        self.config = mock.Mock(weixin_cdn_base_url="https://cdn.invalid")
        self.runtime = mock.Mock()
        self.runtime.read.return_value = {"contextTokens": {}}
        self.calls = []
    def resolve_account(self): return {"baseUrl": "https://api.invalid", "token": "secret"}
    def _post_json(self, endpoint, payload, timeout=15):
        self.calls.append((endpoint, payload))
        return {"upload_full_url": "https://cdn.invalid/full-upload"} if endpoint.endswith("getuploadurl") else {}


class SlowFileChannel:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.sent = []
    def get_min_chunk_chars(self): return 10
    def set_min_chunk_chars(self, value): return value
    def send_text(self, sender_id, text, context_token="", delivery_id="", **kwargs):
        self.sent.append(("text", sender_id, text)); return {"deferredText": ""}
    def send_file(self, sender_id, file_path, context_token="", delivery_id=""):
        self.started.set()
        self.release.wait(2)
        self.sent.append(("file", sender_id, Path(file_path).name))
        return {"ok": True}


class MediaTests(unittest.TestCase):
    def test_aes_128_nist_vector_and_pkcs7_roundtrip(self):
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        plain = bytes.fromhex("00112233445566778899aabbccddeeff")
        cipher = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
        self.assertEqual(encrypt_block(plain, key), cipher)
        self.assertEqual(decrypt_block(cipher, key), plain)
        payload = b"G4W attachment payload"
        self.assertEqual(decrypt_ecb_pkcs7(encrypt_ecb_pkcs7(payload, key), key), payload)

    def test_extract_and_decrypt_incoming_attachment(self):
        key = bytes.fromhex("00112233445566778899aabbccddeeff")
        payload = b"%PDF-test"
        encrypted = encrypt_ecb_pkcs7(payload, key)
        items = [{"type": 4, "file_item": {"file_name": "report.pdf", "media": {"url": "https://example.invalid/file", "aes_key": key.hex(), "encrypt_type": 1}}}]
        attachments = extract_attachments(items)
        with tempfile.TemporaryDirectory() as td, mock.patch("urllib.request.urlopen", return_value=FakeResponse(encrypted)):
            result = AttachmentStore(Path(td), "https://cdn.invalid").persist_all(attachments, "m1", "2026-07-14T10:00:00+08:00")
            self.assertFalse(result["failed"])
            saved = Path(result["saved"][0]["absolutePath"])
            self.assertEqual(saved.read_bytes(), payload)
            self.assertEqual(saved.name, "report.pdf")
            self.assertEqual(result["saved"][0]["contentType"], "application/pdf")

    def test_failed_attachment_is_persisted_and_retried(self):
        with tempfile.TemporaryDirectory() as td:
            config = Config(state_dir=Path(td))
            config.ensure_dirs()
            channel = WeixinChannel(config)
            attachment = {"kind": "file", "fileName": "report.pdf", "directUrls": ["https://example.invalid/report"]}
            message = {
                "accountId": "account", "senderId": "sender", "contextToken": "ctx", "messageId": "m1",
                "text": "请分析", "receivedAt": "2026-07-14T10:00:00+08:00", "attachments": [attachment],
                "savedAttachments": [], "attachmentFailures": [{"kind": "file", "sourceFileName": "report.pdf", "reason": "closed"}],
            }
            channel._queue_attachment_retry(message)
            state = channel.attachment_retries.read()
            state["jobs"][0]["nextAttemptAt"] = 0
            channel.attachment_retries.write(state)
            saved = {"kind": "file", "fileName": "report.pdf", "absolutePath": "D:/inbox/report.pdf"}
            with mock.patch.object(channel.attachments, "persist_all", return_value={"saved": [saved], "failed": []}):
                completed = channel.process_attachment_retries()
            self.assertEqual(len(completed), 1)
            self.assertEqual(completed[0]["savedAttachments"], [saved])
            self.assertEqual(channel.attachment_retries.read()["jobs"][0]["status"], "completed")

    def test_attachment_only_message_is_normalized_and_visible(self):
        with tempfile.TemporaryDirectory() as td:
            config = Config(state_dir=Path(td))
            config.ensure_dirs()
            channel = WeixinChannel(config)
            raw = {"from_user_id": "sender", "message_id": "m1", "context_token": "ctx", "item_list": [{"type": 2, "image_item": {"media": {"url": "https://example.invalid/a.png", "encrypt_type": 0}}}]}
            message = channel.normalize(raw, "account")
            self.assertEqual(message["text"], "")
            self.assertEqual(message["attachments"][0]["kind"], "image")
            visible = format_inbound_message({"savedAttachments": [{"kind": "image", "fileName": "a.png", "absolutePath": "D:/inbox/a.png"}]})
            self.assertIn("D:/inbox/a.png", visible)

    def test_file_send_is_durable_outbox_delivery(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            file_path = root / "result.txt"
            file_path.write_text("done", encoding="utf-8")
            channel = FakeFileChannel()
            service = G4WService(Config(state_dir=root / "state", workspace_root=root), channel=channel, session_factory=lambda *_: None)
            service.conversations.bind("account", "sender", "ctx")
            queued = service.controller.execute_direct("sender", "file.send", {"path": str(file_path)})
            self.assertTrue(queued["queued"])
            service.deliver_outbox()
            self.assertEqual(channel.sent, [("sender", file_path.resolve(), "ctx")])

    def test_slow_file_does_not_block_other_binding_and_keeps_same_binding_fifo(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            file_path = root / "timeline.png"
            file_path.write_bytes(b"png")
            channel = SlowFileChannel()
            service = G4WService(Config(state_dir=root / "state", workspace_root=root), channel=channel, session_factory=lambda *_: None)
            service.conversations.bind("account-a", "sender-a", "ctx-a")
            service.conversations.bind("account-b", "sender-b", "ctx-b")
            service.outbox.prepare("account-a:sender-a", "sender-a", "ctx-a", "Turn 1", "a-turn1", round_final=False)
            service.outbox.prepare_file("account-a:sender-a", "sender-a", "ctx-a", str(file_path), "a-file")
            service.outbox.prepare("account-a:sender-a", "sender-a", "ctx-a", "Turn 2", "a-final")
            service.outbox.prepare("account-b:sender-b", "sender-b", "ctx-b", "Other", "b-final")

            service.deliver_outbox(limit=10, asynchronous_files=True)

            self.assertTrue(channel.started.wait(1))
            self.assertEqual([item[2] for item in channel.sent if item[0] == "text"], ["Turn 1", "Other"])
            channel.release.set()
            deadline = time.time() + 2
            while time.time() < deadline:
                state = {item["dedupeKey"]: item for item in service.outbox.store.read()["messages"]}
                if state["a-file"]["status"] == "sent":
                    break
                time.sleep(0.01)
            service.deliver_outbox(limit=10)
            self.assertEqual([item[2] for item in channel.sent if item[0] == "text"], ["Turn 1", "Other", "Turn 2"])

    def test_real_api_upload_full_url_shape_is_supported(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "image.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\ncontent")
            channel = FakeUploadChannel()
            response = FakeResponse(b"", headers={"x-encrypted-param": "download-param"})
            with mock.patch("urllib.request.urlopen", return_value=response):
                result = send_weixin_file(channel, "sender", path, "ctx")
            self.assertTrue(result["ok"])
            self.assertEqual([item[0] for item in channel.calls], ["ilink/bot/getuploadurl", "ilink/bot/sendmessage"])

    def test_file_retry_uses_stable_weixin_client_id(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "timeline.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\ncontent")
            response = FakeResponse(b"", headers={"x-encrypted-param": "download-param"})
            client_ids = []
            for _ in range(2):
                channel = FakeUploadChannel()
                with mock.patch("urllib.request.urlopen", return_value=response):
                    result = send_weixin_file(channel, "sender", path, "ctx", delivery_id="outbox-123")
                client_ids.append(result["clientId"])
            self.assertEqual(client_ids[0], client_ids[1])


if __name__ == "__main__":
    unittest.main()
