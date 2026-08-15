import argparse
import json
import os
import signal
import subprocess
from pathlib import Path

from ..core.config import Config
from ..memory.migration import bind_legacy_user, migrate_legacy
from ..core.service import G4WService


def _read_pid(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _write_pid(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_pid(path)
    if existing and existing != os.getpid() and _pid_is_running(existing):
        raise RuntimeError(f"G4W is already running (pid={existing})")
    path.write_text(f"{os.getpid()}\n", encoding="utf-8")


def _remove_own_pid(path: Path) -> None:
    if _read_pid(path) == os.getpid():
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def stop_service(config: Config) -> dict:
    pid = _read_pid(config.pid_file)
    if not pid:
        return {"ok": True, "stopped": False, "message": "G4W is not running"}
    if not _pid_is_running(pid):
        config.pid_file.unlink(missing_ok=True)
        return {"ok": True, "stopped": False, "pid": pid, "message": "Removed stale PID file"}
    config.stop_marker_file.write_text(json.dumps({"pid": pid, "requestedAt": __import__("time").time()}), encoding="utf-8")
    if os.name == "nt":
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "taskkill failed").strip())
    else:
        os.kill(pid, signal.SIGTERM)
    config.pid_file.unlink(missing_ok=True)

    # The embed server runs detached (CREATE_NEW_PROCESS_GROUP) and is NOT part
    # of the main process tree, so taskkill /T above cannot reach it. stop_embed
    # kills only config.pid + port listeners whose cmdline looks like our embed
    # server — never unrelated python processes.
    embed_result: dict = {"attempted": False}
    try:
        from ..memory.vector.embed_lifecycle import stop_embed

        embed_result = stop_embed()
        embed_result["attempted"] = True
    except Exception as exc:  # pragma: no cover - defensive
        embed_result = {"attempted": True, "ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return {"ok": True, "stopped": True, "pid": pid, "embed": embed_result}


def main():
    parser = argparse.ArgumentParser(prog="python -m G4W")
    parser.add_argument("command", choices=("start", "stop", "monitor", "login", "accounts", "doctor", "dashboard", "prepare", "key", "init-env", "sync-runtime-paths", "migrate", "bind-legacy", "export-public-sops"), nargs="?", default="start")
    parser.add_argument("--host", default=os.environ.get("G4W_DASHBOARD_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("G4W_DASHBOARD_PORT", "18180")))
    parser.add_argument("--source", default="")
    parser.add_argument("--target", default="")
    parser.add_argument("--sender", default="")
    parser.add_argument("--legacy-sender", default="")
    args = parser.parse_args()
    if args.command in ("prepare", "key", "init-env", "sync-runtime-paths"):
        from .initializer import interactive_env, interactive_ga_key, prepare_portable, safe_json, sync_runtime_paths
        if args.command == "prepare":
            result = prepare_portable()
        elif args.command == "key":
            result = interactive_ga_key()
        elif args.command == "init-env":
            result = interactive_env()
        else:
            result = sync_runtime_paths()
        print(safe_json(result))
        if not result.get("ok", False):
            raise SystemExit(1)
        return
    if args.command == "export-public-sops":
        if not args.target:
            parser.error("export-public-sops requires --target")
        from ..memory.sop_catalog import SopCatalog

        package = Path(__file__).resolve().parents[1]
        result = SopCatalog(package / "memory" / "sop").export_public(Path(args.target))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    config = Config.load()
    if args.command == "dashboard":
        from ..dashboard.server import run_dashboard
        raise SystemExit(run_dashboard(config, args.host, args.port))
    if args.command == "stop":
        print(json.dumps(stop_service(config), ensure_ascii=False, indent=2))
        return
    if args.command == "monitor":
        from .monitor import ModelLogMonitor
        raise SystemExit(ModelLogMonitor(config.state_dir, config.pid_file).run())
    if args.command == "migrate":
        if not args.source:
            parser.error("migrate requires --source")
        print(json.dumps(migrate_legacy(Path(args.source), config.state_dir), ensure_ascii=False, indent=2))
        return
    if args.command == "bind-legacy":
        if not args.sender or not args.legacy_sender:
            parser.error("bind-legacy requires --sender and --legacy-sender")
        print(json.dumps(bind_legacy_user(config.state_dir, args.sender, args.legacy_sender), ensure_ascii=False, indent=2))
        return
    service = G4WService(config)
    if args.command == "login":
        print(json.dumps(service.login(), ensure_ascii=False, indent=2))
    elif args.command == "accounts":
        accounts = service.channel.accounts.read().get("accounts", {})
        print(json.dumps({"accounts": [{key: value.get(key) for key in ("accountId", "baseUrl", "userId", "savedAt")} for value in accounts.values()]}, ensure_ascii=False, indent=2))
    elif args.command == "doctor":
        print(json.dumps(service.doctor(), ensure_ascii=False, indent=2))
    else:
        config.stop_marker_file.unlink(missing_ok=True)
        _write_pid(config.pid_file)
        try:
            service.run()
        finally:
            _remove_own_pid(config.pid_file)
