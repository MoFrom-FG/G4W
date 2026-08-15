"""Scaffold/install G4W-embedding (ST + thin HTTP + independent venv).

No long-run start here (ensure_embed_running). Install never sets enabled=true.
The full install downloads the pinned model into the addon directory.  CI must
use --dry-run / scaffold / mark with fixtures — no multi-GB download.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# Load sibling modules by path so bare portable Python can run install without
# importing G4W.memory.vector.__init__ (numpy / hnsw / full stack).
def _load_sibling(mod_name: str, filename: str):
    import importlib.util

    path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vc = _load_sibling("_cb_install_vector_config", "vector_config.py")
_st_tpl = _load_sibling("_cb_install_st_server_template", "st_server_template.py")
write_server_py = _st_tpl.write_server_py
em = _load_sibling("_cb_install_embed_mirrors", "embed_mirrors.py")

PINNED_MODEL = "Qwen3-Embedding-0.6B"
PINNED_MODEL_REPO = "Qwen/Qwen3-Embedding-0.6B"
PINNED_DIM = 1024
PINNED_PORT = 8081  # SOP: avoid CPA/common 8080
PINNED_BACKEND = "st"
SIZE_HINT = "约 4–8 GB（GPU Torch + sentence-transformers + Qwen3-Embedding-0.6B 权重）"
_LAUNCH_BAT = "start_embed.bat"
_SERVER_PY = "server.py"
_VENV_DIR = ".venv"
_REQ_TXT = "requirements.txt"
_VC_RUNTIME_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
_VC_RUNTIME_EXE = "vc_redist.x64.exe"
_DEFAULT_TORCH_CUDA_CHANNEL = "cu128"
_TORCH_NJU_ROOT = "https://mirrors.nju.edu.cn/pytorch/whl"
_TORCH_ALIYUN_ROOT = "https://mirrors.aliyun.com/pytorch-wheels"
_TORCH_OFFICIAL_ROOT = "https://download.pytorch.org/whl"

# Minimal pip set for addon venv (kept independent of GA env)
_REQ_LINES = [
    "numpy>=1.26,<3",
    "sentence-transformers>=3.0.0",
    "huggingface-hub>=0.34.0",
    "torch",
]

_DEFAULT_MODEL_ENDPOINTS = (
    "https://hf-mirror.com",
    "https://huggingface.co",
)


def runtime_root() -> Path:
    return Path(__file__).resolve().parents[4]


def embedding_root(base: Optional[Path] = None) -> Path:
    """Return …/G4W-embedding. ``base`` may be runtime or embedding dir."""
    if base is None:
        return runtime_root() / "G4W-embedding"
    base = Path(base)
    if base.name == "G4W-embedding":
        return base
    return base / "G4W-embedding"


def _find_base_python() -> Path:
    """GA portable python preferred; else current interpreter."""
    rt = runtime_root()
    for c in (
        rt / "python" / "python.exe",
        rt / "python" / "python",
        rt.parent / "python" / "python.exe",
        Path(sys.executable),
    ):
        if c and Path(c).is_file():
            return Path(c)
    return Path(sys.executable)


def probe_layout(root: Optional[Path] = None) -> Dict[str, Any]:
    """Probe dirs/files embed_lifecycle expects (no side effects)."""
    emb = embedding_root(root)
    out: Dict[str, Any] = {
        "root": str(emb),
        "root_exists": emb.is_dir(),
        "server_py": None,
        "start_bat": None,
        "venv_python": None,
        "requirements": None,
        "model_dir": None,
        "model_ok": False,
        "launchable": False,
        "backend": PINNED_BACKEND,
        "missing": [],
    }
    if not emb.is_dir():
        out["missing"].extend(
            ["root", _SERVER_PY, _LAUNCH_BAT, f"models/{PINNED_MODEL}", f"{_VENV_DIR}"]
        )
        return out

    sp = emb / _SERVER_PY
    if sp.is_file():
        out["server_py"] = str(sp)
    else:
        out["missing"].append(_SERVER_PY)

    bat = emb / _LAUNCH_BAT
    if bat.is_file():
        out["start_bat"] = str(bat)
    else:
        # legacy
        leg = emb / "start_tei.bat"
        if leg.is_file():
            out["start_bat"] = str(leg)
        else:
            out["missing"].append(_LAUNCH_BAT)

    for rel in (
        Path(_VENV_DIR) / "Scripts" / "python.exe",
        Path(_VENV_DIR) / "bin" / "python",
        Path("venv") / "Scripts" / "python.exe",
    ):
        vp = emb / rel
        if vp.is_file():
            out["venv_python"] = str(vp)
            break
    if not out["venv_python"]:
        out["missing"].append(f"{_VENV_DIR}/Scripts/python.exe")

    req = emb / _REQ_TXT
    if req.is_file():
        out["requirements"] = str(req)

    model_dir = emb / "models" / PINNED_MODEL
    if model_dir.is_dir():
        out["model_dir"] = str(model_dir)
        try:
            files = [p for p in model_dir.rglob("*") if p.is_file()]
        except OSError:
            files = []
        names = {p.name for p in files}
        has_config = "config.json" in names
        has_tokenizer = bool(
            names
            & {
                "tokenizer.json",
                "tokenizer.model",
                "vocab.json",
                "sentencepiece.bpe.model",
            }
        )
        by_name = {p.name: p for p in files}
        has_weights = False
        weight_index = by_name.get("model.safetensors.index.json")
        if weight_index is not None:
            try:
                index_data = json.loads(weight_index.read_text(encoding="utf-8"))
                weight_map = index_data.get("weight_map") if isinstance(index_data, dict) else None
                shard_names = set(weight_map.values()) if isinstance(weight_map, dict) else set()
                has_weights = bool(shard_names) and all(
                    (model_dir / name).is_file() and (model_dir / name).stat().st_size > 0
                    for name in shard_names
                )
            except (OSError, ValueError, TypeError):
                has_weights = False
        if not has_weights:
            for name in ("model.safetensors", "pytorch_model.bin"):
                candidate = by_name.get(name)
                if candidate is not None:
                    try:
                        if candidate.stat().st_size > 0:
                            has_weights = True
                            break
                    except OSError:
                        pass
        out["model_ok"] = bool(has_config and has_tokenizer and has_weights)
        out["model_files"] = len(files)
        out["model_checks"] = {
            "config": has_config,
            "tokenizer": has_tokenizer,
            "weights": has_weights,
        }
    if not out["model_ok"]:
        out["missing"].append(f"models/{PINNED_MODEL}")

    # The generated start_embed.bat also requires the addon venv.  Merely
    # finding the BAT must not make an incomplete scaffold look launchable.
    out["launchable"] = bool(
        out["venv_python"] and out["server_py"] and out["model_ok"]
    )
    return out


def _start_embed_bat_text(port: int = PINNED_PORT) -> str:
    return f"""@echo off
