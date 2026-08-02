"""`/export` command: export last assistant reply / locate full conversation log.
Pure functions, no Qt deps. UI wiring lives in the frontend file.
"""
import os
import re
import sys
from datetime import datetime

from continue_cmd import _pairs, _assistant_text

_TEMP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'temp')
_BACKTICK_RUN_RE = re.compile(r'`+')


def wrap_for_clipboard(text, language='markdown'):
    """Wrap text in a markdown code fence that survives nested fences.

    CommonMark closes a fenced block on a line whose backtick run is at least
    as long as the opening one. Pick `max(3, longest_inner_run + 1)` so any
    backticks inside `text` are strictly shorter than the outer fence.
    """
    longest = max((len(m.group(0)) for m in _BACKTICK_RUN_RE.finditer(text)), default=0)
    fence = '`' * max(3, longest + 1)
    return f"{fence}{language}\n{text}\n{fence}"


def last_assistant_text(agent):
    """Last assistant reply as joined plain text from `agent.log_path`.

    Reads the model_responses log, takes the most recent === Response === block,
    and joins only the `text` fields (skips thinking/tool_use/tool_result).

    Returns None when:
      - the LLM backend's history is empty (fresh agent or just-`/new`'d —
        log_path may still hold prior-session content that no longer belongs
        to the current conversation)
      - the log is missing or unreadable
      - there's no Response yet, or the last Response holds no text blocks
        (e.g. tool-only turn)

    OS errors are logged to stderr — small failure radius, the UI falls back
    gracefully.
    """
    if not (agent.llmclient and agent.llmclient.backend.history):
        return None
    log = agent.log_path
    if not os.path.isfile(log):
        return None
    try:
        with open(log, encoding='utf-8', errors='replace') as f:
            content = f.read()
    except OSError as e:
        print(f"[export_cmd] failed to read {log}: {e}", file=sys.stderr)
        return None
    pairs = _pairs(content)
    if not pairs:
        return None
    text = _assistant_text(pairs[-1][1])
    return text if text.strip() else None


def export_to_temp(text, name):
    """Write text to temp/<name>.md, overwriting on collision. Returns full path.

    `name` is sanitized via os.path.basename to keep the write inside temp/.
    Empty/whitespace name falls back to a timestamp. `.md` is appended if
    the user-supplied name has no extension.
    """
    os.makedirs(_TEMP_DIR, exist_ok=True)
    safe = os.path.basename((name or '').strip())
    if not safe:
        safe = f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    if not os.path.splitext(safe)[1]:
        safe = safe + '.md'
    path = os.path.join(_TEMP_DIR, safe)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    return path
