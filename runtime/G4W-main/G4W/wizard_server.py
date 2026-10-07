"""G4W 首次配置引导服务（零第三方依赖，用包内便携 Python 直接运行）。

    python3 -m G4W.wizard_server --host 127.0.0.1 --port 18180

职责：
  1. 渲染四步向导页（与 Windows 桌面壳向导同语义）
  2. 一键安装运行环境：独立 venv（runtime/app/.venv）+ 依赖（包内 wheels 优先）
  3. 保存 API Key / 环境配置（直接写文件，无需 venv）
  4. 扫码登录（用 venv 解释器跑 python -m G4W login）
  5. 四步完成后自动拉起真正的看板，并让出端口，实现同端口无缝切换
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[3]
MAIN_DIR = PACKAGE_ROOT / "runtime" / "G4W-main"
APP_DIR = PACKAGE_ROOT / "runtime" / "app"
PREPARE_LOG = PACKAGE_ROOT / "runtime" / "wizard-prepare.log"
DASHBOARD_LOG = PACKAGE_ROOT / "runtime" / "g4w-dashboard.log"
_PREPARE_THREAD: threading.Thread | None = None
_HANDOFF_LOCK = threading.Lock()
_HANDOFF_REQUESTED = False


def bootstrap_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(MAIN_DIR) + os.pathsep + str(APP_DIR)
    env["G4W_HOME"] = str(MAIN_DIR)
    env["G4W_APP_DIR"] = str(APP_DIR)
    env["G4W_STATE_DIR"] = str(PACKAGE_ROOT / "runtime" / "G4W-data")
    env["G4W_WORKSPACE_ROOT"] = str(PACKAGE_ROOT)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    return env


def _log(message: str) -> None:
    try:
        PREPARE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(PREPARE_LOG, "a", encoding="utf-8", errors="replace") as handle:
            handle.write("[wizard] " + time.strftime("%H:%M:%S") + " " + message + chr(10))
    except OSError:
        pass


def prepare_log_tail(limit: int = 30) -> str:
    try:
        lines = PREPARE_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return chr(10).join(lines[-limit:])


def _run_prepare() -> None:
    script = PACKAGE_ROOT / "tools" / "linux" / "1_prepare.sh"
    _log("开始安装运行环境（独立 venv + 依赖）")
    if not script.is_file():
        _log("找不到 " + str(script))
        return
    try:
        with open(PREPARE_LOG, "a", encoding="utf-8", errors="replace") as out:
            subprocess.run(["bash", str(script)], cwd=str(PACKAGE_ROOT), env=bootstrap_env(),
                           stdout=out, stderr=out, timeout=3600)
        _log("安装流程结束")
    except Exception as error:  # noqa: BLE001
        _log("安装异常: " + repr(error))


def start_prepare() -> dict:
    global _PREPARE_THREAD
    if _PREPARE_THREAD is not None and _PREPARE_THREAD.is_alive():
        return {"ok": True, "running": True}
    _PREPARE_THREAD = threading.Thread(target=_run_prepare, daemon=True)
    _PREPARE_THREAD.start()
    return {"ok": True, "running": True}


def _venv_python() -> Path:
    venv = PACKAGE_ROOT / "runtime" / "app" / ".venv"
    candidate = venv / "bin" / "python" if os.name != "nt" else venv / "Scripts" / "python.exe"
    return candidate


def env_ready() -> bool:
    venv_py = _venv_python()
    if not venv_py.is_file():
        return False
    try:
        result = subprocess.run([str(venv_py), "-c", "import requests, aiohttp, psutil"],
                                capture_output=True, timeout=60)
        return result.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _spawn_dashboard(port: int) -> bool:
    """环境就绪后拉起真正的看板（端口已由本进程释放，子进程稍等再绑定）。"""
    venv_py = _venv_python()
    log = open(DASHBOARD_LOG, "a", encoding="utf-8", errors="replace")
    inner = "sleep 2; exec '" + str(venv_py) + "' -B -u -m G4W.dashboard.server --host 127.0.0.1 --port " + str(port)
    try:
        proc = subprocess.Popen(["bash", "-c", inner], cwd=str(MAIN_DIR), env=bootstrap_env(),
                                stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                start_new_session=True)
        (PACKAGE_ROOT / "runtime" / "g4w-dashboard.pid").write_text(str(proc.pid) + chr(10), encoding="utf-8")
        _log("已安排看板接管端口 " + str(port))
        return True
    except Exception as error:  # noqa: BLE001
        _log("拉起看板失败: " + repr(error))
        return False


class WizardHandler(BaseHTTPRequestHandler):
    server_version = "G4W-Wizard"

    def log_message(self, fmt, *args):  # noqa: D102
        return

    # ---- helpers ----
    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json_message(self, value, status: int = 200) -> None:
        self._send(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8", status)

    def _read_body(self) -> dict:
        length = min(int(self.headers.get("Content-Length", "0") or 0), 1_000_000)
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _payload(self) -> dict:
        from .core.setup_state import setup_status

        status = setup_status()
        status["prepareLog"] = prepare_log_tail(14)
        status["phase"] = "env" if not status["prepare"]["ok"] else ("config" if not status["all_ok"] else "handoff")
        status["bootstrap"] = True
        return status

    def _maybe_handoff(self, port: int) -> bool:
        global _HANDOFF_REQUESTED
        from .core.setup_state import setup_status

        if not setup_status()["all_ok"] or not env_ready():
            return False
        with _HANDOFF_LOCK:
            if _HANDOFF_REQUESTED:
                return True
            _HANDOFF_REQUESTED = True
        _log("四步已完成，准备把端口让给看板")
        threading.Thread(target=_delayed_shutdown, args=(self.server,), daemon=True).start()
        return True

    # ---- routes ----
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        port = int(getattr(self.server, "wizard_port", 18180))
        if path == "/api/setup/status":
            payload = self._payload()
            handoff = self._maybe_handoff(port)
            payload["handoff"] = handoff
            self._send_json_message(payload)
            return
        if path == "/api/setup/login/status":
            from .dashboard.setup_wizard import _runner

            self._send_json_message(_runner.status())
            return
        if path in ("/", "/setup", "/index.html"):
            if self._maybe_handoff(port):
                body = ("<!doctype html><meta charset='utf-8'><title>G4W</title>"
                        "<body style='font:14px sans-serif;padding:40px'>环境已就绪，正在切换到看板…"
                        "<script>setTimeout(function(){location.href='/'},2000)</script>")
                self._send(body.encode("utf-8"), "text/html; charset=utf-8")
                return
            from .dashboard.setup_wizard import render_page

            self._send(render_page(), "text/html; charset=utf-8")
            return
        self._send_json_message({"ok": False, "error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        port = int(getattr(self.server, "wizard_port", 18180))
        try:
            if path == "/api/setup/prepare":
                self._send_json_message(start_prepare())
                return
            if path == "/api/setup/key":
                from .dashboard.setup_wizard import save_key

                self._send_json_message(save_key(self._read_body().get("api_key", "")))
                return
            if path == "/api/setup/key/skip":
                from .dashboard.setup_wizard import skip_key

                self._send_json_message(skip_key())
                return
            if path == "/api/setup/env":
                from .dashboard.setup_wizard import save_env

                self._send_json_message(save_env(self._read_body().get("values") or {}))
                return
            if path == "/api/setup/login/start":
                from .dashboard.setup_wizard import _runner

                self._send_json_message(_runner.start())
                return
            if path == "/api/setup/finish":
                self._send_json_message({"ok": True, "handoff": self._maybe_handoff(port)})
                return
        except Exception as error:  # noqa: BLE001
            self._send_json_message({"ok": False, "error": repr(error)}, 500)
            return
        self._send_json_message({"ok": False, "error": "not found"}, 404)


def _delayed_shutdown(server) -> None:
    time.sleep(1.0)
    try:
        server.shutdown()
    except Exception:  # noqa: BLE001
        os._exit(0)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    host = "127.0.0.1"
    port = 18180
    if "--host" in argv:
        host = argv[argv.index("--host") + 1]
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    state_dir = PACKAGE_ROOT / "runtime" / "G4W-data"
    state_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("G4W_STATE_DIR", str(state_dir))
    os.environ.setdefault("G4W_HOME", str(MAIN_DIR))
    os.environ.setdefault("G4W_WORKSPACE_ROOT", str(PACKAGE_ROOT))

    server = ThreadingHTTPServer((host, port), WizardHandler)
    server.wizard_port = port  # type: ignore[attr-defined]
    print("[G4W] 初始化向导已启动：http://" + host + ":" + str(port) + "/", flush=True)
    print("[G4W] 请在浏览器完成四步（安装环境 → API Key → 环境配置 → 扫码登录）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if _HANDOFF_REQUESTED:
            print("[G4W] 环境就绪 → 正在启动看板（本向导即将退出）", flush=True)
            _spawn_dashboard(port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
