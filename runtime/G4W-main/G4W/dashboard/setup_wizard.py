"""首次配置向导：与 Windows 桌面壳同语义的四步流程，但跑在看板里（跨平台）。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import ipaddress
from pathlib import Path
from urllib.parse import urlparse

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


def providers() -> dict:
    """向导第②步的服务商下拉：内置预设 + Anthropic 协议提示。"""
    from ..core import model_config

    rows = []
    for item in model_config.PROVIDER_PRESETS:
        rows.append({
            "id": item.get("id", ""),
            "name": item.get("name", ""),
            "apibase": item.get("apibase", ""),
            "models": list(item.get("models") or []),
            "api_mode": item.get("api_mode", ""),
            "reasoning_effort": item.get("reasoning_effort", ""),
            "key_hint": item.get("key_hint", ""),
            "desc": item.get("desc", ""),
        })
    return {"ok": True, "providers": rows, "anthropicHint": model_config.ANTHROPIC_HINT}


def probe(apibase: str, apikey: str, model: str = "") -> dict:
    """探测服务商连通性（填完 Key 立即探测）；失败原样返回上游错误，不抛异常。"""
    from ..core import model_config

    try:
        return model_config.probe(apibase, apikey, model)
    except Exception as error:  # noqa: BLE001
        return {"ok": False, "error": str(error)}


def save_key(
    api_key: str,
    provider: str = "",
    apibase: str = "",
    conductor_model: str = "",
    worker_model: str = "",
) -> dict:
    api_key = (api_key or "").strip()
    if not api_key:
        return {"ok": False, "error": "API Key 不能为空"}
    from ..cli.initializer import configure_ga_key

    try:
        result = configure_ga_key(
            api_key,
            replace_existing=True,
            provider=provider,
            apibase=apibase,
            conductor_model=conductor_model,
            worker_model=worker_model,
        )
    except Exception as error:
        return {"ok": False, "error": str(error)}
    if isinstance(result, dict):
        result.setdefault("ok", True)
        return result
    return {"ok": True}


def skip_key() -> dict:
    """向导里“先跳过填 API Key”：按模板建一个**空密钥**的 mykey.py，并留一个标记。

    之后在控制中心「环境配置 → 模型配置」里填任意 OpenAI 兼容模型即可（不强制 DeepSeek）。
    """
    from ..cli.initializer import ensure_ga_key_template

    try:
        result = ensure_ga_key_template()
    except Exception as error:  # noqa: BLE001
        return {"ok": False, "error": str(error)}
    try:
        marker = state_dir() / ".model-key-skipped"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("skipped at " + time.strftime("%Y-%m-%dT%H:%M:%S") + "\n", encoding="utf-8")
    except OSError:
        pass
    result.setdefault("ok", True)
    result["skipped"] = True
    result["message"] = "已跳过 API Key；之后可在控制中心「环境配置 → 模型配置」里填任意 OpenAI 兼容模型。"
    return result


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
        "<div class='card' id='setup-prepare-card'><h2>① 准备环境</h2>"
        "<p class='sub'>创建运行环境并安装依赖（已就绪时可跳过）。</p>"
        "<button id='btn-prepare'>一键准备环境</button><div class='msg' id='msg-prepare'></div>"
        "<pre id='log-prepare' style='display:none'></pre></div>"
        "<div class='card' id='setup-key-card'><a href='#setup-prepare-card' aria-label='返回准备环境'>&#8592; 返回上一步</a><h2>② 配置模型 API Key</h2>"
        "<p class='sub'>不强制 DeepSeek：任何 OpenAI 兼容的服务都能用（通义 / Kimi / 火山 / 中转站…）。"
        "也可以先跳过，装好后在控制中心「环境配置 → 模型配置」里再配置。</p>"
        "<style>.hidden{display:none!important}.field-note{color:#9aa3b2;font-size:12px;margin-top:5px;line-height:1.5}"
        ".check-row{display:flex;align-items:center;gap:8px;margin-top:10px}"
        ".check-row input[type=checkbox]{width:auto;margin:0}.check-row label{margin:0}"
        "select:disabled,input[readonly]{opacity:.75}</style>"
        "<label>服务商</label><select id='provider-select'></select>"
        "<div class='field-note hidden' id='provider-desc'></div>"
        "<label>API 地址（Base URL）</label><input id='provider-apibase' autocomplete='off' placeholder='https://...'>"
        "<div class='field-note hidden' id='anthropic-hint'></div>"
        "<label>API Key</label>"
        "<input id='key' type='password' autocomplete='off' placeholder='sk-...'>"
        "<div class='field-note' id='key-hint'>在 platform.deepseek.com 获取</div>"
        "<button id='btn-key'>验证并继续</button>"
        "<button id='btn-key-continue' class='ghost hidden' style='margin-left:8px'>仍要继续（稍后在看板配）</button>"
        "<button id='btn-key-skip' class='ghost' style='margin-left:8px'>先跳过，稍后在看板配置</button>"
        "<div class='msg' id='msg-key'></div></div>"
        "<div class='card' id='setup-env-card'><a href='#setup-key-card' aria-label='返回 API Key'>&#8592; 返回上一步</a><h2>③ 环境配置</h2>"
        "<label>你的名字</label><input id='env-name' placeholder='必填'>"
        "<label>日常称呼</label><input id='env-identity'>"
        "<label>性别</label><select id='env-gender'><option value='male'>male</option><option value='female'>female</option><option value='neutral'>neutral</option></select>"
        "<label>机器人名字</label><input id='env-bot' placeholder='必填'>"
        "<div id='model-block'><label>conductor 模型（主对话 / 调度）</label><select id='env-conductor-model'></select>"
        "<label>worker 模型（后台执行）</label><select id='env-worker-model'></select>"
        "<div class='check-row'><input id='env-follow-conductor' type='checkbox' checked>"
        "<label for='env-follow-conductor'>跟随 conductor</label></div></div>"
        "<div class='field-note hidden' id='model-skip-hint'>已跳过 API Key：模型稍后在看板「模型配置」里选择，此处不写入。</div>"
        "<select id='env-model' style='display:none'></select>"
        "<button id='btn-env'>保存并继续</button><div class='msg' id='msg-env'></div></div>"
        "<div class='card' id='setup-login-card'><a href='#setup-env-card' aria-label='返回环境配置'>&#8592; 返回上一步</a><h2>④ 微信扫码登录</h2>"
        "<p class='sub'>使用专门用于 G4W 的微信账号扫码；与 Windows 版同一账号会互踢。</p>"
        "<button id='btn-login'>获取二维码</button>"
        "<div id='qr-box'></div><div class='msg' id='msg-login'></div></div>"
        "<p class='foot'>已完成四步？<a href='/' style='color:#5b9dff'>刷新进入看板</a></p>"
        "<script>" + SCRIPT + "</script>"
        "<script>"
        "(function(){function el(id){return document.getElementById(id);}function post(path,body){return fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})}).then(function(r){return r.json();});}function setMsg(id,text,kind){var n=el(id);n.textContent=text||'';n.className='msg'+(kind?' '+kind:'');}var PROVIDER_FALLBACK=[{id:'deepseek',name:'DeepSeek（官方）',apibase:'https://api.deepseek.com',models:['deepseek-v4-flash','deepseek-v4-pro'],key_hint:'在 platform.deepseek.com 获取',desc:'官方直连，国内可访问'},{id:'openai',name:'OpenAI（官方）',apibase:'https://api.openai.com/v1',models:['gpt-4o','gpt-4o-mini'],key_hint:'在 platform.openai.com 获取',desc:'需要可访问海外的网络'},{id:'moonshot',name:'Moonshot / Kimi',apibase:'https://api.moonshot.cn/v1',models:['kimi-k2-0905-preview'],key_hint:'在 platform.moonshot.cn 获取',desc:''},{id:'qwen',name:'通义千问（兼容模式）',apibase:'https://dashscope.a"
        "liyuncs.com/compatible-mode/v1',models:['qwen-max','qwen-plus'],key_hint:'在 dashscope.console.aliyun.com 获取',desc:''},{id:'custom',name:'其它 / 自填 Base URL',apibase:'',models:[],key_hint:'填该服务商提供的 API Key',desc:'任何 OpenAI 兼容接口都行：中转站、火山、自建网关…'}];var providerList=PROVIDER_FALLBACK.slice();var anthropicHint='';var probedModels=[];var keySkipped=false;function currentProvider(){var id=el('provider-select').value;for(var i=0;i<providerList.length;i++){if(providerList[i].id===id){return providerList[i];}}return providerList[0]||null;}function onProviderChange(){var p=currentProvider();if(!p){return;}var base=el('provider-apibase');var custom=(p.id==='custom');base.value=p.apibase||'';base.readOnly=!custom;var d=el('provider-desc');if(p.desc){d.textContent=p.desc;d.classList.remove('hidden');}else{d.textContent='';d.classList.add('hidden');}el('key-hint').textContent=p.key_hint||'填该服务商提供的 API Key"
        "';var ah=el('anthropic-hint');if(anthropicHint){ah.textContent=anthropicHint;ah.classList.remove('hidden');}else{ah.textContent='';ah.classList.add('hidden');}probedModels=[];}function renderProviders(){var sel=el('provider-select');sel.innerHTML='';for(var i=0;i<providerList.length;i++){var p=providerList[i];var o=document.createElement('option');o.value=p.id;o.textContent=p.name||p.id;sel.appendChild(o);}var want='';for(var j=0;j<providerList.length;j++){if(providerList[j].id==='deepseek'){want='deepseek';}}if(!want&&providerList[0]){want=providerList[0].id;}sel.value=want;onProviderChange();}function loadProviders(){return post('/api/setup/providers',{}).then(function(r){if(r&&r.ok&&r.providers&&r.providers.length){providerList=r.providers;anthropicHint=r.anthropicHint||'';}}).catch(function(){}).then(function(){renderProviders();});}function fillModelOptions(models){var list=(models&"
        "&models.length)?models:((currentProvider()||{}).models||[]);var c=el('env-conductor-model'),w=el('env-worker-model');var pc=c.value,pw=w.value;[c,w].forEach(function(sel){sel.innerHTML='';for(var i=0;i<list.length;i++){var o=document.createElement('option');o.value=list[i];o.textContent=list[i];sel.appendChild(o);}});if(pc&&list.indexOf(pc)>=0){c.value=pc;}if(pw&&list.indexOf(pw)>=0){w.value=pw;}syncFollow();}function syncFollow(){var follow=el('env-follow-conductor').checked;var c=el('env-conductor-model'),w=el('env-worker-model');if(follow){w.value=c.value;}w.disabled=follow;}function showContinue(on){var b=el('btn-key-continue');if(b){if(on){b.classList.remove('hidden');}else{b.classList.add('hidden');}}}function applySkipState(){var b=el('model-block'),h=el('model-skip-hint');if(!b||!h){return;}b.classList.toggle('hidden',keySkipped);h.classList.toggle('hidden',!keySkipped);if(!keySk"
        "ipped){fillModelOptions(probedModels);}}function refreshSkip(){return fetch('/api/setup/status').then(function(r){return r.json();}).then(function(s){keySkipped=!!(s.key&&s.key.skipped);applySkipState();return s;}).catch(function(){});}el('provider-select').onchange=onProviderChange;el('env-follow-conductor').onchange=syncFollow;el('env-conductor-model').onchange=syncFollow;el('btn-key').onclick=function(){var v=el('key').value.trim();if(!v){setMsg('msg-key','请输入 API Key','err');return;}var p=currentProvider();var provider=p?p.id:'';var apibase=(el('provider-apibase').value||'').trim();if(provider==='custom'&&!apibase){setMsg('msg-key','请填写 API 地址（Base URL）','err');return;}var btn=el('btn-key');btn.disabled=true;showContinue(false);setMsg('msg-key','正在验证 Key…','');post('/api/setup/probe',{apibase:apibase,apikey:v,model:''}).then(function(pr){if(!(pr&&pr.ok)){var err=(pr&&pr.error)||'上游没有"
        "返回原因';setMsg('msg-key','验证失败：'+err,'err');showContinue(true);btn.disabled=false;return null;}probedModels=(pr.models&&pr.models.length)?pr.models:[];setMsg('msg-key','验证通过 ✓（'+(pr.modelCount||probedModels.length||0)+' 个模型 · '+(pr.mode||'models')+'）','ok');var cands=probedModels.length?probedModels:((p&&p.models)||[]);var m0=cands.length?cands[0]:'';return post('/api/setup/key',{api_key:v,provider:provider,apibase:apibase,conductor_model:m0,worker_model:m0}).then(function(r){if(r&&r.ok){setMsg('msg-key','已保存 ✓','ok');keySkipped=false;fillModelOptions(cands);}else{setMsg('msg-key',(r&&r.error)||'保存失败','err');}});}).catch(function(e){setMsg('msg-key','验证失败：'+(e&&e.message?e.message:e),'err');showContinue(true);}).then(function(){btn.disabled=false;});};function skipKey(){setMsg('msg-key','正在跳过…','');return post('/api/setup/key/skip',{}).then(function(r){if(r&&r.ok){setMsg('msg-key','已跳过：稍后在"
        "看板「环境配置 → 模型配置」里配置 ✓','ok');keySkipped=true;applySkipState();}else{setMsg('msg-key',(r&&r.error)||'跳过失败','err');}});}el('btn-key-skip').onclick=skipKey;el('btn-key-continue').onclick=skipKey;el('btn-env').onclick=function(){var values={G4W_USER_NAME:el('env-name').value.trim(),G4W_USER_IDENTITY:el('env-identity').value.trim(),G4W_USER_GENDER:el('env-gender').value,G4W_BOT_NAME:el('env-bot').value.trim()};var c='',w='';if(!keySkipped){c=el('env-conductor-model').value;w=el('env-follow-conductor').checked?c:el('env-worker-model').value;if(c){values.G4W_CONDUCTOR_MODEL=c;}if(w){values.G4W_WORKER_MODEL=w;}}setMsg('msg-env','保存中…','');var wk=el('key').value.trim();var wp=currentProvider();var wpv=wp?wp.id:'';var wb=(el('provider-apibase').value||'').trim();var pre=Promise.resolve(null);if(!keySkipped&&wk&&wpv){pre=post('/api/setup/key',{api_key:wk,provider:wpv,apibase:wb,conductor_model:c||'',worker_model:w||''}).then(function(kr){if(!(kr&&kr.ok)){throw new Error((kr&&kr.error)||'写回模型失败');}return kr;});}pre.then(function(){return post('/api/setup/env',{values:values});}).then(function(r){if(r&&r.ok){setMsg('msg-env','已保存','ok');}else{setMsg('msg-env',(r&&r.error)||'保存失败','err');}}).catch(function(e){setMsg('msg-env',(e&&e.message)?e.message:String(e),'err');});};renderProviders();loadProviders().then(refreshSkip);})();"
        "</script>"
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
    "if(s.key&&s.key.skipped){var kt=el('txt-key');if(kt){kt.textContent='已跳过（待在看板配置）';}}"
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
    "el('btn-key-skip').onclick=function(){setMsg('msg-key','正在跳过…','');post('/api/setup/key/skip',{}).then(function(r){"
    "if(r.ok){setMsg('msg-key','已跳过：稍后在看板「环境配置 → 模型配置」里配置 ✓','ok');refresh();}"
    "else{setMsg('msg-key',r.error||'跳过失败','err');}});};"
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


def authorize_setup(handler) -> bool:
    """Only an unclaimed local installation may configure itself anonymously."""
    auth = getattr(handler, "auth", None)
    initialized = auth.is_initialized() if auth is not None else (state_dir() / "dashboard-auth.json").is_file()
    if initialized:
        if auth is not None:
            return handler._require_auth()
        handler._send_json({"ok": False, "error": "请启动看板并登录后修改配置"}, 403)
        return False
    try:
        peer = ipaddress.ip_address(handler.client_address[0])
        host = urlparse("//" + str(handler.headers.get("Host") or ""))
        local_host = host.hostname in ("127.0.0.1", "::1", "localhost")
        origin = handler.headers.get("Origin")
        origin_url = urlparse(origin) if origin else None
        same_origin = not origin_url or (
            origin_url.scheme in ("http", "https") and origin_url.netloc.lower() == host.netloc.lower()
        )
        if peer.is_loopback and local_host and same_origin:
            return True
    except (ValueError, TypeError):
        pass
    handler._send_json({"ok": False, "error": "首次配置仅允许本机访问，请使用 SSH 隧道或登录看板"}, 403)
    return False


def handle_post(handler, path: str) -> bool:
    """处理 /api/setup/* POST；返回 True 表示已处理。"""
    if not path.startswith("/api/setup/"):
        return False
    if not authorize_setup(handler):
        return True
    try:
        if path == "/api/setup/prepare":
            handler._send_json(start_prepare())
        elif path == "/api/setup/providers":
            handler._send_json(providers())
        elif path == "/api/setup/probe":
            payload = handler._read_json_body()
            handler._send_json(probe(
                payload.get("apibase", ""),
                payload.get("apikey", ""),
                payload.get("model", ""),
            ))
        elif path == "/api/setup/key":
            payload = handler._read_json_body()
            handler._send_json(save_key(
                payload.get("api_key", ""),
                provider=payload.get("provider", ""),
                apibase=payload.get("apibase", ""),
                conductor_model=payload.get("conductor_model", ""),
                worker_model=payload.get("worker_model", ""),
            ))
        elif path == "/api/setup/key/skip":
            handler._send_json(skip_key())
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
    if path in ("/setup", "/api/setup/status", "/api/setup/login/status", "/api/setup/providers"):
        if not authorize_setup(handler):
            return True
    if path == "/api/setup/providers":
        handler._send_json(providers())
        return True
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
