"""Sandbox + production path resolution for vector indexes.

Sandbox write root: BBS_CWD/workspace/sandbox/vector_index/
Prod INDEX_DIR: G4W_VECTOR_INDEX_DIR or DEFAULT_PROD_INDEX_DIR (parallel tree).
Never points at hybrid/ / daily_primary / production DATA write surfaces.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Tuple, Union

# Default BBS_CWD for H3 window (overridable via env)
_DEFAULT_BBS_CWD = (
    r"D:\G4W\GenericAgent-Desktop-Windows-Portable 1.8"
    r"\GenericAgent-Desktop-Windows-Portable\runtime\app\temp"
    r"\hive_cb_storage_p6_retrieval"
)

def _runtime_root() -> Path:
    """Return package-local runtime directory for portable installs."""
    return Path(__file__).resolve().parents[4]


def _workspace_root() -> Path:
    raw = str(os.environ.get("G4W_WORKSPACE_ROOT", "") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return _runtime_root().parent.resolve()


def _expand_workspace_refs(raw: str) -> str:
    root = str(_workspace_root())
    return (
        str(raw or "")
        .replace("${G4W_WORKSPACE_ROOT}", root)
        .replace("%G4W_WORKSPACE_ROOT%", root)
    )


def _resolve_env_path(raw: str) -> Path:
    path = Path(_expand_workspace_refs(raw)).expanduser()
    if not path.is_absolute():
        path = _workspace_root() / path
    return path.resolve()


# S6: production vector index parallel tree (not under DATA).
#
# Layout v2 keeps product domains parallel under the shared vector-index base:
#   G4W-vector-index/
#     memory/       # conversation/L4 memory vector index (new default)
#     knowledge/    # knowledge-base vector index (owned by G4W.knowledge.paths)
#
# Back-compat: older installs stored the memory index directly in the base dir.
# Read paths prefer ``memory/`` when ready, then fall back to the legacy base.
DEFAULT_VECTOR_INDEX_BASE_DIR = _runtime_root() / "G4W-vector-index"
LEGACY_PROD_INDEX_DIR = DEFAULT_VECTOR_INDEX_BASE_DIR
DEFAULT_PROD_INDEX_DIR = DEFAULT_VECTOR_INDEX_BASE_DIR / "memory"

# Path name fragments that must never receive vector index writes (W3 FIX-1 / W2-P0-1).
_FORBIDDEN_NAME_PARTS: Tuple[str, ...] = (
    "hybrid",
    "daily_primary",
    "transcript",
    "aggregate",
)

# Env: G4W_VECTOR_SANDBOX_ALLOW_TEMP=1 allows unit-test temps that contain
# "sandbox" or common temp markers (tmp/temp) when not under sandbox/vector_index.
_ENV_ALLOW_TEMP = "G4W_VECTOR_SANDBOX_ALLOW_TEMP"
_ENV_INDEX_DIR = "G4W_VECTOR_INDEX_DIR"


def default_bbs_cwd() -> Path:
    raw = str(os.environ.get("G4W_BBS_CWD", "") or "").strip()
    if raw:
        return Path(raw)
    return Path(_DEFAULT_BBS_CWD)


def sandbox_vector_index_root(bbs_cwd: Optional[Path] = None) -> Path:
    """Return sandbox vector_index directory (created on demand by callers)."""
    root = Path(bbs_cwd) if bbs_cwd is not None else default_bbs_cwd()
    return (root / "workspace" / "sandbox" / "vector_index").resolve()


def _norm_path_str(path: Path) -> str:
    return str(Path(path).resolve()).replace("\\", "/").lower()


def _has_path_fragment(s: str, fragment: str) -> bool:
    """True if fragment appears as a path segment (or whole path component)."""
    frag = fragment.lower().strip("/")
    if not frag:
        return False
    parts = [p for p in s.split("/") if p]
    return frag in parts or any(frag == p for p in parts)


def assert_sandbox_write_allowed(path: Path) -> Path:
    """Raise if path is not an allowed sandbox vector write target.

    Allowed:
    - path contains both ``sandbox`` and ``vector_index`` as segments/substrings
      and does not contain a forbidden production name segment
    - or env ``G4W_VECTOR_SANDBOX_ALLOW_TEMP=1`` and path looks like a test
      temp (contains sandbox / tmp / temp)

    Always forbidden name segments: hybrid, daily_primary, transcript, aggregate.
    """
    resolved = Path(path).resolve()
    s = _norm_path_str(resolved)

    for part in _FORBIDDEN_NAME_PARTS:
        if _has_path_fragment(s, part) or f"/{part}/" in f"/{s}/":
            raise ValueError(
                f"vector index write forbidden (name fragment {part!r}): {resolved}"
            )

    # Prefer canonical sandbox/vector_index
    if "sandbox" in s and "vector_index" in s:
        return resolved

    # Explicit unit-test temp allowlist (must still not hit FORBIDDEN above)
    allow_temp = str(os.environ.get(_ENV_ALLOW_TEMP, "") or "").strip().lower()
    if allow_temp in ("1", "true", "yes", "on"):
        if "sandbox" in s or "/tmp" in s or "/temp" in s or "\\tmp" in str(resolved).lower() or "\\temp" in str(resolved).lower():
            return resolved

    raise ValueError(
        f"vector index write forbidden outside sandbox/vector_index: {resolved}"
    )


def ensure_sandbox_dir(bbs_cwd: Optional[Path] = None) -> Path:
    d = sandbox_vector_index_root(bbs_cwd)
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_prod_index_dir() -> Path:
    """Preferred production memory INDEX_DIR (parallel to knowledge/)."""
    return Path(DEFAULT_PROD_INDEX_DIR).resolve()


def prod_index_write_root() -> Path:
    """Root used for production memory writes.

    Env ``G4W_VECTOR_INDEX_DIR`` remains an explicit override.  Without env, new
    writes target ``G4W-vector-index/memory``.  Older root-level memory
    indexes are accepted by read-path fallback and guarded legacy writes.
    """
    raw = str(os.environ.get(_ENV_INDEX_DIR, "") or "").strip()
    if raw:
        return _resolve_env_path(raw)
    return Path(DEFAULT_PROD_INDEX_DIR).resolve()


def legacy_prod_index_dir() -> Path:
    """Legacy production memory INDEX_DIR (pre-layout-v2 root-level index)."""
    return Path(LEGACY_PROD_INDEX_DIR).resolve()


def _prod_index_ready(directory: Path) -> bool:
    """True when directory has meta.json and (index.bin or vectors.npy)."""
    d = Path(directory)
    if not d.is_dir():
        return False
    if not (d / "meta.json").is_file():
        return False
    return (d / "index.bin").is_file() or (d / "vectors.npy").is_file()


def _legacy_prod_index_ready() -> bool:
    """True for old root-level memory indexes.

    Knowledge indexes live below ``knowledge/`` and are intentionally ignored.
    """
    return _prod_index_ready(legacy_prod_index_dir())


def resolve_vector_index_dir(bbs_cwd: Optional[Path] = None) -> Path:
    """Read-path memory index root (S7 prod-prefer, layout-v2 aware).

    Priority:
      1. G4W_VECTOR_INDEX_DIR if set (env wins, even if not ready)
      2. DEFAULT_PROD_INDEX_DIR (``G4W-vector-index/memory``) if ready
      3. LEGACY_PROD_INDEX_DIR (``G4W-vector-index``) if ready
      4. sandbox vector_index root

    Does not enable the retrieval flag. Write guards handle both new and legacy
    roots so incremental upserts can keep existing installations working.
    """
    raw = str(os.environ.get(_ENV_INDEX_DIR, "") or "").strip()
    if raw:
        return _resolve_env_path(raw)
    prod = Path(DEFAULT_PROD_INDEX_DIR).resolve()
    if _prod_index_ready(prod):
        return prod
    legacy = legacy_prod_index_dir()
    if _prod_index_ready(legacy):
        return legacy
    return sandbox_vector_index_root(bbs_cwd)


def prod_index_staging_dir() -> Path:
    """Sibling staging dir: ``{INDEX_DIR}.staging`` (I2 switch path)."""
    root = prod_index_write_root()
    return Path(str(root) + ".staging")


def _is_legacy_write_root(root: Path) -> bool:
    """Whether the current explicit/default write root is the old base dir."""
    try:
        return Path(root).resolve() == legacy_prod_index_dir()
    except OSError:
        return False


def _path_under_or_equal(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _has_forbidden_fragment(resolved: Path) -> Optional[str]:
    parts_lower = {str(p).lower() for p in resolved.parts}
    for frag in _FORBIDDEN_NAME_PARTS:
        if frag in parts_lower:
            return frag
    return None


def assert_prod_index_write_allowed(path: Union[str, Path]) -> Path:
    """Allow writes only under production INDEX_DIR (or staging/bak siblings).

    - FORBIDDEN name fragments always denied (hybrid/daily_primary/transcript/aggregate).
    - Allowed: prod_index_write_root(), ``{root}.staging``, ``{root}/_staging``,
      and ``{root}.bak.<ts>`` trees.
    - Sandbox-only paths are NOT accepted here (use assert_sandbox_write_allowed).
    """
    resolved = Path(path).expanduser().resolve()
    frag = _has_forbidden_fragment(resolved)
    if frag is not None:
        raise ValueError(
            f"vector index prod write forbidden (name fragment {frag}): {resolved}"
        )

    root = prod_index_write_root()
    allowed_roots = [
        root,
        Path(str(root) + ".staging"),
        root / "_staging",
    ]
    for ar in allowed_roots:
        if resolved == ar.resolve() or _path_under_or_equal(resolved, ar):
            return resolved

    # Legacy compatibility: if the active/read index is the old root-level
    # memory index, permit updating that root and its staging/bak siblings.  Do
    # not treat nested product domains such as ``knowledge/`` as memory writes.
    legacy = legacy_prod_index_dir()
    if _legacy_prod_index_ready():
        legacy_roots = [
            legacy,
            Path(str(legacy) + ".staging"),
            legacy / "_staging",
        ]
        for ar in legacy_roots:
            if resolved == ar.resolve() or _path_under_or_equal(resolved, ar):
                try:
                    rel = resolved.relative_to(legacy)
                except ValueError:
                    rel = None
                if rel is None or not rel.parts or rel.parts[0].lower() != "knowledge":
                    return resolved

    # backup trees: same parent, name prefix ``{root.name}.bak.``
    bak_prefix = root.name + ".bak."
    for ancestor in (resolved, *resolved.parents):
        if ancestor.parent == root.parent and ancestor.name.startswith(bak_prefix):
            if resolved == ancestor or _path_under_or_equal(resolved, ancestor):
                return resolved

    legacy_bak_prefix = legacy.name + ".bak."
    if _legacy_prod_index_ready():
        for ancestor in (resolved, *resolved.parents):
            if ancestor.parent == legacy.parent and ancestor.name.startswith(legacy_bak_prefix):
                if resolved == ancestor or _path_under_or_equal(resolved, ancestor):
                    return resolved

    raise ValueError(
        f"vector index prod write forbidden outside INDEX_DIR: {resolved}"
    )
