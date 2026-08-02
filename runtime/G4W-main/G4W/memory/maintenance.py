import json
import re
import time
from pathlib import Path

from . import l4_safe
from ..core.storage import JsonStore


class L4MaintenanceService:
    """Original G4W deterministic L4 prepare/subagent/finalize coordinator."""

    def __init__(
        self,
        state_dir: Path,
        min_new_user_messages: int = 30,
        min_new_transcript_files: int = 2,
        cooldown_seconds: int = 14400,
        sample_rate: float = 0.2,
    ):
        self.state_dir = Path(state_dir).resolve()
        self.workspace_root = self.state_dir.parent
        self.min_new_user_messages = max(1, int(min_new_user_messages))
        self.min_new_transcript_files = max(1, int(min_new_transcript_files))
        self.cooldown_hours = max(0.0, float(cooldown_seconds) / 3600.0)
        self.sample_rate = max(0.0, min(1.0, float(sample_rate)))
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

    def status(self, sender_id: str) -> dict:
        self._ensure_layout(sender_id)
        result = l4_safe.status(self.workspace_root, sender_id)
        coordinator = self.state.read().get("senders", {}).get(sender_id, {})
        return {**result, "coordinator": coordinator}

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
            return self.status(sender_id)
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
        result = l4_safe.auto_check(
            self.workspace_root, sender_id, self.min_new_user_messages,
            self.cooldown_hours, self.sample_rate,
            min_transcript_files=self.min_new_transcript_files,
        )
        if result.get("status") == "prepared":
            return self._spawn(binding_key, sender_id, result, "auto", automatic=True)
        return result

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
