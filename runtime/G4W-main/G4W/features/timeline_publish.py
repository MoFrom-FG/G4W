import base64
import hashlib
import json
import os
import secrets
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.request
import zipfile
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .timeline_analytics import build_timeline_views


INDEX_HTML = """<!doctype html>
<html lang="{locale}">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Timeline for Agent</title>
  <link rel="stylesheet" href="./assets/dashboard.css" />
</head>
<body>
  <div id="root"></div>
  <script src="./assets/dashboard.js"></script>
</body>
</html>"""


class _CdpWebSocket:
    def __init__(self, url: str):
        from urllib.parse import urlparse
        parsed = urlparse(url)
        self.sock = socket.create_connection((parsed.hostname, parsed.port), timeout=10)
        self.sock.settimeout(20)
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        path = parsed.path + (("?" + parsed.query) if parsed.query else "")
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {parsed.hostname}:{parsed.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        response = self._recv_until(b"\r\n\r\n")
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"CDP websocket handshake failed: {response[:200]!r}")
        self.next_id = 0

    def _recv_until(self, marker: bytes) -> bytes:
        data = b""
        while marker not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("CDP websocket closed")
            data += chunk
        return data

    def _recv_exact(self, size: int) -> bytes:
        data = b""
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                raise ConnectionError("CDP websocket closed")
            data += chunk
        return data

    def _send_frame(self, payload: bytes, opcode: int = 1) -> None:
        mask = secrets.token_bytes(4)
        size = len(payload)
        header = bytearray([0x80 | opcode])
        if size < 126:
            header.append(0x80 | size)
        elif size <= 0xFFFF:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", size))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", size))
        header.extend(mask)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _recv_message(self) -> str:
        fragments = []
        while True:
            first, second = self._recv_exact(2)
            fin, opcode = bool(first & 0x80), first & 0x0F
            masked, size = bool(second & 0x80), second & 0x7F
            if size == 126:
                size = struct.unpack("!H", self._recv_exact(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(size)
            if mask:
                payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            if opcode == 8:
                raise ConnectionError("CDP websocket closed")
            if opcode == 9:
                self._send_frame(payload, opcode=10)
                continue
            if opcode in (1, 0):
                fragments.append(payload)
                if fin:
                    return b"".join(fragments).decode("utf-8", errors="replace")

    def call(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        call_id = self.next_id
        self._send_frame(json.dumps({"id": call_id, "method": method, "params": params or {}}, separators=(",", ":")).encode("utf-8"))
        while True:
            message = json.loads(self._recv_message())
            if message.get("id") != call_id:
                continue
            if message.get("error"):
                raise RuntimeError(f"CDP {method} failed: {message['error']}")
            return message.get("result") or {}

    def close(self) -> None:
        try:
            self._send_frame(b"", opcode=8)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


class TimelinePublisher:
    def __init__(self, timeline_store, root: Path, locale: str = "zh-CN", theme: str = "default"):
        self.timeline = timeline_store
        self.root = Path(root)
        self.site_dir = self.root / "site"
        self.screenshot_dir = self.root / "screenshots"
        self.locale = str(locale or "zh-CN")
        self.theme = str(theme or "default").lower()
        if self.theme not in ("default", "neko"):
            self.theme = "default"
        assets_dir = Path(__file__).resolve().parents[1] / "assets"
        themed = assets_dir / f"timeline-dashboard-assets-{self.theme}.zip"
        self.asset_archive = themed if themed.is_file() else assets_dir / "timeline-dashboard-assets.zip"
        self.server = None
        self.thread = None

    @staticmethod
    def _mtime(path: Path) -> str:
        try:
            return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")
        except OSError:
            return ""

    def _ensure_assets(self) -> dict:
        assets = self.site_dir / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        if not self.asset_archive.is_file():
            legacy = self.root.parent / "legacy-import" / "timeline" / "site" / "assets"
            if legacy.is_dir():
                for name in ("dashboard.js", "dashboard.css"):
                    source = legacy / name
                    if source.is_file():
                        shutil.copy2(source, assets / name)
            if not all((assets / name).is_file() for name in ("dashboard.js", "dashboard.css")):
                raise FileNotFoundError(f"timeline dashboard asset archive is missing: {self.asset_archive}")
            return {"source": "legacy", "archive": ""}
        archive_hash = hashlib.sha256(self.asset_archive.read_bytes()).hexdigest()
        marker = assets / ".asset-version"
        try:
            current_hash = marker.read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            current_hash = ""
        if current_hash != archive_hash:
            with zipfile.ZipFile(self.asset_archive) as package:
                allowed = {"dashboard.js", "dashboard.css"}
                for member in package.infolist():
                    name = Path(member.filename).name
                    if name not in allowed or member.is_dir():
                        continue
                    target = assets / name
                    with package.open(member) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
            marker.write_text(archive_hash + "\n", encoding="utf-8")
        return {"source": "archive", "archive": str(self.asset_archive), "sha256": archive_hash}

    def build(self) -> dict:
        self.site_dir.mkdir(parents=True, exist_ok=True)
        asset_info = self._ensure_assets()
        state = self.timeline.merged_state()
        facts_path = self.timeline.path
        taxonomy_path = facts_path.with_name("timeline-taxonomy.json")
        if not taxonomy_path.is_file() and self.timeline.legacy_path:
            taxonomy_path = self.timeline.legacy_path.with_name("timeline-taxonomy.json")
        facts_updated = max(
            (self._mtime(path) for path in (facts_path, self.timeline.legacy_path) if path),
            default="",
        )
        taxonomy_updated = self._mtime(taxonomy_path)
        data = build_timeline_views(state, {
            "updatedAt": max(facts_updated, taxonomy_updated),
            "factsUpdatedAt": facts_updated,
            "taxonomyUpdatedAt": taxonomy_updated,
        }, locale=self.locale)
        data_file = self.site_dir / "dashboard-data.json"
        data_file.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        page = self.site_dir / "index.html"
        page.write_text(INDEX_HTML.format(locale=self.locale) + "\n", encoding="utf-8")
        event_count = sum(len((day or {}).get("events") or []) for day in state.get("facts", {}).values())
        return {
            "ok": True, "siteDir": str(self.site_dir), "indexFile": str(page),
            "dataFile": str(data_file), "dayCount": len(state.get("facts") or {}),
            "eventCount": event_count, "assets": asset_info,
            "theme": self.theme, "locale": self.locale,
        }

    def serve(self, host: str = "127.0.0.1", port: int = 0) -> dict:
        self.build()
        if self.server:
            return {"ok": True, "url": f"http://{host}:{self.server.server_port}/", "alreadyRunning": True}
        directory = str(self.site_dir)

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=directory, **kwargs)

            def log_message(self, format, *args):
                return

        self.server = ThreadingHTTPServer((host, int(port)), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="G4W-timeline-server")
        self.thread.start()
        return {"ok": True, "url": f"http://{host}:{self.server.server_port}/", "siteDir": str(self.site_dir)}

    def screenshot(
        self,
        output_file: str = "",
        width: int = 1680,
        height: int = 1400,
        range_name: str = "week",
        date_value: str = "",
        month_value: str = "",
    ) -> dict:
        self.build()
        browser = self._browser()
        if not browser:
            raise RuntimeError("Edge/Chrome headless browser was not found")
        view_mode = str(range_name or "week").strip().lower()
        if view_mode not in ("day", "week", "month"):
            view_mode = "week"
        selected_key = str(month_value or "").strip() if view_mode == "month" else str(date_value or "").strip()
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        if output_file:
            target = Path(output_file).expanduser().resolve()
        else:
            suffix = selected_key or time.strftime("%Y%m%d-%H%M%S")
            target = self.screenshot_dir / f"timeline-{view_mode}-{suffix}-{int(time.time() * 1000)}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        profile = Path(tempfile.mkdtemp(prefix="G4W-edge-"))
        temporary_server = None
        process = None
        cdp = None
        try:
            if self.server:
                url = f"http://127.0.0.1:{self.server.server_port}/"
            else:
                directory = str(self.site_dir)

                class Handler(SimpleHTTPRequestHandler):
                    def __init__(self, *args, **kwargs):
                        super().__init__(*args, directory=directory, **kwargs)

                    def log_message(self, format, *args):
                        return

                temporary_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                threading.Thread(target=temporary_server.serve_forever, daemon=True).start()
                url = f"http://127.0.0.1:{temporary_server.server_port}/"
            port = self._free_port()
            process = subprocess.Popen([
                browser, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run",
                "--disable-extensions", "--hide-scrollbars", "--force-color-profile=srgb",
                f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
                f"--window-size={max(320, int(width))},{max(240, int(height))}", url,
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            page_info = self._wait_for_debug_page(port, url, timeout=20)
            cdp = _CdpWebSocket(page_info["webSocketDebuggerUrl"])
            cdp.call("Page.enable")
            cdp.call("Runtime.enable")
            cdp.call("Emulation.setDeviceMetricsOverride", {
                "width": max(320, int(width)), "height": max(240, int(height)),
                # A 2x capture produces ~1.5-2MB timeline PNGs and makes the
                # WeChat CDN/sendmessage path much more prone to long retries.
                # The dashboard itself is capped at 1440 CSS pixels, so 1x is
                # still a full-resolution page capture while remaining fast to
                # upload on the bot channel.
                "deviceScaleFactor": 1, "mobile": False,
            })
            ready_expression = """
                (async () => {
                  if (document.fonts && document.fonts.ready) await document.fonts.ready;
                  const deadline = Date.now() + 15000;
                  while (Date.now() < deadline) {
                    const page = document.querySelector('.page');
                    if (document.readyState === 'complete' && page && page.getBoundingClientRect().width > 100) return true;
                    await new Promise(resolve => setTimeout(resolve, 100));
                  }
                  return false;
                })()
            """
            ready = None
            ready_deadline = time.time() + 20
            while time.time() < ready_deadline:
                try:
                    ready = cdp.call("Runtime.evaluate", {"expression": ready_expression, "awaitPromise": True, "returnByValue": True})
                    break
                except RuntimeError as error:
                    # Edge may expose the target just before the initial
                    # navigation replaces its JavaScript execution context.
                    # Retrying here is safer than failing an otherwise healthy
                    # screenshot request.
                    if "Execution context was destroyed" not in str(error):
                        raise
                    time.sleep(0.2)
            if ready is None:
                raise RuntimeError("timeline dashboard navigation did not stabilize")
            if not ((ready.get("result") or {}).get("value")):
                raise RuntimeError("timeline dashboard did not become ready")
            selection_expression = f"""
                (async () => {{
                  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
                  const view = {json.dumps(view_mode)};
                  const key = {json.dumps(selected_key)};
                  const button = document.querySelector(`[data-range-id="${{view}}"]`);
                  if (!button) return {{ok: false, reason: 'range button not found', view}};
                  button.click();
                  await sleep(350);
                  if (key) {{
                    const trigger = document.querySelector('[data-range-trigger]');
                    if (!trigger) return {{ok: false, reason: 'range trigger not found', view, key}};
                    trigger.click();
                    await sleep(250);
                    const option = Array.from(document.querySelectorAll('[data-range-option-value]'))
                      .find(el => el.getAttribute('data-range-option-value') === key);
                    if (!option) {{
                      return {{
                        ok: false,
                        reason: 'range option not found',
                        view,
                        key,
                        available: Array.from(document.querySelectorAll('[data-range-option-value]'))
                          .map(el => el.getAttribute('data-range-option-value')).filter(Boolean)
                      }};
                    }}
                    option.click();
                    await sleep(650);
                  }}
                  window.scrollTo(0, 0);
                  return {{ok: true, view, key}};
                }})()
            """
            selection = cdp.call("Runtime.evaluate", {
                "expression": selection_expression, "awaitPromise": True, "returnByValue": True,
            })
            selection_result = (selection.get("result") or {}).get("value") or {}
            if not selection_result.get("ok"):
                raise RuntimeError(f"timeline screenshot view selection failed: {selection_result}")
            cdp.call("Runtime.evaluate", {
                "expression": """
                  (() => {
                    const style = document.createElement('style');
                    style.dataset.G4WScreenshot = 'true';
                    style.textContent = `.page { width: min(1440px, calc(100vw - 64px)) !important; padding: 32px !important; }`;
                    document.head.appendChild(style);
                    window.scrollTo(0, 0);
                    return true;
                  })()
                """, "returnByValue": True,
            })
            time.sleep(2.4)
            bounds = cdp.call("Runtime.evaluate", {
                "expression": """
                  (() => {
                    const el = document.querySelector('.page');
                    if (!el) throw new Error('timeline .page target not found');
                    const r = el.getBoundingClientRect();
                    return {
                      x: Math.max(0, Math.floor(r.left + window.scrollX)),
                      y: Math.max(0, Math.floor(r.top + window.scrollY)),
                      width: Math.max(1, Math.ceil(Math.max(r.width, el.scrollWidth))),
                      height: Math.max(1, Math.ceil(Math.max(r.height, el.scrollHeight)))
                    };
                  })()
                """, "returnByValue": True,
            })
            box = (bounds.get("result") or {}).get("value") or {}
            if not box.get("width") or not box.get("height"):
                raise RuntimeError(f"invalid timeline screenshot bounds: {box}")
            captured = cdp.call("Page.captureScreenshot", {
                "format": "png", "fromSurface": True, "captureBeyondViewport": True,
                "clip": {"x": box["x"], "y": box["y"], "width": box["width"], "height": box["height"], "scale": 1},
            })
            target.write_bytes(base64.b64decode(captured.get("data") or ""))
            if not target.is_file() or target.stat().st_size <= 0:
                raise RuntimeError("timeline screenshot produced no image data")
        finally:
            if cdp:
                cdp.close()
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            if temporary_server:
                temporary_server.shutdown()
                temporary_server.server_close()
            shutil.rmtree(profile, ignore_errors=True)
        return {
            "ok": True,
            "outputFile": str(target),
            "sizeBytes": target.stat().st_size,
            "url": url,
            "selector": ".page",
            "captureMode": "headless-edge-cdp-element",
            "deviceScaleFactor": 1,
            "range": view_mode,
            "selectedKey": selected_key,
        }

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    @staticmethod
    def _wait_for_debug_page(port: int, expected_url: str, timeout: float = 20) -> dict:
        deadline = time.time() + timeout
        last_error = ""
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1) as response:
                    pages = json.loads(response.read().decode("utf-8"))
                candidates = [item for item in pages if item.get("type") == "page" and item.get("webSocketDebuggerUrl")]
                exact = next((item for item in candidates if str(item.get("url") or "").rstrip("/") == expected_url.rstrip("/")), None)
                if exact or candidates:
                    return exact or candidates[0]
            except Exception as error:
                last_error = str(error)
            time.sleep(0.1)
        raise RuntimeError(f"headless Edge debugging page did not start: {last_error}")

    @staticmethod
    def _browser() -> str:
        candidates = [
            shutil.which("msedge"), shutil.which("chrome"), shutil.which("chromium"),
            str(Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
            str(Path(os.environ.get("PROGRAMFILES", "")) / "Google" / "Chrome" / "Application" / "chrome.exe"),
        ]
        return next((value for value in candidates if value and Path(value).is_file()), "")
