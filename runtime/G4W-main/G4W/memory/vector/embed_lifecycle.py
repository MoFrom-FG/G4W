"""Embedding process lifecycle (Sentence-Transformers thin HTTP).

Public API (replaces tei_lifecycle / TASK-C):
  - ``ensure_embed_running`` — start if vector_enabled and not healthy
  - ``stop_embed`` — precise stop by config.pid then port listener (ST markers only)
  - ``embed_health`` — HTTP /health or OpenAI-compatible embed probe
  - ``discover_embed_launchers`` — probe G4W-embedding layout
  - ``embedding_root``

Compat aliases (deprecated): ensure_tei_running, stop_tei, tei_health,
discover_tei_launchers — thin wrappers so old imports keep working one cycle.
"""
from __future__ import annotations

import logging
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from G4W.memory.vector import vector_config as vc

_log = logging.getLogger(__name__)

# Process cmdline markers — only kill what we own (ST server / start_embed)
_EMBED_CMD_MARKERS = (
    "G4W-embedding",
    "server.py",
    "start_embed.bat",
    "start_embed",
    "sentence_transformers",
    "EMBED_PORT",
    # legacy TEI leftovers still safe to stop if user had old stack
    "text-embeddings-inference",
    "text_embeddings_router",
    "tei-server",
    "start_tei.bat",
)

_LAUNCH_BAT_NAMES = (
    "start_embed.bat",
    "run_embed.bat",
    "start_server.bat",
    # legacy names still discovered so old scaffolds can be migrated
    "start_tei.bat",
    "run_tei.bat",
)

_DEFAULT_PORT = 8081
_DEFAULT_START_TIMEOUT_S = 120.0  # ST first load can be slow on CPU
_DEFAULT_EMBED_DEVICE = "auto"


def _desired_embed_device() -> str:
    """Device policy lives in the embed server (torch build decides).

    The GA main environment has no torch, so the desired device cannot be
    probed here. We hand "auto" to the server, which enforces the policy:
    GPU torch → CUDA mandatory (no CPU fallback); CPU torch (integrated-GPU
    machines decided at install time) → CPU is fine.
    """
    return "auto"


def _health_device_matches(health: Dict[str, Any], desired_device: str) -> bool:
    body = health.get("body")
    if not isinstance(body, dict):
        return True
    actual = str(body.get("device") or "").strip().lower()
    desired = str(desired_device or "").strip().lower()
    if desired == "auto":
        # 设备权威在服务端(auto 按 torch 版本执行策略);但 GPU 机器上若发现
        # CPU 服务(旧 server/手动降级残留)视为不匹配,强制重启纠正。
        if actual == "cpu":
            try:
                import shutil

                if shutil.which("nvidia-smi"):
                    return False
            except Exception:
                pass
        return bool(actual)
    if not actual or not desired:
        return True
    return actual == desired


def _runtime_root() -> Path:
    # G4W/memory/vector/embed_lifecycle.py → parents[4] = runtime
    return Path(__file__).resolve().parents[4]


def embedding_root() -> Path:
    """``<runtime>/G4W-embedding`` (install target)."""
    return _runtime_root() / "G4W-embedding"


def _cfg_base_url(cfg: Optional[Dict[str, Any]] = None) -> str:
    cfg = cfg if cfg is not None else vc.load_config()
    base = str(cfg.get("base_url") or "").strip().rstrip("/")
    if base:
        return base
    port = int(cfg.get("port") or _DEFAULT_PORT)
    return f"http://127.0.0.1:{port}"


def _cfg_port(cfg: Optional[Dict[str, Any]] = None) -> int:
    cfg = cfg if cfg is not None else vc.load_config()
    try:
        return int(cfg.get("port") or _DEFAULT_PORT)
    except (TypeError, ValueError):
        return _DEFAULT_PORT


def _health_field_value(status: str) -> Dict[str, Any]:
    """Write both embed_health (new) and tei_health (compat read by old UIs)."""
    return {"embed_health": status, "tei_health": status}