REM Auto-generated by install_embedding (ST thin HTTP). Discovered by embed_lifecycle.
setlocal EnableExtensions
cd /d "%~dp0"
set "EMBED_PORT={port}"
set "PORT={port}"
set "EMBED_HOST=127.0.0.1"
set "EMBED_MODEL_DIR=%~dp0models\\{PINNED_MODEL}"
set "EMBED_DEVICE=auto"
if exist "%~dp0.venv\\Scripts\\python.exe" (
  "%~dp0.venv\\Scripts\\python.exe" "%~dp0server.py"
  exit /b %ERRORLEVEL%
)
if exist "%~dp0venv\\Scripts\\python.exe" (
  "%~dp0venv\\Scripts\\python.exe" "%~dp0server.py"
  exit /b %ERRORLEVEL%
)
echo [G4W-Embedding] venv python not found. Re-run the product-root 5_embedding_for_G4W.bat
exit /b 1
"""


def _readme_text() -> str:
    return f"""# G4W Embedding (Sentence-Transformers thin HTTP)

Windows 默认后端：**ST + 独立 .venv**（GA portable Python 创建，不污染主环境）。

## 布局
- `server.py` — 薄 HTTP（/health, /v1/embeddings, /embed）
- `start_embed.bat` — 用 `.venv\\Scripts\\python.exe` 启动
- `.venv/` — 仅本外挂依赖（sentence-transformers, torch）
- `models/{PINNED_MODEL}/` — 官方权重（safetensors 等）
- `vector_config.json` — installed/enabled/port/base_url（enabled 默认 false）

## 安装
1. 在 G4W 产品根目录运行 `5_embedding_for_G4W.bat`，或：
   `python -m G4W.memory.vector.install_embedding --yes`
2. 安装器优先通过 `https://hf-mirror.com` 自动下载 `{PINNED_MODEL_REPO}`，
   失败后回退 Hugging Face 官方站；重复运行会复用已有文件并继续下载。
3. 如需指定镜像，设置 `G4W_HF_ENDPOINT` 或 `HF_ENDPOINT`。
4. Torch 默认 `auto`：检测到 NVIDIA GPU 时安装 CUDA 12.8 版，否则安装 CPU 版；
   可用 `G4W_TORCH_MODE=gpu|cpu|auto` 强制选择。
5. PyTorch wheel 优先使用南京大学镜像，失败后依次回退阿里云和 PyTorch 官方源；
   可用 `G4W_TORCH_INDEX_URL` 指定自定义 wheel 索引。
6. Windows缺少Torch运行库时，安装器会从微软官方地址安装VC++ 2015-2022 x64。
7. 微信 `/vector on` 开启产品闸；worker 按需 `ensure_embed_running`

