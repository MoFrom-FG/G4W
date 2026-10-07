import getpass
import json
import os
import shutil
import time
from pathlib import Path

from ..core.config import GA_APP_DIR, MAIN_DIR, RUNTIME_DIR, _read_env_file
from ..core.platform_adapt import portable_python, venv_python


PORTABLE_ROOT = RUNTIME_DIR.parent
STATE_DIR = RUNTIME_DIR / "G4W-data"
ENV_FILE = MAIN_DIR / ".env"
KEY_TEMPLATE = MAIN_DIR / "G4W" / "templates" / "setup" / "mykey-G4W-template.py"
MYKEY_FILE = GA_APP_DIR / "mykey.py"
VECTOR_INDEX_DIR = RUNTIME_DIR / "G4W-vector-index"

MODEL_CHOICES = {
    "0": "deepseek-v4-pro",
    "1": "deepseek-v4-flash",
}
THEME_CHOICES = {
    "0": "default",
    "1": "neko",
}
GENDER_CHOICES = {
    "0": "male",
    "1": "female",
    "2": "neutral",
}


def _timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _backup(path: Path) -> Path | None:
    if not path.is_file():
        return None
    backup = path.with_name(f"{path.stem}.backup-{_timestamp()}{path.suffix}")
    suffix = 1
    while backup.exists():
        backup = path.with_name(f"{path.stem}.backup-{_timestamp()}-{suffix}{path.suffix}")
        suffix += 1
    shutil.copy2(path, backup)
    return backup


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def prepare_portable() -> dict:
    required = {
        "portablePython": portable_python(PORTABLE_ROOT),
        "runtimePython": venv_python(GA_APP_DIR / ".venv"),
        "gaAgent": GA_APP_DIR / "agentmain.py",
        "G4W": MAIN_DIR / "G4W" / "cli" / "main.py",
        "keyTemplate": KEY_TEMPLATE,
    }
    missing = {name: str(path) for name, path in required.items() if not path.is_file()}
    for path in (
        STATE_DIR,
        STATE_DIR / "accounts",
        STATE_DIR / "memory",
        STATE_DIR / "memory" / "conversations",
        STATE_DIR / "memory" / "persona",
        STATE_DIR / "memory" / "sop",
        STATE_DIR / "workers",
        STATE_DIR / "timeline",
    ):
        path.mkdir(parents=True, exist_ok=True)
    return {
        "ok": not missing,
        "portableRoot": str(PORTABLE_ROOT),
        "runtimeDir": str(RUNTIME_DIR),
        "gaAppDir": str(GA_APP_DIR),
        "G4WHome": str(MAIN_DIR),
        "stateDir": str(STATE_DIR),
        "envFile": str(ENV_FILE),
        "pathsAreLocationDerived": True,
        "missing": missing,
    }


def configure_ga_key(api_key: str, *, replace_existing: bool = False) -> dict:
    key = str(api_key or "").strip()
    if not key:
        raise ValueError("API Key cannot be empty")
    if not KEY_TEMPLATE.is_file():
        raise FileNotFoundError(f"G4W GA key template is missing: {KEY_TEMPLATE}")
    if MYKEY_FILE.is_file() and not replace_existing:
        return {"ok": True, "changed": False, "mykeyFile": str(MYKEY_FILE), "message": "Existing mykey.py kept"}
    template = KEY_TEMPLATE.read_text(encoding="utf-8")
    marker = "'apikey': '',"
    if template.count(marker) != 2:
        raise ValueError("G4W GA key template must contain exactly two empty apikey fields")
    content = template.replace(marker, f"'apikey': {key!r},")
    compile(content, str(MYKEY_FILE), "exec")
    backup = _backup(MYKEY_FILE) if MYKEY_FILE.is_file() else None
    _atomic_write(MYKEY_FILE, content)
    return {
        "ok": True,
        "changed": True,
        "mykeyFile": str(MYKEY_FILE),
        "backupFile": str(backup) if backup else "",
        "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
    }


def interactive_ga_key() -> dict:
    replace = not MYKEY_FILE.is_file()
    if MYKEY_FILE.is_file():
        print(f"检测到已有 GA 配置：{MYKEY_FILE}")
        choice = input("0: 保留现有配置（默认）  1: 备份后重新配置\n请选择 0 或 1：").strip() or "0"
        if choice != "1":
            return configure_ga_key("unused", replace_existing=False)
        replace = True
    api_key = getpass.getpass("请输入 DeepSeek API Key（输入内容不会显示）：").strip()
    return configure_ga_key(api_key, replace_existing=replace)