def _write_runtime_fields(
    *,
    pid: Any = ...,
    port: Any = ...,
    embed_health: Any = ...,
    base_url: Any = ...,
    backend: Any = ...,
    last_used_at: Any = ...,
) -> Dict[str, Any]:
    patch: Dict[str, Any] = {}
    if pid is not ...:
        patch["pid"] = pid
    if port is not ...:
        patch["port"] = port
    if embed_health is not ...:
        patch["embed_health"] = embed_health
        patch["tei_health"] = embed_health  # compat
    if base_url is not ...:
        patch["base_url"] = base_url
    if backend is not ...:
        patch["backend"] = backend
    if last_used_at is not ...:
        patch["last_used_at"] = last_used_at
    if not patch:
        return vc.load_config(use_cache=False)
    return vc.save_config(patch)


def _idle_ttl_seconds() -> float:
    raw = (os.environ.get("G4W_EMBEDDING_IDLE_TTL_SECONDS") or "300").strip()
    try:
        ttl = float(raw)
    except ValueError:
        return 300.0
    return ttl if ttl > 0 else 0.0


def mark_embed_used() -> None:
    """Refresh embedding last-use timestamp for idle auto-unload."""
    try:
        _write_runtime_fields(last_used_at=time.time())
    except Exception:
        pass


def _schedule_idle_stop(*, pid: Optional[int], port: int, base_url: str) -> None:
    ttl = _idle_ttl_seconds()
    if ttl <= 0:
        return

    def _stop_if_idle() -> None:
        try:
            cfg = vc.load_config(use_cache=False)
            last_raw = cfg.get("last_used_at") or 0
            try:
                last_used = float(last_raw)
            except (TypeError, ValueError):
                last_used = 0.0
            if time.time() - last_used < ttl:
                _schedule_idle_stop(pid=pid, port=port, base_url=base_url)
                return
            current_pid = cfg.get("pid")
            if pid is not None and current_pid not in (pid, str(pid)):
                return
            h = embed_health(base_url=base_url, timeout_s=2.0, verify_inference=False)
            if h.get("ok"):
                stop_embed(pid=pid, port=port)
        except Exception:
            _log.debug("embedding idle auto-stop failed", exc_info=True)

    timer = threading.Timer(ttl, _stop_if_idle)
    timer.daemon = True
    timer.start()


def _http_get(url: str, *, timeout_s: float = 3.0) -> Tuple[int, str]:
    """GET helper (patchable in unit tests). Returns (status, body_text)."""
    req = Request(url, method="GET")
    with urlopen(req, timeout=timeout_s) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return int(resp.status), raw


def _http_post_json(
    url: str, payload: Dict[str, Any], *, timeout_s: float = 3.0
) -> Tuple[int, str]:
    """POST JSON helper (patchable in unit tests). Returns (status, body_text)."""
    body = json_dumps_safe(payload).encode("utf-8")
    req = Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urlopen(req, timeout=timeout_s) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return int(resp.status), raw


