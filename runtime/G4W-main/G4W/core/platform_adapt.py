"""平台适配层：把 Windows / Linux(POSIX) 的差异收敛到这一处。

产品代码里不要再直接出现 python.exe、Scripts、taskkill、netstat、
CREATE_NO_WINDOW、PROGRAMFILES 这类平台字面量，统一走本模块。
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

IS_WINDOWS = os.name == "nt"
IS_POSIX = not IS_WINDOWS

LISTEN_PROBE_TIMEOUT = 8
KILL_GRACE_SECONDS = 8.0


def venv_python(venv_dir: Path) -> Path:
    """虚拟环境内的解释器：Windows 用 Scripts/python.exe，POSIX 用 bin/python。"""
    venv_dir = Path(venv_dir)
    if IS_WINDOWS:
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def portable_python(root: Path) -> Path:
    """包内便携解释器：Windows 是 runtime/python/python.exe，POSIX 是 runtime/python/bin/python3。"""
    base = Path(root) / "runtime" / "python"
    if IS_WINDOWS:
        return base / "python.exe"
    for name in ("bin/python3", "bin/python"):
        candidate = base / name
        if candidate.is_file():
            return candidate
    return base / "bin" / "python3"


def venv_site_packages(venv_dir: Path) -> list[Path]:
    """虚拟环境的 site-packages 目录（Windows: Lib/site-packages；POSIX: lib/python3.X/site-packages）。"""
    venv_dir = Path(venv_dir)
    candidates: list[Path] = []
    if IS_WINDOWS:
        candidates.append(venv_dir / "Lib" / "site-packages")
    else:
        lib = venv_dir / "lib"
        if lib.is_dir():
            candidates.extend(sorted(path for path in lib.glob("python*/site-packages")))
    return [path for path in candidates if path.is_dir()]


def service_python(root: Path) -> Path:
    """看板拉起内建服务时用的解释器。

    Windows 用包内 base python（venv 的 python 是 redirector，会新开控制台窗口，
    CREATE_NO_WINDOW 对其无效）；POSIX 直接用 venv 解释器（无该问题且依赖齐全）。
    """
    root = Path(root)
    venv = venv_python(root / "runtime" / "app" / ".venv")
    if not IS_WINDOWS and venv.is_file():
        return venv
    base = portable_python(root)
    return base if base.is_file() else venv


def interpreter_candidates(root: Path) -> list[Path]:
    """解释器候选（按优先级）：包内便携 → GA venv → 系统解释器。"""
    root = Path(root)
    candidates = [portable_python(root), venv_python(root / "runtime" / "app" / ".venv")]
    system = shutil.which("python") if IS_WINDOWS else shutil.which("python3")
    if system:
        candidates.append(Path(system))
    return candidates


def first_existing(candidates) -> Path | None:
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def no_window_kwargs() -> dict:
    """隐藏子进程窗口（仅 Windows 有意义）。"""
    if IS_WINDOWS:
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def detached_kwargs() -> dict:
    """脱离父进程后台运行：Windows 用 DETACHED_PROCESS，POSIX 用新会话。"""
    if IS_WINDOWS:
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return {"creationflags": flags}
    return {"start_new_session": True}


def service_launch_kwargs() -> dict:
    """看板拉起常驻服务时的进程参数：Windows 新进程组+脱离控制台，POSIX 新会话。"""
    if IS_WINDOWS:
        flags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
        return {"creationflags": flags, "close_fds": False}
    return {"start_new_session": True}


def wrap_script_command(executable, args: list[str]) -> list[str]:
    """批处理脚本在 Windows 下需要经 cmd.exe 转发。"""
    executable = str(executable)
    args = [str(item) for item in args]
    if IS_WINDOWS and executable.lower().endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", executable, *args]
    return [executable, *args]


def pid_alive(pid: int) -> bool:
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if IS_WINDOWS:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def kill_tree(pid: int) -> dict:
    """结束进程及其子进程：Windows 用 taskkill /T，POSIX 先 SIGTERM 进程组再按需 SIGKILL。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return {"ok": False, "mode": "none", "detail": "invalid pid"}
    if pid <= 0:
        return {"ok": False, "mode": "none", "detail": "invalid pid"}
    if IS_WINDOWS:
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
            )
        except Exception as error:
            return {"ok": False, "mode": "taskkill", "detail": str(error)}
        detail = (result.stderr or result.stdout or "").strip()
        return {"ok": result.returncode == 0, "mode": "taskkill", "detail": detail}
    mode = "killpg"
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except ProcessLookupError:
        return {"ok": True, "mode": mode, "detail": "already gone"}
    except OSError as error:
        mode = "kill"
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return {"ok": True, "mode": mode, "detail": "already gone"}
        except OSError as fallback_error:
            return {"ok": False, "mode": mode, "detail": str(error) + "; " + str(fallback_error)}
    deadline = time.monotonic() + KILL_GRACE_SECONDS
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return {"ok": True, "mode": mode, "detail": "terminated"}
        time.sleep(0.2)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return {"ok": not pid_alive(pid), "mode": mode, "detail": "sigkill"}