## 不做
- 不装进 GA 主 `.venv`
- 不默认 Docker / TEI
- install 不把 enabled 设为 true
"""


def _requirements_text() -> str:
    return "\n".join(_REQ_LINES) + "\n"


def _nvidia_gpu_probe() -> Dict[str, Any]:
    """Detect a usable NVIDIA driver without importing torch."""
    candidates = [shutil.which("nvidia-smi")]
    if os.name == "nt":
        system_root = Path(os.environ.get("SystemRoot") or r"C:\Windows")
        candidates.extend(
            [
                str(system_root / "System32" / "nvidia-smi.exe"),
                str(Path(os.environ.get("ProgramFiles") or r"C:\Program Files") / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe"),
            ]
        )
    exe = next((str(Path(item)) for item in candidates if item and Path(item).is_file()), None)
    if not exe:
        return {"available": False, "reason": "nvidia-smi not found"}
    try:
        result = subprocess.run(
            [exe, "--query-gpu=name,driver_version", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
        return {
            "available": result.returncode == 0 and bool(lines),
            "executable": exe,
            "gpus": lines,
            "returncode": result.returncode,
            "error_tail": (result.stderr or "")[-500:],
        }
    except Exception as exc:
        return {"available": False, "executable": exe, "reason": f"{type(exc).__name__}: {exc}"}


def _torch_install_plan() -> Dict[str, Any]:
    """Resolve auto/gpu/cpu and ordered PyTorch wheel indexes."""
    requested = (
        os.environ.get("G4W_TORCH_MODE")
        or "auto"
    ).strip().lower()
    if requested not in {"auto", "gpu", "cpu"}:
        raise ValueError("G4W_TORCH_MODE must be auto, gpu, or cpu")
    gpu = _nvidia_gpu_probe()
    resolved = "gpu" if requested == "gpu" or (requested == "auto" and gpu.get("available")) else "cpu"
    channel = (
        os.environ.get("G4W_TORCH_CUDA_CHANNEL")
        or _DEFAULT_TORCH_CUDA_CHANNEL
    ).strip().lower()
    if resolved == "cpu":
        channel = "cpu"
    elif not re.fullmatch(r"cu\d{3}", channel):
        raise ValueError("G4W_TORCH_CUDA_CHANNEL must look like cu128")
    custom = (
        os.environ.get("G4W_TORCH_INDEX_URL")
        or ""
    ).strip().rstrip("/")
    if not custom:
        # 配置文件（测速选优）优先；无配置时 resolve 即内置池顺序
        try:
            custom = (em.resolve(_state_dir())["torch"] or [""])[0].rstrip("/")
        except Exception:
            custom = ""
    indexes: List[str] = []
    for url in (
        custom,
        f"{_TORCH_NJU_ROOT}/{channel}",
        f"{_TORCH_ALIYUN_ROOT}/{channel}",
        f"{_TORCH_OFFICIAL_ROOT}/{channel}",
    ):
        if url and url not in indexes:
            indexes.append(url)
    return {
        "requested": requested,
        "resolved": resolved,
        "channel": channel,
        "indexes": indexes,
        "nvidia": gpu,
    }


def _torch_source_args(index_url: str) -> List[str]:
    """Aliyun exposes a flat wheel page; NJU and pytorch.org are pip indexes."""
    normalized = index_url.rstrip("/")
    if normalized.startswith(_TORCH_ALIYUN_ROOT + "/"):
        return ["--no-deps", "--no-index", "--find-links", normalized]
    return ["--index-url", normalized]


def _probe_torch_package(vpy: Path) -> Dict[str, Any]:
    """Read installed Torch metadata without importing it or its dependencies."""
    try:
        result = subprocess.run(
            [
                str(vpy),
                "-c",
                "import importlib.metadata as m; print(m.version('torch'))",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        version = (result.stdout or "").strip().splitlines()
        return {
            "ok": result.returncode == 0 and bool(version),
            "version": version[-1] if version else "",
            "returncode": result.returncode,
            "error_tail": (result.stderr or "")[-500:],
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _pinned(port: int = PINNED_PORT) -> Dict[str, Any]:
    return {
        "model": PINNED_MODEL,
        "model_repo": PINNED_MODEL_REPO,
        "dim": PINNED_DIM,
        "port": port,
        "base_url": f"http://127.0.0.1:{port}",
        "backend": PINNED_BACKEND,
        "size_hint": SIZE_HINT,
    }


def scaffold_layout(
    root: Optional[Path] = None,
    *,
    port: int = PINNED_PORT,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Create dirs + server.py + start_embed.bat + requirements + README. No pip/download."""
    emb = embedding_root(root)
    created: List[str] = []
    planned: List[str] = []
    dirs = [
        emb,
        emb / "models",
        emb / "models" / PINNED_MODEL,
        emb / "bin",  # leftover slot; no TEI required
        emb / "logs",
    ]
    for d in dirs:
        if dry_run:
            planned.append(f"mkdir {d}")
        else:
            d.mkdir(parents=True, exist_ok=True)
            created.append(str(d))

    files: Dict[str, str] = {
        _LAUNCH_BAT: _start_embed_bat_text(port),
        _REQ_TXT: _requirements_text(),
        "README.md": _readme_text(),
    }
    written: List[str] = []
    for name, text in files.items():
        path = emb / name
        if dry_run:
            planned.append(f"write {path}")
        else:
            path.write_text(text, encoding="utf-8")
            written.append(str(path))

    server_path = emb / _SERVER_PY
    if dry_run:
        planned.append(f"write {server_path}")
    else:
        write_server_py(server_path)
        written.append(str(server_path))

    # deprecate notice if old start_tei.bat exists — leave it; prefer start_embed
    return {
        "ok": True,
        "dry_run": dry_run,
        "root": str(emb),
        "created_dirs": created,
        "written": written,
        "planned": planned,
        "pinned": _pinned(port),
        "note": "scaffold only; venv/pip/model via install_embedding --yes",
    }


