"""Process-local adapter for an unmodified GenericAgent runtime.

G4W Conductor and every Worker run in separate processes.  This module
uses that process boundary to bind a role-specific prompt, tool schema and
handler without changing any file under runtime/app.
"""

from __future__ import annotations

import json
import hashlib
import copy
import threading
from pathlib import Path

import agentmain
import llmcore


_thread_context = threading.local()
_original_system_prompt = agentmain.get_system_prompt
_original_agent_runner_loop = agentmain.agent_runner_loop
_installed_handler = None
_installed_tools = None
_installed_max_turns = None
_original_record_usage = llmcore._record_usage
_usage_hook_installed = False
_original_native_chat = llmcore.NativeToolClient.chat
_original_tool_chat = llmcore.ToolClient.chat
_input_hook_installed = False


def original_ga_system_prompt() -> str:
    return _original_system_prompt()


def _process_system_prompt() -> str:
    agent = getattr(_thread_context, "agent", None)
    provider = getattr(agent, "G4W_system_prompt_provider", None)
    if callable(provider):
        value = str(provider(agent) or "")
        if agent is not None:
            fingerprint = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
            agent.G4W_system_fingerprint = fingerprint
        return value
    return _original_system_prompt()


def _bounded_agent_runner_loop(*args, **kwargs):
    if _installed_max_turns:
        kwargs["max_turns"] = min(int(kwargs.get("max_turns", _installed_max_turns)), _installed_max_turns)
    return _original_agent_runner_loop(*args, **kwargs)


def _record_usage_with_sink(usage, api_mode):
    _original_record_usage(usage, api_mode)
    agent = getattr(_thread_context, "agent", None)
    sink = getattr(agent, "G4W_cache_sink", None) if agent is not None else None
    if not callable(sink) or not usage:
        return
    if api_mode == "responses":
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        cached_tokens = int((usage.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0)
    elif api_mode == "chat_completions":
        input_tokens = int(usage.get("prompt_tokens", 0) or 0)
        cached_tokens = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0)
    else:
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        cached_tokens = int(usage.get("cache_read_input_tokens", 0) or 0)
    context = copy.deepcopy(getattr(agent, "G4W_input_context", {}) or {})
    turn = max(1, int(getattr(agent, "G4W_input_turn", 1) or 1))
    clean_history = copy.deepcopy(context.get("cleanHistory") or {})
    sink({
        "inputTokens": input_tokens,
        "cachedTokens": cached_tokens,
        "ratio": (cached_tokens / input_tokens) if input_tokens else 0.0,
        "apiMode": api_mode,
        "model": getattr(getattr(agent.llmclient, "backend", None), "model", ""),
        "systemFingerprint": getattr(agent, "G4W_system_fingerprint", ""),
        "roundId": str(context.get("roundId") or ""),
        "turn": turn,
        "source": str(context.get("source") or ""),
        "deliveryKind": str(context.get("deliveryKind") or ""),
        "cachePhase": "round_first" if turn == 1 else "tool_turn",
        "cleanHistoryMode": str(clean_history.get("mode") or ""),
        "cleanHistoryCompacted": bool(clean_history.get("compacted")),
        "cleanHistoryUserRounds": int(clean_history.get("userRounds", 0) or 0),
        "cleanHistoryMessageCount": int(clean_history.get("messageCount", 0) or 0),
    })


def _install_usage_hook():
    global _usage_hook_installed
    if not _usage_hook_installed:
        llmcore._record_usage = _record_usage_with_sink
        _usage_hook_installed = True