def embed_health(
    *,
    base_url: Optional[str] = None,
    timeout_s: float = 3.0,
    verify_inference: bool = True,
) -> Dict[str, Any]:
    """Probe embed HTTP service. Prefer GET /health, then POST /v1/embeddings.

    ``verify_inference=True`` (default) requires a real embeddings probe even
    when /health responds OK. A half-dead process whose health route responds
    but whose inference path is broken must be reported unhealthy (observed
    2026-08-13: health ok 12.9ms while embed_batch failed → silent index
    lag). Lightweight callers (e.g. idle auto-stop) may pass
    ``verify_inference=False`` to skip the probe.
    """
    base = (base_url or _cfg_base_url()).rstrip("/")
    health_err: Optional[str] = None
    health_ok = False
    health_body: Any = None
    t0 = time.perf_counter()

    # 1) /health
    try:
        status, raw = _http_get(f"{base}/health", timeout_s=timeout_s)
        latency = (time.perf_counter() - t0) * 1000.0
        ok_body = True
        try:
            data = json_loads_safe(raw)
            if isinstance(data, dict) and data.get("ok") is False:
                ok_body = False
                health_err = str(data.get("error") or data.get("status") or raw[:200])
        except Exception:
            data = raw
        if ok_body and 200 <= status < 300:
            health_ok = True
            health_body = data if isinstance(data, dict) else {"raw": str(data)[:300]}
            if not verify_inference:
                return {
                    "ok": True,
                    "latency_ms": round(latency, 1),
                    "detail": "health ok",
                    "base_url": base,
                    "method": "health",
                    "body": health_body,
                }
            # health ok but inference not yet verified → fall through to probe
        else:
            health_err = f"health status={status} body={raw[:120]}"
    except HTTPError as exc:
        health_err = f"health HTTPError {exc.code}"
    except (URLError, TimeoutError, OSError) as exc:
        health_err = f"health {type(exc).__name__}: {exc}"

    # 2) OpenAI-compatible embeddings probe: inference verification when
    #    /health ok; fallback liveness when /health failed.
    t1 = time.perf_counter()
    try:
        status, raw = _http_post_json(
            f"{base}/v1/embeddings",
            {"input": "ping", "model": "G4W-embedding"},
            timeout_s=timeout_s,
        )
        latency = (time.perf_counter() - t1) * 1000.0
        data = json_loads_safe(raw)
        has_emb = False
        if isinstance(data, dict):
            arr = data.get("data")
            if isinstance(arr, list) and arr:
                has_emb = "embedding" in (arr[0] or {})
        if 200 <= status < 300 and has_emb:
            if health_ok:
                return {
                    "ok": True,
                    "latency_ms": round(latency, 1),
                    "detail": "inference verified (embeddings ok)",
                    "base_url": base,
                    "method": "embeddings",
                    "body": health_body,
                }
            return {
                "ok": True,
                "latency_ms": round(latency, 1),
                "detail": f"embeddings ok; prior_health={health_err}",
                "base_url": base,
                "method": "embeddings",
            }
        return {
            "ok": False,
            "latency_ms": round(latency, 1),
            "detail": (
                f"inference broken: embeddings bad shape; prior_health={health_err}; "
                f"body={raw[:160]}"
            ),
            "base_url": base,
            "method": "embeddings",
        }
    except HTTPError as exc:
        return {
            "ok": False,
            "latency_ms": None,
            "detail": f"embeddings HTTPError {exc.code}; prior={health_err}",
            "base_url": base,
            "method": "embeddings",
            "http_status": exc.code,
        }
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        return {
            "ok": False,
            "latency_ms": None,
            "detail": f"{type(exc).__name__}: {exc}; prior={health_err}",
            "base_url": base,
            "method": "embeddings",
        }


def json_loads_safe(raw: str) -> Any:
    import json

    return json.loads(raw)


def json_dumps_safe(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)


def _pid_alive(pid: Optional[int]) -> bool:
    if pid is None:
        return False
    try:
        pid_i = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_i <= 0:
        return False
    try:
        import psutil

        return psutil.pid_exists(pid_i)
    except Exception:
        try:
            os.kill(pid_i, 0)
            return True
        except (OSError, SystemError):
            return False
        except Exception:
            return False


def _process_cmdline(pid: int) -> str:
    try:
        import psutil

        proc = psutil.Process(pid)
        parts = proc.cmdline() or []
        if parts:
            return " ".join(str(x) for x in parts)
        return str(proc.name() or "")
    except Exception:
        return ""


def _looks_like_embed(cmdline: str, *, exe_path: str = "") -> bool:
    blob = f"{cmdline} {exe_path}".lower().replace("/", "\\")
    if not blob.strip():
        return False
    return any(m.lower() in blob for m in _EMBED_CMD_MARKERS)


