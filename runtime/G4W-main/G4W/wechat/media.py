import base64
import hashlib
import mimetypes
import os
import re
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .aes import decrypt_ecb_pkcs7, encrypt_ecb_pkcs7


MEDIA_TYPES = {"image": 1, "video": 2, "file": 3}
SHANGHAI = timezone(timedelta(hours=8))


def normalize_attachment_item(item: dict, index: int) -> dict | None:
    item_type = int(item.get("type", 0) or 0)
    mapping = {2: ("image", "image_item"), 4: ("file", "file_item"), 5: ("video", "video_item")}
    resolved = mapping.get(item_type)
    if not resolved:
        return None
    kind, field = resolved
    body = item.get(field) or {}
    if not isinstance(body, dict):
        return None
    media = body.get("media") if isinstance(body.get("media"), dict) else {}
    direct_urls = []
    for value in (body.get("url"), body.get("download_url"), body.get("cdn_url"), media.get("url"), media.get("download_url"), media.get("cdn_url")):
        text = str(value or "").strip()
        if text and text not in direct_urls:
            direct_urls.append(text)
    return {
        "kind": kind,
        "itemType": item_type,
        "index": index,
        "fileName": str(body.get("file_name") or body.get("filename") or item.get("file_name") or item.get("filename") or "").strip(),
        "sizeBytes": _positive_int(body.get("len") or body.get("file_size") or body.get("size") or body.get("video_size") or item.get("len")),
        "directUrls": direct_urls,
        "mediaRef": {
            "encryptQueryParam": str(media.get("encrypt_query_param") or media.get("encrypted_query_param") or body.get("encrypt_query_param") or body.get("encrypted_query_param") or item.get("encrypt_query_param") or item.get("encrypted_query_param") or "").strip(),
            "aesKey": str(media.get("aes_key") or body.get("aes_key") or item.get("aes_key") or "").strip(),
            "aesKeyHex": str(body.get("aeskey") or body.get("aes_key_hex") or item.get("aeskey") or "").strip(),
            "encryptType": int(media.get("encrypt_type", body.get("encrypt_type", item.get("encrypt_type", 1))) or 0),
            "fileKey": str(media.get("filekey") or body.get("filekey") or item.get("filekey") or "").strip(),
        },
    }


def extract_attachments(items: list[dict]) -> list[dict]:
    result = []
    for index, item in enumerate(items or []):
        normalized = normalize_attachment_item(item or {}, index)
        if normalized:
            result.append(normalized)
    return result


