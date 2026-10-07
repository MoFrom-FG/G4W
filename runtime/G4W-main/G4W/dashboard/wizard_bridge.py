"""把 Windows 桌面壳的向导页（static/wizard.html）通过 HTTP 桥接复用。

Windows 版向导跑在 pywebview 里，页面调用 window.pywebview.api.* ；
这里注入一段同名的 shim，把同样的方法映射到 /api/setup/* 上，
从而 Linux/浏览器里看到的向导与 Windows 完全一致（同一份 HTML）。
"""
from __future__ import annotations

from pathlib import Path

STATIC_DIR = Path(__file__).with_name("static")
WIZARD_PAGE = STATIC_DIR / "wizard.html"

SHIM = """<script>
/* __g4w_bridge : 用 HTTP 端点实现 pywebview 桥，复用桌面壳向导页 */
(function () {
  if (window.pywebview && window.pywebview.api) { return; }
  function post(path, body) {
    return fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}) }).then(function (r) { return r.json(); });
  }
  function get(path) {
    return fetch(path, { cache: 'no-store' }).then(function (r) { return r.json(); });
  }
  window.pywebview = { api: {
    check: function () { return get('/api/setup/status'); },
    prepare_start: function () { return post('/api/setup/prepare', {}); },
    prepare_log: function () {
      return get('/api/setup/status').then(function (s) {
        var text = (s && s.prepareLog) ? String(s.prepareLog) : '';
        var lines = text ? text.split(String.fromCharCode(10)) : [];
        var ok = !!(s && s.prepare && s.prepare.ok);
        return { lines: lines, done: ok, ok: ok };
      });
    },
    save_key: function (key) { return post('/api/setup/key', { api_key: key }); },
    save_env: function (values) { return post('/api/setup/env', { values: values }); },
    login_start: function () { return post('/api/setup/login/start', {}); },
    login_status: function () { return get('/api/setup/login/status'); },
    open_dashboard: function () { window.location.href = '/'; return { ok: true }; }
  } };
  window.__g4w_bridge = 'http';
  try { window.dispatchEvent(new Event('pywebviewready')); } catch (e) {}
})();
</script>
"""


def bridged_wizard_page() -> bytes | None:
    """返回与 Windows 向导同源的页面（注入 HTTP 桥接）；文件缺失时返回 None。"""
    try:
        html = WIZARD_PAGE.read_text(encoding="utf-8")
    except OSError:
        return None
    if "__g4w_bridge" not in html:
        marker = "</head>"
        html = html.replace(marker, SHIM + marker, 1) if marker in html else SHIM + html
    return html.encode("utf-8")
