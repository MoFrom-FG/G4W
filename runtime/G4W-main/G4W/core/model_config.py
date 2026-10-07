# -*- coding: utf-8 -*-
"""模型配置（mykey.py）读写 + 供应商预设 + 连通性探测。

背景与约束：
  · GA 通过 ``import mykey`` 读取 ``runtime/app/mykey.py`` 里所有顶层变量，
    并按变量名前缀区分协议（``native_oai_*`` / ``native_claude_*``）；
    且 ``load_llm_sessions()`` 是 **mtime 门控**的 —— 改完文件即生效，不需要重启。
  · G4W 的产品约定是两份 OpenAI 兼容配置：
    ``native_oai_config_main``（主模型）与 ``native_oai_config_lite``（轻模型）。
  · **本模块只管理这两个变量**：文件里其它变量（用户自己加的中转/第三方配置、
    注释、空行）全部逐字节保留 —— 否则从看板点一次保存就会毁掉用户的手工配置。
  · 密钥只写本机文件，绝不回显全文（只给 ``sk-****abcd`` 形式的掩码），也不写日志。
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

MAIN_VAR = "native_oai_config_main"
LITE_VAR = "native_oai_config_lite"
MANAGED_VARS = (MAIN_VAR, LITE_VAR)
BACKUP_KEEP = 3
PROVIDERS_FILE = "model-providers.json"


def _app_dir(config) -> Path:
    """GA 运行目录：优先配置对象上的覆盖值（测试用），否则用包内常量 runtime/app。"""
    override = getattr(config, "ga_app_dir", None)
    if override:
        return Path(override)
    from .config import GA_APP_DIR

    return Path(GA_APP_DIR)


def _templates_dir(config) -> Path:
    """模板根目录：优先配置对象上的覆盖值，否则用包内 G4W/templates。"""
    override = getattr(config, "templates_dir", None)
    if override:
        return Path(override)
    from .config import PACKAGE_DIR

    return Path(PACKAGE_DIR) / "templates"

# 供应商预设（取自 ga-admin 的 configure_mykey.py，只保留 OpenAI 兼容协议的那些）
PROVIDER_PRESETS = [
    {"id": "deepseek", "name": "DeepSeek（官方）", "protocol": "native_oai",
     "apibase": "https://api.deepseek.com", "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
     "api_mode": "chat_completions", "reasoning_effort": "xhigh",
     "key_hint": "在 https://platform.deepseek.com/api_keys 获取", "desc": "官方直连；也可填中转站地址"},
    {"id": "oai_chat", "name": "OpenAI Chat Completions", "protocol": "native_oai",
     "apibase": "https://api.openai.com/v1", "models": ["gpt-5.5", "gpt-5.4"],
     "api_mode": "chat_completions", "reasoning_effort": "high",
     "key_hint": "官方在 https://platform.openai.com/api-keys 获取；中转站填其提供的 Key", "desc": "标准 OpenAI 协议"},
    {"id": "qwen", "name": "阿里通义千问（百炼）", "protocol": "native_oai",
     "apibase": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "models": ["qwen3.6-max-preview", "qwen3.5-plus", "qwen3-coder-plus"],
     "api_mode": "chat_completions", "reasoning_effort": "high",
     "key_hint": "在 https://bailian.console.aliyun.com/ 获取 API Key", "desc": "兼容模式端点"},
    {"id": "stepfun", "name": "阶跃星辰 Step", "protocol": "native_oai",
     "apibase": "https://api.stepfun.com/v1", "models": ["step-3.5-flash", "step-3.5-flash-2603"],
     "api_mode": "chat_completions", "reasoning_effort": "high",
     "key_hint": "在 https://platform.stepfun.com/ 获取 API Key", "desc": "推理较强"},
    {"id": "volcengine", "name": "火山引擎（豆包 / Ark）", "protocol": "native_oai",
     "apibase": "https://ark.cn-beijing.volces.com/api/v3",
     "models": ["doubao-seed-code-preview-251028", "doubao-seed-1-8-251228"],
     "api_mode": "chat_completions", "reasoning_effort": "high",
     "key_hint": "在 https://console.volcengine.com/ark/ 创建推理接入点后获取 API Key", "desc": "需先在火山控制台建接入点"},
    {"id": "qianfan", "name": "百度千帆", "protocol": "native_oai",
     "apibase": "https://qianfan.baidubce.com/v2", "models": ["ernie-5.0-thinking-preview", "deepseek-v3.2"],
     "api_mode": "chat_completions", "reasoning_effort": "high",
     "key_hint": "在 https://console.bce.baidu.com/qianfan/ 创建应用获取 API Key", "desc": "含第三方模型"},
    {"id": "xiaomi", "name": "小米 MiMo", "protocol": "native_oai",
     "apibase": "https://api.xiaomimimo.com/v1", "models": ["mimo-v2.5-pro", "mimo-v2-flash"],
     "api_mode": "chat_completions", "reasoning_effort": "high",
     "key_hint": "在 https://x.xiaomi.com/ 获取 API Key", "desc": ""},
    {"id": "tencent_tokenhub", "name": "腾讯混元 TokenHub", "protocol": "native_oai",
     "apibase": "https://tokenhub.tencentmaas.com/v1", "models": ["hy3-preview"],
     "api_mode": "chat_completions", "reasoning_effort": "high",
     "key_hint": "在 https://console.cloud.tencent.com/tokenhub 获取 API Key", "desc": ""},
    {"id": "custom", "name": "自定义 / 中转站（OpenAI 兼容）", "protocol": "native_oai",
     "apibase": "", "models": [], "api_mode": "chat_completions", "reasoning_effort": "high",
     "key_hint": "填入你的中转站 apibase 与 Key（需 OpenAI 兼容）", "desc": "手动填写地址与模型名"},
]

# 这些供应商走 Anthropic 协议（native_claude_*），v1 不支持一键写入，只做提示
ANTHROPIC_HINT = "Anthropic 协议（Claude / Kimi / 智谱 / MiniMax / CC Switch 透传）需要手写 native_claude_* 配置，本页暂不代写。"


class ModelConfigError(ValueError):
    """模型配置操作失败（文件损坏、格式不支持等）。"""


def mask_key(value: str) -> str:
    key = str(value or "")
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return f"{key[:3]}****{key[-4:]}"


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _backup(path: Path) -> str:
    if not path.is_file():
        return ""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    target = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, target)
    keep_backups(path)
    return str(target)


def keep_backups(path: Path, keep: int = BACKUP_KEEP) -> list[str]:
    pattern = f"{path.name}.bak-*"
    rows = sorted(path.parent.glob(pattern), key=lambda item: item.stat().st_mtime, reverse=True)
    removed = []
    for stale in rows[max(1, int(keep)):]:
        try:
            stale.unlink()
            removed.append(stale.name)
        except OSError:
            pass
    return removed


def _parse_assignments(text: str) -> dict:
    """返回 {变量名: {'span': (start, end), 'value': dict}}；只取顶层 dict 赋值。"""
    tree = ast.parse(text)
    found = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or not isinstance(node.value, ast.Dict):
            continue
        try:
            value = ast.literal_eval(node.value)
        except Exception:
            continue
        start = node.lineno - 1
        end = getattr(node, "end_lineno", node.lineno)
        found[target.id] = {"value": value if isinstance(value, dict) else {}, "lines": (start, end)}
    return found


def _line_spans(text: str) -> list[int]:
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    return offsets


def render_config_block(variable: str, config: dict) -> str:
    """按 GA 习惯渲染成可读的 Python 字面量块。"""
    order = ["name", "apikey", "apibase", "model", "api_mode", "reasoning_effort", "stream"]
    keys = [key for key in order if key in config] + [key for key in config if key not in order]
    lines = [f"{variable} = {{"]
    for key in keys:
        value = config[key]
        if isinstance(value, bool):
            rendered = "True" if value else "False"
        else:
            rendered = repr(value)
        lines.append(f"    {key!r}: {rendered},")
    lines.append("}")
    return "\n".join(lines) + "\n"


def read_config(config) -> dict:
    """读取 runtime/app/mykey.py 的当前状态（不回显密钥明文）。"""
    path = _app_dir(config) / "mykey.py"
    state = {
        "file": str(path),
        "exists": path.is_file(),
        "updatedAt": "",
        "parseError": "",
        "entries": [],
        "managed": {MAIN_VAR: {}, LITE_VAR: {}},
        "extraVars": [],
    }
    if not path.is_file():
        return state
    state["updatedAt"] = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    try:
        assignments = _parse_assignments(text)
    except SyntaxError as error:
        state["parseError"] = f"mykey.py 语法错误：{error}"
        return state
    for name, item in assignments.items():
        value = dict(item["value"])
        entry = {
            "var": name,
            "managed": name in MANAGED_VARS,
            "name": str(value.get("name") or ""),
            "model": str(value.get("model") or ""),
            "apibase": str(value.get("apibase") or ""),
            "apiMode": str(value.get("api_mode") or ""),
            "reasoningEffort": str(value.get("reasoning_effort") or ""),
            "stream": bool(value.get("stream")),
            "hasKey": bool(str(value.get("apikey") or "").strip()),
            "keyMask": mask_key(value.get("apikey")),
        }
        state["entries"].append(entry)
        if name in MANAGED_VARS:
            state["managed"][name] = entry
        else:
            state["extraVars"].append(name)
    state["entries"].sort(key=lambda item: (not item["managed"], item["var"]))
    return state


def _validate_block(value: dict, variable: str) -> dict:
    row = dict(value)
    if not str(row.get("apibase") or "").strip():
        raise ModelConfigError(f"{variable} 缺少 apibase（接口地址）")
    if not str(row.get("model") or "").strip():
        raise ModelConfigError(f"{variable} 缺少 model（模型名）")
    row.setdefault("api_mode", "chat_completions")
    row.setdefault("stream", True)
    return row


def upsert_variables(config, payload: dict) -> dict:
    """写入/更新若干顶层变量；文件里其它内容（注释、空行、其它变量）逐字节保留。"""
    path = _app_dir(config) / "mykey.py"
    blocks = {name: render_config_block(name, _validate_block(value, name)) for name, value in payload.items()}
    if not path.is_file():
        text = "# G4W model config (managed by the dashboard)\n\n" + "\n".join(blocks.values())
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, text)
        return {"ok": True, "file": str(path), "backup": "", **read_config(config)}
    original = path.read_text(encoding="utf-8-sig", errors="replace")
    try:
        assignments = _parse_assignments(original)
    except SyntaxError as error:
        raise ModelConfigError(
            f"当前 mykey.py 无法解析（{error}）。为避免破坏你的手工配置，已中止保存；"
            f"请先修好该文件，或先备份再手动编辑。"
        )
    offsets = _line_spans(original)
    replacements = []
    for name, block in blocks.items():
        if name in assignments:
            start_line, end_line = assignments[name]["lines"]
            start = offsets[start_line]
            end = offsets[end_line] if end_line < len(offsets) else len(original)
            replacements.append((start, end, block))
        else:
            replacements.append((len(original), len(original), ("\n" if original.endswith("\n") else "\n\n") + block))
    replacements.sort(key=lambda item: item[0], reverse=True)
    text = original
    for start, end, block in replacements:
        text = text[:start] + block + text[end:]
    backup = _backup(path)
    _atomic_write(path, text)
    return {"ok": True, "file": str(path), "backup": backup, **read_config(config)}


def delete_variables(config, variables) -> dict:
    """按变量名删除顶层赋值；文件里其它内容逐字节保留。"""
    names = [str(name).strip() for name in (variables or []) if str(name).strip()]
    if not names:
        raise ModelConfigError("未指定要删除的变量")
    path = _app_dir(config) / "mykey.py"
    if not path.is_file():
        raise ModelConfigError("mykey.py 不存在")
    original = path.read_text(encoding="utf-8-sig", errors="replace")
    try:
        assignments = _parse_assignments(original)
    except SyntaxError as error:
        raise ModelConfigError(f"当前 mykey.py 无法解析（{error}），已中止删除。")
    missing = [name for name in names if name not in assignments]
    if missing:
        raise ModelConfigError(f"找不到变量：{', '.join(missing)}")
    offsets = _line_spans(original)
    spans = []
    for name in names:
        start_line, end_line = assignments[name]["lines"]
        start = offsets[start_line]
        end = offsets[end_line] if end_line < len(offsets) else len(original)
        spans.append((start, end))
    spans.sort(reverse=True)
    text = original
    for start, end in spans:
        text = text[:start] + text[end:]
    backup = _backup(path)
    _atomic_write(path, text)
    return {"ok": True, "file": str(path), "backup": backup, "removed": names, **read_config(config)}


def save_config(config, main: dict, lite: dict) -> dict:
    """兼容旧接口：一次写入 main / lite 两个变量（新界面按“可用模型”逐个管理）。"""
    return upsert_variables(config, {MAIN_VAR: dict(main), LITE_VAR: dict(lite)})


def ensure_template(config, *, empty_keys: bool = True) -> dict:
    """按包内模板创建 mykey.py（用于向导“跳过填 Key”）。已存在则不覆盖。"""
    path = _app_dir(config) / "mykey.py"
    if path.is_file():
        return {"ok": True, "created": False, "file": str(path)}
    template = _templates_dir(config) / "setup" / "mykey-G4W-template.py"
    if not template.is_file():
        raise ModelConfigError(f"包内模板缺失：{template}")
    text = template.read_text(encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, text)
    return {"ok": True, "created": True, "file": str(path), "emptyKeys": empty_keys}


def probe(apibase: str, apikey: str, model: str = "", timeout: float = 12.0) -> dict:
    """探测接口连通性：先试 GET /models，再退回一次最小 chat 请求。"""
    base = str(apibase or "").strip().rstrip("/")
    key = str(apikey or "").strip()
    if not base:
        return {"ok": False, "error": "缺少 apibase"}
    if not key:
        return {"ok": False, "error": "缺少 API Key"}
    headers = {"Authorization": f"Bearer {key}", "User-Agent": "G4W-model-config"}
    try:
        request = urllib.request.Request(f"{base}/models", headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace") or "{}")
        rows = payload.get("data") if isinstance(payload, dict) else None
        models = [str(item.get("id")) for item in (rows or []) if isinstance(item, dict) and item.get("id")]
        return {"ok": True, "mode": "models", "models": models[:60], "modelCount": len(models)}
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = error.read().decode("utf-8", "replace")[:200]
        except Exception:
            detail = ""
        if error.code in (404, 405) and model:
            return _probe_chat(base, key, model, timeout)
        return {"ok": False, "mode": "models", "error": f"{base}/models 返回 HTTP {error.code} {detail}".strip()}
    except Exception as error:
        if model:
            fallback = _probe_chat(base, key, model, timeout)
            if fallback.get("ok"):
                return fallback
            return {"ok": False, "mode": "models",
                    "error": f"连不上 {base}（{type(error).__name__}: {error}）；chat 探测：{fallback.get('error')}"}
        return {"ok": False, "mode": "models", "error": f"连不上 {base}（{type(error).__name__}: {error}）"}


def _probe_chat(base: str, key: str, model: str, timeout: float, want_reply: bool = False) -> dict:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1 if not want_reply else 8,
        "stream": False,
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{base}/chat/completions", data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "User-Agent": "G4W-model-config"},
    )
    try:
        with urllib.request.urlopen(request, timeout=max(20.0, timeout)) as response:
            payload = json.loads(response.read().decode("utf-8", "replace") or "{}")
        result = {"ok": True, "mode": "chat", "model": model}
        if want_reply and isinstance(payload, dict):
            choices = payload.get("choices") or []
            first = choices[0] if choices and isinstance(choices[0], dict) else {}
            message = first.get("message") if isinstance(first.get("message"), dict) else {}
            snippet = str(message.get("content") or first.get("text") or "").strip()
            if snippet:
                result["reply"] = snippet[:80]
        return result
    except urllib.error.HTTPError as error:
        try:
            detail = error.read().decode("utf-8", "replace")[:200]
        except Exception:
            detail = ""
        return {"ok": False, "mode": "chat", "error": f"{base}/chat/completions 返回 HTTP {error.code} {detail}".strip()}
    except Exception as error:
        return {"ok": False, "mode": "chat", "error": f"连不上 {base}/chat/completions（{type(error).__name__}: {error}）"}


def raw_key(config, variable: str = MAIN_VAR) -> str:
    """取指定变量的**明文**密钥。

    仅供本进程内部做连通性探测用；调用方严禁把它回传前端或写日志
    （前端只能看到 ``mask_key()`` 的结果）。
    """
    path = _app_dir(config) / "mykey.py"
    if not path.is_file():
        return ""
    try:
        assignments = _parse_assignments(path.read_text(encoding="utf-8-sig", errors="replace"))
    except SyntaxError:
        return ""
    value = (assignments.get(variable) or {}).get("value") or {}
    if not isinstance(value, dict):
        return ""
    return str(value.get("apikey") or "")


def build_payload(config, main: dict, lite: dict) -> dict:
    return {"main": main, "lite": lite, "providers": PROVIDER_PRESETS, "anthropicHint": ANTHROPIC_HINT}


# ---------- 供应商仓库（记住 base + key，用于拉取该供应商的模型列表） ----------

def _slug(text: str) -> str:
    return re.sub(r"[^0-9a-zA-Z]+", "-", str(text or "")).strip("-").lower()


def _provider_id(apibase: str, existing) -> str:
    host = re.sub(r"^https?://", "", str(apibase or "")).strip("/")
    base = "p-" + (_slug(host) or "provider")
    candidate, index = base[:48], 2
    taken = {str(item) for item in (existing or [])}
    while candidate in taken:
        candidate = f"{base[:44]}-{index}"
        index += 1
    return candidate


def _providers_path(config) -> Path:
    return Path(config.state_dir) / PROVIDERS_FILE


def _normalize_provider(row: dict) -> dict:
    apibase = str(row.get("apibase") or "").strip()
    if not apibase:
        return {}
    return {
        "id": str(row.get("id") or _slug(apibase)),
        "name": str(row.get("name") or apibase),
        "apibase": apibase,
        "apikey": str(row.get("apikey") or ""),
        "addedAt": str(row.get("addedAt") or ""),
    }


def _host_label(apibase: str) -> str:
    """从 base 里取出便于辨认的短名（域名），作为种子供应商的默认名称。"""
    value = re.sub(r"^https?://", "", str(apibase or "")).strip("/")
    return value.split("/")[0] or ""


def _seed_providers(config) -> list[dict]:
    """首次读取时，从 mykey.py 现有配置按 apibase 归并出供应商（只读，不落盘）。"""
    state = read_config(config)
    rows, seen = [], set()
    for entry in state.get("entries") or []:
        base = str(entry.get("apibase") or "").strip()
        if not base or base in seen:
            continue
        seen.add(base)
        rows.append({
            "id": _provider_id(base, {row["id"] for row in rows}),
            "name": _host_label(base) or entry.get("name") or base,
            "apibase": base,
            "apikey": raw_key_by_apibase(config, base),
            "addedAt": state.get("updatedAt") or "",
        })
    return rows


def load_providers(config) -> list[dict]:
    path = _providers_path(config)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        rows = data.get("providers") if isinstance(data, dict) else None
        if isinstance(rows, list) and rows:
            return [row for row in (_normalize_provider(item) for item in rows) if row]
    return _seed_providers(config)


def save_providers(config, rows: list[dict]) -> dict:
    path = _providers_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"providers": [dict(row) for row in rows]}
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return {"ok": True, "file": str(path), "providers": payload["providers"]}


def add_provider(config, name: str, apibase: str, apikey: str) -> dict:
    base = str(apibase or "").strip().rstrip("/")
    if not base:
        raise ModelConfigError("请填接口地址 apibase")
    rows = load_providers(config)
    for row in rows:
        if row["apibase"].rstrip("/") == base:
            if str(name or "").strip():
                row["name"] = str(name).strip()
            if str(apikey or "").strip():
                row["apikey"] = str(apikey).strip()
            save_providers(config, rows)
            return {"ok": True, "updated": row["id"], "providers": rows}
    row = {
        "id": _provider_id(base, {item["id"] for item in rows}),
        "name": str(name or base).strip(),
        "apibase": base,
        "apikey": str(apikey or "").strip(),
        "addedAt": datetime.now().isoformat(timespec="seconds"),
    }
    rows.append(row)
    save_providers(config, rows)
    return {"ok": True, "added": row["id"], "providers": rows}


def delete_provider(config, provider_id: str) -> dict:
    rows = [row for row in load_providers(config) if row["id"] != str(provider_id)]
    save_providers(config, rows)
    return {"ok": True, "providers": rows}


def provider_by_id(config, provider_id: str) -> dict:
    target = str(provider_id or "")
    for row in load_providers(config):
        if row["id"] == target:
            return row
    raise ModelConfigError(f"找不到供应商：{target}")


def raw_key_by_apibase(config, apibase: str) -> str:
    """按 apibase 找第一条配置的明文密钥（仅供进程内探测，绝不回传前端）。"""
    target = str(apibase or "").strip().rstrip("/")
    if not target:
        return ""
    path = _app_dir(config) / "mykey.py"
    if not path.is_file():
        return ""
    try:
        assignments = _parse_assignments(path.read_text(encoding="utf-8-sig", errors="replace"))
    except SyntaxError:
        return ""
    for name, item in assignments.items():
        if not name.startswith("native_oai_"):
            continue
        value = item.get("value") or {}
        if str(value.get("apibase") or "").strip().rstrip("/") == target:
            return str(value.get("apikey") or "")
    return ""


def fetch_models(config, provider_id: str, timeout: float = 15.0) -> dict:
    """用该供应商的 base + key 拉取可用模型列表。"""
    row = provider_by_id(config, provider_id)
    key = str(row.get("apikey") or "").strip() or raw_key_by_apibase(config, row["apibase"])
    result = probe(row["apibase"], key, timeout=timeout)
    return {
        "ok": bool(result.get("ok")),
        "providerId": row["id"],
        "apibase": row["apibase"],
        "models": [str(item) for item in (result.get("models") or [])],
        "error": str(result.get("error") or ""),
        "mode": str(result.get("mode") or ""),
    }


def model_variable(model: str, existing) -> str:
    stem = (_slug(model) or "model").replace("-", "_")
    candidate, index = f"native_oai_config_{stem}"[:64], 2
    taken = {str(item) for item in (existing or [])}
    while candidate in taken:
        candidate = f"native_oai_config_{stem}_{index}"[:64]
        index += 1
    return candidate


def add_model(config, provider_id: str, model: str) -> dict:
    """把某个模型加入可用模型（写进 mykey.py；GA 按 mtime 自动加载）。"""
    name = str(model or "").strip()
    if not name:
        raise ModelConfigError("请选择一个模型")
    row = provider_by_id(config, provider_id)
    key = str(row.get("apikey") or "").strip() or raw_key_by_apibase(config, row["apibase"])
    if not key:
        raise ModelConfigError("该供应商还没有可用的 API Key，请先补上再添加模型")
    state = read_config(config)
    existing = {entry["var"] for entry in state["entries"]}
    for entry in state["entries"]:
        if entry["model"] == name and str(entry["apibase"]).rstrip("/") == row["apibase"].rstrip("/"):
            return {"ok": True, "already": entry["var"], **state}
    variable = model_variable(name, existing)
    result = upsert_variables(config, {variable: {
        "name": name, "apikey": key, "apibase": row["apibase"], "model": name,
        "api_mode": "chat_completions", "reasoning_effort": "high", "stream": True,
    }})
    result["added"] = variable
    return result


def delete_model(config, variable: str) -> dict:
    """从可用模型里删除一个（只允许删本页管理的 native_oai_* 变量）。"""
    var = str(variable or "").strip()
    if not var.startswith("native_oai_"):
        raise ModelConfigError(f"不接受删除非模型配置的变量：{var}")
    return delete_variables(config, [var])


def test_model(config, provider_id: str, model: str, timeout: float = 30.0) -> dict:
    """测试某个模型是否真的可用：发一次最小 chat 请求，返回耗时与回复片段。"""
    name = str(model or "").strip()
    if not name:
        raise ModelConfigError("请选择要测试的模型")
    row = provider_by_id(config, provider_id)
    key = str(row.get("apikey") or "").strip() or raw_key_by_apibase(config, row["apibase"])
    if not key:
        raise ModelConfigError("该供应商还没有可用的 API Key，请先补上再测试")
    started = time.perf_counter()
    result = _probe_chat(row["apibase"], key, name, timeout, want_reply=True)
    result["latencyMs"] = int((time.perf_counter() - started) * 1000)
    result["model"] = name
    result["apibase"] = row["apibase"]
    result["providerId"] = row["id"]
    return result