def _probe_torch(vpy: Path) -> Dict[str, Any]:
    """Import torch in the addon interpreter; captures DLL/runtime failures."""
    try:
        probe_code = (
            "import json,torch;"
            "print(json.dumps({'version':torch.__version__,"
            "'cuda_build':torch.version.cuda,"
            "'cuda_available':torch.cuda.is_available(),"
            "'device_count':torch.cuda.device_count()}))"
        )
        result = subprocess.run(
            [str(vpy), "-c", probe_code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        stdout = (result.stdout or "").strip()
        details: Dict[str, Any] = {}
        if stdout:
            try:
                details = json.loads(stdout.splitlines()[-1])
            except (TypeError, ValueError):
                details = {"version": stdout.splitlines()[-1]}
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            **details,
            "error_tail": (result.stderr or "")[-1200:],
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def ensure_windows_vc_runtime(
    root: Optional[Path] = None,
    *,
    venv_python: Optional[Path] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Ensure the Microsoft VC++ runtime required by Windows PyTorch is usable."""
    emb = embedding_root(root)
    vpy = Path(venv_python) if venv_python else emb / _VENV_DIR / "Scripts" / "python.exe"
    installer = emb / "bin" / _VC_RUNTIME_EXE
    if os.name != "nt":
        return {"ok": True, "skipped": "non_windows"}
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "probe": [str(vpy), "-c", "import torch"],
            "download": _VC_RUNTIME_URL,
            "installer": str(installer),
            "note": "runs only when torch reports a missing Microsoft VC++ runtime",
        }
    if not vpy.is_file():
        return {"ok": False, "error": "addon venv python missing for torch runtime probe"}
    before = _probe_torch(vpy)
    if before.get("ok"):
        return {"ok": True, "skipped": "torch_import_ready", "probe": before}

    detail = str(before.get("error_tail") or before.get("error") or "")
    if "c10.dll" not in detail and "Visual C++ Redistributable" not in detail:
        return {
            "ok": False,
            "error": "torch import failed for a non-VC-runtime reason",
            "probe": before,
        }

    installer.parent.mkdir(parents=True, exist_ok=True)
    try:
        if not installer.is_file() or installer.stat().st_size < 1_000_000:
            import urllib.parse
            import urllib.request

            part = installer.with_suffix(installer.suffix + ".part")
            req = urllib.request.Request(
                _VC_RUNTIME_URL,
                headers={"User-Agent": "G4W-Embedding-Installer/1.0"},
            )
            print(f"[install] vc_runtime_download: {_VC_RUNTIME_URL}", flush=True)
            with urllib.request.urlopen(req, timeout=60) as response:
                final_host = (urllib.parse.urlparse(response.geturl()).hostname or "").lower()
                if final_host != "aka.ms" and not final_host.endswith(".microsoft.com"):
                    raise RuntimeError(f"unexpected VC runtime download host: {final_host}")
                total = int(response.headers.get("Content-Length") or 0)
                received = 0
                with part.open("wb") as output:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                        received += len(chunk)
                        if received % (5 * 1024 * 1024) < len(chunk):
                            progress = f"[install] vc_runtime_download: {received // (1024 * 1024)} MB"
                            if total:
                                progress += f" / {total // (1024 * 1024)} MB"
                            print(progress, flush=True)
            with part.open("rb") as downloaded:
                magic = downloaded.read(2)
            if part.stat().st_size < 1_000_000 or magic != b"MZ":
                raise RuntimeError("downloaded VC runtime installer is invalid")
            os.replace(str(part), str(installer))
    except Exception as exc:
        return {
            "ok": False,
            "error": f"VC runtime download failed: {type(exc).__name__}: {exc}",
            "probe": before,
            "url": _VC_RUNTIME_URL,
        }

    try:
        print("[install] vc_runtime_install: Microsoft VC++ 2015-2022 x64", flush=True)
        installed = subprocess.run(
            [str(installer), "/install", "/quiet", "/norestart"],
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": f"VC runtime installer failed: {type(exc).__name__}: {exc}",
            "probe": before,
            "installer": str(installer),
        }
    accepted = installed.returncode in (0, 1638, 3010)
    after = _probe_torch(vpy) if accepted else before
    if accepted and after.get("ok"):
        return {
            "ok": True,
            "installed": True,
            "returncode": installed.returncode,
            "reboot_recommended": installed.returncode == 3010,
            "probe": after,
        }
    return {
        "ok": False,
        "error": (
            "Microsoft VC++ runtime installation did not make torch usable; "
            "accept the UAC prompt and rerun, or reboot if requested"
        ),
        "returncode": installed.returncode,
        "probe_before": before,
        "probe_after": after,
        "installer": str(installer),
    }


def ensure_venv(
    root: Optional[Path] = None,
    *,
    dry_run: bool = False,
    pip_install: bool = True,
) -> Dict[str, Any]:
    """Create G4W-embedding/.venv with base python; optionally pip install reqs."""
    emb = embedding_root(root)
    venv_dir = emb / _VENV_DIR
    base_py = _find_base_python()
    # Portable-local pip cache and temp dirs (never touch the user's C: %TEMP%/pip cache)
    portable_root = emb.parent.parent
    pip_cache_dir = portable_root / ".pip-cache"
    tmp_dir = portable_root / ".tmp"
    pip_cache_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    out: Dict[str, Any] = {
        "ok": False,
        "base_python": str(base_py),
        "venv_dir": str(venv_dir),
        "venv_python": None,
        "steps": [],
    }
    if dry_run:
        out["ok"] = True
        out["dry_run"] = True
        vpy = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        out["steps"].append({"action": "venv", "cmd": [str(base_py), "-m", "venv", str(venv_dir)]})
        if pip_install:
            out["steps"].append(
                {
                    "action": "pip",
                    "req": str(emb / _REQ_TXT),
                    "torch": _torch_install_plan(),
                }
            )
        out["steps"].append(
            {"vc_runtime": ensure_windows_vc_runtime(emb, venv_python=vpy, dry_run=True)}
        )
        return out

    emb.mkdir(parents=True, exist_ok=True)
    vpy = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not vpy.is_file():
        try:
            r = subprocess.run(
                [str(base_py), "-m", "venv", str(venv_dir)],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            out["steps"].append(
                {
                    "action": "venv",
                    "returncode": r.returncode,
                    "stderr": (r.stderr or "")[-400:],
                }
            )
            if r.returncode != 0 or not vpy.is_file():
                out["error"] = f"venv create failed rc={r.returncode}"
                return out
        except Exception as exc:
            out["error"] = f"venv: {type(exc).__name__}: {exc}"
            return out
    else:
        out["steps"].append({"action": "venv", "skipped": "already_exists"})

    out["venv_python"] = str(vpy)
    if pip_install:
        req = emb / _REQ_TXT
        if not req.is_file():
            req.write_text(_requirements_text(), encoding="utf-8")
        try:
            # Prefer CN mirror when reachable; fall back to default PyPI.
            # 优先级：环境变量 G4W_PIP_INDEX_URL → 配置（测速选优）→ 内置池探测
            index_args: List[str] = []
            pip_override = (os.environ.get("G4W_PIP_INDEX_URL") or "").strip().rstrip("/")
            if not pip_override:
                try:
                    pip_override = (em.resolve(_state_dir())["pip"] or [""])[0].rstrip("/")
                except Exception:
                    pip_override = ""
            if pip_override:
                index_args = ["-i", pip_override, "--trusted-host", pip_override.split("//", 1)[1].split("/")[0]]
            else:
                for mirror in (
                    "https://pypi.tuna.tsinghua.edu.cn/simple",
                    "https://mirrors.aliyun.com/pypi/simple",
                ):
                    try:
                        import urllib.request as _u

                        _u.urlopen(mirror + "/pip/", timeout=5)
                        index_args = ["-i", mirror, "--trusted-host", mirror.split("//", 1)[1].split("/")[0]]
                        break
                    except Exception:
                        continue

            def _pip_stream(cmd: List[str], timeout_s: int, action: str) -> int:
                """Run pip with live stdout (no capture_output hang illusion)."""
                print(f"[install] {action}: {' '.join(cmd)}", flush=True)
                child_env = dict(os.environ)
                child_env.update(
                    {
                        "PIP_CACHE_DIR": str(pip_cache_dir),
                        "TMP": str(tmp_dir),
                        "TEMP": str(tmp_dir),
                    }
                )
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=child_env,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                tail: List[str] = []
                assert proc.stdout is not None
                try:
                    import time as _time

                    deadline = _time.time() + timeout_s
                    while True:
                        if _time.time() > deadline:
                            proc.kill()
                            out["steps"].append({"action": action, "error": "timeout", "tail": tail[-20:]})
                            return 124
                        line = proc.stdout.readline()
                        if line:
                            print(line.rstrip(), flush=True)
                            tail.append(line.rstrip())
                            if len(tail) > 80:
                                tail = tail[-80:]
                        elif proc.poll() is not None:
                            break
                        else:
                            _time.sleep(0.05)
                    rc = int(proc.wait(timeout=5) or 0)
                except Exception as exc:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    out["steps"].append({"action": action, "error": f"{type(exc).__name__}: {exc}", "tail": tail[-20:]})
                    return 1
                out["steps"].append({"action": action, "returncode": rc, "stdout_tail": tail[-30:]})
                return rc

            up_cmd = [str(vpy), "-m", "pip", "install", "--upgrade", "pip", *index_args]
            if _pip_stream(up_cmd, 180, "pip_upgrade") != 0:
                # non-fatal: continue with existing pip
                print("[install] pip upgrade failed (continue)", flush=True)

            torch_plan = _torch_install_plan()
            out["torch"] = torch_plan
            mode = str(torch_plan["resolved"])
            channel = str(torch_plan["channel"])
            gpu_names = torch_plan.get("nvidia", {}).get("gpus") or []
            print(
                f"[install] torch mode={mode} channel={channel}"
                + (f" gpu={'; '.join(gpu_names)}" if gpu_names else ""),
                flush=True,
            )

            before_torch = _probe_torch(vpy)
            torch_ready = bool(before_torch.get("ok"))
            if mode == "gpu":
                torch_ready = bool(
                    torch_ready
                    and before_torch.get("cuda_build")
                    and before_torch.get("cuda_available")
                )
            if torch_ready:
                out["steps"].append(
                    {"action": f"pip_torch_{mode}", "skipped": "compatible_torch_already_installed", "probe": before_torch}
                )
            else:
                for index_no, index_url in enumerate(torch_plan["indexes"], 1):
                    torch_cmd = [
                        str(vpy),
                        "-m",
                        "pip",
                        "install",
                        "--upgrade",
                        "--prefer-binary",
                        "--timeout",
                        "30",
                        "--retries",
                        "3",
                        *_torch_source_args(str(index_url)),
                        "torch",
                    ]
                    if before_torch.get("ok"):
                        torch_cmd.insert(6, "--force-reinstall")
                    action = f"pip_torch_{mode}_{index_no}"
                    trc = _pip_stream(torch_cmd, 3600, action)
                    if trc != 0:
                        print(f"[install] {action} failed; trying next PyTorch index", flush=True)
                        continue
                    probe = _probe_torch_package(vpy)
                    out["steps"].append({"action": f"probe_torch_package_{mode}", "probe": probe})
                    version = str(probe.get("version") or "").lower()
                    torch_ready = bool(probe.get("ok"))
                    if mode == "gpu":
                        torch_ready = bool(torch_ready and "+cu" in version)
                    if torch_ready:
                        break
                    print(
                        f"[install] installed Torch package does not match {mode} mode; trying next index",
                        flush=True,
                    )
                    before_torch = _probe_torch(vpy)
            if not torch_ready:
                out["error"] = (
                    "GPU Torch installation failed or CUDA is unavailable; check NVIDIA driver/GPU passthrough, "
                    "or set G4W_TORCH_MODE=cpu explicitly"
                    if mode == "gpu"
                    else "CPU Torch installation failed from all configured indexes"
                )
                return out

            r2 = _pip_stream(
                [str(vpy), "-m", "pip", "install", "-r", str(req), *index_args],
                3600,
                "pip_requirements",
            )
            if r2 != 0:
                out["error"] = f"pip install failed rc={r2}"
                out["ok"] = False
                return out
            final_torch = _probe_torch(vpy)
            if not final_torch.get("ok") or (
                mode == "gpu"
                and (not final_torch.get("cuda_build") or not final_torch.get("cuda_available"))
            ):
                out["error"] = "Torch validation failed after installing sentence-transformers requirements"
                out["torch_probe"] = final_torch
                return out
        except Exception as exc:
            out["error"] = f"pip: {type(exc).__name__}: {exc}"
            return out
    vc_runtime = ensure_windows_vc_runtime(emb, venv_python=vpy, dry_run=False)
    out["steps"].append({"vc_runtime": vc_runtime})
    if not vc_runtime.get("ok"):
        out["error"] = vc_runtime.get("error") or "Microsoft VC++ runtime is not ready"
        return out
    out["ok"] = True
    return out


def write_config(
    *,
    installed: bool,
    enabled: bool = False,
    port: int = PINNED_PORT,
    dry_run: bool = False,
    require_ready: bool = False,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Write embedding-root vector_config.json.

    ``require_ready``: only mark installed if probe says launchable.
    Never forces enabled=true.
    """
    emb = embedding_root(root)
    probe = probe_layout(emb)
    if require_ready and installed and not probe.get("launchable"):
        return {
            "ok": False,
            "error": "not launchable; refuse installed=true",
            "probe": probe,
            "hint": "need .venv+server.py+model or start_embed.bat+model",
        }
    payload = {
        "installed": bool(installed),
        "enabled": bool(enabled),  # install path should pass False
        "model": PINNED_MODEL,
        "dim": PINNED_DIM,
        "port": int(port),
        "base_url": f"http://127.0.0.1:{port}",
        "backend": PINNED_BACKEND,
        "embed_health": None,
        "tei_health": None,  # compat key
        "pid": None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if dry_run:
        return {"ok": True, "dry_run": True, "would_write": payload, "probe": probe}

    # Prefer writing via vector_config so cache/path rules apply; force primary under emb
    cfg_path = emb / "vector_config.json"
    emb.mkdir(parents=True, exist_ok=True)
    # merge with existing
    existing: Dict[str, Any] = {}
    if cfg_path.is_file():
        try:
            existing = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        except Exception:
            existing = {}
    merged = dict(existing)
    merged.update(payload)
    # preserve enabled if already set and caller left default False? Install should not clobber on.
    # Spec: install never sets enabled=true; if user already enabled, keep it unless explicit.
    if not enabled and existing.get("enabled") is True and installed:
        merged["enabled"] = True
    if not enabled and not installed:
        merged["enabled"] = bool(existing.get("enabled", False))

    cfg_path.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        vc.reset_cache_for_tests()
    except Exception:
        pass
    return {"ok": True, "path": str(cfg_path), "config": merged, "probe": probe}


def _state_dir() -> Path:
    return runtime_root() / "G4W-data"


def _model_endpoints(explicit: Optional[Sequence[str]] = None) -> List[str]:
    """Ordered model endpoints: explicit/env first, config (speedtest) pick,
    CN mirror, then official."""
    raw: List[str] = []
    if explicit:
        raw.extend(str(x or "").strip() for x in explicit)
    else:
        for key in ("G4W_HF_ENDPOINT", "G4W_HF_ENDPOINT", "HF_ENDPOINT"):
            value = (os.environ.get(key) or "").strip()
            if value:
                raw.append(value)
        if not raw:
            try:
                configured = (em.resolve(_state_dir())["hf"] or [""])[0]
                if configured:
                    raw.append(configured)
            except Exception:
                pass
        raw.extend(_DEFAULT_MODEL_ENDPOINTS)
    out: List[str] = []
    seen = set()
    for value in raw:
        endpoint = value.rstrip("/")
        if endpoint and endpoint not in seen:
            seen.add(endpoint)
            out.append(endpoint)
    return out


def _stream_command(
    cmd: List[str],
    *,
    timeout_s: int,
    action: str,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Run a long installer command with visible progress and a short tail."""
    print(f"[install] {action}: {' '.join(cmd)}", flush=True)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    tail: List[str] = []
    assert proc.stdout is not None
    try:
        import time as _time

        deadline = _time.time() + timeout_s
        while True:
            if _time.time() > deadline:
                proc.kill()
                return {"action": action, "returncode": 124, "error": "timeout", "tail": tail[-20:]}
            line = proc.stdout.readline()
            if line:
                clean = line.rstrip()
                print(clean, flush=True)
                tail.append(clean)
                if len(tail) > 80:
                    tail = tail[-80:]
            elif proc.poll() is not None:
                break
            else:
                _time.sleep(0.05)
        rc = int(proc.wait(timeout=5) or 0)
        return {"action": action, "returncode": rc, "stdout_tail": tail[-30:]}
    except Exception as exc:
        try:
            proc.kill()
        except Exception:
            pass
        return {
            "action": action,
            "returncode": 1,
            "error": f"{type(exc).__name__}: {exc}",
            "tail": tail[-20:],
        }


def download_model(
    root: Optional[Path] = None,
    *,
    dry_run: bool = False,
    endpoints: Optional[Sequence[str]] = None,
    timeout_s: int = 7200,
) -> Dict[str, Any]:
    """Download the pinned model in the addon venv with mirror fallback/resume."""
    emb = embedding_root(root)
    model_dir = emb / "models" / PINNED_MODEL
    ordered_endpoints = _model_endpoints(endpoints)
    expected_vpy = emb / _VENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    # Portable-local temp dir for HF downloads (never the user's C: %TEMP%)
    portable_root = emb.parent.parent
    tmp_dir = portable_root / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    before = probe_layout(emb)
    if before.get("model_ok"):
        return {
            "ok": True,
            "skipped": "model_already_ready",
            "repo": PINNED_MODEL_REPO,
            "model_dir": str(model_dir),
            "probe": before,
        }
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "repo": PINNED_MODEL_REPO,
            "model_dir": str(model_dir),
            "venv_python": str(expected_vpy),
            "endpoints": ordered_endpoints,
            "resume": True,
        }

    vpy_raw = before.get("venv_python")
    vpy = Path(vpy_raw) if vpy_raw else expected_vpy
    if not vpy.is_file():
        return {
            "ok": False,
            "error": "addon venv python missing; cannot download model",
            "venv_python": str(vpy),
        }
    model_dir.mkdir(parents=True, exist_ok=True)
    code = (
        "import os,sys; "
        "from huggingface_hub import snapshot_download; "
        "repo,dest,endpoint=sys.argv[1:4]; "
        "os.environ['HF_ENDPOINT']=endpoint; "
        "snapshot_download(repo_id=repo, local_dir=dest, endpoint=endpoint)"
    )
    attempts: List[Dict[str, Any]] = []
    for endpoint in ordered_endpoints:
        child_env = dict(os.environ)
        child_env.update(
            {
                "HF_ENDPOINT": endpoint,
                "HF_HOME": str(emb / ".cache" / "huggingface"),
                "HF_HUB_DISABLE_TELEMETRY": "1",
                "HF_HUB_DISABLE_XET": "1",
                "HF_HUB_DOWNLOAD_TIMEOUT": "120",
                "HF_HUB_ETAG_TIMEOUT": "20",
                "TMP": str(tmp_dir),
                "TEMP": str(tmp_dir),
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
            }
        )
        result = _stream_command(
            [str(vpy), "-u", "-c", code, PINNED_MODEL_REPO, str(model_dir), endpoint],
            timeout_s=timeout_s,
            action=f"model_download[{endpoint}]",
            env=child_env,
        )
        attempts.append(result)
        after = probe_layout(emb)
        if result.get("returncode") == 0 and after.get("model_ok"):
            return {
                "ok": True,
                "repo": PINNED_MODEL_REPO,
                "model_dir": str(model_dir),
                "endpoint": endpoint,
                "resume": True,
                "attempts": attempts,
                "probe": after,
            }
        print(f"[install] model endpoint failed or incomplete: {endpoint}; trying next", flush=True)
    return {
        "ok": False,
        "error": "model download failed or incomplete on all endpoints",
        "repo": PINNED_MODEL_REPO,
        "model_dir": str(model_dir),
        "endpoints": ordered_endpoints,
        "attempts": attempts,
        "probe": probe_layout(emb),
    }


def run_install(
    *,
    yes: bool = False,
    dry_run: bool = False,
    scaffold_only: bool = False,
    mark_only: bool = False,
    skip_pip: bool = False,
    skip_model_download: bool = False,
    port: int = PINNED_PORT,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Full install pipeline. Never starts long-running server; never enables product gate."""
    if not (yes or dry_run or scaffold_only or mark_only):
        return {
            "ok": False,
            "error": "refusing: pass --yes (or --dry-run / --scaffold-only / --mark-only)",
            "size_hint": SIZE_HINT,
        }

    emb = embedding_root(root)
    steps: List[Dict[str, Any]] = []

    sc = scaffold_layout(emb, port=port, dry_run=dry_run)
    steps.append({"scaffold": sc})
    if not sc.get("ok"):
        return {"ok": False, "steps": steps, "error": "scaffold failed"}

    if scaffold_only:
        return {
            "ok": True,
            "dry_run": dry_run,
            "target": str(emb),
            "pinned": _pinned(port),
            "steps": steps,
            "note": "scaffold_only — no venv/pip/model/mark",
        }

    if not mark_only:
        ven = ensure_venv(emb, dry_run=dry_run, pip_install=not skip_pip)
        steps.append({"venv": ven})
        if not dry_run and not ven.get("ok"):
            w = write_config(
                installed=False,
                enabled=False,
                port=port,
                dry_run=False,
                require_ready=False,
                root=emb,
            )
            steps.append({"config": w})
            return {
                "ok": False,
                "error": ven.get("error") or "venv/pip failed",
                "steps": steps,
                "target": str(emb),
                "hint": "fix pip, then rerun --yes; the model download will resume",
            }

        if skip_model_download:
            model = {
                "ok": True,
                "skipped": "requested_by_flag",
                "repo": PINNED_MODEL_REPO,
                "model_dir": str(emb / "models" / PINNED_MODEL),
            }
        else:
            model = download_model(emb, dry_run=dry_run)
        steps.append({"model_download": model})
        if not dry_run and not model.get("ok"):
            w = write_config(
                installed=False,
                enabled=False,
                port=port,
                dry_run=False,
                require_ready=False,
                root=emb,
            )
            steps.append({"config": w})
            return {
                "ok": False,
                "error": model.get("error") or "model download failed",
                "steps": steps,
                "target": str(emb),
                "hint": (
                    "rerun to resume; optionally set G4W_HF_ENDPOINT, "
                    "or use --skip-model-download for a manual/offline model"
                ),
            }

    probe = probe_layout(emb)
    steps.append({"probe": probe})
    # A successful install/mark means the local server, addon venv and complete
    # model files are all present. Partial downloads stay explicitly uninstalled.
    want_installed = bool(probe.get("launchable"))

    w = write_config(
        installed=want_installed,
        enabled=False,
        port=port,
        dry_run=dry_run,
        require_ready=want_installed,
        root=emb,
    )
    if not want_installed and not dry_run:
        w = {
            **w,
            "ok": w.get("ok", True),
            "warning": "installed left false — missing/incomplete model or venv; rerun to resume",
        }
    steps.append({"config": w})
    result = {
        "ok": bool(sc.get("ok")) and bool(w.get("ok")) and (dry_run or want_installed),
        "ready": want_installed,
        "dry_run": dry_run,
        "target": str(emb),
        "pinned": _pinned(port),
        "steps": steps,
        "enabled_note": "install does not set enabled; use /vector on",
        "backend": PINNED_BACKEND,
    }
    if not want_installed and not dry_run:
        result.update(
            {
                "error": "embedding layout is not ready",
                "hint": (
                    "rerun --yes to resume automatic model download, or place complete model "
                    "weights manually and rerun --yes --mark-only"
                ),
            }
        )
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Install G4W-embedding (ST HTTP)")
    p.add_argument("--yes", "-y", action="store_true", help="allow real install side effects")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--scaffold-only", action="store_true")
    p.add_argument("--mark-only", action="store_true", help="probe + write config only")
    p.add_argument("--skip-pip", action="store_true", help="create venv but skip pip install")
    p.add_argument(
        "--skip-model-download",
        action="store_true",
        help="do not download the model (manual/offline model placement)",
    )
    p.add_argument("--port", type=int, default=PINNED_PORT)
    p.add_argument(
        "--root",
        default=None,
        help="runtime or G4W-embedding path (default: sibling of G4W-main)",
    )
    args = p.parse_args(list(argv) if argv is not None else None)
    root = Path(args.root).resolve() if args.root else None
    result = run_install(
        yes=args.yes or args.dry_run,
        dry_run=args.dry_run,
        scaffold_only=args.scaffold_only,
        mark_only=args.mark_only,
        skip_pip=args.skip_pip,
        skip_model_download=args.skip_model_download,
        port=args.port,
        root=root,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
