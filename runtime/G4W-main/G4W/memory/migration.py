import json
import re
import shutil
import time
from pathlib import Path

from ..core.storage import safe_segment


LAYOUT_VERSION = 3


def archive_obsolete_state(state_dir: Path, archive_root: Path) -> dict:
    """Move obsolete Portable artifacts outside G4W-data without deleting them."""
    state_dir = Path(state_dir).resolve()
    archive_root = Path(archive_root).resolve()
    if state_dir == archive_root or state_dir in archive_root.parents:
        raise RuntimeError("archive root must be outside G4W-data")
    candidates = [
        state_dir / "test-temp",
        state_dir / "legacy-import",
        state_dir / "conductor-outputs",
        state_dir / "xiaoyi-jobs",
        state_dir / "workers",
        state_dir / "memory" / "sop",
    ]
    conversation_root = state_dir / "memory" / "conversations"
    flat_files = []
    if conversation_root.is_dir():
        for conversation in conversation_root.iterdir():
            if not conversation.is_dir():
                continue
            flat_files.extend(conversation.glob("conductor-history*.json"))
            for path in (conversation / "prompt-inputs", conversation / "runtime" / "model_responses"):
                if path.exists():
                    candidates.append(path)
    existing = [path for path in candidates if path.exists()] + [path for path in flat_files if path.exists()]
    if not existing:
        return {"ok": True, "moved": [], "archive": ""}
    batch = archive_root / f"portable-state-cleanup-{time.strftime('%Y%m%d-%H%M%S')}"
    moved = []
    for source in existing:
        try:
            relative = source.relative_to(state_dir)
        except ValueError:
            continue
        destination = batch / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            destination = destination.with_name(destination.name + f"-{time.time_ns()}")
        shutil.move(str(source), str(destination))
        moved.append({"source": str(source), "destination": str(destination)})
    batch.mkdir(parents=True, exist_ok=True)
    report = {"ok": True, "stateDir": str(state_dir), "archive": str(batch), "moved": moved, "completedAt": time.time()}
    (batch / "cleanup-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def _merge_tree(source: Path, destination: Path, copied: list[str], *, overwrite: bool = False) -> None:
    if not source.exists():
        return
    if source.is_file():
        if overwrite or not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied.append(str(destination))
        return
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif overwrite or not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            copied.append(str(target))


def _archive(source: Path, archive_root: Path, archived: list[str]) -> None:
    if not source.exists():
        return
    destination = archive_root / source.name
    if destination.exists():
        destination = archive_root / f"{source.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))
    archived.append(str(destination))


def migrate_portable_wechat_layout(state_dir: Path) -> dict:
    """Migrate all Portable layouts to the single G4W-data/memory root."""
    state_dir = Path(state_dir).resolve()
    target = state_dir / "memory"
    marker = target / ".layout-version.json"
    try:
        current = json.loads(marker.read_text(encoding="utf-8"))
    except Exception:
        current = {}
    if int(current.get("version", 0) or 0) >= LAYOUT_VERSION:
        for folder in ("conversations", "users", "operational", "persona"):
            (target / folder).mkdir(parents=True, exist_ok=True)
        return {"ok": True, "version": LAYOUT_VERSION, "alreadyMigrated": True, "target": str(target)}

    target.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    archived: list[str] = []
    authoritative = state_dir / "wechat-memory"

    # Existing memory is adopted in place. The former wechat-memory tree is
    # authoritative and overwrites collisions; the older conversations tree
    # only fills gaps.
    _merge_tree(authoritative, target, copied, overwrite=True)
    _merge_tree(state_dir / "conversations", target / "conversations", copied, overwrite=False)

    legacy_import = state_dir / "legacy-import" / "wechat-memory" / "conversations"
    for transcript in (target / "conversations").glob("*/transcript.md"):
        try:
            header = transcript.read_text(encoding="utf-8", errors="replace")[:500]
        except Exception:
            continue
        match = re.search(r"^legacy-sender:\s*(.+)$", header, re.MULTILINE)
        if not match:
            continue
        old_conversation = legacy_import / safe_segment(match.group(1).strip())
        for folder in ("transcripts", "assistant-replies", "summaries"):
            _merge_tree(old_conversation / folder, transcript.parent / folder, copied, overwrite=False)

    root_persona = state_dir / "weixin-instructions.md"
    root_operations = state_dir / "weixin-operations.md"
    _merge_tree(root_persona, target / "persona" / "weixin-instructions.md", copied, overwrite=False)
    # SOPs are package-owned from layout v3 onward. Legacy writable SOP files
    # are archived by archive_obsolete_state instead of being reactivated.

    for folder in ("conversations", "users", "operational", "persona"):
        (target / folder).mkdir(parents=True, exist_ok=True)

    archive_root = state_dir / "legacy-import" / "portable-layout"
    _archive(authoritative, archive_root, archived)
    _archive(state_dir / "conversations", archive_root, archived)
    _archive(root_persona, archive_root, archived)
    _archive(root_operations, archive_root, archived)

    report = {
        "ok": True,
        "version": LAYOUT_VERSION,
        "target": str(target),
        "authoritativeSource": str(authoritative),
        "copiedCount": len(copied),
        "copiedSample": copied[:50],
        "archived": archived,
        "completedAt": time.time(),
    }
    archive_root.mkdir(parents=True, exist_ok=True)
    (archive_root / "migration-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    marker.write_text(json.dumps({"version": LAYOUT_VERSION, "completedAt": report["completedAt"]}, indent=2) + "\n", encoding="utf-8")
    return report


def migrate_legacy(source: Path, state_dir: Path) -> dict:
    source = Path(source).resolve()
    state_dir = Path(state_dir).resolve()
    if not source.is_dir():
        raise FileNotFoundError(source)
    legacy = state_dir / "legacy-import"
    legacy.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in ("wechat-memory", "memory", "diary", "timeline", "genericagent-sessions", "inbox"):
        src = source / name
        dst = legacy / name
        if not src.exists():
            continue
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        copied.append(name)
    for name in ("weixin-instructions.md", "weixin-operations.md", "checkin-config.json", "turn-progress-config.json", "todo-state.json", "reminder-history.json"):
        src = source / name
        if src.is_file():
            shutil.copy2(src, legacy / name)
            copied.append(name)
    return {"ok": True, "source": str(source), "legacyDir": str(legacy), "copied": copied}


def bind_legacy_user(state_dir: Path, new_sender_id: str, legacy_sender_id: str) -> dict:
    state_dir = Path(state_dir).resolve()
    legacy = state_dir / "legacy-import" / "wechat-memory"
    if not legacy.exists():
        raise FileNotFoundError("Run migrate before bind-legacy")
    target_memory = state_dir / "memory"
    new_seg = safe_segment(new_sender_id)
    old_seg = safe_segment(legacy_sender_id)
    copied = []
    for folder in ("users", "operational"):
        src = legacy / folder / f"{old_seg}.md"
        dst = target_memory / folder / f"{new_seg}.md"
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            legacy_text = src.read_text(encoding="utf-8", errors="replace").strip()
            current_text = dst.read_text(encoding="utf-8", errors="replace").strip() if dst.exists() else ""
            merged = legacy_text if not current_text or current_text in legacy_text else legacy_text + "\n\n## New G4W memory\n\n" + current_text
            dst.write_text(merged.strip() + "\n", encoding="utf-8")
            copied.append(str(dst))
    transcript_root = legacy / "conversations" / old_seg / "transcripts"
    conversation_target = target_memory / "conversations" / new_seg
    transcript_target = conversation_target / "transcript.md"
    pattern = re.compile(r"^\[[^\]]+\] (?:User|Assistant):\n[\s\S]*?(?=^\[[^\]]+\] (?:User|Assistant):\n|\Z)", re.MULTILINE)
    blocks = []
    if transcript_root.exists():
        for path in sorted(transcript_root.rglob("*.md")):
            blocks.extend(match.group(0).strip() for match in pattern.finditer(path.read_text(encoding="utf-8", errors="replace")))
    if blocks:
        transcript_target.parent.mkdir(parents=True, exist_ok=True)
        current_blocks = []
        if transcript_target.exists():
            current_blocks = [match.group(0).strip() for match in pattern.finditer(transcript_target.read_text(encoding="utf-8", errors="replace"))]
        merged_blocks, seen = [], set()
        for block in blocks + current_blocks:
            if block not in seen:
                seen.add(block); merged_blocks.append(block)
        transcript_target.write_text(
            f"# G4W Transcript\nsender: {new_sender_id}\nlegacy-sender: {legacy_sender_id}\n\n" + "\n\n".join(merged_blocks) + "\n",
            encoding="utf-8",
        )
        copied.append(str(transcript_target))
    old_conversation = legacy / "conversations" / old_seg
    for folder in ("transcripts", "assistant-replies", "summaries"):
        src = old_conversation / folder
        dst = conversation_target / folder
        if src.exists():
            shutil.copytree(src, dst, dirs_exist_ok=True)
            copied.append(str(dst))
    return {"ok": True, "newSenderId": new_sender_id, "legacySenderId": legacy_sender_id, "copied": copied}
