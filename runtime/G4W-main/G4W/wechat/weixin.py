import base64
import json
import os
import random
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .qr import make_svg
from .media import AttachmentStore, extract_attachments, send_weixin_file
from .account_store import WeixinAccountStore
from ..core.storage import JsonStore, safe_segment
from .weixin_delivery import DEFAULT_MIN_WEIXIN_CHUNK, MAX_WEIXIN_CHUNK, join_delivery_chunks, pack_final_burst, prepare_reply_chunks, strip_sentence_tail_chinese_full_stops, take_live_delivery


class WeixinError(RuntimeError):
    pass


def _is_timeout(error: Exception) -> bool:
    if isinstance(error, (TimeoutError, socket.timeout)):
        return True
    if isinstance(error, urllib.error.URLError):
        return _is_timeout(error.reason) if isinstance(error.reason, Exception) else False
    return False


def _post(base_url: str, endpoint: str, token: str, payload: dict, timeout: int = 15, timeout_is_empty: bool = False) -> dict:
    url = urllib.parse.urljoin(base_url.rstrip("/") + "/", endpoint)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    uin = base64.b64encode(str(random.getrandbits(32)).encode()).decode()
    headers = {"Content-Type": "application/json", "AuthorizationType": "ilink_bot_token", "X-WECHAT-UIN": uin}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout + 5) as response:
            result = json.loads(response.read().decode("utf-8"))
    except Exception as error:
        if timeout_is_empty and _is_timeout(error):
            return {}
        raise WeixinError(f"{endpoint} failed: {error}") from error
    if result.get("ret", 0) != 0 or result.get("errcode", 0) != 0:
        raise WeixinError(f"{endpoint} ret={result.get('ret')} errcode={result.get('errcode')} {result.get('errmsg', '')}")
    return result