class AttachmentStore:
    def __init__(self, state_dir: Path, cdn_base_url: str):
        self.state_dir = Path(state_dir).resolve()
        self.cdn_base_url = str(cdn_base_url or "").rstrip("/")

    def persist_all(self, attachments: list[dict], message_id: str = "", received_at: str = "") -> dict:
        saved, failed = [], []
        for attachment in attachments or []:
            try:
                saved.append(self.persist(attachment, message_id, received_at))
            except Exception as error:
                failed.append({"kind": attachment.get("kind", "file"), "sourceFileName": attachment.get("fileName", ""), "reason": str(error)[:500]})
        return {"saved": saved, "failed": failed}

    def persist(self, attachment: dict, message_id: str = "", received_at: str = "") -> dict:
        data, content_type = self._download(attachment)
        data = self._decrypt(data, attachment, content_type)
        detected_extension = _detect_extension(data)
        if detected_extension:
            content_type = _content_type_from_extension(detected_extension) or content_type
        day = _date_folder(received_at)
        target_dir = self.state_dir / "inbox" / day
        target_dir.mkdir(parents=True, exist_ok=True)
        file_name = _target_name(attachment, data, content_type, message_id)
        path = _write_unique(target_dir, file_name, data)
        return {
            "kind": attachment.get("kind", "file"),
            "contentType": content_type,
            "isImage": attachment.get("kind") == "image" or content_type.startswith("image/"),
            "sourceFileName": attachment.get("fileName", ""),
            "fileName": path.name,
            "absolutePath": str(path),
            "relativePath": path.relative_to(self.state_dir).as_posix(),
            "sizeBytes": len(data),
        }

    def _download(self, attachment: dict) -> tuple[bytes, str]:
        candidates = list(attachment.get("directUrls") or [])
        ref = attachment.get("mediaRef") or {}
        query = str(ref.get("encryptQueryParam") or "").strip()
        if query and self.cdn_base_url:
            base = f"{self.cdn_base_url}/download?encrypted_query_param={urllib.parse.quote(query, safe='')}"
            candidates.append(base)
            if ref.get("fileKey"):
                candidates.append(base + "&filekey=" + urllib.parse.quote(str(ref["fileKey"]), safe=""))
        if not candidates:
            raise ValueError("attachment did not include a supported download reference")
        last_error = None
        for url in candidates:
            try:
                request = urllib.request.Request(url, headers={"Accept": "*/*"})
                with urllib.request.urlopen(request, timeout=30) as response:
                    return response.read(), str(response.headers.get_content_type() or "application/octet-stream").lower()
            except Exception as error:
                last_error = error
        raise RuntimeError(f"attachment download failed: {last_error}")

    @staticmethod
    def _decrypt(data: bytes, attachment: dict, content_type: str) -> bytes:
        ref = attachment.get("mediaRef") or {}
        if int(ref.get("encryptType", 0) or 0) != 1:
            return data
        keys = _decode_key_candidates(ref.get("aesKeyHex"), ref.get("aesKey"))
        if not keys:
            return data
        for key in keys:
            try:
                return decrypt_ecb_pkcs7(data, key)
            except Exception:
                continue
        if _detect_extension(data) or content_type.startswith("text/"):
            return data
        raise ValueError("failed to decrypt attachment payload")


class MediaSender:
    def __init__(self, channel, allowed_roots: list[Path]):
        self.channel = channel
        self.allowed_roots = [Path(root).resolve() for root in allowed_roots]

    def send(self, sender_id: str, context_token: str, file_path: str) -> dict:
        path = Path(file_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"file not found: {path}")
        if not any(path == root or root in path.parents for root in self.allowed_roots):
            raise PermissionError("file is outside G4W allowed roots")
        return self.channel.send_file(sender_id, path, context_token)


def send_weixin_file(channel, sender_id: str, path: Path, context_token: str = "", delivery_id: str = "") -> dict:
    account = channel.resolve_account()
    token = context_token or channel.runtime.read().get("contextTokens", {}).get(sender_id, "")
    if not token:
        raise ValueError(f"missing context_token for {sender_id}")
    plaintext = Path(path).read_bytes()
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    kind = "image" if mime.startswith("image/") else "video" if mime.startswith("video/") else "file"
    key = os.urandom(16)
    ciphertext = encrypt_ecb_pkcs7(plaintext, key)
    file_key = os.urandom(16).hex()
    upload = channel._post_json("ilink/bot/getuploadurl", {
        "filekey": file_key,
        "media_type": MEDIA_TYPES[kind],
        "to_user_id": sender_id,
        "rawsize": len(plaintext),
        "rawfilemd5": hashlib.md5(plaintext).hexdigest(),
        "filesize": len(ciphertext),
        "no_need_thumb": True,
        "aeskey": key.hex(),
        "base_info": {"channel_version": "python-G4W-0.3"},
    })
    upload_full_url = str(upload.get("upload_full_url") or "").strip()
    upload_param = str(upload.get("upload_param") or "").strip()
    if upload_full_url:
        url = upload_full_url
    elif upload_param:
        url = f"{channel.config.weixin_cdn_base_url.rstrip('/')}/upload?encrypted_query_param={urllib.parse.quote(upload_param, safe='')}&filekey={urllib.parse.quote(file_key, safe='')}"
    else:
        raise RuntimeError("getuploadurl returned neither upload_full_url nor upload_param")
    request = urllib.request.Request(url, data=ciphertext, headers={"Content-Type": "application/octet-stream"}, method="POST")
    with urllib.request.urlopen(request, timeout=60) as response:
        download_param = str(response.headers.get("x-encrypted-param") or "")
        if response.status != 200 or not download_param:
            raise RuntimeError("CDN upload failed or returned no x-encrypted-param")
    media = {"encrypt_query_param": download_param, "aes_key": base64.b64encode(key.hex().encode()).decode(), "encrypt_type": 1}
    if kind == "image":
        item = {"type": 2, "image_item": {"media": media, "aeskey": key.hex(), "mid_size": len(ciphertext), "hd_size": len(ciphertext)}}
    elif kind == "video":
        item = {"type": 5, "video_item": {"media": media, "video_size": len(ciphertext)}}
    else:
        item = {"type": 4, "file_item": {"media": media, "file_name": path.name, "len": str(len(plaintext))}}
    client_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"G4W-file:{delivery_id}") if delivery_id else uuid.uuid4()
    client_id = f"pycb-file-{client_uuid}"
    channel._post_json("ilink/bot/sendmessage", {"msg": {"from_user_id": "", "to_user_id": sender_id, "client_id": client_id, "message_type": 2, "message_state": 2, "item_list": [item], "context_token": token}, "base_info": {"channel_version": "python-G4W-0.3"}}, timeout=10)
    return {"ok": True, "kind": kind, "fileName": path.name, "sizeBytes": len(plaintext), "clientId": client_id}


