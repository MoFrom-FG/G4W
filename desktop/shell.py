# -*- coding: utf-8 -*-
"""G4W 控制中心 —— WebView2 桌面壳（GA 便携包同款方案：pywebview）

职责：拉起看板后端（无控制台窗口）→ 等待就绪 → 弹窗加载 →
      托盘驻留（X 最小化到托盘）→ 托盘"退出"彻底清理后端进程。

- exe 必须放在 G4W 根目录（与 runtime 平级）：靠自身位置定位 python 和数据
- 端口默认 18180；环境变量 G4W_DASHBOARD_PORT 可覆盖
- 若 18180 已有看板在跑（手动启动的）→ 启动时复用；托盘「退出」仍会按端口关掉服务
"""
import ctypes
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from ctypes import wintypes

if getattr(sys, "frozen", False):
    ROOT = os.path.dirname(os.path.abspath(sys.executable))          # exe 所在目录 = G4W 根
    BUNDLE_DIR = getattr(sys, "_MEIPASS", ROOT)                      # PyInstaller 资源解压目录
else:
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # desktop 的上级 = G4W 根
    BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))

PORT = int(os.environ.get("G4W_DASHBOARD_PORT") or 18180)
DASHBOARD_URL = f"http://127.0.0.1:{PORT}"
READY_URL = f"{DASHBOARD_URL}/api/auth/status"
LOCK_PORT = 18200  # 单实例锁

PYTHON = os.path.join(ROOT, "runtime", "python", "python.exe")
G4W_HOME = os.path.join(ROOT, "runtime", "G4W-main")
GA_APP_DIR = os.path.join(ROOT, "runtime", "app")
G4W_STATE_DIR = os.path.join(ROOT, "runtime", "G4W-data")
ICON = os.path.join(BUNDLE_DIR, "icon.ico")
LOADING_FILE = os.path.join(BUNDLE_DIR, "loading.html")
WIZARD_FILE = os.path.join(BUNDLE_DIR, "wizard.html")
PYWEBVIEW_LOG = os.path.join(ROOT, "runtime", "shell-pywebview.log")
# WebView2 Evergreen Bootstrapper 官方下载地址（缺运行时时的引导用）
WEBVIEW2_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"

backend_proc = None      # 本次启动的后端（复用时为 None）
quitting = False


