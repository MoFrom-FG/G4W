"""首次使用向导的四步判定（跨平台，与桌面壳 wizard_status 语义保持一致）。"""
from __future__ import annotations

import os
from pathlib import Path

from .config import GA_APP_DIR, MAIN_DIR, RUNTIME_DIR
from .platform_adapt import venv_python

STEP_ORDER = ("prepare", "key", "env", "login")


def state_dir() -> Path:
    override = os.environ.get("G4W_STATE_DIR")
    if override:
        return Path(override)
    return RUNTIME_DIR / "G4W-data"


def portable_root() -> Path:
    return RUNTIME_DIR.parent


def venv_python_path() -> Path:
    return venv_python(GA_APP_DIR / ".venv")


def _read_env_values() -> dict:
    path = MAIN_DIR / ".env"
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def setup_status() -> dict:
    force = os.environ.get("G4W_FORCE_WIZARD") == "1"
    env_values = _read_env_values()
    accounts = state_dir() / "accounts"
    login_ok = False
    try:
        login_ok = accounts.is_dir() and any(item.suffix == ".json" for item in accounts.iterdir())
    except OSError:
        login_ok = False
    prepare_ok = venv_python_path().is_file()
    mykey_path = GA_APP_DIR / "mykey.py"
    key_ok = mykey_path.is_file()
    # 向导允许“先跳过填 Key”：此时文件已按模板建好（空密钥）并留了标记，
    # 之后在控制中心「环境配置 → 模型配置」里填任意 OpenAI 兼容模型即可。
    key_skipped = (state_dir() / ".model-key-skipped").is_file()
    if key_skipped and key_ok:
        try:
            key_skipped = "apikey': ''" in mykey_path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            key_skipped = False
    env_ok = "G4W_USER_NAME" in env_values and "G4W_BOT_NAME" in env_values
    if force:
        prepare_ok = key_ok = env_ok = login_ok = False
    return {
        "prepare": {"ok": prepare_ok, "detail": str(venv_python_path())},
        "key": {"ok": key_ok, "detail": str(mykey_path), "skipped": bool(key_skipped)},
        "env": {"ok": env_ok, "preset": env_values},
        "login": {"ok": login_ok, "detail": str(accounts)},
        "all_ok": prepare_ok and key_ok and env_ok and login_ok,
        "portableRoot": str(portable_root()),
        "stateDir": str(state_dir()),
    }


def gate_active() -> bool:
    if os.environ.get("G4W_SKIP_SETUP_GATE") == "1":
        return False
    return not setup_status()["all_ok"]
