import json
import re
import time
from pathlib import Path

from . import l4_safe
from ..core.storage import JsonStore


class L4MaintenanceService:
    """Original G4W deterministic L4 prepare/subagent/finalize coordinator."""

    # Longest a spawned L4 worker is treated as "still running" without finalize.
    RUNNING_GRACE_SECONDS = 6 * 3600

    def __init__(
        self,
        state_dir: Path,
        min_new_user_messages: int = 15,
        min_new_transcript_files: int = 0,
        cooldown_seconds: int = 14400,
        sample_rate: float = 0.2,
        idle_days: int = 3,
    ):
        self.state_dir = Path(state_dir).resolve()
        self.workspace_root = self.state_dir.parent
        self.min_new_user_messages = max(1, int(min_new_user_messages))
        self.min_new_transcript_files = max(0, int(min_new_transcript_files))
        self.cooldown_hours = max(0.0, float(cooldown_seconds) / 3600.0)
        self.sample_rate = max(0.0, min(1.0, float(sample_rate)))
        self.idle_days = max(0, int(idle_days))
        self.state = JsonStore(self.state_dir / "memory" / "l4-coordinator.json", {"senders": {}, "workers": {}})
        self.workers = None
        self.events = None

    def attach_workers(self, workers) -> None:
        self.workers = workers

    def attach_events(self, events) -> None:
        self.events = events

    def _ensure_layout(self, sender_id: str) -> None:
        conversation = l4_safe.conversation_root(self.workspace_root, sender_id)
        (conversation / "transcripts").mkdir(parents=True, exist_ok=True)
        (conversation / "summaries" / "chunks").mkdir(parents=True, exist_ok=True)
        (conversation / "summaries" / "user_only").mkdir(parents=True, exist_ok=True)
        (conversation / "summaries" / "history_insight").mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _user_id(binding_key: str, sender_id: str = "") -> str:
        """Resolve the L4 user id: explicit sender wins, else strip "<account>:"."""
        if sender_id:
            return str(sender_id)
        key = str(binding_key or "")
        return key.split(":", 1)[1] if ":" in key else key

    def _running(self, sender_id: str) -> bool:
        if l4_safe.pipeline_running(self.workspace_root, sender_id):
            return True
        sender = self.state.read().get("senders", {}).get(sender_id) or {}
        if not sender.get("pending"):
            return False
        # A worker that never finalizes must not block the trigger forever.
        return (time.time() - float(sender.get("startedAt") or 0)) < self.RUNNING_GRACE_SECONDS

    def status(self, binding_key: str) -> dict:
        """Contract for the dashboard: current L4 state of one binding."""
        sender_id = self._user_id(binding_key)
        self._ensure_layout(sender_id)
        has_markers = l4_safe.markers_path(self.workspace_root, sender_id).is_file()
        markers = l4_safe.load_markers(self.workspace_root, sender_id)
        all_messages = l4_safe.collect_user_messages(self.workspace_root, sender_id)
        pending = l4_safe.pending_user_messages(markers, all_messages) if has_markers else all_messages
        last_run = l4_safe.last_run_at(markers) if has_markers else ""
        ago = l4_safe.elapsed_seconds(last_run)
        ago_seconds = int(ago) if ago is not None else None
        remaining = 0
        if ago_seconds is not None:
            remaining = max(0, int(self.cooldown_hours * 3600.0 - ago_seconds))
        return {
            "hasMarkers": has_markers,
            # Kept for callers written against the pre-status() shape (/l4compress, older tests).
            "marker_exists": has_markers,
            "bootstrapped": bool(has_markers and markers.get("last_processed_timestamp")),
            "lastRunAt": last_run,
            "lastRunAgoSeconds": ago_seconds,
            "pendingMessages": len(pending),
            "pendingFiles": len(l4_safe.pending_transcript_files(pending)),
            "cooldownRemainingSeconds": remaining,
            "running": self._running(sender_id),
            "threshold": {"minMessages": self.min_new_user_messages, "idleDays": self.idle_days},
        }

    def trigger(self, binding_key: str, sender_id: str = "") -> dict:
        """Force one run, ignoring threshold/cooldown/sampling (bootstraps if needed)."""
        user_id = self._user_id(binding_key, sender_id)
        self._ensure_layout(user_id)
        has_markers = l4_safe.markers_path(self.workspace_root, user_id).is_file()
        if self._running(user_id):
            return {"started": False, "reason": "already running", "mode": "deep" if has_markers else "bootstrap"}
        all_messages = l4_safe.collect_user_messages(self.workspace_root, user_id)
        if not all_messages:
            return {"started": False, "reason": "no user messages", "mode": "deep" if has_markers else "bootstrap"}
        if has_markers:
            mode = "deep"
            label = "manual deep"
            reason = f"manual trigger: deep over {len(all_messages)} transcript user messages"
            prepared = l4_safe.prepare_run(self.workspace_root, user_id, trigger_mode="deep", dry_run=False)
        else:
            start, end = l4_safe.bootstrap_window(all_messages)
            mode = "bootstrap"
            label = f"bootstrap {start}..{end}"
            reason = f"manual trigger: bootstrap {start}..{end}"
            prepared = l4_safe.prepare_run(
                self.workspace_root, user_id, trigger_mode="bootstrap", start=start, end=end, dry_run=False,
            )
        if prepared.get("status") != "prepared":
            detail = str(prepared.get("reason") or prepared.get("status") or "not prepared")
            return {"started": False, "reason": detail, "mode": mode}
        if mode == "bootstrap":
            # Same baseline the auto bootstrap writes: markers exist, nothing consumed yet.
            l4_safe.save_initial_markers(self.workspace_root, user_id)
        spawned = self._spawn(binding_key or user_id, user_id, prepared, label, automatic=False)
        return {"started": bool(spawned.get("l4Started")), "reason": reason, "mode": mode}

    @staticmethod
    def parse_command(args: str) -> dict | None:
        normalized = str(args or "").strip()
        if not normalized:
            return {"mode": "prepare", "trigger": "deep", "label": "增量深压", "pipeline": True}
        tokens = normalized.split()
        subcommand = tokens[0].lower()
        if subcommand == "status":
            return {"mode": "status", "label": "status", "pipeline": False}
        if subcommand == "dryrun":
            return {"mode": "dryrun", "trigger": "deep", "label": "dryrun", "pipeline": False}
        if subcommand != "bootstrap":
            return None
        def flag(*names):
            for name in names:
                if name in tokens:
                    index = tokens.index(name)
                    return tokens[index + 1] if index + 1 < len(tokens) else ""
            return ""
        start = flag("--from", "--from-date")
        end = flag("--to", "--to-date")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end):
            return None
        return {"mode": "prepare", "trigger": "bootstrap", "label": f"bootstrap {start}..{end}", "start": start, "end": end, "pipeline": True}

    def command(self, binding_key: str, sender_id: str, args: str = "") -> dict:
        parsed = self.parse_command(args)
        if not parsed:
            return {"status": "usage", "message": "用法：/l4compress、/l4compress dryrun、/l4compress status、/l4compress bootstrap --from YYYY-MM-DD --to YYYY-MM-DD"}
        self._ensure_layout(sender_id)
        if parsed["mode"] == "status":
            coordinator = self.state.read().get("senders", {}).get(sender_id, {})
            return {**self.status(sender_id), "coordinator": coordinator}
        if parsed["mode"] == "dryrun":
            return l4_safe.prepare_run(self.workspace_root, sender_id, trigger_mode="deep", dry_run=True)
        prepared = l4_safe.prepare_run(
            self.workspace_root, sender_id, trigger_mode=parsed["trigger"],
            start=parsed.get("start", ""), end=parsed.get("end", ""), dry_run=False,
        )
        if prepared.get("status") != "prepared":
            return prepared
        return self._spawn(binding_key, sender_id, prepared, parsed["label"], automatic=False)

    def request(self, binding_key: str, sender_id: str, trigger: str = "manual") -> dict:
        return self.command(binding_key, sender_id, "")

    def auto_check(self, binding_key: str, sender_id: str) -> dict:
        self._ensure_layout(sender_id)
        if self._running(sender_id):
            return {"status": "skipped", "reason": "already running", "mode": "none", "l4Started": False}
        result = l4_safe.auto_check(
            self.workspace_root, sender_id, self.min_new_user_messages,
            self.cooldown_hours, self.sample_rate,
            min_transcript_files=self.min_new_transcript_files,
            idle_days=self.idle_days,
        )
        mode = str(result.get("mode") or "none")
        reason = str(result.get("reason") or "")
        if result.get("status") == "prepared":
            label = "auto bootstrap" if mode == "bootstrap" else "auto deep"
            spawned = self._spawn(binding_key, sender_id, result, label, automatic=True)
            return {**spawned, "mode": mode, "reason": reason, "l4Started": True}
        return {**result, "mode": mode, "reason": reason, "l4Started": False}

    def _spawn(self, binding_key: str, sender_id: str, prepared: dict, label: str, automatic: bool) -> dict:
        if self.workers is None:
            raise RuntimeError("L4 Worker manager is unavailable")
        run_id = prepared["run_id"]
        manifest = prepared["run_manifest"]
        output_dir = prepared["allowed_output_dir"]
        sop = Path(__file__).resolve().parent / "sop" / "core" / "l4" / "l4_semantic_mining_sop.md"
        task = "\n".join([
            f"L4语义记忆整理｜{label}",
            "G4W L4 semantic mining job.",
            f"label: {label}", f"run_id: {run_id}", f"manifest: {manifest}",
            f"allowed_output_dir: {output_dir}", f"SOP: {sop}", "",
            "Read the SOP and run_manifest. Read only manifest-listed inputs.",
            "Write every required candidate file into allowed_output_dir.",
            "Do not modify transcripts, assistant-replies, users, operational, or GA global memory.",
            "Return <worker_result> with status=completed only after all candidate files exist.",
        ])
        worker = self.workers.spawn(binding_key, sender_id, "worker.l4", task, "persistent", model_tier="flash")
        def update(state):
            state.setdefault("senders", {})[sender_id] = {
                "pending": True, "runId": run_id, "workerId": worker["id"],
                "automatic": automatic, "label": label, "startedAt": time.time(),
            }
            state.setdefault("workers", {})[worker["id"]] = {"senderId": sender_id, "runId": run_id}
        self.state.update(update)
        if automatic and self.events is not None:
            self.events.enqueue(
                "memory.l4_started", binding_key,
                {"senderId": sender_id, "runId": run_id, "workerId": worker["id"], "label": label},
                dedupe_key=f"memory.l4_started:{sender_id}:{run_id}",
            )
        return {**prepared, "workerId": worker["id"], "automatic": automatic, "l4Started": True}

    def finalize_worker(self, worker_id: str) -> dict:
        mapping = self.state.read().get("workers", {}).get(worker_id)
        if not mapping:
            return {"status": "not_l4_worker"}
        sender_id, run_id = mapping["senderId"], mapping["runId"]
        result = l4_safe.validate_finalize(self.workspace_root, sender_id, run_id, dry_run=False)
        completed = result.get("status") in ("finalized", "already_finalized")
        def update(state):
            sender = state.setdefault("senders", {}).setdefault(sender_id, {})
            sender.update({"pending": not completed, "result": result})
            if completed:
                sender["completedAt"] = time.time()
                state.setdefault("workers", {}).pop(worker_id, None)
            else:
                sender["lastValidationAt"] = time.time()
        self.state.update(update)
        return result
