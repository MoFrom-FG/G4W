# -*- coding: utf-8 -*-
"""Embedding 镜像管理与测速选优（看板配置页 ↔ install_embedding.py 共用）。

配置文件：<state_dir>/embedding-config.json
{
  "network": "domestic" | "abroad",        # 默认 domestic
  "mirrors": {"pip": url, "torch": url, "hf": url},  # 最近一次测速选出的最优源
  "lastSpeedTestAt": 秒时间戳               # 测速结果缓存（1 小时）
}

优先级（从高到低）：
1. 环境变量显式覆盖（G4W_PIP_INDEX_URL / G4W_TORCH_INDEX_URL / G4W_HF_ENDPOINT）
2. 配置文件（network + 测速结果）
3. 内置默认（国内池按顺序回退，最后官方兜底）

本模块零第三方依赖（标准库），供看板后端与安装脚本直接 import。
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

CONFIG_NAME = "embedding-config.json"
SPEEDTEST_CACHE_SECONDS = 3600  # 测速结果缓存 1 小时

# ---- 内置镜像池（国内优先；每项含探测 URL） ----
_PIP_DOMESTIC = [
    ("阿里云", "https://mirrors.aliyun.com/pypi/simple/", "https://mirrors.aliyun.com/pypi/simple/pip/"),
    ("清华", "https://pypi.tuna.tsinghua.edu.cn/simple/", "https://pypi.tuna.tsinghua.edu.cn/simple/pip/"),
    ("南京大学", "https://mirror.nju.edu.cn/pypi/web/simple/", "https://mirror.nju.edu.cn/pypi/web/simple/pip/"),
    ("华为云", "https://mirrors.huaweicloud.com/repository/pypi/simple/", "https://mirrors.huaweicloud.com/repository/pypi/simple/pip/"),
    ("腾讯云", "https://mirrors.cloud.tencent.com/pypi/simple/", "https://mirrors.cloud.tencent.com/pypi/simple/pip/"),
]
_PIP_ABROAD = [
    ("官方 PyPI", "https://pypi.org/simple/", "https://pypi.org/simple/pip/"),
]

_TORCH_DOMESTIC = [
    ("南京大学", "https://mirrors.nju.edu.cn/pytorch/whl", "https://mirrors.nju.edu.cn/pytorch/whl/cu128/"),
    ("阿里云", "https://mirrors.aliyun.com/pytorch-wheels", "https://mirrors.aliyun.com/pytorch-wheels/cu128/"),
]
_TORCH_ABROAD = [
    ("官方 PyTorch", "https://download.pytorch.org/whl", "https://download.pytorch.org/whl/cu128/"),
]

_HF_DOMESTIC = [
    ("hf-mirror", "https://hf-mirror.com", "https://hf-mirror.com"),
]
_HF_ABROAD = [
    ("HuggingFace", "https://huggingface.co", "https://huggingface.co"),
]

POOL = {
    "pip": {"domestic": _PIP_DOMESTIC, "abroad": _PIP_ABROAD},
    "torch": {"domestic": _TORCH_DOMESTIC, "abroad": _TORCH_ABROAD},
    "hf": {"domestic": _HF_DOMESTIC, "abroad": _HF_ABROAD},
}

# 环境变量显式覆盖（最高优先级）
_ENV_KEYS = {"pip": "G4W_PIP_INDEX_URL", "torch": "G4W_TORCH_INDEX_URL", "hf": "G4W_HF_ENDPOINT"}


def config_path(state_dir: Path | str) -> Path:
    return Path(state_dir) / CONFIG_NAME


def load_config(state_dir: Path | str) -> Dict[str, Any]:
    default = {"network": "domestic", "mirrors": {}, "lastSpeedTestAt": 0}
    try:
        data = json.loads(config_path(state_dir).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return default
        merged = dict(default)
        merged.update({k: data[k] for k in ("network", "mirrors", "lastSpeedTestAt") if k in data})
        if merged["network"] not in ("domestic", "abroad"):
            merged["network"] = "domestic"
        if not isinstance(merged["mirrors"], dict):
            merged["mirrors"] = {}
        return merged
    except Exception:
        return default


def save_config(state_dir: Path | str, config: Dict[str, Any]) -> None:
    path = config_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _probe_latency(url: str, timeout: float = 4.0) -> Optional[float]:
    """返回连接+首字节延迟（秒）；失败返回 None。"""
    import urllib.request

    start = time.monotonic()
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "G4W/1.0", "Range": "bytes=0-0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(1)
            return time.monotonic() - start
    except Exception:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "G4W/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp.read(1)
                return time.monotonic() - start
        except Exception:
            return None


def speedtest(state_dir: Path | str, *, force: bool = False, timeout: float = 4.0) -> Dict[str, Any]:
    """并发测速全部候选源，写回 config.mirrors（每类最优）并缓存 1 小时。

    返回：
    {
      "ok": true, "network": "...", "cached": bool,
      "results": {"pip": [{"name", "url", "latency_ms", "reachable"}...], ...},
      "picked": {"pip": url, "torch": url, "hf": url}
    }
    """
    state = Path(state_dir)
    config = load_config(state)
    now = int(time.time())
    if (
        not force
        and config.get("lastSpeedTestAt")
        and now - int(config.get("lastSpeedTestAt") or 0) < SPEEDTEST_CACHE_SECONDS
        and config.get("mirrors")
    ):
        picked = {k: config["mirrors"].get(k) for k in POOL if config["mirrors"].get(k)}
        return {"ok": True, "network": config["network"], "cached": True, "results": {}, "picked": picked}

    network = config.get("network", "domestic")
    results: Dict[str, List[Dict[str, Any]]] = {}
    picked: Dict[str, str] = {}

    def _measure(kind: str, name: str, url: str, probe: str) -> Dict[str, Any]:
        ms = _probe_latency(probe, timeout=timeout)
        return {"name": name, "url": url, "latency_ms": int(ms * 1000) if ms is not None else None, "reachable": ms is not None}

    with ThreadPoolExecutor(max_workers=10) as pool:
        for kind, by_network in POOL.items():
            entries = by_network[network]
            futures = [pool.submit(_measure, kind, name, url, probe) for name, url, probe in entries]
            items = [f.result() for f in futures]
            reachable = [it for it in items if it["reachable"]]
            reachable.sort(key=lambda it: it["latency_ms"])
            results[kind] = reachable + [it for it in items if not it["reachable"]]
            if reachable:
                picked[kind] = reachable[0]["url"]

    config["network"] = network
    config["mirrors"] = picked
    config["lastSpeedTestAt"] = now
    save_config(state, config)
    return {"ok": True, "network": network, "cached": False, "results": results, "picked": picked}


def resolve(state_dir: Path | str) -> Dict[str, List[str]]:
    """按优先级展开每类的有序源列表（配置最优 → 池顺序 → 官方兜底）。

    install 侧按此顺序逐个尝试；无配置时即池本身顺序。
    """
    config = load_config(state_dir)
    network = config.get("network", "domestic")
    out: Dict[str, List[str]] = {}
    for kind, by_network in POOL.items():
        entries = by_network[network]
        pool_urls = {url for _name, url, _probe in entries}
        ordered: List[str] = []
        seen: set[str] = set()
        configured = config.get("mirrors", {}).get(kind)
        if configured and configured not in pool_urls:
            configured = None  # 测速结果与当前 network 不匹配（切国外后残留国内源）→ 忽略
        for url in (configured,):
            if url and url not in seen:
                seen.add(url)
                ordered.append(url)
        for _name, url, _probe in entries:
            if url not in seen:
                seen.add(url)
                ordered.append(url)
        out[kind] = ordered
    return out


def env_override(kind: str) -> Optional[str]:
    """环境变量显式覆盖（G4W_PIP_INDEX_URL / G4W_TORCH_INDEX_URL / G4W_HF_ENDPOINT）。"""
    value = os.environ.get(_ENV_KEYS.get(kind, ""), "").strip().rstrip("/")
    return value or None


def install_env(state_dir: Path | str) -> Dict[str, str]:
    """组装安装进程要注入的环境变量（配置测速结果 → 环境变量）。"""
    env = {}
    resolved = resolve(state_dir)
    if resolved.get("pip"):
        env["G4W_PIP_INDEX_URL"] = resolved["pip"][0]
    if resolved.get("torch"):
        env["G4W_TORCH_INDEX_URL"] = resolved["torch"][0]
    if resolved.get("hf"):
        env["G4W_HF_ENDPOINT"] = resolved["hf"][0]
    return env


_STATUS_CACHE: Dict[str, Any] = {"at": 0, "data": None}
_STATUS_CACHE_SECONDS = 60


def installed_status(embedding_root: Path | str, *, force: bool = False) -> Dict[str, Any]:
    """探测 G4W-embedding 安装状态（供配置页/向导显示）。

    注意：torch 探测会 spawn 子进程（venv python 是 redirector），
    必须加 CREATE_NO_WINDOW 防弹控制台窗口；结果缓存 60 秒避免反复探测。
    """
    root = Path(embedding_root)
    now = time.time()
    cached = _STATUS_CACHE.get("data")
    if cached and not force and now - float(_STATUS_CACHE.get("at") or 0) < _STATUS_CACHE_SECONDS:
        return cached

    venv_py = root / ".venv" / "Scripts" / "python.exe"
    config_file = root / "vector_config.json"
    installed = venv_py.is_file()
    enabled = False
    port = 8081
    backend = "st"
    if config_file.is_file():
        try:
            data = json.loads(config_file.read_text(encoding="utf-8-sig"))
            enabled = bool(data.get("enabled"))
            port = int(data.get("port") or 8081)
            backend = str(data.get("backend") or "st")
        except Exception:
            pass
    mode = ""
    if installed:
        try:
            import subprocess

            flags = 0
            if os.name == "nt":
                flags = subprocess.CREATE_NO_WINDOW  # venv redirector 子进程会弹控制台
            result = subprocess.run(
                [str(venv_py), "-c", "import json,torch;print(json.dumps({'v':torch.__version__,'c':torch.version.cuda}))"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                creationflags=flags,
            )
            if result.returncode == 0:
                info = json.loads(result.stdout.splitlines()[-1])
                mode = "CUDA " + str(info.get("c") or "?") if info.get("c") else "CPU"
        except Exception:
            pass
    data = {"installed": installed, "enabled": enabled, "port": port, "backend": backend, "mode": mode}
    _STATUS_CACHE.update({"at": time.time(), "data": data})
    return data


if __name__ == "__main__":
    import sys

    # 默认状态目录从包位置推导（不写死开发机路径）；G4W_STATE_DIR 可覆盖
    default_state = Path(os.environ.get("G4W_STATE_DIR") or (Path(__file__).resolve().parents[4] / "G4W-data"))
    state = Path(sys.argv[1]) if len(sys.argv) > 1 else default_state
    print(json.dumps(speedtest(state, force=True), ensure_ascii=False, indent=1))
