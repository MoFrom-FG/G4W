import os
import shutil
from dataclasses import dataclass
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parents[1]
MAIN_DIR = PACKAGE_DIR.parent
RUNTIME_DIR = MAIN_DIR.parent
GA_APP_DIR = RUNTIME_DIR / "app"


def _env(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default) or "").strip()


def _read_env_file(path: Path) -> dict[str, str]:
    values = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except Exception:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value[:1] == value[-1:] and value[:1] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _bool_value(value: str, default: bool = False) -> bool:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return bool(default)
    return normalized not in ("0", "false", "off", "no", "disable", "disabled")


def _expand_workspace_refs(value: str, workspace_root: Path | None = None) -> str:
    """Expand portable path references used inside the package .env file."""
    root = str((workspace_root or RUNTIME_DIR.parent).resolve())
    return (
        str(value or "")
        .replace("${G4W_WORKSPACE_ROOT}", root)
        .replace("%G4W_WORKSPACE_ROOT%", root)
    )


def _resolve_portable_path(value: str, default: Path, base: Path | None = None) -> Path:
    raw = _expand_workspace_refs(str(value or "").strip(), base)
    if not raw:
        return Path(default).resolve()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (base or RUNTIME_DIR.parent) / path
    return path.resolve()


def _config_values(state_dir: Path) -> dict[str, str]:
    # Portable G4W must never inherit another installation's .env from
    # an ancestor directory.  Its package-local file is the single source of
    # deployment configuration; runtime paths remain location-derived.
    return _read_env_file(MAIN_DIR / ".env")


