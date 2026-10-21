import base64
import json
import os
import random
import socket
import sys
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
from .weixin_delivery import SPLIT_MARKER_RE, DEFAULT_MIN_WEIXIN_CHUNK, MAX_WEIXIN_CHUNK, join_delivery_chunks, pack_final_burst, prepare_reply_chunks, strip_sentence_tail_chinese_full_stops, take_live_delivery


class WeixinError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# iLink 协议声明：与腾讯官方 @tencent-weixin/openclaw-weixin 对齐
#   · iLink-App-Id          取自官方 package.json 的 ilink_appid
#   · iLink-App-ClientVersion 官方编码 = (major<<16)|(minor<<8)|patch，即 0x00MMNNPP
#   · base_info.channel_version 官方用包版本 semver；bot_agent 是产品身份（官方默认 "OpenClaw"）
# 之所以要带：服务端可能按声明做能力门禁（社区实测过旧声明会导致手机端缺附件入口等）。
# 说明（2026-10-12 实测）：getconfig 端点当前**不**校验该声明（新旧返回一致），
# 因此这部分属于兼容性对齐，不是某个已知故障的修复。
# ---------------------------------------------------------------------------
ILINK_APP_ID = "bot"
ILINK_CHANNEL_VERSION = "2.4.10"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (4 << 8) | 10      # 2.4.10 -> 0x02040A = 132106
G4W_BOT_AGENT = "G4W"


def _base_info() -> dict:
    return {"channel_version": ILINK_CHANNEL_VERSION, "bot_agent": G4W_BOT_AGENT}


def _stdin_is_interactive() -> bool:
    """看板向导 / 桌面壳用管道后台跑 `G4W login`，此时 input() 拿不到用户输入。"""
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:
        return False


def _ilink_headers(token: str = "") -> dict:
    """iLink 请求头（POST 与二维码轮询都用同一套声明）。"""
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": base64.b64encode(str(random.getrandbits(32)).encode()).decode(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _is_timeout(error: Exception) -> bool:
    if isinstance(error, (TimeoutError, socket.timeout)):
        return True
    if isinstance(error, urllib.error.URLError):
        return _is_timeout(error.reason) if isinstance(error.reason, Exception) else False
    return False