def _capture_input(client, messages, tools) -> None:
    agent = getattr(_thread_context, "agent", None)
    sink = getattr(agent, "G4W_input_sink", None) if agent is not None else None
    ack_sink = getattr(agent, "G4W_intervention_ack_sink", None) if agent is not None else None
    if not callable(sink) and not callable(ack_sink):
        return
    turn = int(getattr(agent, "G4W_input_turn", 0) or 0) + 1
    agent.G4W_input_turn = turn
    if callable(ack_sink):
        ack_sink(turn, copy.deepcopy(messages or []))
    if not callable(sink):
        return
    controller_system_prompt = next((str(item.get("content") or "") for item in messages or [] if item.get("role") == "system"), "")
    system_prompt = controller_system_prompt or str(getattr(getattr(client, "backend", None), "system", "") or "")
    if isinstance(client, llmcore.NativeToolClient):
        thinking_prompt = str(client._thinking_prompt() or "")
        # Capture runs immediately before GA's NativeToolClient.chat.  On the
        # first Turn the controller system is still in `messages`; subsequent
        # Turns omit that message because the full value persists in
        # backend.system.  Snapshot the effective API instructions in both
        # cases instead of incorrectly showing only THINKING_PROMPT on Turn 2+.
        if controller_system_prompt:
            system_prompt = f"{controller_system_prompt}\n\n{thinking_prompt}" if thinking_prompt else controller_system_prompt
        else:
            system_prompt = str(getattr(getattr(client, "backend", None), "system", "") or thinking_prompt)
    history = copy.deepcopy(getattr(getattr(client, "backend", None), "history", []) or [])
    current = copy.deepcopy(messages or [])
    submitted_current = [item for item in copy.deepcopy(current) if item.get("role") != "system"]
    if isinstance(client, llmcore.NativeToolClient):
        combined_content = []
        tool_results = []
        for msg in current:
            if msg.get("role") == "system":
                continue
            content = msg.get("content", "")
            if isinstance(content, str):
                combined_content.append({"type": "text", "text": content})
            elif isinstance(content, list):
                combined_content.extend(copy.deepcopy(content))
            if msg.get("role") == "user" and msg.get("tool_results"):
                tool_results.extend(copy.deepcopy(msg.get("tool_results") or []))
        result_ids = set()
        tool_result_blocks = []
        for result in tool_results:
            tool_use_id = str(result.get("tool_use_id") or "")
            result_ids.add(tool_use_id)
            if tool_use_id:
                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": result.get("content", ""),
                })
            else:
                combined_content = [{
                    "type": "text",
                    "text": f'<tool_result>{result.get("content", "")}</tool_result>',
                }] + combined_content
        for tool_use_id in copy.deepcopy(getattr(client, "_pending_tool_ids", []) or []):
            if tool_use_id not in result_ids:
                tool_result_blocks.append({"type": "tool_result", "tool_use_id": tool_use_id, "content": ""})
        filtered_content = [
            item for item in combined_content
            if item.get("type") != "text" or str(item.get("text") or "").strip()
        ]
        final_content = tool_result_blocks + filtered_content
        input_context = getattr(agent, "G4W_input_context", None) or {}
        retrieval_context = ""
        if isinstance(input_context, dict):
            retrieval_context = str(input_context.get("retrievalContext") or "").strip()
        if retrieval_context:
            final_content.append({
                "type": "text",
                "text": "<current_round_retrieval_context>\n"
                + retrieval_context
                + "\n</current_round_retrieval_context>",
            })
        if not final_content:
            final_content = [{"type": "text", "text": "."}]
        submitted_current = [{"role": "user", "content": final_content}]
    canonical = []
    if system_prompt:
        canonical.append({"role": "system", "content": system_prompt})
    canonical.extend(copy.deepcopy(history))
    canonical.extend(copy.deepcopy(submitted_current))
    sink({
        "turn": turn,
        "model": getattr(getattr(client, "backend", None), "model", ""),
        "systemFingerprint": hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()[:16] if system_prompt else "",
        "controllerSystemFingerprint": getattr(agent, "G4W_system_fingerprint", ""),
        "controllerSystemPrompt": controller_system_prompt,
        "systemPrompt": system_prompt,
        "tools": copy.deepcopy(tools or []),
        "historyBeforeCall": history,
        "messages": current,
        "submittedCurrentMessages": submitted_current,
        "canonicalMessages": canonical,
        "roundContext": copy.deepcopy(getattr(agent, "G4W_input_context", {}) or {}),
    })


def _native_chat_with_capture(self, messages, tools=None):
    _capture_input(self, messages, tools)
    return (yield from _original_native_chat(self, messages, tools))


def _tool_chat_with_capture(self, messages, tools=None):
    _capture_input(self, messages, tools)
    return (yield from _original_tool_chat(self, messages, tools))


def _install_input_hook():
    global _input_hook_installed
    if _input_hook_installed:
        return
    llmcore.NativeToolClient.chat = _native_chat_with_capture
    llmcore.ToolClient.chat = _tool_chat_with_capture
    _input_hook_installed = True


def configure_process(handler_class, tools_schema=None, max_turns=None) -> None:
    """Bind role behavior inside the current G4W-owned process only."""
    global _installed_handler, _installed_tools, _installed_max_turns
    if _installed_handler not in (None, handler_class):
        raise RuntimeError("A G4W process cannot mix Conductor and Worker GA roles")
    _installed_handler = handler_class
    agentmain.GenericAgentHandler = handler_class
    agentmain.get_system_prompt = _process_system_prompt
    agentmain.agent_runner_loop = _bounded_agent_runner_loop
    _install_usage_hook()
    _install_input_hook()
    if max_turns is not None:
        resolved = max(1, int(max_turns))
        if _installed_max_turns not in (None, resolved):
            raise RuntimeError("A G4W process cannot mix different turn limits")
        _installed_max_turns = resolved
    if tools_schema is not None:
        normalized = json.loads(json.dumps(tools_schema, ensure_ascii=False))
        if _installed_tools is not None and _installed_tools != normalized:
            raise RuntimeError("A G4W process cannot mix different Conductor tool schemas")
        _installed_tools = normalized
        agentmain.TOOLS_SCHEMA = normalized