@dataclass(frozen=True)
class Config:
    state_dir: Path
    env_path: Path | None = None
    shared_memory_root: Path | None = None
    workspace_root: Path = RUNTIME_DIR.parent
    model_no: int = 0
    conductor_model: str = "deepseek-v4-flash"
    worker_model: str = "deepseek-v4-flash"
    pro_model: str = "deepseek-v4-pro"
    user_name: str = ""
    user_identity: str = ""
    user_gender: str = "neutral"
    bot_name: str = ""
    account_id: str = ""
    weixin_base_url: str = "https://ilinkai.weixin.qq.com"
    weixin_cdn_base_url: str = "https://novac2c.cdn.weixin.qq.com/c2c"
    bot_type: str = "3"
    poll_timeout_seconds: int = 10
    recent_pairs: int = 20
    recent_transcript_max_chars: int = 12000
    long_assistant_reply_chars: int = 300
    long_user_prompt_chars: int = 1500
    conductor_history_max_messages: int = 48
    conductor_history_max_chars: int = 60000
    conductor_max_turns: int = 180
    worker_timeout_seconds: int = 1200
    worker_stall_seconds: int = 600
    xiaoyi_bridge_url: str = "http://127.0.0.1:21991"
    location_enabled: bool = False
    location_host: str = "127.0.0.1"
    location_port: int = 4318
    location_token: str = ""
    location_history_limit: int = 1000
    location_major_move_meters: int = 1000
    location_known_places: tuple = ()
    l4_min_new_user_turns: int = 30
    l4_min_new_transcript_files: int = 2
    l4_cooldown_seconds: int = 14400
    l4_sample_rate: float = 0.2
    dida_command: str = ""
    supervision_default_delay_minutes: int = 15
    supervision_default_focus_minutes: int = 25
    checkin_minimum_minutes: int = 10
    checkin_maximum_minutes: int = 90
    checkin_enabled: bool = True
    timeline_locale: str = "zh-CN"
    timeline_theme: str = "default"
    short_path_dual_write: bool = False
    # F1 read path: "legacy" (default, aggregate transcript.md), "daily_primary"
    # (concat daily files under transcripts/), or "mirror_primary" (stub → legacy).
    # Production must stay on legacy; daily_primary is shadow/compare-only.
    f1_read_path: str = "legacy"
    # S3: when True, ConversationStore.append skips aggregate transcript.md
    # (daily continues; dual-write short-path unchanged).
    stop_aggregate_write: bool = False

    @classmethod
    def load(cls) -> "Config":
        file_values = _config_values(RUNTIME_DIR / "G4W-data")
        def value(name: str, default: str = "") -> str:
            return _env(name, file_values.get(name, default))

        workspace_root = _resolve_portable_path(
            value("G4W_WORKSPACE_ROOT", ""),
            RUNTIME_DIR.parent,
        )
        default_state = workspace_root / "runtime" / "G4W-data"
        state_dir = _resolve_portable_path(value("G4W_STATE_DIR", ""), default_state, workspace_root)
        try:
            known_places = tuple(__import__("json").loads(value("G4W_LOCATION_KNOWN_PLACES", "[]")))
        except Exception:
            known_places = ()
        checkin_minimum_ms = max(60_000, int(value("G4W_CHECKIN_MIN_INTERVAL_MS", "600000") or 600000))
        checkin_maximum_ms = max(
            checkin_minimum_ms,
            int(value("G4W_CHECKIN_MAX_INTERVAL_MS", "5400000") or 5400000),
        )
        f1_raw = (value("G4W_F1_READ_PATH", "legacy") or "legacy").strip().lower()
        if f1_raw in ("daily_primary", "daily", "daily-primary"):
            f1_read_path = "daily_primary"
        elif f1_raw in ("mirror_primary", "mirror", "mirror-primary"):
            # Stub: accepted as config value; ConversationStore falls back to legacy.
            f1_read_path = "mirror_primary"
        else:
            f1_read_path = "legacy"
        return cls(
            state_dir=state_dir,
            shared_memory_root=_resolve_portable_path(
                value("G4W_SHARED_MEMORY_ROOT", ""),
                workspace_root / "runtime" / "G4W-main" / "G4W" / "memory" / "sop",
                workspace_root,
            ),
            workspace_root=workspace_root,
            model_no=int(value("G4W_LLM_NO", "0") or 0),
            conductor_model=value("G4W_CONDUCTOR_MODEL", "deepseek-v4-flash"),
            worker_model=value("G4W_WORKER_MODEL", "deepseek-v4-flash"),
            pro_model=value("G4W_PRO_MODEL", "deepseek-v4-pro"),
            user_name=value("G4W_USER_NAME", ""),
            user_identity=value("G4W_USER_IDENTITY", ""),
            user_gender=value("G4W_USER_GENDER", "neutral") or "neutral",
            bot_name=value("G4W_BOT_NAME", ""),
            account_id=value("G4W_ACCOUNT_ID"),
            weixin_base_url=value("G4W_WEIXIN_BASE_URL", "https://ilinkai.weixin.qq.com"),
            weixin_cdn_base_url=value("G4W_WEIXIN_CDN_BASE_URL", "https://novac2c.cdn.weixin.qq.com/c2c"),
            bot_type=value("G4W_WEIXIN_QR_BOT_TYPE", "3"),
            poll_timeout_seconds=max(2, int(value("G4W_POLL_TIMEOUT_SECONDS", "10") or 10)),
            recent_pairs=max(1, int(value("G4W_RECENT_PAIRS", "20") or 20)),
            recent_transcript_max_chars=max(2000, int(value("G4W_RECENT_TRANSCRIPT_MAX_CHARS", "12000") or 12000)),
            long_assistant_reply_chars=max(0, int(value("G4W_LONG_ASSISTANT_REPLY_CHARS", "300") or 300)),
            long_user_prompt_chars=max(500, int(value("G4W_LONG_USER_PROMPT_CHARS", "1500") or 1500)),
            conductor_history_max_messages=max(16, int(value("G4W_HISTORY_MAX_MESSAGES", "48") or 48)),
            conductor_history_max_chars=max(20000, int(value("G4W_HISTORY_MAX_CHARS", "60000") or 60000)),
            conductor_max_turns=max(3, min(300, int(value("G4W_CONDUCTOR_MAX_TURNS", "180") or 180))),
            worker_timeout_seconds=max(60, int(value("G4W_WORKER_TIMEOUT_SECONDS", "1200") or 1200)),
            worker_stall_seconds=max(60, int(value("G4W_WORKER_STALL_SECONDS", "600") or 600)),
            xiaoyi_bridge_url=value("G4W_XIAOYI_BRIDGE_URL", "http://127.0.0.1:21991"),
            location_enabled=value("G4W_ENABLE_LOCATION_SERVER", "").lower() in ("1", "true", "yes", "on"),
            location_host=value("G4W_LOCATION_HOST", "127.0.0.1"),
            location_port=int(value("G4W_LOCATION_PORT", "4318") or 4318),
            location_token=value("G4W_LOCATION_TOKEN"),
            location_history_limit=max(10, int(value("G4W_LOCATION_HISTORY_LIMIT", "1000") or 1000)),
            location_major_move_meters=max(50, int(value("G4W_LOCATION_MAJOR_MOVE_THRESHOLD_METERS", "1000") or 1000)),
            location_known_places=known_places,
            l4_min_new_user_turns=max(5, int(value("G4W_L4_MIN_NEW_USER_MESSAGES", value("G4W_L4_MIN_NEW_USER_TURNS", "30")) or 30)),
            l4_min_new_transcript_files=max(1, int(value("G4W_L4_MIN_NEW_TRANSCRIPT_FILES", "2") or 2)),
            l4_cooldown_seconds=max(60, int(float(value("G4W_L4_COOLDOWN_HOURS", "4") or 4) * 3600)),
            l4_sample_rate=max(0.0, min(1.0, float(value("G4W_L4_SAMPLE_RATE", "0.2") or 0.2))),
            dida_command=value("G4W_DIDA_COMMAND", ""),
            supervision_default_delay_minutes=max(1, int(value("G4W_SUPERVISION_DEFAULT_DELAY_MINUTES", "15") or 15)),
            supervision_default_focus_minutes=max(1, int(value("G4W_SUPERVISION_DEFAULT_FOCUS_MINUTES", "25") or 25)),
            checkin_minimum_minutes=max(1, checkin_minimum_ms // 60_000),
            checkin_maximum_minutes=max(1, checkin_maximum_ms // 60_000),
            checkin_enabled=_bool_value(value("G4W_CHECKIN_ENABLED", "1"), True),
            timeline_locale=value("G4W_TIMELINE_LOCALE", "zh-CN") or "zh-CN",
            timeline_theme=(value("G4W_TIMELINE_UI_THEME", "default") or "default").lower(),
            short_path_dual_write=_bool_value(value("G4W_SHORT_PATH_DUAL_WRITE", "0"), False),
            f1_read_path=f1_read_path,
            stop_aggregate_write=_bool_value(value("G4W_STOP_AGGREGATE_WRITE", "0"), False),
        )

    @property
    def env_file(self) -> Path:
        return Path(self.env_path) if self.env_path else MAIN_DIR / ".env"

    @property
    def accounts_dir(self) -> Path:
        return self.state_dir / "accounts"

    @property
    def conversations_dir(self) -> Path:
        return self.memory_dir / "conversations"

    @property
    def workers_dir(self) -> Path:
        """Legacy global Worker directory; new Worker data is conversation-scoped."""
        return self.state_dir / "workers"

    @property
    def worker_registry_file(self) -> Path:
        return self.memory_dir / "worker-registry.json"

    @property
    def ga_worker_memory_dir(self) -> Path:
        return self.state_dir / "ga-worker-memory"

    @property
    def memory_dir(self) -> Path:
        return self.state_dir / "memory"

    @property
    def wechat_memory_dir(self) -> Path:
        """Legacy pre-v2 memory root retained only for migration diagnostics."""
        return self.state_dir / "wechat-memory"

    @property
    def persona_dir(self) -> Path:
        return self.memory_dir / "persona"

    @property
    def sop_dir(self) -> Path:
        return Path(self.shared_memory_root or (PACKAGE_DIR / "memory" / "sop")).resolve()

    @property
    def runtime_cache_dir(self) -> Path:
        return self.state_dir / "runtime" / "cache"

    @property
    def persona_file(self) -> Path:
        return self.persona_dir / "weixin-instructions.md"

    @property
    def operations_file(self) -> Path:
        return self.sop_dir / "wechat" / "weixin-operations" / "weixin_operations_sop.md"

    @property
    def templates_dir(self) -> Path:
        return PACKAGE_DIR / "templates"

    @property
    def persona_template_file(self) -> Path:
        return self.templates_dir / "persona" / "weixin-instructions.md"

    @property
    def operations_template_file(self) -> Path:
        return self.operations_file

    @property
    def capabilities_template_file(self) -> Path:
        return self.capabilities_file

    @property
    def legacy_conversations_dir(self) -> Path:
        return self.state_dir / "conversations"

    @property
    def legacy_memory_dir(self) -> Path:
        return self.state_dir / "legacy-import" / "portable-layout" / "memory"

    @property
    def diary_dir(self) -> Path:
        return self.state_dir / "diary"

    @property
    def timeline_dir(self) -> Path:
        return self.state_dir / "timeline"

    @property
    def conductor_outputs_dir(self) -> Path:
        """Compatibility monitor root; outputs now live below conversations."""
        return self.conversations_dir

    @property
    def capabilities_file(self) -> Path:
        return self.runtime_cache_dir / "capability-registry.json"

    @property
    def xiaoyi_jobs_dir(self) -> Path:
        return self.memory_dir / "operational" / "xiaoyi-jobs"

    @property
    def external_archive_dir(self) -> Path:
        if self.state_dir.name == "G4W-data" and self.state_dir.parent.name == "runtime":
            portable_root = self.state_dir.parent.parent
            container = portable_root.parent if portable_root.parent.name == portable_root.name else portable_root
            return container / "_legacy-G4W-artifacts"
        return self.state_dir.parent / "_legacy-G4W-artifacts"

    @property
    def pid_file(self) -> Path:
        return self.state_dir / "G4W.pid"

    @property
    def stop_marker_file(self) -> Path:
        return self.state_dir / "G4W.stop-requested"

    def ensure_dirs(self) -> None:
        for path in (
            self.state_dir, self.accounts_dir, self.memory_dir, self.conversations_dir,
            self.persona_dir, self.sop_dir, self.runtime_cache_dir,
            self.timeline_dir, self.ga_worker_memory_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
