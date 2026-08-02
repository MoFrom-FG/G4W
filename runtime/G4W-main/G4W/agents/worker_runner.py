import argparse
import json
import os
import re
import time
from pathlib import Path

from .handlers import WorkerHandler
from .ga_adapter import create_agent, load_ga_tool_schema, mark_tool_ownership, merge_tool_schemas, original_ga_system_prompt, select_model_name, start_agent_runner
from ..memory.sop_catalog import SopCatalog


def extract_result(text: str, request: str = "") -> dict:
    if request:
        return {"status": "needs_input", "summary": request, "question": request}
    matches = re.findall(r"<worker_result>\s*([\s\S]*?)\s*</worker_result>", str(text or ""), flags=re.I)
    if matches:
        try:
            value = json.loads(matches[-1])
            if value.get("status") in ("completed", "needs_input", "failed", "cancelled"):
                return value
        except Exception:
            pass
    cleaned = re.sub(r"<thinking>[\s\S]*?</thinking>|<summary>[\s\S]*?</summary>", "", str(text or ""), flags=re.I).strip()
    return {"status": "completed" if cleaned else "failed", "summary": cleaned[-6000:] or "Worker returned no result"}


def write_progress(path: Path, text: str, turn: int = 0) -> None:
    summaries = re.findall(r"<summary>\s*([\s\S]*?)\s*</summary>", str(text or ""), flags=re.I)
    summary = summaries[-1].strip() if summaries else ""
    if not summary:
        summary = f"Worker 正在执行（Turn {turn}）" if turn else "Worker 正在执行"
    value = {"summary": summary[-1200:], "turn": turn, "updatedAt": time.time()}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def external_memory_prompt(root_value: str) -> str:
    value = str(root_value or "").strip()
    if not value:
        return ""
    root = Path(value).resolve()
    root.mkdir(parents=True, exist_ok=True)
    insight = root / "global_mem_insight.txt"
    facts = root / "global_mem.txt"
    for path, initial in (
        (insight, "# G4W Worker GA memory index\n\n"),
        (facts, "# G4W Worker GA facts\n\n"),
    ):
        if not path.exists():
            path.write_text(initial, encoding="utf-8")
    return "\n".join([
        "[Memory C] Worker External GA Experience Memory",
        "This is separate from the G4W shared SOP index and from runtime/app GA memory.",
        f"This external memory root is authoritative for Worker long-term GA memory: {root}",
        f"L1 index: {insight}",
        f"L2 facts: {facts}",
        f"L3 SOP directory: {root / 'sop'}",
        "Do not write runtime/app/memory. Do not store G4W user facts, persona, relationships or WeChat chat content here.",
    ])


def build_worker_job_context(job: dict, contract: str, sop_catalog: SopCatalog) -> str:
    """Build the stable Worker contract and G4W L1 discovery prefix.

    Workers keep GA's execution identity and tools.  The G4W SOP index is
    an additional shared knowledge pointer, not a replacement for GA memory and
    not a permission grant.
    """
    memory_prompt = external_memory_prompt(job.get("gaMemoryRoot", ""))
    structure_path = sop_catalog.root / "insight_fixed_structure.txt"
    try:
        structure = structure_path.read_text(encoding="utf-8-sig", errors="replace").strip()
    except Exception:
        structure = ""
    workspace_root = str(Path(os.environ.get("G4W_WORKSPACE_ROOT", "") or Path(__file__).resolve().parents[3]).resolve()).rstrip("\\/")
    structure = (
        structure
        .replace("{{MEMORY_ROOT}}", str(sop_catalog.root))
        .replace("{{CODE_ROOT}}", str(Path(__file__).resolve().parents[1]))
        .replace("{{USER_MEMORY_INDEX}}", "Worker不注入用户记忆；只使用任务明确提供的资料")
        .replace("${G4W_WORKSPACE_ROOT}/", workspace_root + "\\")
        .replace("%G4W_WORKSPACE_ROOT%/", workspace_root + "\\")
        .replace("${G4W_WORKSPACE_ROOT}", workspace_root)
        .replace("%G4W_WORKSPACE_ROOT%", workspace_root)
    )
    return "\n".join([
        contract,
        "[Memory A] GA Native Memory is already present in the original GA system prompt above.",
        "[Memory B] G4W Shared SOP Index (authoritative for G4W business SOP discovery)",
        structure,
        f"G4W shared L1 absolute path: {sop_catalog.index_path}",
        sop_catalog.index_text(role="worker"),
        "G4W WORKER JOB",
        f"Worker id: {job['id']}",
        f"Capability: {job['capabilityId']}",
        "The following task was assigned by G4W. It is not a direct user chat:",
        "<task>", str(job.get("task", "")), "</task>",
        f"Default to Flash. If the task is clearly complex, use GA file_read on {sop_catalog.root / 'worker' / 'model-routing' / 'model_routing_sop.md'} and follow its GA-native next_llm hot-switch procedure through G4W_worker_switch_model once; never restart this Worker merely to change models.",
        memory_prompt,
    ])