def _log(msg):
    """诊断日志（windowed exe 无 stdout，落盘便于排查）。"""
    try:
        with open(os.path.join(ROOT, "runtime", "shell.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


def msgbox(title, text):
    """简单的原生消息框（仅 Windows）。"""
    try:
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x40)
    except Exception:
        pass


def port_open(port, timeout=0.6):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def acquire_lock():
    """单实例锁：绑定固定端口，失败说明已有实例在跑。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


def request_show_existing():
    """向已有实例发送显示窗口指令；返回是否送达。"""
    try:
        with socket.create_connection(("127.0.0.1", LOCK_PORT), timeout=1.0) as conn:
            conn.sendall(b"show")
            conn.recv(4)  # 等 ack
            return True
    except Exception:
        return False


def _pids_listening_on(port: int) -> list[int]:
    """返回本机监听 port 的进程 PID 列表（Windows netstat）。"""
    pids: list[int] = []
    try:
        # -ano 输出里本地地址可能是 0.0.0.0:18180 / 127.0.0.1:18180 / [::]:18180
        completed = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=8,
        )
        needle = f":{int(port)}"
        for line in (completed.stdout or "").splitlines():
            # 例:  TCP    127.0.0.1:18180    0.0.0.0:0    LISTENING    12345
            if "LISTENING" not in line.upper():
                continue
            parts = line.split()
            if len(parts) < 5:
                continue
            local = parts[1] if parts[0].upper() in {"TCP", "UDP"} else ""
            if not local.endswith(needle):
                continue
            try:
                pid = int(parts[-1])
            except ValueError:
                continue
            if pid > 0 and pid not in pids:
                pids.append(pid)
    except Exception as exc:
        _log(f"pids_listening_on({port}) failed: {type(exc).__name__}: {exc}")
    return pids


def stop_backend(force_port: bool = True):
    """彻底关闭看板后端。

    - 先杀本次壳拉起的 backend_proc
    - 默认再按端口清掉仍占用 18180 的进程（含手动 bat 启动的），
      避免托盘「退出」后服务还在跑、浏览器仍能打开
    """
    global backend_proc
    killed: list[int] = []
    if backend_proc is not None:
        proc, backend_proc = backend_proc, None
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=15)
            killed.append(int(proc.pid))
        except Exception:
            try:
                proc.kill()
                killed.append(int(proc.pid))
            except Exception:
                pass
    if force_port:
        for pid in _pids_listening_on(PORT):
            if pid in killed:
                continue
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=15)
                killed.append(pid)
                _log(f"stop_backend: killed listener pid={pid} on :{PORT}")
            except Exception as exc:
                _log(f"stop_backend: fail pid={pid}: {type(exc).__name__}: {exc}")
    if killed:
        _log(f"stop_backend: done pids={killed}")


def _base_env() -> dict:
    """G4W 子进程标准环境（与 ensure_backend 一致）。"""
    env = os.environ.copy()
    env.update({
        "GA_APP_DIR": GA_APP_DIR,
        "G4W_HOME": G4W_HOME,
        "G4W_STATE_DIR": G4W_STATE_DIR,
        "G4W_WORKSPACE_ROOT": ROOT,
        "PYTHONPATH": ";".join([G4W_HOME, GA_APP_DIR,
                                os.path.join(GA_APP_DIR, ".venv", "Lib", "site-packages")]),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return env


def ensure_backend():
    """端口已占用则复用（不杀）；否则 base python 直启（无窗口，日志落盘）。"""
    global backend_proc
    if port_open(PORT):
        return
    if not os.path.isfile(PYTHON):
        return
    env = _base_env()
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    out = open(os.path.join(ROOT, "runtime", "dashboard.stdout.log"), "a", encoding="utf-8", errors="replace")
    err = open(os.path.join(ROOT, "runtime", "dashboard.stderr.log"), "a", encoding="utf-8", errors="replace")
    backend_proc = subprocess.Popen(
        [PYTHON, "-B", "-u", "-m", "G4W.dashboard.server", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=G4W_HOME, env=env, creationflags=flags,
        stdin=subprocess.DEVNULL, stdout=out, stderr=err,
    )


def wait_ready(timeout=25.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(READY_URL, timeout=1.5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def bootstrap(window):
    """窗口创建后：设置窗口图标，后台线程启动后端并等就绪，然后切换到看板地址。"""
    # pywebview 6.x 的 create_window 没有 icon 参数——通过 WinForms 原生句柄设置
    try:
        from System.Drawing import Icon
        window.native.Icon = Icon(ICON)
    except Exception:
        pass

    def run():
        ensure_backend()
        ready = wait_ready()
        try:
            if ready:
                window.load_url(DASHBOARD_URL)
            else:
                window.load_html(
                    "<body style='background:#10141a;color:#e6a7a7;font:14px sans-serif;"
                    "display:flex;align-items:center;justify-content:center'>"
                    "<div><h2>看板启动失败</h2><p>请确认已运行 1_prepare_G4W_ga.bat，"
                    "或查看 runtime\\dashboard.stderr.log</p></div></body>")
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


def wizard_status() -> dict:
    """首次使用自检：返回各步骤完成状态（向导页与 main 共用）。

    G4W_FORCE_WIZARD=1 时强制进入向导（开发/测试用）。
    """
    force = os.environ.get("G4W_FORCE_WIZARD") == "1"
    venv_py = os.path.join(GA_APP_DIR, ".venv", "Scripts", "python.exe")
    mykey = os.path.join(GA_APP_DIR, "mykey.py")
    env_file = os.path.join(G4W_HOME, ".env")
    accounts_dir = os.path.join(G4W_STATE_DIR, "accounts")
    prepare_ok = not force and os.path.isfile(venv_py)
    key_ok = not force and os.path.isfile(mykey)
    env_ok = False
    env_preset = {}
    if not force and os.path.isfile(env_file):
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                text = f.read()
            env_ok = "G4W_USER_NAME=" in text and "G4W_BOT_NAME=" in text
            for line in text.splitlines():
                if "=" in line:
                    k, _, v = line.partition("=")
                    env_preset[k.strip()] = v.strip().strip('"').strip("'")
        except Exception:
            pass
    login_ok = (not force) and os.path.isdir(accounts_dir) and any(
        f.endswith(".json") for f in os.listdir(accounts_dir))
    return {
        "prepare": {"ok": prepare_ok},
        "key": {"ok": key_ok},
        "env": {"ok": env_ok, "preset": env_preset},
        "login": {"ok": login_ok},
        "all_ok": prepare_ok and key_ok and env_ok and login_ok,
    }


class WizardApi:
    """向导页 js_api：自检 / 准备环境 / 保存配置 / 扫码登录。"""

    def __init__(self):
        self.window = None
        self.bridge_ready = False   # 前端确认 window.pywebview.api 可用后置 True
        self._prepare_proc = None
        self._login_proc = None
        self._prepare_log = os.path.join(ROOT, "runtime", "wizard-prepare.log")
        self._login_log = os.path.join(ROOT, "runtime", "login.log")
        self._qr_page = os.path.join(G4W_STATE_DIR, "login-qrcode.html")

    def _call_initializer(self, func: str, payload: dict) -> dict:
        """子进程调用 G4W.cli.initializer 函数（stdin JSON 传参，避免命令行泄露密钥）。"""
        code = (
            "import sys,json;"
            "from G4W.cli.initializer import %s as f;"
            "print(json.dumps(f(**json.load(sys.stdin)),ensure_ascii=False))"
        ) % func
        try:
            proc = subprocess.run(
                [PYTHON, "-B", "-c", code],
                input=json.dumps(payload, ensure_ascii=False),
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=180, env=_base_env(), cwd=G4W_HOME,
            )
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if proc.returncode != 0:
            return {"ok": False, "error": (proc.stderr or proc.stdout or "")[-400:]}
        try:
            return json.loads(proc.stdout.strip().splitlines()[-1])
        except Exception:
            return {"ok": False, "error": (proc.stdout or "")[-300:]}

    def check(self) -> dict:
        try:
            r = wizard_status()
            _log(f"api check -> prepare={r['prepare']['ok']} key={r['key']['ok']} "
                 f"env={r['env']['ok']} login={r['login']['ok']}")
            return r
        except Exception as exc:
            _log(f"api check ERROR: {type(exc).__name__}: {exc}")
            return {"prepare": {"ok": False}, "key": {"ok": False},
                    "env": {"ok": False, "preset": {}}, "login": {"ok": False},
                    "all_ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def log_event(self, text: str) -> dict:
        """前端向导页上报的状态/错误（桥接就绪、自检失败原因等）→ runtime\\shell.log。

        用于远程排障：对方机器只要把 runtime\\shell.log 发回来，就能判定
        JS 桥接是否就绪、自检究竟是哪一步失败。
        """
        try:
            text = str(text)[:400]
            _log(f"js: {text}")
            if text.startswith("bridge ready"):
                self.bridge_ready = True
        except Exception:
            pass
        return {"ok": True}

    # ---- ① 准备环境 ----
    def prepare_start(self) -> dict:
        if self._prepare_proc is not None and self._prepare_proc.poll() is None:
            return {"ok": False, "error": "准备已在运行中"}
        prep_py = os.path.join(BUNDLE_DIR, "wizard_prepare.py")
        if not os.path.isfile(prep_py) or not os.path.isfile(PYTHON):
            return {"ok": False, "error": "环境不完整：缺少内置 Python 或向导组件"}
        try:
            out = open(self._prepare_log, "w", encoding="utf-8", errors="replace")  # 清空旧日志，本次准备从头写
        except OSError:
            out = subprocess.DEVNULL
        flags = subprocess.CREATE_NO_WINDOW
        self._prepare_proc = subprocess.Popen(
            [PYTHON, "-B", "-u", prep_py, ROOT],
            env=_base_env(), stdout=out, stderr=out, creationflags=flags,
        )
        _log(f"prepare started pid={self._prepare_proc.pid}")
        return {"ok": True}

    def prepare_log(self) -> dict:
        lines = []
        try:
            with open(self._prepare_log, "r", encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()[-200:]
        except Exception:
            pass
        proc_done = self._prepare_proc is not None and self._prepare_proc.poll() is not None
        # 双保险：进程句柄检测 或 日志出现完成标记（防 Popen 状态丢失导致卡死）
        log_done = any("[完成]" in ln or "[错误]" in ln or "[退出码" in ln for ln in lines)
        finished = bool(proc_done or log_done)
        ok = bool(proc_done and self._prepare_proc.returncode == 0)
        if log_done and not proc_done:
            # 日志已到终点但进程句柄异常：以日志为准报告完成，进程留待回收
            ok = bool("[完成]" in lines[-1] if lines else False)
            _log(f"prepare_log fallback: proc_done={proc_done} log_done=True ok={ok}")
        if proc_done and self._prepare_proc.returncode != 0:
            lines.append(f"[退出码 {self._prepare_proc.returncode}]")
        return {"ok": ok, "finished": finished, "log": "\n".join(lines)}

    # ---- ② API Key ----
    def save_key(self, api_key: str) -> dict:
        r = self._call_initializer("configure_ga_key",
                                   {"api_key": api_key, "replace_existing": True})
        _log(f"api save_key -> ok={r.get('ok')} err={str(r.get('error'))[:120] if not r.get('ok') else ''}")
        return r

    # ---- ③ 环境配置 ----
    def save_env(self, values: dict) -> dict:
        r = self._call_initializer("configure_env", {"values": values})
        _log(f"api save_env -> ok={r.get('ok')} err={str(r.get('error'))[:120] if not r.get('ok') else ''}")
        return r

    # ---- ④ 扫码登录 ----
    def login_start(self) -> dict:
        if self._login_proc is not None and self._login_proc.poll() is None:
            return {"ok": True, "running": True}
        try:
            out = open(self._login_log, "a", encoding="utf-8", errors="replace")
        except OSError:
            out = subprocess.DEVNULL
        flags = subprocess.CREATE_NO_WINDOW
        env = _base_env()
        env["G4W_NO_BROWSER"] = "1"   # 向导自渲染二维码，禁止 login 子进程弹浏览器
        self._login_proc = subprocess.Popen(
            [PYTHON, "-B", "-u", "-m", "G4W", "login"],
            cwd=G4W_HOME, env=env, stdout=out, stderr=out, creationflags=flags,
        )
        _log(f"login started pid={self._login_proc.pid}")
        return {"ok": True}
    def login_status(self) -> dict:
        done = wizard_status()["login"]["ok"]
        svg, link = "", ""
        try:
            if os.path.isfile(self._qr_page):
                text = open(self._qr_page, "r", encoding="utf-8", errors="replace").read()
                start = text.find("<svg")
                end = text.find("</svg>")
                if start != -1 and end != -1:
                    svg = text[start:end + 6]
                href = text.find("<a href=\"")
                if href != -1:
                    rest = text[href + 9:]
                    link = rest[:rest.find("\"")]
        except Exception:
            pass
        running = self._login_proc is not None and self._login_proc.poll() is None
        return {"done": done, "running": running, "svg": svg, "link": link}

    # ---- 完成 ----
    def _open_dashboard_worker(self) -> None:
        """后台线程：拉起/复用看板后端 → 等端口就绪 → 切页（v3.0.3）。

        注意：open_dashboard 是 js_api，运行在 WebView2 的 **UI 线程** 上；
        在这里 sleep 等端口会让整个窗口无响应（3.0.2「扫码后看板登录页卡死、
        任务管理器强杀才行」的根因之一）。所以等待必须放在后台线程，
        UI 线程立刻返回，由前端显示「正在启动看板…」。
        """
        try:
            ensure_backend()
            deadline = time.time() + 45.0
            while time.time() < deadline:
                if port_open(PORT):
                    break
                time.sleep(0.5)
            if port_open(PORT):
                _log("open_dashboard worker: backend ready -> load_url")
                self.window.load_url(DASHBOARD_URL)   # 内部 Invoke 到 UI 线程
            else:
                _log("open_dashboard worker: backend NOT ready after 45s")
        except Exception as exc:
            _log(f"open_dashboard worker ERROR: {type(exc).__name__}: {exc}")

    def open_dashboard(self) -> dict:
        try:
            # 立刻返回，等待放到后台线程：UI 线程绝不允许阻塞
            threading.Thread(target=self._open_dashboard_worker, daemon=True).start()
            _log("api open_dashboard -> scheduled async backend start")
            return {"ok": True, "starting": True}
        except Exception as exc:
            _log(f"api open_dashboard ERROR: {type(exc).__name__}: {exc}")
            return {"ok": False, "error": str(exc)}


class WinTray:
    """纯 Win32 托盘（Shell_NotifyIcon，ctypes 实现，零 .NET/pystray 依赖）。

    独立线程创建隐藏窗口 + 消息循环；支持右键菜单、左键双击、
    TaskbarCreated 重注册（Explorer 重启后图标恢复）、气泡提示。
    """

    _WM_TRAYICON = 0x0400 + 20
    _NIM_ADD = 0
    _NIM_MODIFY = 1
    _NIM_DELETE = 2
    _NIF_MESSAGE = 0x1
    _NIF_ICON = 0x2
    _NIF_TIP = 0x4
    _NIF_INFO = 0x10
    _WM_RBUTTONUP = 0x0205
    _WM_LBUTTONDBLCLK = 0x0203
    _MF_STRING = 0
    _TPM_RIGHTBUTTON = 0x2
    _TPM_RETURNCMD = 0x0100
    _LR_LOADFROMFILE = 0x0010
    _IMAGE_ICON = 1

    def __init__(self, icon_path, title, on_show, on_quit):
        self.icon_path = icon_path
        self.title = title
        self.on_show = on_show
        self.on_quit = on_quit
        self._user32 = ctypes.windll.user32
        self._shell32 = ctypes.windll.shell32
        self._hicon = None
        self._hwnd = None
        self._nid = None
        self._taskbar_created = self._user32.RegisterWindowMessageW("TaskbarCreated")
        # 显式 argtypes：64 位指针若按默认 int 传会 OverflowError（"argument N: int too long"）
        u32 = self._user32
        u32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                   ctypes.c_int, ctypes.c_int, wintypes.UINT]
        u32.LoadImageW.restype = wintypes.HANDLE
        u32.RegisterClassW.argtypes = [ctypes.POINTER(self._WNDCLASSW)]
        u32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                        wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                        wintypes.HINSTANCE, ctypes.c_void_p]
        u32.CreateWindowExW.restype = wintypes.HWND
        u32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u32.DefWindowProcW.restype = ctypes.c_ssize_t
        u32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        u32.GetMessageW.restype = ctypes.c_int
        u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        u32.CreatePopupMenu.restype = wintypes.HMENU
        u32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        u32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
        u32.TrackPopupMenu.restype = wintypes.UINT
        u32.DestroyMenu.argtypes = [wintypes.HMENU]
        self._shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(self._NOTIFYICONDATA)]
        self._shell32.Shell_NotifyIconW.restype = wintypes.BOOL
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._started = threading.Event()

    class _NOTIFYICONDATA(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HANDLE),
            ("szTip", ctypes.c_wchar * 128), ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD), ("szInfo", ctypes.c_wchar * 256),
            ("uTimeoutOrVersion", wintypes.UINT), ("szInfoTitle", ctypes.c_wchar * 64),
            ("dwInfoFlags", wintypes.DWORD), ("guidItem", ctypes.c_byte * 16),
            ("hBalloonIcon", wintypes.HANDLE),
        ]

    class _WNDCLASSW(ctypes.Structure):
        """ctypes.wintypes 没有 WNDCLASSW，按 WinUser.h 定义。"""
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", ctypes.c_void_p),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HANDLE),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HANDLE),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    def start(self):
        self._thread.start()
        self._started.wait(3.0)
        return self

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == self._WM_TRAYICON:
            if lparam == self._WM_RBUTTONUP:
                self._show_menu()
                return 0
            if lparam == self._WM_LBUTTONDBLCLK:
                try:
                    self.on_show()
                except Exception:
                    pass
                return 0
        elif msg == self._taskbar_created:
            # Explorer 重启后重注册图标
            try:
                self._shell32.Shell_NotifyIconW(self._NIM_ADD, ctypes.byref(self._nid))
            except Exception:
                pass
            return 0
        return self._user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _show_menu(self):
        try:
            menu = self._user32.CreatePopupMenu()
            self._user32.AppendMenuW(menu, self._MF_STRING, 1, "显示看板")
            self._user32.AppendMenuW(menu, self._MF_STRING, 2, "退出（关闭看板）")
            pos = wintypes.POINT()
            self._user32.GetCursorPos(ctypes.byref(pos))
            cmd = self._user32.TrackPopupMenu(menu, self._TPM_RIGHTBUTTON | self._TPM_RETURNCMD,
                                              pos.x, pos.y, 0, self._hwnd, None)
            self._user32.DestroyMenu(menu)
            if cmd == 1:
                self.on_show()
            elif cmd == 2:
                self.on_quit()
        except Exception as exc:
            _log(f"tray menu error: {exc}")

    def _run(self):
        try:
            user32 = self._user32
            self._hicon = user32.LoadImageW(None, self.icon_path, self._IMAGE_ICON, 32, 32, self._LR_LOADFROMFILE)
            if not self._hicon:
                raise OSError("LoadImage failed")
            # WNDPROC 回调需保持强引用，防止被 GC（LRESULT = LONG_PTR = ssize_t）
            self._wnd_proc_cb = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                                   wintypes.WPARAM, wintypes.LPARAM)(self._wnd_proc)
            wc = self._WNDCLASSW()
            wc.style = 0
            wc.lpfnWndProc = ctypes.cast(self._wnd_proc_cb, ctypes.c_void_p).value
            wc.cbClsExtra = 0
            wc.cbWndExtra = 0
            wc.hInstance = wintypes.HINSTANCE(ctypes.windll.kernel32.GetModuleHandleW(None))
            wc.hIcon = None
            wc.hCursor = None
            wc.hbrBackground = None
            wc.lpszMenuName = None
            wc.lpszClassName = "G4WTrayWindow"
            user32.RegisterClassW(ctypes.byref(wc))
            self._hwnd = user32.CreateWindowExW(0, "G4WTrayWindow", "G4W", 0, 0, 0, 0, 0,
                                                None, None, wc.hInstance, None)
            if not self._hwnd:
                raise OSError("CreateWindow failed")
            nid = self._NOTIFYICONDATA()
            nid.cbSize = ctypes.sizeof(nid)
            nid.hWnd = self._hwnd
            nid.uID = 1
            nid.uFlags = self._NIF_MESSAGE | self._NIF_ICON | self._NIF_TIP
            nid.uCallbackMessage = self._WM_TRAYICON
            nid.hIcon = self._hicon
            nid.szTip = self.title[:127]
            self._nid = nid
            if not self._shell32.Shell_NotifyIconW(self._NIM_ADD, ctypes.byref(nid)):
                raise OSError("Shell_NotifyIcon failed")
            self._started.set()
            _log("tray started (Win32)")
            msg = wintypes.MSG()
            while self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                self._user32.TranslateMessage(ctypes.byref(msg))
                self._user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:
            _log(f"tray FAILED: {type(exc).__name__}: {exc}")
            self._started.set()

    def notify(self, text, title="G4W 控制中心"):
        try:
            if self._nid is not None:
                self._nid.uFlags = self._NIF_INFO
                self._nid.szInfo = text[:255]
                self._nid.szInfoTitle = title[:63]
                self._nid.dwInfoFlags = 1  # NIIF_INFO
                self._shell32.Shell_NotifyIconW(self._NIM_MODIFY, ctypes.byref(self._nid))
        except Exception:
            pass

    def stop(self):
        try:
            if self._nid is not None:
                self._shell32.Shell_NotifyIconW(self._NIM_DELETE, ctypes.byref(self._nid))
            if self._hwnd:
                self._user32.PostMessageW(self._hwnd, 0x0012, 0, 0)  # WM_QUIT
        except Exception:
            pass


# ---------------------------------------------------------------- WebView2 预检
# 背景：pywebview 在 **缺 WebView2 运行时**（或 .NET < 4.6.2）时会静默退回 mshtml
# (IE11) 渲染器（webview/platforms/winforms.py），只写一条 logger.warning ——
# windowed exe 里完全看不见。IE11 跑不了向导页的 ES6 语法，用户只会看到界面异常 /
# 桥接永远不就绪（向导报"自检失败"）。所以启动前显式检测，缺了就给出可操作提示。
WEBVIEW2_GUIDS = (
    "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",  # WebView2 Runtime
    "{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}",  # Beta
    "{0D50BFEC-CD6A-4F9A-964C-C7416E3ACB10}",  # Developer
    "{65C35B14-6C1D-4122-AC46-7148CC9D6497}",  # Canary
)


def webview2_status() -> tuple:
    """检测 WebView2 运行时：返回 (是否存在, 版本号)。非 Windows 直接视为可用。"""
    if os.name != "nt":
        return True, "n/a"
    try:
        import winreg
    except Exception:
        return True, "unknown"
    for hive_name in ("HKEY_CURRENT_USER", "HKEY_LOCAL_MACHINE"):
        hive = getattr(winreg, hive_name, None)
        if hive is None:
            continue
        for guid in WEBVIEW2_GUIDS:
            for sub in (rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{guid}",
                        rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}"):
                try:
                    with winreg.OpenKey(hive, sub) as key:
                        ver, _ = winreg.QueryValueEx(key, "pv")
                    if ver and str(ver) not in ("", "0.0.0.0"):
                        return True, str(ver)
                except Exception:
                    continue
    return False, ""


def dotnet_release() -> int:
    """返回 .NET Framework 4.x 的 Release 号（0 = 读不到）。pywebview 要求 >= 394802。"""
    if os.name != "nt":
        return 999999
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full"
        ) as key:
            value, _ = winreg.QueryValueEx(key, "Release")
        return int(value)
    except Exception:
        return 0


def confirm_renderer(webview2_ok: bool, build: str) -> bool:
    """WebView2/.NET 不满足时弹原生提示；返回 True = 继续启动，False = 中止。"""
    net_ok = dotnet_release() >= 394802
    if webview2_ok and net_ok:
        return True
    missing = []
    if not webview2_ok:
        missing.append("Microsoft Edge WebView2 运行时（未在注册表中找到）")
    if not net_ok:
        missing.append(".NET Framework 4.6.2 或更高版本")
    text = (
        "G4W 的窗口依赖 WebView2 运行时，但本机缺少：\n\n  · "
        + "\n  · ".join(missing)
        + "\n\n缺少时界面会退化成 IE 内核，向导只会一直提示"
          "「正在初始化本地界面组件」或「自检失败」。\n\n"
          "点「确定」→ 打开官方下载页（装完重新双击 G4W.exe）。\n"
          "点「取消」→ 仍要尝试启动（已手动装过固定版本运行时的话）。"
    )
    try:
        r = ctypes.windll.user32.MessageBoxW(0, text, "G4W 需要 WebView2 运行时", 0x1 | 0x30)
    except Exception:
        return True
    if r == 1:  # IDOK
        try:
            os.startfile(WEBVIEW2_URL)
        except Exception as exc:
            _log(f"open webview2 download page failed: {type(exc).__name__}: {exc}")
        return False
    return True


def enable_pywebview_logging():
    """把 pywebview 自己的日志落到 runtime\\shell-pywebview.log（渲染器降级/桥接报错可见）。"""
    try:
        import logging
        handler = logging.FileHandler(PYWEBVIEW_LOG, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger = logging.getLogger("pywebview")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
    except Exception as exc:
        _log(f"pywebview logging setup failed: {type(exc).__name__}: {exc}")


def main():
    _log(f"start frozen={getattr(sys, 'frozen', False)} root={ROOT} bundle={BUNDLE_DIR}")
    import webview

    lock = acquire_lock()
    if lock is None:
        _log("second instance: lock busy, requesting show")
        # 已有实例：请求它唤起窗口（不依赖托盘图标是否可见）
        if request_show_existing():
            msgbox("G4W 控制中心", "看板已在运行，已为你恢复窗口。")
        else:
            msgbox("G4W 控制中心", "看板已在运行，但无法唤起窗口。\n请在任务管理器中结束 G4W 进程后重试。")
        return

    wv2_ok, wv2_build = webview2_status()
    _log(f"renderer preflight: webview2={wv2_ok} build={wv2_build or '-'} dotnet_release={dotnet_release()}")
    if not confirm_renderer(wv2_ok, wv2_build):
        _log("aborted by user: WebView2 / .NET missing")
        try:
            lock.close()
        except Exception:
            pass
        return
    enable_pywebview_logging()

    tray = None

    def show_window():
        try:
            window.show()
            window.restore()
        except Exception:
            pass

    def quit_all():
        global quitting
        quitting = True
        # 先停后端服务，再拆托盘/窗口，避免窗口已关而 18180 仍在监听
        try:
            stop_backend(force_port=True)
        except Exception as exc:
            _log(f"quit_all stop_backend: {type(exc).__name__}: {exc}")
        try:
            if tray is not None:
                tray.stop()
        except Exception:
            pass
        try:
            window.destroy()
        except Exception:
            pass

    def on_closing():
        # pywebview 6.x 语义（event.py）：Event.set() 在 handler 返回 False 时返回 True；
        # winforms on_closing 里 should_cancel=True → args.Cancel=True → 阻止关闭。
        # 因此：return False = 阻止关闭，return True = 允许关闭。
        # ⚠ 禁止在 FormClosing 处理中调用 window.hide()：异常会被事件系统吞掉，
        #   返回值丢失 → 阻止失效（实证：X 后进程退出）。
        if quitting:
            return True  # 托盘"退出" → 允许关闭
        _log("X pressed -> minimize (tray=" + ("yes" if tray is not None else "NO") + ")")
        if tray is not None:
            tray.notify("看板仍在后台运行。右键托盘图标 → 退出，可彻底关闭看板。")
        # 先返回阻止关闭，再延迟隐藏窗口（FormClosing 处理完成之后）
        threading.Timer(0.1, _hide_safe).start()
        return False  # 阻止关闭

    def _hide_safe():
        try:
            window.hide()
        except Exception as exc:
            _log(f"hide error: {type(exc).__name__}: {exc}")

    # 首次使用自检：未初始化 → 加载向导；已初始化 → 正常进看板
    status = wizard_status()
    wizard = None
    if status["all_ok"]:
        window = webview.create_window(
            "G4W 控制中心", url="file:///" + LOADING_FILE.replace("\\", "/"),
            width=1300, height=860, min_size=(940, 620),
            background_color="#10141a", text_select=False,
        )
    else:
        _log("wizard mode: prepare=" + str(status["prepare"]["ok"]) +
             " key=" + str(status["key"]["ok"]) + " env=" + str(status["env"]["ok"]) +
             " login=" + str(status["login"]["ok"]))
        wizard = WizardApi()
        window = webview.create_window(
            "G4W 首次使用向导", url="file:///" + WIZARD_FILE.replace("\\", "/"),
            width=860, height=760, min_size=(760, 640),
            background_color="#10141a", text_select=False, js_api=wizard,
        )
        wizard.window = window

        # ---- 桥接自愈（v3.0.3 重写）----
        # 故障（3.0.2 现场）：pywebview 的注入偶发丢失 → window.pywebview 在但 api 是空对象
        # （前端报 `方法缺失:xxx [pw=object api=obj:0]`）。
        # 3.0.2 用后台线程重放 inject_pywebview + window.expose 兜底，但 **WebView2 的 COM
        # 只允许 UI 线程访问**：后台线程一碰就抛 E_NOINTERFACE /
        # "CoreWebView2 can only be accessed from the UI thread"（见 runtime\shell-pywebview.log），
        # 并与点击触发的 js_api 调用（在 WebMessageReceived=UI 线程里执行）争抢 →
        # 界面概率性卡死（"刚启动就点配置环境会卡，等几秒就好"）。
        # v3.0.3 改法：① 绝不做跨线程注入/暴露；② 桥接迟迟不来时只做一次"重新加载页面"——
        # window.load_url 内部走 Invoke 到 UI 线程，pywebview 会在自己的 NavigationCompleted
        # 里正常注入；③ 后台线程只 sleep 与读一个 bool，不碰任何 COM 对象。
        _expose_state = {"started": False}

        def _bridge_watchdog():
            url = "file:///" + WIZARD_FILE.replace("\\", "/")
            waited = 0.0
            while waited < 24.0 and not wizard.bridge_ready:   # 最多等 24s
                time.sleep(2.0)
                waited += 2.0
            if wizard.bridge_ready:
                _log(f"bridge watchdog: ready after ~{waited:.0f}s")
                return
            _log("bridge watchdog: still not ready -> reload page once (Invoke -> UI thread)")
            try:
                window.load_url(url)
            except Exception as exc:
                _log(f"bridge watchdog reload failed: {type(exc).__name__}: {exc}")
                return
            waited = 0.0
            while waited < 30.0 and not wizard.bridge_ready:
                time.sleep(2.0)
                waited += 2.0
            _log("bridge watchdog: " + ("recovered after reload" if wizard.bridge_ready
                                        else "GAVE UP (frontend never confirmed bridge)"))

        def _on_window_loaded():
            if _expose_state["started"]:
                _log("window loaded event fired again -> ignored")
                return
            _expose_state["started"] = True
            _log("window loaded event fired -> start bridge watchdog")
            threading.Thread(target=_bridge_watchdog, daemon=True).start()

        window.events.loaded += _on_window_loaded
    window.events.closing += on_closing

    # 托盘：纯 Win32 Shell_NotifyIcon（零 .NET/pystray 依赖）
    try:
        tray = WinTray(ICON, "G4W 控制中心", show_window, quit_all).start()
    except Exception as exc:
        tray = None
        _log(f"tray FAILED: {type(exc).__name__}: {exc}")

    # 监听单实例"唤起窗口"指令：第二个实例双击时发 "show" 到锁端口
    def _listen_show():
        try:
            while True:
                conn, _addr = lock.accept()
                try:
                    data = conn.recv(16)
                    if data.strip() == b"show":
                        conn.sendall(b"ack")
                        show_window()
                except Exception:
                    pass
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
        except Exception:
            pass

    threading.Thread(target=_listen_show, daemon=True).start()

    # 登录态持久化：默认 private_mode=True 时 WebView2 用随机临时目录，
    # 每次启动 cookie 全丢 → 每次都要重新登录；关掉并固定存储路径即可免登
    # 登录态持久化 + 强制 EdgeChromium 渲染器：默认 private_mode=True 时 WebView2 用
    # 随机临时目录，每次启动 cookie 全丢 → 每次都要重新登录；关掉并固定存储路径即可免登。
    # gui="edgechromium" 显式锁渲染器（缺 WebView2 的情况已在启动预检里拦下/确认）。
    storage_path = os.path.join(
        os.environ.get("APPDATA") or os.path.expanduser("~"), "G4W", "pywebview"
    )
    _log(f"webview.start renderer=edgechromium storage_path={storage_path}")
    if wizard is not None:
        # 向导模式：不拉起看板后端（向导完成后 open_dashboard 才需要）；
        # 但看板可能已由外部启动（复用），等待期间不阻塞向导
        webview.start(None, window, gui="edgechromium", private_mode=False,
                      storage_path=storage_path)
    else:
        webview.start(bootstrap, window, gui="edgechromium", private_mode=False,
                      storage_path=storage_path)
    # 主循环退出 = 彻底退出（再兜底一次，含端口占用进程）
    stop_backend(force_port=True)
    # 清理向导子进程（login/prepare 残留：壳退出后子进程会继续跑并锁文件）
    if wizard is not None:
        for proc in (wizard._prepare_proc, wizard._login_proc):
            try:
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    _log(f"killed wizard child pid={proc.pid}")
            except Exception:
                pass
    try:
        lock.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
