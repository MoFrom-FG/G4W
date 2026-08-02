"""Path resolution for G4W Knowledge Base.

The knowledge roots are parallel to, but separate from, conversation memory.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def _setting(name: str) -> str | None:
    return os.environ.get(name)


def _expand_workspace_refs(value: str, base: Path | None = None) -> str:
    root = str((base or _portable_root()).resolve())
    return (
        str(value or "")
        .replace("${G4W_WORKSPACE_ROOT}", root)
        .replace("%G4W_WORKSPACE_ROOT%", root)
    )


def _resolve_setting_path(value: str, base: Path | None = None) -> Path:
    path = Path(_expand_workspace_refs(value, base)).expanduser()
    if not path.is_absolute():
        path = (base or _portable_root()) / path
    return path.resolve()


def _context_value(name: str) -> str | None:
    """Read launch roots from the per-conversation control context when present."""
    runtime_dir = _setting("G4W_RUNTIME_DIR")
    if not runtime_dir:
        return None
    path = Path(runtime_dir).expanduser() / "G4W-context.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    value = payload.get(name)
    return str(value).strip() if value else None


def _inside_conversation_runtime(path: Path) -> bool:
    parts = {part.lower() for part in path.parts}
    return "memory" in parts and "conversations" in parts and "runtime" in parts


def _looks_like_workspace_root(path: Path) -> bool:
    if _inside_conversation_runtime(path):
        return False
    return (
        (path / "start_G4W_ga.bat").exists()
        or (path / "runtime" / "G4W-main").exists()
        or (path / "runtime" / "G4W-data").exists()
    )


def _portable_root() -> Path:
    """Return the launch-time portable root used by /bind defaults."""
    workspace_root = _setting("G4W_WORKSPACE_ROOT") or _context_value("workspaceRoot")
    if workspace_root:
        candidate = Path(workspace_root).expanduser().resolve()
        if not _inside_conversation_runtime(candidate):
            return candidate
    runtime_dir = _setting("G4W_RUNTIME_DIR")
    if runtime_dir:
        candidate = Path(runtime_dir).expanduser().resolve().parent
        if _looks_like_workspace_root(candidate):
            return candidate
    bbs_cwd = _setting("BBS_CWD")
    if bbs_cwd:
        candidate = Path(bbs_cwd).expanduser().resolve()
        if not _inside_conversation_runtime(candidate):
            return candidate
    return Path(__file__).resolve().parents[4]


def data_root() -> Path:
    override = _setting("G4W_KNOWLEDGE_DATA_DIR") or _setting("G4W_KNOWLEDGE_DATA_DIR")
    root = _portable_root()
    if override:
        return _resolve_setting_path(override, root)
    data_dir = _setting("G4W_DATA_DIR") or _setting("G4W_STATE_DIR") or _context_value("stateDir")
    if data_dir:
        base = _resolve_setting_path(data_dir, root)
    else:
        base = _portable_root() / "runtime" / "G4W-data"
    return base / "knowledge"


def _shared_vector_base(path: Path) -> Path:
    """Return the shared vector-index root, not a product child root."""
    if path.name.lower() == "memory":
        return path.parent
    return path


def vector_root() -> Path:
    portable_root = _portable_root()
    override = _setting("G4W_KNOWLEDGE_VECTOR_INDEX_DIR")
    if override:
        return _resolve_setting_path(override, portable_root)
    root = _setting("G4W_VECTOR_INDEX_DIR")
    if root:
        base = _shared_vector_base(_resolve_setting_path(root, portable_root))
    else:
        base = portable_root / "runtime" / "G4W-vector-index"
    return base / "knowledge"


def documents_dir() -> Path:
    return data_root() / "documents"


def chunks_path() -> Path:
    return data_root() / "chunks.jsonl"


def manifest_path() -> Path:
    return data_root() / "manifest.json"


def list_map_path() -> Path:
    return data_root() / "list_map.json"


def ensure_dirs() -> None:
    documents_dir().mkdir(parents=True, exist_ok=True)
    vector_root().mkdir(parents=True, exist_ok=True)