def _decode_key_candidates(*values) -> list[bytes]:
    result = []
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        variants = []
        if re.fullmatch(r"[0-9a-fA-F]{32}", text):
            variants.append(bytes.fromhex(text))
        if len(text.encode()) == 16:
            variants.append(text.encode())
        try:
            decoded = base64.b64decode(text, validate=True)
            if len(decoded) == 16:
                variants.append(decoded)
            elif re.fullmatch(rb"[0-9a-fA-F]{32}", decoded.strip()):
                variants.append(bytes.fromhex(decoded.decode()))
        except Exception:
            pass
        for value in variants:
            if value not in result:
                result.append(value)
    return result


def _target_name(attachment: dict, data: bytes, content_type: str, message_id: str) -> str:
    source = _sanitize_name(attachment.get("fileName", ""))
    extension = Path(source).suffix if source else ""
    if not extension:
        extension = _extension_from_content_type(content_type) or _detect_extension(data) or (".png" if attachment.get("kind") == "image" else ".mp4" if attachment.get("kind") == "video" else ".bin")
    if source:
        return source if Path(source).suffix else source + extension
    return _sanitize_name(f"{attachment.get('kind', 'file')}-{message_id or int(time.time())}-{int(attachment.get('index', 0)) + 1}{extension}")


def _sanitize_name(value: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", str(value or "").strip())
    path = Path(name)
    stem = (path.stem or "attachment")[:120]
    return stem + path.suffix[:16]


def _write_unique(root: Path, name: str, data: bytes) -> Path:
    parsed = Path(name)
    for index in range(50):
        suffix = "" if index == 0 else f"-{index + 1}"
        candidate = root / f"{parsed.stem}{suffix}{parsed.suffix}"
        try:
            with candidate.open("xb") as handle:
                handle.write(data)
            return candidate
        except FileExistsError:
            continue
    raise RuntimeError("unable to allocate a unique attachment file name")


def _date_folder(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        parsed = datetime.now(timezone.utc)
    return parsed.astimezone(SHANGHAI).date().isoformat()


def _extension_from_content_type(value: str) -> str:
    return {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp", "video/mp4": ".mp4", "application/pdf": ".pdf", "text/plain": ".txt"}.get(str(value).split(";", 1)[0].lower(), "")


def _content_type_from_extension(value: str) -> str:
    return {".png": "image/png", ".jpg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp", ".mp4": "video/mp4", ".pdf": "application/pdf"}.get(str(value).lower(), "")


def _detect_extension(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"): return ".png"
    if data.startswith(b"\xff\xd8\xff"): return ".jpg"
    if data.startswith(b"GIF8"): return ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP": return ".webp"
    if data[4:8] == b"ftyp": return ".mp4"
    if data.startswith(b"%PDF-"): return ".pdf"
    return ""


def _positive_int(value) -> int:
    try:
        parsed = int(value)
        return parsed if parsed > 0 else 0
    except Exception:
        return 0
