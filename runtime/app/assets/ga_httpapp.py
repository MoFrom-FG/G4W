import threading, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi import FastAPI, Header, HTTPException, Query, Depends; from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from agentmain import GenericAgent as GA

PORT, API_KEY = int(sys.argv[1]), sys.argv[2]
app, agent, lock = FastAPI(), GA(), threading.Lock()
outputs, stopped = [], True
threading.Thread(target=agent.run, daemon=True).start()
class Req(BaseModel): prompt: str = ""
agent.verbose = False

def require_key(key: str = Query(None), x_api_key: str = Header(None, alias="X-API-Key")):
    if API_KEY not in (key, x_api_key): raise HTTPException(404)

def run_task(prompt):
    global stopped
    segs = []  # 本任务按 turn 索引的分段输出
    with lock: task_start = len(outputs)
    def flush():
        with lock: outputs[task_start:] = segs
    try:
        dq = agent.put_task(prompt, source="http")
        while "done" not in (item := dq.get(timeout=2200)):
            outs = item.get("outputs")
            if not outs: continue
            idx = max(0, int(item.get("turn", 0) or 0) - 1)  # turn 1-based → 槽位 0-based
            while len(segs) <= idx: segs.append("")
            segs[idx] = str(outs[-1])                         # 当前 turn
            if len(outs) >= 2 and idx >= 1: segs[idx - 1] = str(outs[-2])  # 前一 turn 落定值
            flush()
        segs = [str(s) for s in item.get("outputs", [])]      # done 时全量替换
        flush()
    finally: stopped = True

@app.post("/put_task")
def put_task(req: Req, _=Depends(require_key)):
    global stopped
    with lock:
        if not stopped: return {"ok": False, "error": "should abort first"}
        stopped = False
    threading.Thread(target=run_task, args=(req.prompt,), daemon=True).start()
    return {"ok": True}

@app.post("/abort")
def abort(_=Depends(require_key)): agent.abort(); return {"ok": True}

@app.post("/input")
def input_task(req: Req, _=Depends(require_key)):
    global stopped
    if not stopped: agent.intervene = req.prompt; return {"ok": True, "mode": "intervene"}
    with lock:
        if not stopped: agent.intervene = req.prompt; return {"ok": True, "mode": "intervene"}
        stopped = False
    threading.Thread(target=run_task, args=(req.prompt,), daemon=True).start()
    return {"ok": True, "mode": "task"}

@app.get("/output")
def get_output(k: int = Query(5), _=Depends(require_key)):
    with lock: r = outputs[-k:]
    return {"stopped": stopped, "output": "\n".join(r),
            "history": "\n".join(str(h) for h in agent.history)}

@app.get("/llm")
def llm_ep(llm_no: int = Query(None), _=Depends(require_key)):
    if llm_no is not None:
        agent.next_llm(llm_no)
    return {"llm_no": agent.llm_no, "name": agent.get_llm_name(),
            "llms": [{"no": i, "name": n, "current": a} for i, n, a in agent.list_llms()]}

@app.get("/sysprompt")
def sysprompt_ep(text: str = Query(None), _=Depends(require_key)):
    if text is not None:
        agent.extra_sys_prompts = [text] if text else []
    return {"extra_sys_prompts": agent.extra_sys_prompts}

HELP = """GA HTTP 操作协议（所有请求带 ?key=API_KEY，或 Header X-API-Key）
GET  /output?k=N      查看状态：{stopped, output(末N条), history}。stopped=true 表示空闲
POST /input  {prompt} 下发指令：空闲时作为新任务，忙时作为中途干预(intervene)
POST /abort           中止当前任务
GET  /llm[?llm_no=N]  查/切模型：返回 {llm_no,name,llms:[{no,name,current}]}
GET  /sysprompt[?text]查/设附加系统提示(extra_sys_prompts)，text 为空则清空
纠偏流程：先 GET /output 读 history 判断状态→需要时 POST /input 注入纠偏指令"""

@app.get("/help")
def help_ep(_=Depends(require_key)): return {"help": HELP}

@app.get("/")
def ui():
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GA Monitor</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font:14px/1.6 system-ui,sans-serif;background:#f8f9fa;color:#212529;padding:24px;max-width:900px;margin:0 auto}}
#status{{font-size:18px;padding:8px 16px;border-radius:8px;margin-bottom:16px;display:inline-block;font-weight:600}}
.stopped{{background:#d4edda;color:#155724}}.running{{background:#fff3cd;color:#856404}}
.section{{background:#fff;border-radius:10px;padding:18px;margin-bottom:14px;box-shadow:0 1px 4px rgba(0,0,0,.06)}}
.section h3{{color:#533483;margin-bottom:8px;font-size:15px}}
textarea{{width:100%;height:80px;background:#fff;color:#212529;border:1px solid #ced4da;border-radius:6px;padding:10px;font:inherit;resize:vertical}}
button{{padding:10px 24px;background:#533483;color:#fff;border:none;border-radius:6px;font:inherit;cursor:pointer;font-weight:500}}
button:hover{{background:#7c3aed}}
pre{{background:#f1f3f5;padding:12px;border-radius:6px;overflow-x:auto;font:13px monospace;max-height:400px;overflow-y:auto}}
</style></head><body>
<div id="status" class="stopped">● Loading...</div>
<div class="section"><h3>Output</h3><div id="output"></div></div>
<div class="section"><h3>History</h3><div id="history"></div></div>
<textarea id="prompt" placeholder="Enter instruction..."></textarea>
<button onclick="send()">Send</button>
<script>
const K=new URLSearchParams(location.search).get('key')||'';
async function poll(){{let r=await fetch('/output',{{headers:{{'X-API-Key':K}}}});let d=await r.json();
let s=document.getElementById('status');s.textContent=(d.stopped?'● Stopped':'● Running');s.className=d.stopped?'stopped':'running';
document.getElementById('output').innerHTML=marked.parse(d.output||'_empty_');
document.getElementById('history').innerHTML=marked.parse(d.history||'_empty_');}}
async function send(){{let p=document.getElementById('prompt').value;if(!p)return;
await fetch('/input',{{method:'POST',headers:{{'X-API-Key':K,'Content-Type':'application/json'}},body:JSON.stringify({{prompt:p}})}});
document.getElementById('prompt').value='';poll();}}
poll();setInterval(poll,3000);
</script></body></html>""")

if __name__ == "__main__":
    import uvicorn; uvicorn.run(app, host="0.0.0.0", port=PORT)