def _post(base_url: str, endpoint: str, token: str, payload: dict, timeout: int = 15, timeout_is_empty: bool = False) -> dict:
    url = urllib.parse.urljoin(base_url.rstrip("/") + "/", endpoint)
    # 所有请求统一带上 base_info（调用方自带的优先，不再各处手写版本串）
    if "base_info" not in payload:
        payload = dict(payload, base_info=_base_info())
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=_ilink_headers(token), method="POST")
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

    # ---- 扫码登录：状态机照腾讯官方 login-qr.js 对齐（2026-10-12）----
    # 官方处理这 7 个状态：wait / scaned / need_verifycode / expired /
    # verify_code_blocked / scaned_but_redirect / binded_redirect / confirmed。
    # 此前 G4W 只认 confirmed，其余一律"继续等"——于是需要验证码、二维码过期、
    # IDC 重定向、账号已绑定的用户都会表现为"手机上扫了、电脑上不动"，无法自查。
    LOGIN_QR_REFRESH_LIMIT = 3
    LOGIN_TOTAL_TIMEOUT = 480

    def _write_login_page(self, content: str) -> Path:
        page = self.config.state_dir / "login-qrcode.html"
        escaped = content.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
        svg = make_svg(content)
        page.write_text(
            "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>G4W 微信登录</title><style>body{font-family:Segoe UI,Microsoft YaHei,sans-serif;background:#f5f6f8;"
            "display:grid;place-items:center;min-height:100vh;margin:0}main{text-align:center}.qr{display:inline-block;padding:18px;background:white;"
            "border:1px solid #d1d5db}p{color:#4b5563;max-width:520px}a{word-break:break-all}"
            ".warn{color:#b45309}</style><main><h2>G4W 微信测试账号登录</h2>"
            f"<div class='qr'>{svg}</div><p>请使用<b>微信</b>扫描二维码，保持登录命令运行。</p>"
            "<p class='warn'>若手机上是国际版 WeChat 且扫码后一直没反应，请改用中国大陆版微信再试；"
            "若微信提示输入数字，请在运行登录命令的窗口里输入。</p>"
            f"<p><a href=\"{escaped}\">{escaped}</a></p></main></html>",
            encoding="utf-8",
        )
        return page

    def _local_token_list(self) -> list:
        """本机已保存的 bot token 列表。

        官方取二维码时会把已有 token 一起报给服务端（`local_token_list`）：
        服务端据此把“这台机器已经连过”识别成可重连，而不是判定成“账号已绑定”。
        issue #2 那位用户先在官方插件里扫过码，带上这个列表可能就是它的解法。
        """
        try:
            accounts = self.accounts.read().get("accounts", {}) or {}
            return [str(item.get("token")) for item in accounts.values() if str(item.get("token") or "").strip()]
        except Exception:
            return []

    def _fetch_login_qr(self) -> dict:
        query = urllib.parse.urlencode({"bot_type": self.config.bot_type})
        tokens = self._local_token_list()
        # 官方是 POST + body {local_token_list: [...]}；服务端若不接受，回退到原来的裸 GET
        try:
            return _post(self.config.weixin_base_url, f"ilink/bot/get_bot_qrcode?{query}", "",
                         {"local_token_list": tokens}, timeout=15)
        except Exception as error:
            print(f"[G4W] get_bot_qrcode POST 失败（{type(error).__name__}），回退为 GET")
            url = f"{self.config.weixin_base_url.rstrip('/')}/ilink/bot/get_bot_qrcode?{query}"
            request = urllib.request.Request(url, headers=_ilink_headers())
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))

    def _poll_login_status(self, base_url: str, qrcode: str, verify_code: str = "") -> dict:
        query = {"qrcode": qrcode}
        if verify_code:
            query["verify_code"] = verify_code
        url = f"{base_url.rstrip('/')}/ilink/bot/get_qrcode_status?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(url, headers=_ilink_headers())
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as error:
            if os.environ.get("G4W_LOGIN_DEBUG") == "1":
                print(f"[G4W] login poll error: {type(error).__name__}: {error}")
            return {"status": "wait"}

    def _ask_verify_code(self, prompt: str) -> str:
        """默认从终端读；GUI（看板向导/桌面壳）可通过 verify_code_provider 覆盖。"""
        provider = getattr(self, "verify_code_provider", None)
        if callable(provider):
            try:
                return str(provider(prompt) or "").strip()
            except Exception as error:
                print(f"[G4W] verify code provider failed: {error}")
                return ""
        try:
            return input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            return ""

    def login(self, *, interactive: bool = True) -> dict:
        qr = self._fetch_login_qr()
        qrcode = str(qr.get("qrcode") or "")
        page = self._write_login_page(str(qr.get("qrcode_img_content") or ""))
        if os.environ.get("G4W_NO_BROWSER") != "1":
            # 桌面向导模式（G4W_NO_BROWSER=1）由壳层读取本 HTML 自渲染二维码，不弹浏览器
            try: webbrowser.open(page.as_uri())
            except Exception: pass
        print(f"[G4W] login page: {page}")
        print("[G4W] 请使用微信扫码；若手机是国际版 WeChat 且扫码无反应，请改用中国大陆版微信。")
        if not qrcode:
            raise WeixinError("login failed: server did not return qrcode")

        current_base = self.config.weixin_base_url
        pending_verify_code = ""
        refresh_count = 0
        last_status = ""
        scanned_printed = False
        deadline = time.time() + self.LOGIN_TOTAL_TIMEOUT

        while time.time() < deadline:
            status = self._poll_login_status(current_base, qrcode, pending_verify_code)
            name = str(status.get("status") or "")
            if name != last_status:
                if name and (name != "wait" or os.environ.get("G4W_LOGIN_DEBUG") == "1"):
                    extra = ""
                    if name == "scaned_but_redirect":
                        extra = f" redirect_host={status.get('redirect_host')}"
                    print(f"[G4W] login status: {name}{extra}")
                last_status = name

            if name in ("wait", ""):
                pass
            elif name == "scaned":
                if pending_verify_code:
                    print("[G4W] 验证码已接受，继续等待确认…")
                    pending_verify_code = ""
                if not scanned_printed:
                    print("[G4W] 已扫描，正在验证（请在手机上确认）…")
                    scanned_printed = True
            elif name == "need_verifycode":
                if not interactive:
                    raise WeixinError("login needs a verify code from the phone (non-interactive mode)")
                prompt = "❌ 数字不匹配，请重新输入：" if pending_verify_code else "请输入手机微信上显示的数字："
                if not _stdin_is_interactive() and not callable(getattr(self, "verify_code_provider", None)):
                    # 看板向导/桌面壳是后台管道运行 login 的，用户看不到也回不了这里的提示
                    print("[G4W] 微信要求输入手机上的数字，但当前是后台运行、无法接收输入。\n"
                          "[G4W] 请在控制台手动完成登录：托盘退出 G4W → 双击 tools\\4_login_G4W_ga.bat → 按提示输入数字。")
                    raise WeixinError("login needs a verify code; run tools\\4_login_G4W_ga.bat in a console")
                pending_verify_code = self._ask_verify_code(prompt)
                continue
            elif name == "verify_code_blocked":
                print("[G4W] ⛔ 验证码多次错误，正在刷新二维码…")
                pending_verify_code = ""
                refresh_count += 1
                if refresh_count > self.LOGIN_QR_REFRESH_LIMIT:
                    raise WeixinError("login failed: verify code blocked too many times")
                qr = self._fetch_login_qr()
                qrcode = str(qr.get("qrcode") or "")
                self._write_login_page(str(qr.get("qrcode_img_content") or ""))
                scanned_printed = False
            elif name == "expired":
                refresh_count += 1
                if refresh_count > self.LOGIN_QR_REFRESH_LIMIT:
                    raise WeixinError("login failed: QR code expired too many times")
                print(f"[G4W] ⏳ 二维码已过期，正在刷新…({refresh_count}/{self.LOGIN_QR_REFRESH_LIMIT})")
                qr = self._fetch_login_qr()
                qrcode = str(qr.get("qrcode") or "")
                self._write_login_page(str(qr.get("qrcode_img_content") or ""))
                scanned_printed = False
                last_status = ""
            elif name == "scaned_but_redirect":
                host = str(status.get("redirect_host") or "").strip()
                if host:
                    # 官方实现是 `https://${redirect_host}`；这里额外兼容服务端带上 scheme 的情况
                    current_base = host if host.startswith(("http://", "https://")) else f"https://{host}"
                    print(f"[G4W] 服务端要求切换到 {current_base}，已切换轮询主机")
                last_status = ""
            elif name == "binded_redirect":
                raise WeixinError(
                    "该微信账号已绑定过其他 OpenClaw 实例（服务端返回 binded_redirect）。"
                    "请先在微信「ClawBot / 龙虾插件」里解绑旧实例，或换一个微信账号后重试。"
                )
            elif name == "confirmed":
                account = {
                    "accountId": status["ilink_bot_id"],
                    "token": status["bot_token"],
                    "baseUrl": status.get("baseurl") or current_base,
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
                print(f"[G4W] ✅ 登录成功，账号 {account['accountId']}")
                return account
            else:
                if name:
                    raw = json.dumps(status, ensure_ascii=False)[:200]
                    print(f"[G4W] 未识别的登录状态 {name!r}（原始响应：{raw}）")
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
            {"get_updates_buf": sync},
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
        items = raw.get("item_list") or []
        if os.environ.get("G4W_DEBUG_INBOUND_ITEMS") == "1":
            # 只读诊断（默认关闭）：打印收到的 item 类型与字段名，**不打印内容**。
            # 用途：确认服务端是否会下发“链接/卡片”这类 item（当前 normalize 只认 1/3，
            # 附件只认 2/4/5，其余会被静默丢弃 → 先确认有没有，再决定要不要支持）。
            try:
                seen = [(int(item.get("type", 0) or 0), sorted(k for k in item.keys())) for item in items]
                print(f"[G4W] inbound items: {seen}")
            except Exception:
                pass
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
        if len(chunks) > 1 and not SPLIT_MARKER_RE.search(str(text or "")):
            _raw_reply = str(text or "")
            print("[G4W] reply had no <split/> -> punctuation fallback (" + str(len(chunks)) + " bubbles, " + str(len(_raw_reply)) + " chars)")
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
        failed_chunks = []
        for index, chunk in enumerate(send_chunks):
            compact_chunk = str(chunk or "") if preserve_markdown else strip_sentence_tail_chinese_full_stops(chunk)
            compact_chunk = compact_chunk or "Completed."
            client_id = (
                f"pycb-{uuid.uuid5(uuid.NAMESPACE_URL, f'{delivery_id}:{index}')}"
                if delivery_id else f"pycb-{uuid.uuid4()}"
            )
            payload = {
                "msg": {
                    "from_user_id": "", "to_user_id": sender_id, "client_id": client_id,
                    "message_type": 2, "message_state": 2,
                    "item_list": [{"type": 1, "text_item": {"text": compact_chunk}}],
                    "context_token": token,
                }
            }
            # 网络抖动（Clash 掐 TLS 时常见 SSL EOF / 连接被关）不应该让整轮剩余气泡全部丢失：
            # 单条最多重试 3 次，仍失败就把这条和后面所有未发送的内容整体转为 deferred，等下次补发。
            sent_ok = False
            last_error = ""
            for attempt in range(3):
                try:
                    _post(account["baseUrl"], "ilink/bot/sendmessage", account["token"], payload, timeout=10)
                    sent_ok = True
                    break
                except Exception as error:
                    last_error = f"{type(error).__name__}: {error}"
                    if attempt < 2:
                        time.sleep(0.5 if attempt == 0 else 1.5)
            if not sent_ok:
                print(
                    "[G4W] sendmessage failed at bubble %d/%d after 3 attempts -> deferring %d remaining chunk(s): %s"
                    % (index + 1, len(send_chunks), len(send_chunks) - index, last_error[:160])
                )
                failed_chunks.append(compact_chunk)
                failed_chunks.extend(str(item) for item in send_chunks[index + 1:] if str(item or "").strip())
                break
            delivered.append(compact_chunk)
            delivered_chunks.append({
                "bubble": sent_count + index + 1,
                "text": compact_chunk,
                "clientId": client_id,
                "sentAt": time.time(),
            })
            time.sleep(0.35)
        state["sentCount"] = sent_count + len(delivered)
        if failed_chunks:
            remainder = "\n\n".join(failed_chunks)
            deferred_text = (deferred_text + "\n\n" + remainder).strip() if deferred_text else remainder
            state["heldChunks"] = []
        held_text = join_delivery_chunks(state.get("heldChunks") or [], bool(state.get("preserveMarkdown")))
        if round_final:
            self.delivery_states.pop(budget_key, None)
        result = {
            "deliveredText": ("\n\n" if preserve_markdown else "\n").join(delivered),
            "deliveredChunks": delivered_chunks,
            "deferredText": deferred_text,
            "failedBubbles": failed_chunks,
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
            {"ilink_user_id": sender_id, "context_token": token},
            timeout=10,
        )
        typing_ticket = str(config_response.get("typing_ticket") or "").strip()
        if not typing_ticket:
            return
        _post(
            account["baseUrl"],
            "ilink/bot/sendtyping",
            account["token"],
            {"ilink_user_id": sender_id, "typing_ticket": typing_ticket, "status": int(status)},
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