def _pids_listening_on_port(port: int) -> List[int]:
    pids: List[int] = []
    try:
        import psutil

        for conn in psutil.net_connections(kind="inet"):
            try:
                if conn.laddr and int(conn.laddr.port) == int(port):
                    if conn.status in (
                        getattr(psutil, "CONN_LISTEN", "LISTEN"),
                        "LISTEN",
                    ) or str(conn.status).upper() == "LISTEN":
                        if conn.pid:
                            pids.append(int(conn.pid))
            except Exception:
                continue
    except Exception as exc:
        _log.debug("embed_lifecycle: net_connections failed: %s", type(exc).__name__)
        return []
    seen = set()
    out: List[int] = []
    for p in pids:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _port_open(host: str, port: int, *, timeout_s: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout_s):
            return True
    except OSError:
        return False


def _terminate_pid(pid: int, *, timeout_s: float = 5.0) -> Dict[str, Any]:
    info: Dict[str, Any] = {"pid": pid, "killed": False}
    try:
        import psutil

        proc = psutil.Process(pid)
        info["name"] = proc.name()
        info["cmdline"] = " ".join(proc.cmdline() or [])[:300]
        # kill children first (venv python may spawn)
        try:
            for ch in proc.children(recursive=True):
                try:
                    ch.terminate()
                except Exception:
                    pass
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=timeout_s)
        except psutil.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2.0)
        info["killed"] = not proc.is_running()
        return info
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    timeout=8,
                    check=False,
                )
                info["killed"] = not _pid_alive(pid)
                info["method"] = "taskkill"
            except Exception as exc2:
                info["error2"] = f"{type(exc2).__name__}: {exc2}"
        else:
            try:
                os.kill(pid, 15)
                time.sleep(0.5)
                if _pid_alive(pid):
                    os.kill(pid, 9)
                info["killed"] = not _pid_alive(pid)
            except Exception as exc2:
                info["error2"] = f"{type(exc2).__name__}: {exc2}"
        return info


def stop_embed(pid: Optional[int] = None, port: Optional[int] = None) -> Dict[str, Any]:
    """Stop embed server by explicit pid (optional) then config.pid then port listeners.

    ``pid``/``port`` are optional overrides — all callers (idle auto-stop,
    device-mismatch restart, half-dead relaunch, CLI stop) pass either or none.
    """
    cfg = vc.load_config(use_cache=False)
    port = int(port) if port is not None else _cfg_port(cfg)
    result: Dict[str, Any] = {
        "ok": True,
        "stopped": False,
        "actions": [],
        "skipped": [],
        "port": port,
        "pid_cfg": cfg.get("pid"),
    }

    candidates: List[int] = []
    if pid is not None:
        try:
            if int(pid) > 0:
                candidates.append(int(pid))
        except (TypeError, ValueError):
            pass
    raw_pid = cfg.get("pid")
    try:
        if raw_pid is not None and int(raw_pid) > 0:
            if int(raw_pid) not in candidates:
                candidates.append(int(raw_pid))
    except (TypeError, ValueError):
        pass
    for p in _pids_listening_on_port(port):
        if p not in candidates:
            candidates.append(p)

    for pid in candidates:
        if not _pid_alive(pid):
            result["skipped"].append({"pid": pid, "reason": "not_alive"})
            continue
        cmdline = _process_cmdline(pid)
        if not _looks_like_embed(cmdline):
            result["skipped"].append(
                {"pid": pid, "reason": "not_embed_cmdline", "cmdline": cmdline[:200]}
            )
            continue
        info = _terminate_pid(pid)
        result["actions"].append(info)
        if info.get("killed"):
            result["stopped"] = True

    try:
        _write_runtime_fields(pid=None, embed_health="stopped")
    except Exception as exc:
        result["config_write_error"] = f"{type(exc).__name__}: {exc}"

    health_after = embed_health(timeout_s=1.5)
    still_up = bool(health_after.get("ok"))
    result["health_after"] = health_after
    result["still_listening"] = still_up
    if still_up and not result["stopped"]:
        result["detail"] = (
            "embed still healthy after stop attempt (external or unprotected process?)"
        )
    elif still_up and result["stopped"]:
        result["ok"] = False
        result["detail"] = "stop issued but health still ok"
    else:
        result["detail"] = "stopped or already down"
    return result