class WeixinChannel:
    def __init__(self, config):
        self.config = config
        self.accounts = WeixinAccountStore(config.accounts_dir)
        self.runtime = JsonStore(config.state_dir / "weixin-runtime.json", {"sync": {}, "contextTokens": {}, "seen": []})
        self.delivery_config = JsonStore(config.state_dir / "weixin-config.json", {"minChunkChars": DEFAULT_MIN_WEIXIN_CHUNK})
        self.attachment_retries = JsonStore(config.state_dir / "attachment-retries.json", {"version": 1, "jobs": []})
        self.delivery_audit = JsonStore(config.state_dir / "delivery-audit.json", {"version": 1, "deliveries": []})
        self.attachments = AttachmentStore(config.state_dir, config.weixin_cdn_base_url)
        self.account = None
        self.delivery_states: dict[str, dict] = {}

    def _post_json(self, endpoint: str, payload: dict, timeout: int = 15) -> dict:
        account = self.resolve_account()
        return _post(account["baseUrl"], endpoint, account["token"], payload, timeout=timeout)

    def login(self) -> dict:
        query = urllib.parse.urlencode({"bot_type": self.config.bot_type})
        with urllib.request.urlopen(f"{self.config.weixin_base_url.rstrip('/')}/ilink/bot/get_bot_qrcode?{query}", timeout=15) as response:
            qr = json.loads(response.read().decode("utf-8"))
        content = qr["qrcode_img_content"]
        page = self.config.state_dir / "login-qrcode.html"
        escaped = content.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
        svg = make_svg(content)
        page.write_text(
            "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>G4W 微信登录</title><style>body{font-family:Segoe UI,Microsoft YaHei,sans-serif;background:#f5f6f8;"
            "display:grid;place-items:center;min-height:100vh;margin:0}main{text-align:center}.qr{display:inline-block;padding:18px;background:white;"
            "border:1px solid #d1d5db}p{color:#4b5563}a{word-break:break-all}</style><main><h2>G4W 微信测试账号登录</h2>"
            f"<div class='qr'>{svg}</div><p>请使用微信扫描二维码，保持登录命令运行。</p><p><a href=\"{escaped}\">{escaped}</a></p></main></html>",
            encoding="utf-8",
        )
        try: webbrowser.open(page.as_uri())
        except Exception: pass
        print(f"[G4W] login page: {page}")
        deadline = time.time() + 480
        while time.time() < deadline:
            url = f"{self.config.weixin_base_url.rstrip('/')}/ilink/bot/get_qrcode_status?{urllib.parse.urlencode({'qrcode': qr['qrcode']})}"
            request = urllib.request.Request(url, headers={"iLink-App-ClientVersion": "1"})
            try:
                with urllib.request.urlopen(request, timeout=40) as response:
                    status = json.loads(response.read().decode("utf-8"))
            except Exception:
                status = {"status": "wait"}
            if status.get("status") == "confirmed":
                account = {
                    "accountId": status["ilink_bot_id"],
                    "token": status["bot_token"],
                    "baseUrl": status.get("baseurl") or self.config.weixin_base_url,
                    "userId": status.get("ilink_user_id", ""),
                    "savedAt": time.time(),
                }
                def save(state): state.setdefault("accounts", {})[account["accountId"]] = account
                self.accounts.update(save)
                self.account = account
                # 登录成功后把当前账号写入 package-local ENV,
                # 保证后续 start 能精确选中(多账号/重复登录时 G4W_ACCOUNT_ID 不会为空或过期)
                try:
                    from ..memory.instructions import update_env_file
                    update_env_file(self.config.env_file, {"G4W_ACCOUNT_ID": account["accountId"]})
                except Exception as error:
                    print(f"[G4W] warning: failed to write G4W_ACCOUNT_ID to ENV: {error}")
                return account
            time.sleep(1)
        raise WeixinError("login timed out")

    def resolve_account(self) -> dict:
        if self.account:
            return self.account
        accounts = self.accounts.read().get("accounts", {})
        if self.config.account_id:
            self.account = accounts.get(self.config.account_id)
        if not self.account and len(accounts) == 1:
            self.account = next(iter(accounts.values()))
        if not self.account:
            if accounts:
                available = ", ".join(sorted(accounts))
                raise WeixinError(
                    f"Configured G4W_ACCOUNT_ID={self.config.account_id!r} was not found. "
                    f"Available test accounts: {available}"
                )
            raise WeixinError("No test WeChat account configured. Run: python -m G4W login")
        return self.account

    def get_updates(self) -> list[dict]:
        account = self.resolve_account()
        runtime = self.runtime.read()
        sync = runtime.get("sync", {}).get(account["accountId"], "")
        result = _post(
            account["baseUrl"],
            "ilink/bot/getupdates",
            account["token"],
            {"get_updates_buf": sync, "base_info": {"channel_version": "python-G4W-0.1"}},
            max(35, self.config.poll_timeout_seconds),
            timeout_is_empty=True,
        )
        new_sync = str(result.get("get_updates_buf") or "").strip()
        if new_sync:
            def save(state): state.setdefault("sync", {})[account["accountId"]] = new_sync
            self.runtime.update(save)
        output = []
        for raw in result.get("msgs") or []:
            if int(raw.get("message_type", 0)) == 2:
                continue
            normalized = self.normalize(raw, account["accountId"])
            if normalized:
                persisted = self.attachments.persist_all(
                    normalized.get("attachments") or [],
                    normalized.get("messageId", ""),
                    normalized.get("receivedAt", ""),
                )
                normalized["savedAttachments"] = persisted["saved"]
                normalized["attachmentFailures"] = persisted["failed"]
                if persisted["failed"]:
                    self._queue_attachment_retry(normalized)
                    normalized["attachmentPending"] = True
                output.append(normalized)
        return output

    def _queue_attachment_retry(self, message: dict) -> None:
        failed_names = {
            (str(item.get("kind") or ""), str(item.get("sourceFileName") or ""))
            for item in message.get("attachmentFailures") or []
        }
        pending = [
            item for item in message.get("attachments") or []
            if (str(item.get("kind") or ""), str(item.get("fileName") or "")) in failed_names
        ]
        if not pending:
            pending = list(message.get("attachments") or [])
        job = {
            "id": f"attachment:{message.get('accountId')}:{message.get('messageId')}",
            "status": "pending",
            "attempts": 0,
            "nextAttemptAt": time.time() + 2,
            "message": {
                key: message.get(key) for key in (
                    "accountId", "senderId", "contextToken", "messageId", "text", "receivedAt"
                )
            },
            "pendingAttachments": pending,
            "savedAttachments": list(message.get("savedAttachments") or []),
            "lastFailures": list(message.get("attachmentFailures") or []),
            "createdAt": time.time(),
        }

        def update(state):
            jobs = state.setdefault("jobs", [])
            if not any(item.get("id") == job["id"] and item.get("status") == "pending" for item in jobs):
                jobs.append(job)
            return job

        self.attachment_retries.update(update)

    def process_attachment_retries(self, limit: int = 3, max_attempts: int = 6) -> list[dict]:
        now = time.time()
        state = self.attachment_retries.read()
        due = [
            item for item in state.get("jobs", [])
            if item.get("status") == "pending" and float(item.get("nextAttemptAt", 0)) <= now
        ][:max(1, int(limit))]
        completed = []
        for candidate in due:
            job_id = candidate.get("id")
            attempted_attachments = list(candidate.get("pendingAttachments") or [])
            persisted = self.attachments.persist_all(
                attempted_attachments,
                (candidate.get("message") or {}).get("messageId", ""),
                (candidate.get("message") or {}).get("receivedAt", ""),
            )

            def update(current):
                job = next((item for item in current.get("jobs", []) if item.get("id") == job_id), None)
                if not job or job.get("status") != "pending":
                    return None
                job.setdefault("savedAttachments", []).extend(persisted["saved"])
                if not persisted["failed"]:
                    job["status"] = "completed"
                    job["completedAt"] = time.time()
                    message = dict(job.get("message") or {})
                    message["savedAttachments"] = list(job.get("savedAttachments") or [])
                    message["attachmentFailures"] = []
                    return message
                attempts = int(job.get("attempts", 0)) + 1
                job["attempts"] = attempts
                job["lastFailures"] = list(persisted["failed"])
                failed_names = {
                    (str(item.get("kind") or ""), str(item.get("sourceFileName") or ""))
                    for item in persisted["failed"]
                }
                job["pendingAttachments"] = [
                    item for item in attempted_attachments
                    if (str(item.get("kind") or ""), str(item.get("fileName") or "")) in failed_names
                ] or attempted_attachments
                job["updatedAt"] = time.time()
                if attempts >= max_attempts:
                    job["status"] = "failed"
                    job["failedAt"] = time.time()
                    message = dict(job.get("message") or {})
                    message["savedAttachments"] = list(job.get("savedAttachments") or [])
                    message["attachmentFailures"] = list(persisted["failed"])
                    return message
                job["nextAttemptAt"] = time.time() + min(300, 2 ** min(attempts + 1, 8))
                return None

            message = self.attachment_retries.update(update)
            if message:
                completed.append(message)
        return completed

    def normalize(self, raw: dict, account_id: str) -> dict | None:
        sender_id = str(raw.get("from_user_id") or "").strip()
        if not sender_id:
            return None
        items = raw.get("item_list") or []
        text = ""
        for item in items:
            if int(item.get("type", 0)) == 1:
                text = str((item.get("text_item") or {}).get("text") or "").strip()
                if text: break
            if int(item.get("type", 0)) == 3:
                text = str((item.get("voice_item") or {}).get("text") or "").strip()
                if text: break
        attachments = extract_attachments(items)
        if not text and not attachments:
            return None
        message_id = str(raw.get("message_id") or raw.get("client_id") or f"{sender_id}:{raw.get('create_time_ms', 0)}")
        runtime = self.runtime.read()
        if message_id in runtime.get("seen", []):
            return None
        context_token = str(raw.get("context_token") or "").strip()
        def remember(state):
            seen = state.setdefault("seen", [])
            seen.append(message_id)
            state["seen"] = seen[-1000:]
            if context_token:
                state.setdefault("contextTokens", {})[sender_id] = context_token
        self.runtime.update(remember)
        created_ms = int(raw.get("create_time_ms", 0) or 0)
        received_at = datetime.fromtimestamp(created_ms / 1000, timezone.utc).isoformat() if created_ms > 0 else datetime.now(timezone.utc).isoformat()
        return {"accountId": account_id, "senderId": sender_id, "contextToken": context_token, "messageId": message_id, "text": text, "attachments": attachments, "receivedAt": received_at}

    def send_text(self, sender_id: str, text: str, context_token: str = "", delivery_id: str = "", round_id: str = "", round_final: bool = True, source: str = "conductor", turn: int = 1, deferred_kind: str = "plain_reply", preserve_block: bool = False) -> dict:
        account = self.resolve_account()
        token = context_token or self.runtime.read().get("contextTokens", {}).get(sender_id, "")
        if not token:
            raise WeixinError(f"missing context_token for {sender_id}")
        chunks, preserve_markdown = prepare_reply_chunks(text, self.get_min_chunk_chars())
        if preserve_block:
            chunks = [str(text or "").strip()] if str(text or "").strip() else []
            preserve_markdown = True
        chunks = chunks or ([str(text or "")] if str(text or "") else [])
        budget_key = str(round_id or delivery_id or f"{sender_id}:{token}")
        state = self.delivery_states.setdefault(budget_key, {
            "sentCount": 0, "heldChunks": [], "preserveMarkdown": False,
        })
        sent_count = int(state.get("sentCount", 0) or 0)
        state["preserveMarkdown"] = bool(state.get("preserveMarkdown") or preserve_markdown)
        deferred_text = ""
        if round_final:
            final_chunks = list(state.get("heldChunks") or []) + list(chunks)
            send_chunks, deferred_text = pack_final_burst(
                final_chunks,
                sent_count=sent_count,
                max_messages=10,
                max_chars=MAX_WEIXIN_CHUNK,
                preserve_markdown=bool(state.get("preserveMarkdown")),
            )
            preserve_markdown = bool(state.get("preserveMarkdown"))
        else:
            send_chunks, held_chunks = take_live_delivery(
                chunks,
                sent_count=sent_count,
                live_messages=8,
                preserve_markdown=preserve_markdown,
            )
            state.setdefault("heldChunks", []).extend(held_chunks)
        delivered = []
        delivered_chunks = []
        for index, chunk in enumerate(send_chunks):
            compact_chunk = str(chunk or "") if preserve_markdown else strip_sentence_tail_chinese_full_stops(chunk)
            compact_chunk = compact_chunk or "Completed."
            client_id = (
                f"pycb-{uuid.uuid5(uuid.NAMESPACE_URL, f'{delivery_id}:{index}')}"
                if delivery_id else f"pycb-{uuid.uuid4()}"
            )
            _post(account["baseUrl"], "ilink/bot/sendmessage", account["token"], {"msg": {"from_user_id": "", "to_user_id": sender_id, "client_id": client_id, "message_type": 2, "message_state": 2, "item_list": [{"type": 1, "text_item": {"text": compact_chunk}}], "context_token": token}, "base_info": {"channel_version": "python-G4W-0.1"}}, timeout=5)
            delivered.append(compact_chunk)
            delivered_chunks.append({
                "bubble": sent_count + index + 1,
                "text": compact_chunk,
                "clientId": client_id,
                "sentAt": time.time(),
            })
            time.sleep(0.35)
        state["sentCount"] = sent_count + len(delivered)
        held_text = join_delivery_chunks(state.get("heldChunks") or [], bool(state.get("preserveMarkdown")))
        if round_final:
            self.delivery_states.pop(budget_key, None)
        result = {
            "deliveredText": ("\n\n" if preserve_markdown else "\n").join(delivered),
            "deliveredChunks": delivered_chunks,
            "deferredText": deferred_text,
            "heldText": "" if round_final else held_text,
            "deliveredCount": len(delivered),
            "preserveMarkdown": preserve_markdown,
            "roundFinal": bool(round_final),
            "preserveBlock": bool(preserve_block),
        }
        audit = {
            "deliveryId": str(delivery_id or ""),
            "roundId": str(round_id or ""),
            "senderId": sender_id,
            "turn": max(1, int(turn or 1)),
            "source": str(source or "conductor"),
            "deferredKind": str(deferred_kind or "plain_reply"),
            "originalText": str(text or ""),
            "createdAt": time.time(),
            **result,
        }
        def remember(audit_state):
            deliveries = audit_state.setdefault("deliveries", [])
            deliveries.append(audit)
            if len(deliveries) > 2000:
                audit_state["deliveries"] = deliveries[-1500:]
        self.delivery_audit.update(remember)
        return result

    def send_file(self, sender_id: str, file_path: Path, context_token: str = "", delivery_id: str = "") -> dict:
        return send_weixin_file(self, sender_id, Path(file_path), context_token, delivery_id=delivery_id)

    def get_min_chunk_chars(self) -> int:
        try:
            value = int(self.delivery_config.read().get("minChunkChars", DEFAULT_MIN_WEIXIN_CHUNK))
        except Exception:
            value = DEFAULT_MIN_WEIXIN_CHUNK
        return max(1, min(value, MAX_WEIXIN_CHUNK))

    def set_min_chunk_chars(self, value: int) -> int:
        normalized = max(1, min(int(value), MAX_WEIXIN_CHUNK))
        self.delivery_config.write({"minChunkChars": normalized})
        return normalized

    def send_typing(self, sender_id: str, status: int = 1, context_token: str = "") -> None:
        account = self.resolve_account()
        token = context_token or self.runtime.read().get("contextTokens", {}).get(sender_id, "")
        if not token:
            return
        config_response = _post(
            account["baseUrl"],
            "ilink/bot/getconfig",
            account["token"],
            {"ilink_user_id": sender_id, "context_token": token, "base_info": {"channel_version": "python-G4W-0.2"}},
            timeout=10,
        )
        typing_ticket = str(config_response.get("typing_ticket") or "").strip()
        if not typing_ticket:
            return
        _post(
            account["baseUrl"],
            "ilink/bot/sendtyping",
            account["token"],
            {"ilink_user_id": sender_id, "typing_ticket": typing_ticket, "status": int(status), "base_info": {"channel_version": "python-G4W-0.2"}},
            timeout=10,
        )

    @contextmanager
    def typing_keepalive(self, sender_id: str, context_token: str = "", interval_seconds: int = 10):
        stopped = threading.Event()
        def send(status):
            try:
                self.send_typing(sender_id, status, context_token)
            except Exception:
                pass
        send(1)
        def keepalive():
            while not stopped.wait(interval_seconds):
                send(1)
        thread = threading.Thread(target=keepalive, daemon=True, name=f"typing-{safe_segment(sender_id)}")
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            send(0)