def render_report(job: dict, result: dict, progress: dict, model_events: list[dict]) -> str:
    data = result.get("data")
    details = ""
    if data not in (None, "", {}, []):
        details = "\n\n## 结构化结果\n\n```json\n" + json.dumps(data, ensure_ascii=False, indent=2, default=str)[:20000] + "\n```"
    switches = "\n".join(
        f"- {item.get('from', '')} → {item.get('to', '')}：{item.get('reason', '')}"
        for item in model_events
    ) or "- 无"
    return (
        f"# Worker任务报告：{job.get('topic') or job.get('capabilityId', 'worker')}\n\n"
        f"- Worker：`{job.get('id', '')}`\n"
        f"- Run：{job.get('runIndex', 0)}\n"
        f"- 能力：`{job.get('capabilityId', '')}`\n"
        f"- 生命周期：{job.get('lifecycle', '')}\n"
        f"- 状态：{result.get('status', '')}\n"
        f"- 最终模型：{result.get('model', '')}\n"
        f"- GA Turn：{progress.get('turn', 0)}\n\n"
        "## 任务\n\n"
        f"{str(job.get('task', '')).strip()}\n\n"
        "## 总结\n\n"
        f"{str(result.get('summary', '')).strip() or 'Worker未提供总结。'}"
        f"{details}\n\n"
        "## 模型切换\n\n"
        f"{switches}\n"
    )


def run_job(job_file: Path) -> dict:
    job = json.loads(job_file.read_text(encoding="utf-8"))
    package = Path(__file__).resolve().parent
    contract = (package.parent / "templates" / "agents" / "worker-contract.md").read_text(encoding="utf-8")
    sop_catalog = SopCatalog(package.parent / "memory" / "sop")
    job_context = build_worker_job_context(job, contract, sop_catalog)
    worker_dir = Path(job["dir"]).resolve()
    worker_dir.mkdir(parents=True, exist_ok=True)
    worker_tools = json.loads((package / "worker_tools.json").read_text(encoding="utf-8"))
    tools = merge_tool_schemas(
        mark_tool_ownership(worker_tools, "G4W共享/Worker本地"),
        mark_tool_ownership(load_ga_tool_schema(job.get("modelName") or "deepseek-v4-flash"), "GA原生/执行平面"),
    )
    agent = create_agent(
        handler_class=WorkerHandler,
        tools_schema=tools,
        runtime_dir=worker_dir / "runtime",
        system_prompt_provider=lambda _: original_ga_system_prompt() + "\n\n" + job_context,
        max_turns=40,
    )
    agent.peer_hint = False
    agent.verbose = False
    agent.inc_out = True
    agent.no_print = True
    agent.task_dir = str(worker_dir)
    agent.G4W_ga_memory_root = str(job.get("gaMemoryRoot") or "")
    agent.G4W_pro_model = str(job.get("proModelName") or "deepseek-v4-pro")
    agent.G4W_model_events_file = str(job.get("modelEventsFile") or "")
    agent.G4W_model_switches = 0
    selected_model = select_model_name(agent, job.get("modelName") or "deepseek-v4-flash", int(job.get("modelNo", 0) or 0))
    history_file = Path(job["historyFile"])
    if job.get("preserveHistory", True) and history_file.exists():
        try:
            agent.llmclient.backend.history = json.loads(history_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    start_agent_runner(agent, f"worker-{job['id']}")
    queue = agent.put_task("Execute the assigned G4W Worker job and return a structured result.", source=f"worker:{job['id']}")
    full = ""
    progress_file = Path(job["progressFile"])
    while True:
        item = queue.get(timeout=1300)
        if "next" in item:
            full += item.get("next", "")
            write_progress(progress_file, full, int(item.get("turn", 0) or 0))
        if "done" in item:
            full = item.get("done") or full
            write_progress(progress_file, full, int(item.get("turn", 0) or 0))
            break
    if job.get("preserveHistory", True):
        try:
            history_file.write_text(json.dumps(agent.llmclient.backend.history, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    result = extract_result(full, getattr(agent, "worker_input_request", ""))
    final_model = str(getattr(getattr(agent.llmclient, "backend", None), "model", "") or selected_model.get("model", ""))
    result["model"] = final_model
    result["modelSwitches"] = int(getattr(agent, "G4W_model_switches", 0) or 0)
    progress = {}
    try:
        progress = json.loads(progress_file.read_text(encoding="utf-8"))
    except Exception:
        pass
    model_events = []
    model_events_file = Path(str(job.get("modelEventsFile") or "")) if job.get("modelEventsFile") else None
    if model_events_file and model_events_file.is_file():
        for line in model_events_file.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                model_events.append(json.loads(line))
            except Exception:
                continue
    Path(job["resultFile"]).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(job["reportFile"]).write_text(render_report(job, result, progress, model_events), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    args = parser.parse_args()
    run_job(Path(args.job).resolve())


if __name__ == "__main__":
    main()