def discover_embed_launchers(root: Optional[Path] = None) -> Dict[str, Any]:
    """Probe ``G4W-embedding`` for bat/server/venv/model (no side effects)."""
    root = root if root is not None else embedding_root()
    found: Dict[str, Any] = {
        "root": str(root),
        "root_exists": root.is_dir(),
        "bats": [],
        "server_py": None,
        "venv_python": None,
        "model_dirs": [],
        "launch_cmd": None,
        "backend": "st",
    }
    if not root.is_dir():
        return found

    for name in _LAUNCH_BAT_NAMES:
        p = root / name
        if p.is_file():
            found["bats"].append(str(p))

    sp = root / "server.py"
    if sp.is_file():
        found["server_py"] = str(sp)

    for rel in (
        Path(".venv") / "Scripts" / "python.exe",
        Path("venv") / "Scripts" / "python.exe",
        Path(".venv") / "bin" / "python",
        Path("venv") / "bin" / "python",
    ):
        vp = root / rel
        if vp.is_file():
            found["venv_python"] = str(vp)
            break

    for cand in (
        "models/Qwen3-Embedding-0.6B",
        "models/qwen3-embedding-0.6b",
        "models",
        "model",
        "Qwen3-Embedding-0.6B",
    ):
        mp = root / cand
        if mp.exists():
            found["model_dirs"].append(str(mp))

    cmd_file = root / "embed_launch_cmd.txt"
    if not cmd_file.is_file():
        cmd_file = root / "tei_launch_cmd.txt"  # legacy
    if cmd_file.is_file():
        try:
            line = cmd_file.read_text(encoding="utf-8-sig").strip().splitlines()[0].strip()
            if line:
                found["launch_cmd"] = line
        except OSError:
            pass

    return found


def _find_ga_python() -> Optional[Path]:
    """Prefer GA portable python next to runtime, then sys.executable."""
    rt = _runtime_root()
    candidates = [
        rt / "python" / "python.exe",
        rt / "python" / "python",
        rt.parent / "python" / "python.exe",
        Path(sys_executable()),
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)
    return None


def sys_executable() -> str:
    import sys

    return sys.executable or ""


def _build_launch_command(
    cfg: Dict[str, Any], discovery: Dict[str, Any]
) -> Tuple[Optional[Sequence[str]], Optional[str], bool]:
    """Return (argv, error_message, shell)."""
    if discovery.get("launch_cmd"):
        return ([str(discovery["launch_cmd"])], None, True)

    root = Path(discovery.get("root") or embedding_root())
    port = int(cfg.get("port") or _cfg_port(cfg))
    model_dirs = discovery.get("model_dirs") or []
    model_dir = ""
    for md in model_dirs:
        p = Path(md)
        # prefer leaf model dir with config.json
        if (p / "config.json").is_file() or p.name.lower().startswith("qwen"):
            model_dir = str(p)
            break
    if not model_dir and model_dirs:
        model_dir = str(model_dirs[0])

    vpy = discovery.get("venv_python")
    server_py = discovery.get("server_py")
    if vpy and server_py:
        env_prefix_ok = True  # caller sets env
        argv = [str(vpy), str(server_py)]
        # port/model via env in Popen
        return (argv, None, False)

    bats = discovery.get("bats") or []
    # prefer start_embed.bat
    bats_sorted = sorted(
        bats,
        key=lambda s: (0 if "start_embed" in s.lower() else 1, s),
    )
    if bats_sorted:
        bat = bats_sorted[0]
        if os.name == "nt":
            return (["cmd.exe", "/c", bat], None, False)
        return ([bat], None, False)

    if server_py:
        # fall back to GA/current python (may lack ST — last resort)
        py = vpy or (str(_find_ga_python()) if _find_ga_python() else sys_executable())
        if py:
            return ([py, str(server_py)], None, False)

    return (
        None,
        "no launcher: need start_embed.bat or (.venv python + server.py)",
        False,
    )


