import os
import shutil
import threading
from pathlib import Path


VALID_GENDERS = {"male", "female", "neutral"}


def user_pronoun(gender: str) -> str:
    normalized = str(gender or "").strip().lower()
    if normalized == "male":
        return "他"
    if normalized == "female":
        return "她"
    return "ta"


def render_instruction_template(text: str, *, user_name: str, user_identity: str = "", user_gender: str, bot_name: str) -> str:
    resolved_name = str(user_name or "用户")
    resolved_identity = str(user_identity or user_name or "用户")
    resolved_bot = str(bot_name or "G4W")
    return (
        str(text or "")
        .replace("{{USER_NAME}}", resolved_name)
        .replace("{{USER_IDENTITY}}", resolved_identity)
        .replace("{{BOT_NAME}}", resolved_bot)
        .replace("{{USER_PRONOUN}}", user_pronoun(user_gender))
    )


def _copy_if_missing(source: Path, destination: Path) -> bool:
    if destination.exists():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return True


class InstructionManager:
    """Own the packaged-template -> writable-data instruction lifecycle."""

    def __init__(self, config):
        self.config = config
        self.lock = threading.RLock()
        self.cache: dict[tuple, str] = {}
        self.ensure_runtime_files()

    def ensure_runtime_files(self) -> dict:
        created = []
        pairs = [
            (self.config.persona_template_file, self.config.persona_file),
        ]
        operations_source = getattr(self.config, "operations_template_file", None)
        operations_destination = getattr(self.config, "operations_file", None)
        if operations_source is not None and operations_destination is not None and Path(operations_source) != Path(operations_destination):
            pairs.append((Path(operations_source), Path(operations_destination)))
        for source, destination in pairs:
            if not source.is_file():
                raise FileNotFoundError(source)
            if _copy_if_missing(source, destination):
                created.append(str(destination))
        return {"created": created}

    def clear(self) -> None:
        with self.lock:
            self.cache.clear()

    def _load(self, path: Path, *, user_name: str, user_identity: str, user_gender: str, bot_name: str) -> str:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size, user_name, user_identity, user_gender, bot_name)
        with self.lock:
            cached = self.cache.get(key)
            if cached is not None:
                return cached
            raw = path.read_text(encoding="utf-8-sig")
            rendered = render_instruction_template(
                raw, user_name=user_name, user_identity=user_identity, user_gender=user_gender, bot_name=bot_name,
            ).strip()
            self.cache = {key: rendered}
            return rendered

    def load(self, *, user_name: str, user_identity: str = "", user_gender: str, bot_name: str) -> tuple[str, str]:
        self.ensure_runtime_files()
        if not self.config.operations_file.is_file():
            raise FileNotFoundError(self.config.operations_file)
        return (
            self._load(self.config.persona_file, user_name=user_name, user_identity=user_identity, user_gender=user_gender, bot_name=bot_name),
            self._load(self.config.operations_file, user_name=user_name, user_identity=user_identity, user_gender=user_gender, bot_name=bot_name),
        )


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """Update selected package-local ENV values without discarding comments."""
    path = Path(path)
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        lines = []
    pending = {str(key): str(value) for key, value in updates.items()}
    rendered = []
    for line in lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped and not stripped.startswith("#") else ""
        if key in pending:
            rendered.append(f"{key}={pending.pop(key)}")
        else:
            rendered.append(line)
    if pending:
        if rendered and rendered[-1].strip():
            rendered.append("")
        rendered.extend(f"{key}={value}" for key, value in pending.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(rendered).rstrip() + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, path)