def _parse_ss_line(line: str, port: int) -> int | None:
    parts = line.split()
    if len(parts) < 4:
        return None
    if not parts[3].endswith(":" + str(port)):
        return None
    for token in parts[4:]:
        if token.startswith("pid="):
            digits = "".join(ch for ch in token[4:].split(",")[0] if ch.isdigit())
            return int(digits) if digits else None
    return None


def pids_listening_on(port: int) -> list[int]:
    """列出监听指定端口的进程号。"""
    try:
        port = int(port)
    except (TypeError, ValueError):
        return []
    pids: list[int] = []
    if IS_WINDOWS:
        try:
            out = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=LISTEN_PROBE_TIMEOUT,
            ).stdout
        except Exception:
            return []
        needle = ":" + str(port)
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 5 or parts[0].upper() != "TCP" or parts[3].upper() != "LISTENING":
                continue
            if not parts[1].endswith(needle):
                continue
            try:
                pids.append(int(parts[4]))
            except ValueError:
                continue
        return sorted(set(pids))
    try:
        out = subprocess.run(
            ["ss", "-ltnpH"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=LISTEN_PROBE_TIMEOUT,
        ).stdout
        for line in out.splitlines():
            found = _parse_ss_line(line, port)
            if found:
                pids.append(found)
    except Exception:
        pass
    if not pids:
        try:
            out = subprocess.run(
                ["lsof", "-ti", "tcp:" + str(port), "-sTCP:LISTEN"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=LISTEN_PROBE_TIMEOUT,
            ).stdout
            pids = [int(item) for item in out.split() if item.strip().isdigit()]
        except Exception:
            pass
    return sorted(set(pids))


def browser_candidates() -> list[Path]:
    """可用的 Chromium 系浏览器可执行文件（用于生成时间线截图/发布）。"""
    if IS_WINDOWS:
        program_files = Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
        program_files_x86 = Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)"))
        return [
            program_files_x86 / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            program_files / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            program_files / "Google" / "Chrome" / "Application" / "chrome.exe",
            program_files_x86 / "Google" / "Chrome" / "Application" / "chrome.exe",
        ]
    names = (
        "google-chrome-stable", "google-chrome", "chromium", "chromium-browser",
        "microsoft-edge-stable", "microsoft-edge", "brave-browser", "firefox",
    )
    found: list[Path] = []
    override = os.environ.get("G4W_BROWSER_PATH", "")
    if override:
        found.append(Path(override))
    for name in names:
        located = shutil.which(name)
        if located:
            found.append(Path(located))
    return found


def first_browser() -> Path | None:
    return first_existing(browser_candidates())