def _clean_value(value: str, field: str) -> str:
    normalized = str(value or "").strip()
    if "\r" in normalized or "\n" in normalized:
        raise ValueError(f"{field} cannot contain a newline")
    return normalized


def _choose(prompt: str, choices: dict[str, str], current: str, default_key: str) -> str:
    current_key = next((key for key, value in choices.items() if value == current), default_key)
    value = input(f"{prompt} [{current_key}]：").strip() or current_key
    if value not in choices:
        raise ValueError(f"请选择：{', '.join(choices)}")
    return choices[value]


def _render_env(values: dict[str, str], unknown: dict[str, str]) -> str:
    sections = [
        "# G4W Portable environment",
        "# G4W_WORKSPACE_ROOT is refreshed from the launcher location whenever G4W starts.",
        "# Other runtime paths are relative to G4W_WORKSPACE_ROOT for portable moves.",
        "G4W_ENV_VERSION=2",
        f"G4W_WORKSPACE_ROOT={values['G4W_WORKSPACE_ROOT']}",
        f"G4W_SHARED_MEMORY_ROOT={values['G4W_SHARED_MEMORY_ROOT']}",
        f"G4W_VECTOR_INDEX_DIR={values['G4W_VECTOR_INDEX_DIR']}",
        "",
        "# Identity",
        f"G4W_USER_NAME={values['G4W_USER_NAME']}",
        f"G4W_USER_IDENTITY={values['G4W_USER_IDENTITY']}",
        f"G4W_USER_GENDER={values['G4W_USER_GENDER']}",
        f"G4W_BOT_NAME={values['G4W_BOT_NAME']}",
        "",
        "# WeChat account selection is automatic while only one account exists.",
        f"G4W_ACCOUNT_ID={values['G4W_ACCOUNT_ID']}",
        "",
        "# Models",
        f"G4W_CONDUCTOR_MODEL={values['G4W_CONDUCTOR_MODEL']}",
        f"G4W_WORKER_MODEL={values['G4W_WORKER_MODEL']}",
        "G4W_PRO_MODEL=deepseek-v4-pro",
        "",
        "# Timeline",
        "G4W_TIMELINE_LOCALE=zh-CN",
        f"G4W_TIMELINE_UI_THEME={values['G4W_TIMELINE_UI_THEME']}",
        "",
        "# Optional integrations. Intentionally left blank during initialization.",
        f"G4W_DIDA_COMMAND={values['G4W_DIDA_COMMAND']}",
        f"G4W_DIDA_TOKEN={values['G4W_DIDA_TOKEN']}",
        "",
        "# Default random check-in range: 10-90 minutes.",
        f"G4W_CHECKIN_ENABLED={values['G4W_CHECKIN_ENABLED']}",
        f"G4W_CHECKIN_MIN_INTERVAL_MS={values['G4W_CHECKIN_MIN_INTERVAL_MS']}",
        f"G4W_CHECKIN_MAX_INTERVAL_MS={values['G4W_CHECKIN_MAX_INTERVAL_MS']}",
    ]
    if unknown:
        sections.extend(["", "# Preserved custom values"])
        sections.extend(f"{key}={value}" for key, value in sorted(unknown.items()))
    return "\n".join(sections).rstrip() + "\n"


