"""首次配置向导：与 Windows 桌面壳同语义的四步流程，但跑在看板里（跨平台）。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..core.config import MAIN_DIR
from ..core.platform_adapt import venv_python
from ..core.setup_state import gate_active, setup_status, state_dir

LOG_PATH = Path(MAIN_DIR).parent / "wizard-prepare.log"
LOGIN_LOG = Path(MAIN_DIR).parent / "login.log"


def runner_python() -> str:
    """登录子进程用哪个解释器：优先独立 venv（引导向导跑在包内基础解释器上时依赖不全）。"""
    candidate = venv_python(Path(MAIN_DIR).parent / "app" / ".venv")
    return str(candidate) if candidate.is_file() else sys.executable


class LoginRunner:
    """扫码头：启动 CLI login 子进程并读取二维码页面。"""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None

    def _qr_page(self) -> Path:
        return state_dir() / "login-qrcode.html"

    def start(self) -> dict:
        if self._proc is not None and self._proc.poll() is None:
            return {"ok": True, "running": True}
        try:
            out = open(LOGIN_LOG, "a", encoding="utf-8", errors="replace")
        except OSError:
            out = subprocess.DEVNULL
        env = dict(os.environ)
        env["G4W_NO_BROWSER"] = "1"
        env.setdefault("PYTHONUTF8", "1")
        try:
            self._proc = subprocess.Popen(
                [runner_python(), "-B", "-u", "-m", "G4W", "login"],
                cwd=str(MAIN_DIR), env=env,
                stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                start_new_session=(os.name != "nt"),
            )
        except Exception as error:
            return {"ok": False, "error": str(error)}
        return {"ok": True, "pid": self._proc.pid}

    def status(self) -> dict:
        status = setup_status()
        svg = ""
        link = ""
        page = self._qr_page()
        if page.is_file():
            try:
                text = page.read_text(encoding="utf-8", errors="replace")
                start = text.find("<svg")
                end = text.find("</svg>")
                if start != -1 and end != -1:
                    svg = text[start:end + 6]
                href = text.find('<a href="')
                if href != -1:
                    rest = text[href + 9:]
                    link = rest[:rest.find('"')]
            except OSError:
                pass
        running = self._proc is not None and self._proc.poll() is None
        return {"done": status["login"]["ok"], "running": running, "svg": svg, "link": link}


_runner = LoginRunner()
_prepare_thread: threading.Thread | None = None


def prepare_log_tail(limit: int = 40) -> str:
    try:
        lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return chr(10).join(lines[-limit:])


def _run_prepare() -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8", errors="replace") as out:
            out.write("[wizard] " + time.strftime("%Y-%m-%d %H:%M:%S") + " 开始准备环境" + chr(10))
            out.flush()
            env = dict(os.environ)
            env.setdefault("PYTHONUTF8", "1")
            subprocess.run(
                [sys.executable, "-B", "-u", "-m", "G4W", "prepare"],
                cwd=str(MAIN_DIR), env=env, stdout=out, stderr=out, timeout=1800,
            )
            out.write("[wizard] prepare 结束" + chr(10))
    except Exception as error:
        try:
            with open(LOG_PATH, "a", encoding="utf-8", errors="replace") as out:
                out.write("[wizard] prepare 异常: " + str(error) + chr(10))
        except OSError:
            pass


def start_prepare() -> dict:
    global _prepare_thread
    if _prepare_thread is not None and _prepare_thread.is_alive():
        return {"ok": True, "running": True}
    _prepare_thread = threading.Thread(target=_run_prepare, daemon=True)
    _prepare_thread.start()
    return {"ok": True, "running": True}


def save_key(api_key: str) -> dict:
    api_key = (api_key or "").strip()
    if not api_key:
        return {"ok": False, "error": "API Key 不能为空"}
    from ..cli.initializer import configure_ga_key

    try:
        result = configure_ga_key(api_key, replace_existing=True)
    except Exception as error:
        return {"ok": False, "error": str(error)}
    if isinstance(result, dict):
        result.setdefault("ok", True)
        return result
    return {"ok": True}


def save_env(values: dict) -> dict:
    from ..cli.initializer import configure_env

    payload = {key: str(value) for key, value in (values or {}).items() if value is not None}
    if not payload.get("G4W_USER_NAME") or not payload.get("G4W_BOT_NAME"):
        return {"ok": False, "error": "「你的名字」和「机器人名字」都要填"}
    try:
        result = configure_env(payload)
    except Exception as error:
        return {"ok": False, "error": str(error)}
    if isinstance(result, dict):
        result.setdefault("ok", True)
        return result
    return {"ok": True}


def status_payload() -> dict:
    payload = setup_status()
    payload["gate"] = gate_active()
    payload["prepareLog"] = prepare_log_tail(12)
    return payload


def _page_shell(body: str) -> bytes:
    style = (
        ":root{color-scheme:dark}body{margin:0;background:#0f1115;color:#e6e8ee;"
        "font:14px/1.7 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif}"
        ".wrap{max-width:760px;margin:0 auto;padding:34px 20px 60px}h1{font-size:22px;margin:0 0 6px}"
        ".sub{color:#9aa3b2;margin:0 0 22px}.steps{display:flex;gap:8px;margin:0 0 22px}"
        ".step{flex:1;padding:9px 10px;border:1px solid #262b36;border-radius:10px;font-size:12px;color:#9aa3b2}"
        ".step b{display:block;color:#cfd6e4;font-size:12px}.step.ok{border-color:#2f9e63;color:#5fd39a}"
        ".step.ok b{color:#5fd39a}.card{border:1px solid #262b36;border-radius:12px;padding:16px 18px;margin:0 0 14px}"
        ".card h2{font-size:15px;margin:0 0 10px}label{display:block;font-size:12px;color:#9aa3b2;margin:10px 0 4px}"
        "input,select{width:100%;box-sizing:border-box;background:#161a22;border:1px solid #2b3140;border-radius:8px;color:#e6e8ee;padding:9px 10px;font-size:14px}"
        "button{margin-top:12px;background:#2f6df6;border:0;color:#fff;padding:9px 16px;border-radius:8px;font-size:14px;cursor:pointer}"
        "button.ghost{background:#222836}pre{background:#12151c;border:1px solid #232936;border-radius:8px;padding:10px;max-height:180px;overflow:auto;font-size:12px;color:#9aa3b2}"
        ".msg{font-size:13px;margin-top:8px;color:#9aa3b2}.msg.ok{color:#5fd39a}.msg.err{color:#ff7b72}"
        ".qr{background:#fff;border-radius:10px;padding:10px;display:inline-block;margin-top:10px}.qr svg{display:block}"
        ".foot{color:#9aa3b2;font-size:13px}code{background:#161a22;padding:2px 6px;border-radius:6px}"
    )
    page = (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>G4W 首次配置向导</title><style>" + style + "</style></head><body><div class='wrap'>"
        + body + "</div></body></html>"
    )
    return page.encode("utf-8")


def render_page() -> bytes:
    """优先复用与 Windows 桌面壳一致的向导页（static/wizard.html + HTTP 桥接）。"""
    try:
        from .wizard_bridge import bridged_wizard_page

        page = bridged_wizard_page()
        if page:
            return page
    except Exception:
        pass
    return _render_inline_page()


def _render_inline_page() -> bytes:
    body = (
        "<h1>G4W 首次配置向导</h1>"
        "<p class='sub'>与 Windows 版一致的四步流程；四步全绿后本页自动放行进入看板。</p>"
        "<div class='steps' id='steps'>"
        "<div class='step' id='st-prepare'><b>① 准备环境</b><span id='txt-prepare'>检查中</span></div>"
        "<div class='step' id='st-key'><b>② API Key</b><span id='txt-key'>检查中</span></div>"
        "<div class='step' id='st-env'><b>③ 环境配置</b><span id='txt-env'>检查中</span></div>"
        "<div class='step' id='st-login'><b>④ 扫码登录</b><span id='txt-login'>检查中</span></div>"
        "</div>"
        "<div class='card'><h2>① 准备环境</h2>"
        "<p class='sub'>创建运行环境并安装依赖（已就绪时可跳过）。</p>"
        "<button id='btn-prepare'>一键准备环境</button><div class='msg' id='msg-prepare'></div>"
        "<pre id='log-prepare' style='display:none'></pre></div>"
        "<div class='card'><h2>② 配置 DeepSeek API Key</h2>"
        "<label>API Key（platform.deepseek.com 获取）</label>"
        "<input id='key' type='password' autocomplete='off' placeholder='sk-...'>"
        "<button id='btn-key'>保存并继续</button><div class='msg' id='msg-key'></div></div>"
        "<div class='card'><h2>③ 环境配置</h2>"
        "<label>你的名字</label><input id='env-name' placeholder='必填'>"
        "<label>日常称呼</label><input id='env-identity'>"
        "<label>性别</label><select id='env-gender'><option value='male'>male</option><option value='female'>female</option><option value='neutral'>neutral</option></select>"
        "<label>机器人名字</label><input id='env-bot' placeholder='必填'>"
        "<label>默认模型</label><select id='env-model'><option value='deepseek-v4-flash'>deepseek-v4-flash（快）</option><option value='deepseek-v4-pro'>deepseek-v4-pro（强）</option></select>"
        "<button id='btn-env'>保存并继续</button><div class='msg' id='msg-env'></div></div>"
        "<div class='card'><h2>④ 微信扫码登录</h2>"
        "<p class='sub'>使用专门用于 G4W 的微信账号扫码；与 Windows 版同一账号会互踢。</p>"
        "<button id='btn-login'>获取二维码</button>"
        "<div id='qr-box'></div><div class='msg' id='msg-login'></div></div>"
        "<p class='foot'>已完成四步？<a href='/' style='color:#5b9dff'>刷新进入看板</a></p>"
        "<script>" + SCRIPT + "</script>"
    )
    return _page_shell(body)


SCRIPT = (
    "(function(){"
    "function el(id){return document.getElementById(id);}"
    "function setMsg(id,text,kind){var n=el(id);n.textContent=text||'';n.className='msg'+(kind?' '+kind:'');}"
    "function post(path,body){return fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})}).then(function(r){return r.json();});}"
    "function mark(step,ok){var box=el('st-'+step);if(ok){box.classList.add('ok');}else{box.classList.remove('ok');}"
    "var t=el('txt-'+step);if(t){t.textContent=ok?'已完成':'待完成';}}"
    "function refresh(){return fetch('/api/setup/status').then(function(r){return r.json();}).then(function(s){"
    "mark('prepare',s.prepare.ok);mark('key',s.key.ok);mark('env',s.env.ok);mark('login',s.login.ok);"
    "if(s.env&&s.env.preset){var p=s.env.preset;if(!el('env-name').value){el('env-name').value=p.G4W_USER_NAME||'';}"
    "if(!el('env-identity').value){el('env-identity').value=p.G4W_USER_IDENTITY||'';}"
    "if(!el('env-bot').value){el('env-bot').value=p.G4W_BOT_NAME||'';}"
    "if(p.G4W_USER_GENDER){el('env-gender').value=p.G4W_USER_GENDER;}"
    "if(p.G4W_CONDUCTOR_MODEL){el('env-model').value=p.G4W_CONDUCTOR_MODEL;}}"
    "if(s.prepareLog){var lg=el('log-prepare');lg.style.display='block';lg.textContent=s.prepareLog;}"
    "if(s.all_ok){setMsg('msg-login','四步已完成，正在进入看板…','ok');setTimeout(function(){location.href='/';},1200);}"
    "return s;});}"
    "el('btn-prepare').onclick=function(){setMsg('msg-prepare','已开始准备环境，稍候…','');post('/api/setup/prepare',{}).then(function(){setTimeout(refresh,1500);});};"
    "el('btn-key').onclick=function(){var v=el('key').value.trim();if(!v){setMsg('msg-key','请填写 API Key','err');return;}"
    "setMsg('msg-key','保存中…','');post('/api/setup/key',{api_key:v}).then(function(r){"
    "if(r.ok){el('key').value='';setMsg('msg-key','已保存','ok');refresh();}else{setMsg('msg-key',r.error||'保存失败','err');}});};"
    "el('btn-env').onclick=function(){var values={G4W_USER_NAME:el('env-name').value.trim(),G4W_USER_IDENTITY:el('env-identity').value.trim(),"
    "G4W_USER_GENDER:el('env-gender').value,G4W_BOT_NAME:el('env-bot').value.trim(),"
    "G4W_CONDUCTOR_MODEL:el('env-model').value,G4W_WORKER_MODEL:el('env-model').value};"
    "setMsg('msg-env','保存中…','');post('/api/setup/env',{values:values}).then(function(r){"
    "if(r.ok){setMsg('msg-env','已保存','ok');refresh();}else{setMsg('msg-env',r.error||'保存失败','err');}});};"
    "var timer=null;"
    "function poll(){fetch('/api/setup/login/status').then(function(r){return r.json();}).then(function(s){"
    "if(s.svg){el('qr-box').innerHTML='<div class=qr>'+s.svg+'</div>';}"
    "if(s.done){setMsg('msg-login','登录成功 ✓','ok');if(timer){clearInterval(timer);timer=null;}refresh();return;}"
    "if(s.running){setMsg('msg-login','等待扫码…','info');}else if(!s.svg){setMsg('msg-login','未收到二维码，可重试','err');}});}"
    "el('btn-login').onclick=function(){setMsg('msg-login','正在获取二维码…','');post('/api/setup/login/start',{}).then(function(){"
    "setTimeout(poll,800);if(!timer){timer=setInterval(poll,2500);}});};"
    "refresh();"
    "})();"
)


def handle_post(handler, path: str) -> bool:
    """处理 /api/setup/* POST；返回 True 表示已处理。"""
    if not path.startswith("/api/setup/"):
        return False
    try:
        if path == "/api/setup/prepare":
            handler._send_json(start_prepare())
        elif path == "/api/setup/key":
            payload = handler._read_json_body()
            handler._send_json(save_key(payload.get("api_key", "")))
        elif path == "/api/setup/env":
            payload = handler._read_json_body()
            handler._send_json(save_env(payload.get("values") or {}))
        elif path == "/api/setup/login/start":
            handler._send_json(_runner.start())
        else:
            handler._send_json({"ok": False, "error": "unknown setup endpoint"}, 404)
    except Exception as error:
        handler._send_json({"ok": False, "error": str(error)}, 500)
    return True


def handle_get(handler, path: str) -> bool:
    if path == "/api/setup/status":
        handler._send_json(status_payload())
        return True
    if path == "/api/setup/login/status":
        handler._send_json(_runner.status())
        return True
    if path == "/setup":
        handler._send_bytes(render_page(), "text/html; charset=utf-8")
        return True
    return False