def update_process_tools(tools_schema) -> None:
    """Replace the role-local schema after an explicit model family switch."""
    global _installed_tools
    normalized = json.loads(json.dumps(tools_schema or [], ensure_ascii=False))
    _installed_tools = normalized
    agentmain.TOOLS_SCHEMA = normalized


def create_agent(handler_class, system_prompt_provider, tools_schema=None, runtime_dir: Path | None = None, max_turns=None):
    configure_process(handler_class, tools_schema, max_turns=max_turns)
    agent = agentmain.GenericAgent()
    agent.G4W_system_prompt_provider = system_prompt_provider
    if runtime_dir is not None:
        runtime = Path(runtime_dir).resolve()
        runtime.mkdir(parents=True, exist_ok=True)
        agent.G4W_runtime_dir = str(runtime)
        agent.log_path = str(runtime / "model_responses" / f"model_responses_{id(agent):x}.txt")
    return agent


def select_model(agent, model_no: int) -> None:
    """Switch GA model, then restore the process-local role tool boundary."""
    agent.next_llm(int(model_no))
    if _installed_tools is not None:
        agentmain.TOOLS_SCHEMA = json.loads(json.dumps(_installed_tools, ensure_ascii=False))


def model_catalog(agent) -> list[dict]:
    result = []
    for index, client in enumerate(getattr(agent, "llmclients", []) or []):
        backend = getattr(client, "backend", None)
        model = str(getattr(backend, "model", "") or "").strip()
        name = str(getattr(backend, "name", "") or model).strip()
        result.append({"index": index, "model": model, "name": name})
    return result


def resolve_model(agent, query: str, fallback_no: int = 0) -> dict:
    catalog = model_catalog(agent)
    normalized = str(query or "").strip().lower()

    # /model 列表展示的是零基索引，因此纯数字参数必须按该索引直接解析。
    # 旧逻辑把 "3" 当模型名称；匹配失败后又回退到 fallback_no，导致 /model 3
    # 实际切换成索引 0。
    if normalized.isdecimal():
        requested_index = int(normalized)
        matched = next((item for item in catalog if item["index"] == requested_index), None)
        if matched:
            return matched
        if catalog:
            valid = f"0-{max(item['index'] for item in catalog)}"
            raise ValueError(f"模型索引 {requested_index} 无效，可用范围：{valid}")
        raise ValueError("当前没有可用模型")

    aliases = {"flash": "flash", "pro": "pro"}
    needle = aliases.get(normalized, normalized)
    if needle:
        exact = next((item for item in catalog if needle in (item["model"].lower(), item["name"].lower())), None)
        matched = exact or next((item for item in catalog if needle in item["model"].lower() or needle in item["name"].lower()), None)
        if matched:
            return matched
    if catalog:
        index = max(0, min(int(fallback_no or 0), len(catalog) - 1))
        return catalog[index]
    return {"index": int(fallback_no or 0), "model": normalized, "name": normalized}


def select_model_name(agent, query: str, fallback_no: int = 0) -> dict:
    selected = resolve_model(agent, query, fallback_no)
    select_model(agent, selected["index"])
    return selected


def load_ga_tool_schema(model_name: str = "") -> list[dict]:
    suffix = "_cn" if any(name in str(model_name or "").lower() for name in ("glm", "minimax", "kimi")) else ""
    path = Path(agentmain.script_dir) / "assets" / f"tools_schema{suffix}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def mark_tool_ownership(schema, owner_label: str) -> list[dict]:
    prefix = f"[{str(owner_label or '').strip()}] "
    marked = json.loads(json.dumps(schema or [], ensure_ascii=False))
    for item in marked:
        function = item.get("function") or {}
        description = str(function.get("description") or "")
        if prefix.strip() and not description.startswith(prefix):
            function["description"] = prefix + description
    return marked


def merge_tool_schemas(*schemas) -> list[dict]:
    merged = []
    seen = set()
    for schema in schemas:
        for item in schema or []:
            name = str((item.get("function") or {}).get("name") or "")
            if not name or name in seen:
                continue
            seen.add(name)
            merged.append(item)
    return merged


def start_agent_runner(agent, name: str) -> threading.Thread:
    def run():
        _thread_context.agent = agent
        try:
            agent.run()
        finally:
            _thread_context.agent = None

    thread = threading.Thread(target=run, daemon=True, name=name)
    thread.start()
    return thread