def configure_env(values: dict[str, str]) -> dict:
    existing = _read_env_file(ENV_FILE)
    known_keys = {
        "G4W_ENV_VERSION", "G4W_USER_NAME", "G4W_USER_IDENTITY", "G4W_USER_GENDER", "G4W_BOT_NAME",
        "G4W_ACCOUNT_ID", "G4W_CONDUCTOR_MODEL", "G4W_WORKER_MODEL", "G4W_PRO_MODEL",
        "G4W_TIMELINE_LOCALE", "G4W_TIMELINE_UI_THEME", "G4W_DIDA_COMMAND",
        "G4W_DIDA_TOKEN", "G4W_TODAY_TASK_AUTH_CODE", "G4W_TODAY_TASK_PUSH_URL",
        "G4W_TODAY_TASK_TIMEOUT_MS", "G4W_CHECKIN_ENABLED",
        "G4W_CHECKIN_MIN_INTERVAL_MS", "G4W_CHECKIN_MAX_INTERVAL_MS",
        "G4W_WORKSPACE_ROOT", "G4W_SHARED_MEMORY_ROOT",
        # Old location-bound and Node bridge values must not survive into the portable file.
        "G4W_HOME", "G4W_STATE_DIR", "G4W_CONDA_ENV",
        "G4W_RUNTIME", "G4W_GA_AGENTMAIN", "G4W_GA_TASK_DIR", "G4W_SHARED_PORT",
        "G4W_CODEX_ENDPOINT", "TIMELINE_FOR_AGENT_STATE_DIR", "TIMELINE_FOR_AGENT_LOCALE",
        "G4W_VECTOR_INDEX_DIR",
    }
    merged = {
        "G4W_USER_NAME": _clean_value(values.get("G4W_USER_NAME", existing.get("G4W_USER_NAME", "")), "用户名"),
        "G4W_WORKSPACE_ROOT": str(PORTABLE_ROOT.resolve()),
        "G4W_SHARED_MEMORY_ROOT": "${G4W_WORKSPACE_ROOT}/runtime/G4W-main/G4W/memory/sop",
        "G4W_USER_IDENTITY": _clean_value(values.get("G4W_USER_IDENTITY", existing.get("G4W_USER_IDENTITY", "")), "用户身份"),
        "G4W_USER_GENDER": _clean_value(values.get("G4W_USER_GENDER", existing.get("G4W_USER_GENDER", "neutral")), "用户性别") or "neutral",
        "G4W_BOT_NAME": _clean_value(values.get("G4W_BOT_NAME", existing.get("G4W_BOT_NAME", "")), "机器人名字"),
        "G4W_ACCOUNT_ID": existing.get("G4W_ACCOUNT_ID", ""),
        "G4W_CONDUCTOR_MODEL": values.get("G4W_CONDUCTOR_MODEL", existing.get("G4W_CONDUCTOR_MODEL", "deepseek-v4-flash")),
        "G4W_WORKER_MODEL": values.get("G4W_WORKER_MODEL", existing.get("G4W_WORKER_MODEL", "deepseek-v4-flash")),
        "G4W_TIMELINE_UI_THEME": values.get("G4W_TIMELINE_UI_THEME", existing.get("G4W_TIMELINE_UI_THEME", "default")),
        "G4W_DIDA_COMMAND": existing.get("G4W_DIDA_COMMAND", ""),
        "G4W_DIDA_TOKEN": existing.get("G4W_DIDA_TOKEN", ""),
        "G4W_CHECKIN_ENABLED": values.get("G4W_CHECKIN_ENABLED", existing.get("G4W_CHECKIN_ENABLED", "1")),
        "G4W_CHECKIN_MIN_INTERVAL_MS": existing.get("G4W_CHECKIN_MIN_INTERVAL_MS", "600000"),
        "G4W_CHECKIN_MAX_INTERVAL_MS": existing.get("G4W_CHECKIN_MAX_INTERVAL_MS", "5400000"),
        "G4W_VECTOR_INDEX_DIR": existing.get("G4W_VECTOR_INDEX_DIR", "${G4W_WORKSPACE_ROOT}/runtime/G4W-vector-index/memory"),
    }
    if merged["G4W_CONDUCTOR_MODEL"] not in MODEL_CHOICES.values():
        raise ValueError("Unsupported Conductor model")
    if merged["G4W_WORKER_MODEL"] not in MODEL_CHOICES.values():
        raise ValueError("Unsupported Worker model")
    if merged["G4W_TIMELINE_UI_THEME"] not in THEME_CHOICES.values():
        raise ValueError("Unsupported Timeline theme")
    if merged["G4W_USER_GENDER"] not in GENDER_CHOICES.values():
        raise ValueError("Unsupported user gender")
    unknown = {key: value for key, value in existing.items() if key not in known_keys}
    backup = _backup(ENV_FILE)
    _atomic_write(ENV_FILE, _render_env(merged, unknown))
    return {
        "ok": True,
        "envFile": str(ENV_FILE),
        "backupFile": str(backup) if backup else "",
        "portableRoot": str(PORTABLE_ROOT),
        "pathsAreLocationDerived": True,
        "sharedMemoryRoot": merged["G4W_SHARED_MEMORY_ROOT"],
        "conductorModel": merged["G4W_CONDUCTOR_MODEL"],
        "workerModel": merged["G4W_WORKER_MODEL"],
        "proModel": "deepseek-v4-pro",
        "timelineLocale": "zh-CN",
        "timelineTheme": merged["G4W_TIMELINE_UI_THEME"],
        "checkinMinutes": [int(merged["G4W_CHECKIN_MIN_INTERVAL_MS"]) // 60000, int(merged["G4W_CHECKIN_MAX_INTERVAL_MS"]) // 60000],
        "didaConfigured": bool(merged["G4W_DIDA_COMMAND"] or merged["G4W_DIDA_TOKEN"]),
    }


def interactive_env() -> dict:
    current = _read_env_file(ENV_FILE)
    print(f"G4W ENV 将写入：{ENV_FILE}")
    print(f"运行路径会根据启动脚本位置动态推导，整包移动后无需修改 ENV。")
    user_name = input(f"用户名 [{current.get('G4W_USER_NAME', '')}]：").strip() or current.get("G4W_USER_NAME", "")
    user_identity = input(f"用户身份/日常称呼 [{current.get('G4W_USER_IDENTITY', '')}]：").strip() or current.get("G4W_USER_IDENTITY", "")
    print("用户性别：0: male；1: female；2: neutral")
    user_gender = _choose("请输入 0、1 或 2", GENDER_CHOICES, current.get("G4W_USER_GENDER", "neutral"), "2")
    bot_name = input(f"机器人名字 [{current.get('G4W_BOT_NAME', '')}]：").strip() or current.get("G4W_BOT_NAME", "")
    print("Conductor 默认模型：0: deepseek-v4-pro；1: deepseek-v4-flash（默认）")
    conductor_model = _choose("请输入 0 或 1", MODEL_CHOICES, current.get("G4W_CONDUCTOR_MODEL", "deepseek-v4-flash"), "1")
    print("Worker 默认模型：0: deepseek-v4-pro；1: deepseek-v4-flash（默认）")
    worker_model = _choose("请输入 0 或 1", MODEL_CHOICES, current.get("G4W_WORKER_MODEL", "deepseek-v4-flash"), "1")
    print("Timeline 主题：0: default（默认）；1: neko")
    timeline_theme = _choose("请输入 0 或 1", THEME_CHOICES, current.get("G4W_TIMELINE_UI_THEME", "default"), "0")
    return configure_env({
        "G4W_USER_NAME": user_name,
        "G4W_USER_IDENTITY": user_identity,
        "G4W_USER_GENDER": user_gender,
        "G4W_BOT_NAME": bot_name,
        "G4W_CONDUCTOR_MODEL": conductor_model,
        "G4W_WORKER_MODEL": worker_model,
        "G4W_TIMELINE_UI_THEME": timeline_theme,
    })


def sync_runtime_paths() -> dict:
    """Refresh location-derived paths without touching unrelated ENV values."""
    from ..memory.instructions import update_env_file

    expected_workspace = str(PORTABLE_ROOT.resolve())
    expected_shared = "${G4W_WORKSPACE_ROOT}/runtime/G4W-main/G4W/memory/sop"
    expected_vector = "${G4W_WORKSPACE_ROOT}/runtime/G4W-vector-index/memory"
    current = _read_env_file(ENV_FILE)
    updates = {}
    if current.get("G4W_WORKSPACE_ROOT", "") != expected_workspace:
        updates["G4W_WORKSPACE_ROOT"] = expected_workspace
    if current.get("G4W_SHARED_MEMORY_ROOT", "") != expected_shared:
        updates["G4W_SHARED_MEMORY_ROOT"] = expected_shared
    if current.get("G4W_VECTOR_INDEX_DIR", "") != expected_vector:
        updates["G4W_VECTOR_INDEX_DIR"] = expected_vector
    if not updates:
        return {
            "ok": True,
            "changed": False,
            "envFile": str(ENV_FILE),
            "sharedMemoryRoot": expected_shared,
            "vectorIndexDir": expected_vector,
        }
    update_env_file(ENV_FILE, updates)
    return {
        "ok": True,
        "changed": True,
        "envFile": str(ENV_FILE),
        "sharedMemoryRoot": expected_shared,
        "vectorIndexDir": expected_vector,
    }


def safe_json(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2)
