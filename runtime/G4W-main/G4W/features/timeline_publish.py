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
from ..core.platform_adapt import first_browser, no_window_kwargs


INDEX_HTML = """<!doctype html>
<html lang="__LOCALE__">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Timeline for Agent</title>
  <script>
    // 主题引导：?theme= 参数 > localStorage > 服务端默认（.env 的 G4W_TIMELINE_UI_THEME）
    // 这里只决定主题并注入 CSS；主题的渲染脚本必须在 #root 之后加载（见 body 末尾），
    // 否则脚本会先于容器执行，页面主体空白。
    (function () {
      var VERS = __ASSETVERS__;
      window.__g4wTimelineAssetVers = VERS;
      var themes = ["default", "neko"];
      var fallback = "__THEME__";
      var theme = "default";
      try {
        var q = new URLSearchParams(location.search).get("theme");
        var s = localStorage.getItem("g4w-timeline-theme");
        if (q && themes.indexOf(q) >= 0) theme = q;
        else if (s && themes.indexOf(s) >= 0) theme = s;
        else if (themes.indexOf(fallback) >= 0) theme = fallback;
      } catch (e) {
        if (themes.indexOf(fallback) >= 0) theme = fallback;
      }
      window.__g4wTimelineTheme = theme;
      var v = VERS[theme] ? "?v=" + VERS[theme] : "";
      document.write('<link rel="stylesheet" href="./assets/' + theme + '/dashboard.css' + v + '" />');
    })();
  </script>
</head>
<body>
  <div id="root"></div>
  <script>
    (function () {
      // 主题脚本：必须在 #root 之后加载，页面才能渲染
      var VERS = window.__g4wTimelineAssetVers || {};
      var theme = window.__g4wTimelineTheme || "default";
      var v = VERS[theme] ? "?v=" + VERS[theme] : "";
      document.write('<script src="./assets/' + theme + '/dashboard.js' + v + '"><\\/script>');
    })();
    (function () {
      // 主题选择器：克隆站点自己的「日期控件」（.range-select）外壳与图标，插到它左侧，
      // 菜单也用主题自己的 .range-select-menu / .range-select-option 样式 —— 保证与页面同款。
      // 站点由各主题自己的脚本渲染，所以这里等它渲染出来再插（最多轮询 6 秒）。
      var THEME = window.__g4wTimelineTheme || "default";
      var LABELS = { default: "默认", neko: "neko" };
      function labelText(value) { return "主题 · " + (LABELS[value] || value); }
      function buildControl(anchor) {
        var wrap = anchor.cloneNode(true);
        wrap.id = "g4w-theme-select";
        // 克隆体会带上主题自己的 data-range-trigger，必须摘掉，否则点它会触发主题的日期选择逻辑
        [].slice.call(wrap.querySelectorAll("[data-range-trigger]")).forEach(function (el) {
          el.removeAttribute("data-range-trigger");
          el.removeAttribute("aria-controls");
        });
        var trigger = wrap.querySelector(".range-select-trigger") || wrap.querySelector("button");
        var icon = trigger ? trigger.querySelector(".range-select-icon") : null;
        if (trigger) {
          trigger.setAttribute("aria-label", "时间轴主题");
          trigger.setAttribute("title", "时间轴主题");
          while (trigger.firstChild) trigger.removeChild(trigger.firstChild);
          var label = document.createElement("span");
          label.setAttribute("style", "pointer-events: none;");
          label.textContent = labelText(THEME);
          trigger.appendChild(label);
          if (icon) trigger.appendChild(icon);
        }
        var menu = document.createElement("div");
        menu.className = "range-select-menu";
        menu.setAttribute("style", "position:absolute;top:calc(100% + 6px);left:0;right:0;display:none;");
        var viewport = document.createElement("div");
        viewport.className = "range-select-viewport";
        ["default", "neko"].forEach(function (value) {
          var opt = document.createElement("button");
          opt.type = "button";
          opt.className = "range-select-option";
          opt.setAttribute("data-theme", value);
          opt.textContent = LABELS[value] || value;
          if (value === THEME) opt.setAttribute("data-state", "checked");
          opt.addEventListener("click", function (event) {
            event.preventDefault();
            event.stopPropagation();
            try { localStorage.setItem("g4w-timeline-theme", value); } catch (e) {}
            location.replace(location.pathname + "?theme=" + value);
          });
          viewport.appendChild(opt);
        });
        menu.appendChild(viewport);
        wrap.appendChild(menu);
        if (trigger) {
          trigger.addEventListener("click", function (event) {
            event.preventDefault();
            event.stopPropagation();
            var open = menu.style.display === "none";
            menu.style.display = open ? "grid" : "none";
            trigger.setAttribute("data-state", open ? "open" : "closed");
            trigger.setAttribute("aria-expanded", open ? "true" : "false");
          });
        }
        document.addEventListener("click", function () {
          menu.style.display = "none";
          if (trigger) trigger.setAttribute("data-state", "closed");
        });
        return wrap;
      }
      // 关键：不能只把自己插成日期控件的兄弟节点 —— 各主题的工具条是
      // justify-content: space-between 的多列布局，那样会被挤到行中间（default 主题就是这样）。
      // 正确做法：把「主题控件 + 日期控件」包进一个 flex 小组，让它整体占据日期原来那一格。
      function dateWrapper() {
        var wraps = [].slice.call(document.querySelectorAll(".range-select"));
        for (var i = 0; i < wraps.length; i++) {
          if (wraps[i].id !== "g4w-theme-select" && wraps[i].querySelector(".range-select-trigger")) return wraps[i];
        }
        return null;
      }
      function ensurePicker() {
        var anchor = dateWrapper();
        if (!anchor || !anchor.parentNode) return false;
        var group = anchor.parentNode.classList && anchor.parentNode.classList.contains("g4w-theme-group")
          ? anchor.parentNode : null;
        var existing = document.getElementById("g4w-theme-select");
        if (!group) {
          group = document.createElement("div");
          group.className = "g4w-theme-group";
          group.setAttribute("style", "display:flex;align-items:center;gap:12px;flex:none;");
          anchor.parentNode.insertBefore(group, anchor);
          group.appendChild(anchor);
        }
        if (existing && existing.parentNode === group && existing.nextElementSibling === anchor) return true;
        // 每次重建：确保类名/图标与当前主题一致（主题切换会整页重载，这里主要是首次插入）
        var control = buildControl(anchor);
        if (existing && existing.parentNode) existing.parentNode.removeChild(existing);
        group.insertBefore(control, anchor);
        return true;
      }
      var tries = 0;
      var timer = setInterval(function () {
        tries += 1;
        var done = ensurePicker();
        // 主题重新渲染可能把我们挤掉/挪走：先密集重试 6 秒，之后低频守护
        if (done && tries > 40) { clearInterval(timer); setInterval(ensurePicker, 2000); }
        if (!done && tries > 40) { clearInterval(timer); }
      }, 150);
    })();
  </script>
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
    THEMES = ("default", "neko")

    def __init__(self, timeline_store, root: Path, locale: str = "zh-CN", theme: str | None = None):
        self.timeline = timeline_store
        self.root = Path(root)
        self.site_dir = self.root / "site"
        self.screenshot_dir = self.root / "screenshots"
        self.locale = str(locale or "zh-CN")
        # theme 显式传入（default/neko）时固定使用；传 None/空时**每次 build 重新读 .env**
        # （G4W_TIMELINE_UI_THEME）。这样看板里切换主题后，主服务/时间线服务无需重启就会用新主题，
        # 也不会因为某个进程内存里还是旧值而把站点主题改回去。
        forced = str(theme or "").strip().lower()
        self._theme_override = forced if forced in self.THEMES else ""
        self.server = None
        self.thread = None

    @property
    def theme(self) -> str:
        if self._theme_override:
            return self._theme_override
        try:
            from ..core.config import Config

            value = str(Config.load().timeline_theme or "default").strip().lower()
        except Exception:
            value = "default"
        return value if value in self.THEMES else "default"

    def asset_archive(self) -> Path:
        """主题资产包：优先本主题 → 其次 default → 最后通用包。

        （历史遗留：通用包 timeline-dashboard-assets.zip 与 neko 版内容相同，所以它只能当
        最后兜底，否则“主题包缺失”会静默变成 neko，而默认语义应该是 default。）
        """
        assets_dir = Path(__file__).resolve().parents[1] / "assets"
        candidates = [
            assets_dir / f"timeline-dashboard-assets-{self.theme}.zip",
            assets_dir / "timeline-dashboard-assets-default.zip",
            assets_dir / "timeline-dashboard-assets.zip",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return candidates[0]

    @staticmethod
    def _mtime(path: Path) -> str:
        try:
            return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")
        except OSError:
            return ""

    def theme_archives(self) -> dict:
        """{theme: archive} —— 每套主题各自的资产包（缺的那套用现有包兜底）。

        历史遗留：通用包 timeline-dashboard-assets.zip 与 neko 版内容相同，所以它只作最后兜底；
        default 优先用 timeline-dashboard-assets-default.zip，避免“缺包时静默变 neko”。
        """
        assets_dir = Path(__file__).resolve().parents[1] / "assets"
        generic = assets_dir / "timeline-dashboard-assets.zip"
        fallback = generic if generic.is_file() else None
        found: dict = {}
        for name in self.THEMES:
            candidate = assets_dir / f"timeline-dashboard-assets-{name}.zip"
            if candidate.is_file():
                found[name] = candidate
        if not found and fallback is not None:
            found = {name: fallback for name in self.THEMES}
        for name in self.THEMES:
            if name not in found:
                base = found.get("default") or found.get("neko") or fallback
                if base is not None:
                    found[name] = base
        return found

    def _ensure_assets(self) -> dict:
        """把**两套**主题的 dashboard.{css,js} 都释放到 site/assets/<theme>/。

        站点页面自带主题下拉，切换只是换一个目录；只释放一套会导致切过去 404。
        """
        assets_root = self.site_dir / "assets"
        assets_root.mkdir(parents=True, exist_ok=True)
        archives = self.theme_archives()
        if not archives:
            legacy = self.root.parent / "legacy-import" / "timeline" / "site" / "assets"
            if legacy.is_dir():
                for name in ("dashboard.js", "dashboard.css"):
                    source = legacy / name
                    if source.is_file():
                        for theme in self.THEMES:
                            target_dir = assets_root / theme
                            target_dir.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(source, target_dir / name)
            if not all((assets_root / theme / "dashboard.css").is_file() for theme in self.THEMES):
                raise FileNotFoundError("timeline dashboard asset archive is missing")
            return {"source": "legacy", "archive": "", "theme": self.theme,
                    "themes": list(self.THEMES), "versions": {}}
        versions: dict = {}
        for theme in sorted(archives):
            archive_path = archives[theme]
            digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            versions[theme] = digest[:12]
            target_dir = assets_root / theme
            target_dir.mkdir(parents=True, exist_ok=True)
            marker = target_dir / ".asset-version"
            try:
                current_hash = marker.read_text(encoding="utf-8", errors="ignore").strip()
            except OSError:
                current_hash = ""
            if current_hash != digest:
                with zipfile.ZipFile(archive_path) as package:
                    allowed = {"dashboard.js", "dashboard.css"}
                    for member in package.infolist():
                        name = Path(member.filename).name
                        if name not in allowed or member.is_dir():
                            continue
                        target = target_dir / name
                        with package.open(member) as source, target.open("wb") as output:
                            shutil.copyfileobj(source, output)
                marker.write_text(digest + "\n", encoding="utf-8")
        # 清理旧版“单主题布局”留下的文件（现在只从 assets/<theme>/ 读取）
        for stale in ("dashboard.js", "dashboard.css", ".asset-version"):
            try:
                (assets_root / stale).unlink()
            except OSError:
                pass
        return {
            "source": "archive", "theme": self.theme, "themes": sorted(archives),
            "archives": {theme: str(path) for theme, path in sorted(archives.items())},
            "versions": versions,
        }

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
        versions = dict((asset_info or {}).get("versions") or {})
        page_html = (INDEX_HTML
                     .replace("__LOCALE__", self.locale)
                     .replace("__THEME__", self.theme)
                     .replace("__ASSETVERS__", json.dumps(versions, ensure_ascii=False)))
        page.write_text(page_html + "\n", encoding="utf-8")
        event_count = sum(len((day or {}).get("events") or []) for day in state.get("facts", {}).values())
        return {
            "ok": True, "siteDir": str(self.site_dir), "indexFile": str(page),
            "dataFile": str(data_file), "dayCount": len(state.get("facts") or {}),
            "eventCount": event_count, "assets": asset_info,
            "theme": self.theme, "themes": (asset_info or {}).get("themes") or list(self.THEMES),
            "locale": self.locale,
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
                **no_window_kwargs())
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
        located = first_browser()
        if located:
            return str(located)
        candidates = [shutil.which("msedge"), shutil.which("chrome"), shutil.which("chromium")]
        return next((value for value in candidates if value and Path(value).is_file()), "")