def ensure_embed_running(*, timeout_s: float = _DEFAULT_START_TIMEOUT_S) -> Dict[str, Any]:
    """Start ST embed HTTP if product gate on and not healthy.

    No-op (ok=False, status=disabled) when vector_enabled() is False.
    """
    out: Dict[str, Any] = {
        "ok": False,
        "status": "unknown",
        "pid": None,
        "port": None,
        "base_url": None,
        "health": None,
        "backend": "st",
    }

    try:
        enabled = bool(vc.vector_enabled())
    except Exception as exc:
        out["status"] = "error"
        out["error"] = f"vector_enabled failed: {type(exc).__name__}: {exc}"
        return out

    if not enabled:
        out["status"] = "disabled"
        out["detail"] = "vector_enabled() is False — no embed start"
        return out

    cfg = vc.load_config(use_cache=False)
    port = _cfg_port(cfg)
    base = _cfg_base_url(cfg)
    desired_device = _desired_embed_device()
    out["port"] = port
    out["base_url"] = base
    out["desired_device"] = desired_device

    # already healthy? (inference-verified: health ok alone is NOT enough —
    # a half-dead server can answer /health while embed_batch fails)
    h = embed_health(base_url=base, timeout_s=3.0)
    out["health"] = h
    if h.get("ok") and _health_device_matches(h, desired_device):
        out["ok"] = True
        out["status"] = "already_running"
        out["pid"] = cfg.get("pid")
        try:
            _write_runtime_fields(
                port=port,
                embed_health="ok",
                base_url=base,
                backend="st",
                last_used_at=time.time(),
            )
            _schedule_idle_stop(pid=None, port=port, base_url=base)
        except Exception:
            pass
        return out
    if h.get("ok"):
        out["health_mismatch"] = "device_mismatch"
        stop_embed(pid=None, port=port)

    # pid alive + port open → wait a bit for health (model loading)
    raw_pid = cfg.get("pid")
    try:
        pid_i = int(raw_pid) if raw_pid is not None else None
    except (TypeError, ValueError):
        pid_i = None
    if pid_i and _pid_alive(pid_i) and _port_open("127.0.0.1", port):
        deadline = time.time() + min(timeout_s, 60.0)
        while time.time() < deadline:
            h = embed_health(base_url=base, timeout_s=3.0)
            out["health"] = h
            if h.get("ok") and _health_device_matches(h, desired_device):
                out["ok"] = True
                out["status"] = "already_running"
                out["pid"] = pid_i
                try:
                    _write_runtime_fields(
                        pid=pid_i,
                        port=port,
                        embed_health="ok",
                        base_url=base,
                        backend="st",
                        last_used_at=time.time(),
                    )
                    _schedule_idle_stop(pid=pid_i, port=port, base_url=base)
                except Exception:
                    pass
                return out
            if h.get("ok"):
                out["health_mismatch"] = "device_mismatch"
                stop_embed(pid=pid_i, port=port)
                break
            time.sleep(1.0)

    # Half-dead server: process alive + port open, but inference verification
    # keeps failing. Stop it before launching a replacement so the new server
    # can bind the port (2026-08-13 incident: health ok, embed_batch failed).
    if _port_open("127.0.0.1", port):
        _log.warning(
            "embed_lifecycle: port %s occupied by unresponsive/half-dead embed server; "
            "stopping before relaunch",
            port,
        )
        stop_embed(pid=pid_i if pid_i else None, port=port)
        time.sleep(0.5)

    discovery = discover_embed_launchers()
    argv, err, shell = _build_launch_command(cfg, discovery)
    if not argv:
        out["status"] = "error"
        out["error"] = err or "no launch command"
        out["discovery"] = discovery
        try:
            _write_runtime_fields(embed_health="no_launcher")
        except Exception:
            pass
        return out

    model_dir = ""
    for md in discovery.get("model_dirs") or []:
        model_dir = md
        break
    env = os.environ.copy()
    env["EMBED_PORT"] = str(port)
    env["PORT"] = str(port)
    env["EMBED_HOST"] = "127.0.0.1"
    if model_dir:
        env["EMBED_MODEL_DIR"] = model_dir
    env["EMBED_DEVICE"] = desired_device

    popen_kwargs: Dict[str, Any] = {
        "cwd": str(embedding_root()),
        "env": env,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "shell": bool(shell),
    }
    if os.name == "nt":
        # new process group so we don't kill G4W on stop mishap
        popen_kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
        popen_kwargs["close_fds"] = False
    else:
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(list(argv) if not shell else argv[0], **popen_kwargs)
    except Exception as exc:
        out["status"] = "error"
        out["error"] = f"Popen failed: {type(exc).__name__}: {exc}"
        out["argv"] = list(argv) if argv else None
        try:
            _write_runtime_fields(embed_health="start_failed")
        except Exception:
            pass
        return out

    pid = proc.pid
    out["pid"] = pid
    try:
        _write_runtime_fields(
            pid=pid, port=port, embed_health="starting", base_url=base, backend="st"
        )
    except Exception as exc:
        out["config_write_error"] = f"{type(exc).__name__}: {exc}"

    deadline = time.time() + float(timeout_s)
    last_health: Dict[str, Any] = {}
    while time.time() < deadline:
        if proc.poll() is not None:
            out["status"] = "error"
            out["ok"] = False
            out["error"] = f"embed server exited early with code {proc.returncode}"
            try:
                _write_runtime_fields(pid=None, embed_health="exited_early")
            except Exception:
                pass
            return out
        last_health = embed_health(timeout_s=2.0)
        if last_health.get("ok") and _health_device_matches(last_health, desired_device):
            try:
                _write_runtime_fields(
                    pid=pid,
                    port=port,
                    embed_health="ok",
                    base_url=base,
                    backend="st",
                    last_used_at=time.time(),
                )
                _schedule_idle_stop(pid=pid, port=port, base_url=base)
            except Exception as exc:
                out["config_write_error"] = f"{type(exc).__name__}: {exc}"
            out.update(
                {
                    "status": "started",
                    "ok": True,
                    "health": last_health,
                    "detail": "embed server started and healthy",
                }
            )
            return out
        if last_health.get("ok"):
            out["health_mismatch"] = "device_mismatch"
        time.sleep(0.8)

    out["status"] = "error"
    out["ok"] = False
    out["error"] = f"embed start timeout after {timeout_s}s"
    out["health"] = last_health
    try:
        _write_runtime_fields(pid=pid, port=port, embed_health="start_timeout")
    except Exception:
        pass
    return out


# ----- deprecated TEI aliases (one-cycle compat) -----
def ensure_tei_running(*, timeout_s: float = _DEFAULT_START_TIMEOUT_S) -> Dict[str, Any]:
    return ensure_embed_running(timeout_s=timeout_s)


def stop_tei() -> Dict[str, Any]:
    return stop_embed()


def tei_health(
    *,
    base_url: Optional[str] = None,
    timeout_s: float = 3.0,
) -> Dict[str, Any]:
    return embed_health(base_url=base_url, timeout_s=timeout_s)


def discover_tei_launchers(root: Optional[Path] = None) -> Dict[str, Any]:
    return discover_embed_launchers(root=root)


__all__ = [
    "ensure_embed_running",
    "stop_embed",
    "embed_health",
    "discover_embed_launchers",
    "embedding_root",
    # compat
    "ensure_tei_running",
    "stop_tei",
    "tei_health",
    "discover_tei_launchers",
]
