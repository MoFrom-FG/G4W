import json
import os
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..core.storage import JsonStore, safe_segment


INLINE_COMPLETION_RESULT_CHARS = 4000
SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")


def readable_topic(task: str, capability_id: str = "") -> str:
    """Return a short Windows-safe CJK-friendly Worker topic."""
    value = re.sub(r"<[^>]+>|[`*_#]+", " ", str(task or ""))
    value = re.sub(r"[A-Za-z]:\\[^\s\r\n]+", lambda match: Path(match.group(0).strip()).name or "文件", value)
    value = re.sub(r"[\r\n\t]+", " ", value)
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .-_，。；;：:")
    labels = {
        "worker.research": "资料研究",
        "worker.general": "通用任务",
        "worker.supervisor": "自制力监督",
        "worker.l4": "L4语义记忆整理",
    }
    label = labels.get(str(capability_id or ""), "Worker")
    if str(capability_id or "") == "worker.l4":
        return label
    if not value:
        return label
    if len(value) > 24:
        value = value[:24].rstrip(" .-_，。；;：:")
    return f"{label}-{value}" if value and value != label else label


def worker_folder_name(topic: str, worker_id: str) -> str:
    suffix = str(worker_id or "worker").replace("worker-", "")[-10:]
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", " ", str(topic or "Worker"))
    value = re.sub(r"\s+", " ", value).strip(" .-_") or "Worker"
    return f"{value}--{suffix}"


class WorkerManager:
    def __init__(
        self,
        root: Path,
        registry,
        event_store,
        timeout_seconds: int = 1200,
        default_model: str = "deepseek-v4-flash",
        pro_model: str = "deepseek-v4-pro",
        *,
        conversations_root: Path | None = None,
        state_path: Path | None = None,
        ga_memory_root: Path | None = None,
    ):
        self.root = Path(root)
        self.conversations_root = Path(conversations_root).resolve() if conversations_root else None
        self.ga_memory_root = Path(ga_memory_root).resolve() if ga_memory_root else None
        self.registry = registry
        self.event_store = event_store
        self.timeout_seconds = timeout_seconds
        self.default_model = default_model
        self.pro_model = pro_model
        resolved_state = Path(state_path).resolve() if state_path else self.root / "registry.json"
        legacy_state = self.root / "registry.json"
        if state_path and not resolved_state.exists() and legacy_state.is_file():
            resolved_state.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy_state, resolved_state)
        self.state = JsonStore(resolved_state, {"workers": {}})
        self.processes: dict[str, subprocess.Popen] = {}
        self.lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)
        if self.ga_memory_root:
            self._ensure_ga_memory_overlay()
        if self.conversations_root:
            self._migrate_legacy_worker_dirs()
            self._normalize_worker_dirs()
            self._migrate_flat_worker_runs()
            self._consolidate_persistent_workers()
            self._archive_finished_ephemeral_workers()
        self._recover_running_records()

    def _ensure_ga_memory_overlay(self) -> None:
        self.ga_memory_root.mkdir(parents=True, exist_ok=True)
        defaults = {
            "global_mem.txt": "# G4W Worker GA facts\n\n",
            "global_mem_insight.txt": "# G4W Worker GA memory index\n\n",
        }
        for name, content in defaults.items():
            path = self.ga_memory_root / name
            if not path.exists():
                path.write_text(content, encoding="utf-8")
        (self.ga_memory_root / "sop").mkdir(parents=True, exist_ok=True)
        (self.ga_memory_root / "L4_raw_sessions").mkdir(parents=True, exist_ok=True)

    def _conversation_workers_root(self, sender_id: str) -> Path:
        if self.conversations_root is None:
            return self.root
        return self.conversations_root / safe_segment(sender_id) / "workers"

    def _new_worker_dir(self, sender_id: str, topic: str, worker_id: str, created_at: float | None = None) -> Path:
        when = datetime.fromtimestamp(float(created_at or time.time()), SHANGHAI)
        return self._conversation_workers_root(sender_id) / when.strftime("%Y") / when.strftime("%m") / worker_folder_name(topic, worker_id)

    def _migrate_legacy_worker_dirs(self) -> None:
        if not (self.root / "registry.json").exists() and not self.root.exists():
            return
        changed = False
        state = self.state.read()
        for item in state.get("workers", {}).values():
            old_dir_value = str(item.get("dir") or "").strip()
            if not old_dir_value:
                continue
            old_dir = Path(old_dir_value)
            if not old_dir.exists() or self.conversations_root in old_dir.parents:
                continue
            topic = str(item.get("topic") or readable_topic(item.get("task", ""), item.get("capabilityId", "")))
            destination = self._new_worker_dir(item.get("senderId", ""), topic, item.get("id", ""), item.get("createdAt"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                destination = destination.with_name(destination.name + f"-{int(time.time())}")
            shutil.move(str(old_dir), str(destination))
            item.update({"dir": str(destination), "topic": topic, "migratedAt": time.time()})
            changed = True
        if changed:
            self.state.write(state)

    def _normalize_worker_dirs(self) -> None:
        state = self.state.read()
        changed = False
        for item in state.get("workers", {}).values():
            source_value = str(item.get("dir") or "").strip()
            if not source_value:
                continue
            source = Path(source_value)
            if not source.is_dir() or self.conversations_root not in source.parents:
                continue
            topic = readable_topic(item.get("task", ""), item.get("capabilityId", ""))
            expected_name = worker_folder_name(topic, item.get("id", ""))
            if source.name != expected_name:
                destination = source.with_name(expected_name)
                if destination.exists() and destination != source:
                    destination = destination.with_name(destination.name + f"-{int(time.time())}")
                source.rename(destination)
                item["dir"] = str(destination)
            item["topic"] = topic
            changed = True
        if changed:
            self.state.write(state)

    def _migrate_flat_worker_runs(self) -> None:
        """Move pre-layout Worker artifacts into dated run directories.

        Long-term Workers keep one shared history file at their readable Worker
        root.  Every historical execution becomes a self-contained run with a
        job, result, progress snapshot, raw model output and Markdown report.
        The migration is intentionally idempotent: after the flat job files are
        consumed, subsequent starts have nothing to do.
        """
        from .worker_runner import render_report

        state = self.state.read()
        changed = False
        for item in state.get("workers", {}).values():
            source_value = str(item.get("dir") or "").strip()
            if not source_value:
                continue
            worker_dir = Path(source_value)
            if not worker_dir.is_dir() or self.conversations_root not in worker_dir.parents:
                continue

            flat_jobs = []
            for path in worker_dir.glob("job-*.json"):
                match = re.fullmatch(r"job-(\d+)\.json", path.name)
                if match:
                    flat_jobs.append((int(match.group(1)), path))
            if not flat_jobs:
                continue
            flat_jobs.sort(key=lambda value: value[0])

            job_times = {run_index: path.stat().st_mtime for run_index, path in flat_jobs}
            response_root = worker_dir / "runtime" / "model_responses"
            response_files = list(response_root.glob("*.txt")) if response_root.is_dir() else []
            response_map: dict[int, list[Path]] = {run_index: [] for run_index, _ in flat_jobs}
            for response in response_files:
                response_time = response.stat().st_mtime
                nearest = min(job_times, key=lambda run_index: abs(response_time - job_times[run_index]))
                response_map.setdefault(nearest, []).append(response)

            latest_run_index = max(run_index for run_index, _ in flat_jobs)
            latest_paths: dict[str, str] = {}
            for run_index, old_job_file in flat_jobs:
                try:
                    job = json.loads(old_job_file.read_text(encoding="utf-8"))
                except Exception:
                    job = {"id": item.get("id", ""), "runIndex": run_index, "task": item.get("task", "")}
                when = datetime.fromtimestamp(job_times[run_index], SHANGHAI)
                run_dir = worker_dir / "runs" / when.strftime("%Y") / when.strftime("%m") / f"run-{run_index:04d}"
                run_dir.mkdir(parents=True, exist_ok=True)

                result_file = run_dir / "result.json"
                old_result_file = worker_dir / f"result-{run_index}.json"
                if old_result_file.is_file() and not result_file.exists():
                    shutil.move(str(old_result_file), str(result_file))
                try:
                    result = json.loads(result_file.read_text(encoding="utf-8")) if result_file.is_file() else {}
                except Exception:
                    result = {}

                progress_file = run_dir / "progress.json"
                old_progress_file = worker_dir / "progress.json"
                if run_index == latest_run_index and old_progress_file.is_file() and not progress_file.exists():
                    shutil.move(str(old_progress_file), str(progress_file))
                if progress_file.is_file():
                    try:
                        progress = json.loads(progress_file.read_text(encoding="utf-8"))
                    except Exception:
                        progress = {}
                else:
                    progress = {
                        "summary": str(result.get("summary") or "历史Worker运行已迁移"),
                        "turn": 0,
                        "updatedAt": result_file.stat().st_mtime if result_file.is_file() else job_times[run_index],
                    }
                    progress_file.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")

                model_output_dir = run_dir / "model-responses"
                for index, response in enumerate(sorted(response_map.get(run_index, [])), start=1):
                    model_output_dir.mkdir(parents=True, exist_ok=True)
                    target_name = "model-responses.txt" if index == 1 else f"model-responses-{index}.txt"
                    target = model_output_dir / target_name
                    if not target.exists():
                        shutil.move(str(response), str(target))

                job.update({
                    "runIndex": run_index,
                    "runDir": str(run_dir),
                    "resultFile": str(result_file),
                    "reportFile": str(run_dir / "report.md"),
                    "historyFile": str(worker_dir / "history.json"),
                    "progressFile": str(progress_file),
                    "modelEventsFile": str(run_dir / "model-events.jsonl"),
                    "topic": item.get("topic", ""),
                })
                new_job_file = run_dir / "job.json"
                new_job_file.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
                old_job_file.unlink(missing_ok=True)

                report_file = run_dir / "report.md"
                if not report_file.exists():
                    report_file.write_text(render_report(job, result, progress, []), encoding="utf-8")

                if run_index == latest_run_index:
                    latest_paths = {
                        "currentRunDir": str(run_dir),
                        "currentResultFile": str(result_file),
                        "currentReportFile": str(report_file),
                        "currentProgressFile": str(progress_file),
                    }

            for empty_candidate in (response_root, response_root.parent):
                if empty_candidate.is_dir() and not any(empty_candidate.iterdir()):
                    empty_candidate.rmdir()
            if latest_paths:
                item.update(latest_paths)
            item["flatRunsMigratedAt"] = time.time()
            changed = True

        if changed:
            self.state.write(state)

    def _recover_running_records(self):
        def apply(state):
            for item in state.get("workers", {}).values():
                if item.get("status") == "running":
                    item["status"] = "sleeping"
                    item["pid"] = 0
                    item["lastError"] = "service restarted while worker was running; state preserved for explicit resume"
                if item.get("result") and not item.get("review"):
                    item["review"] = {
                        "runIndex": int(item.get("runIndex", 0)),
                        "state": "accept",
                        "note": "Migrated result that had already been delivered before review tracking was enabled.",
                        "updatedAt": time.time(),
                    }
        self.state.update(apply)

    def spawn(self, binding_key: str, sender_id: str, capability_id: str, task: str, lifecycle: str = "", model_tier: str = "") -> dict:
        capability = self.registry.require_route(capability_id, "worker")
        resolved_lifecycle = lifecycle if lifecycle in ("ephemeral", "persistent") else capability.get("lifecycle", "ephemeral")
        if resolved_lifecycle == "persistent":
            existing = self._find_persistent(sender_id, capability_id)
            if existing:
                if existing.get("bindingKey") != binding_key:
                    self.state.update(lambda state: state["workers"][existing["id"]].update({"bindingKey": binding_key, "updatedAt": time.time()}))
                explicit_tier = str(model_tier or "").lower()
                requested_model = self.pro_model if explicit_tier == "pro" else self.default_model
                if explicit_tier in ("flash", "pro") and existing.get("modelName") != requested_model and existing.get("status") != "running":
                    def change_model(state):
                        state["workers"][existing["id"]].update({"modelTier": explicit_tier, "modelName": requested_model})
                    self.state.update(change_model)
                return self.send(existing["id"], task)
        worker_id = f"worker-{uuid.uuid4().hex[:10]}"
        topic = readable_topic(task, capability_id)
        worker_dir = self._new_worker_dir(sender_id, topic, worker_id)
        worker_dir.mkdir(parents=True, exist_ok=True)
        item = {
            "id": worker_id,
            "bindingKey": binding_key,
            "senderId": sender_id,
            "capabilityId": capability_id,
            "lifecycle": resolved_lifecycle,
            "status": "created",
            "task": task,
            "topic": topic,
            "createdAt": time.time(),
            "updatedAt": time.time(),
            "runIndex": 0,
            "dir": str(worker_dir),
            "modelTier": "pro" if str(model_tier).lower() == "pro" else "flash",
            "modelName": self.pro_model if str(model_tier).lower() == "pro" else self.default_model,
            "progressReporting": capability_id not in ("worker.l4", "worker.supervisor"),
            "lastProgressMilestone": 0,
            "gaMemoryRoot": str(self.ga_memory_root or ""),
        }
        def add(state): state.setdefault("workers", {})[worker_id] = item
        self.state.update(add)
        self._start(item, task)
        return self.public(item)

    def send(self, worker_id: str, message: str) -> dict:
        item = self.get(worker_id)
        if not item:
            raise KeyError(f"worker not found: {worker_id}")
        if item.get("status") == "running":
            worker_dir = Path(item["dir"])
            intervene = worker_dir / "_intervene"
            existing = intervene.read_text(encoding="utf-8") if intervene.exists() else ""
            intervene.write_text((existing + "\n" + str(message or "").strip()).strip() + "\n", encoding="utf-8")
            def mark(state):
                state["workers"][worker_id].update({
                    "lastControl": "intervene",
                    "lastControlAt": time.time(),
                    "updatedAt": time.time(),
                })
            self.state.update(mark)
            result = self.public(self.get(worker_id))
            result["control"] = "intervene_injected"
            return result
        if item.get("lifecycle") == "ephemeral" and item.get("status") in ("completed", "needs_input", "failed"):
            review_state = (item.get("review") or {}).get("state", "pending")
            if review_state == "accept":
                raise RuntimeError("accepted ephemeral Worker is archived; spawn a new Worker for an explicit refresh")
            if review_state not in ("revise", "needs_input", "reject"):
                raise RuntimeError("review the current Worker run before continuing it")
        self._start(item, message)
        return self.public(self.get(worker_id))

    def _start(self, item: dict, task: str) -> None:
        worker_id = item["id"]
        worker_dir = Path(item["dir"])
        run_index = int(item.get("runIndex", 0)) + 1
        now = datetime.now(SHANGHAI)
        run_dir = worker_dir / "runs" / now.strftime("%Y") / now.strftime("%m") / f"run-{run_index:04d}"
        run_dir.mkdir(parents=True, exist_ok=True)
        job = {
            **item,
            "task": task,
            "runIndex": run_index,
            "runDir": str(run_dir),
            "resultFile": str(run_dir / "result.json"),
            "reportFile": str(run_dir / "report.md"),
            "historyFile": str(worker_dir / "history.json"),
            "progressFile": str(run_dir / "progress.json"),
            "modelEventsFile": str(run_dir / "model-events.jsonl"),
            "gaMemoryRoot": str(self.ga_memory_root or ""),
            "preserveHistory": item.get("capabilityId") != "worker.l4",
            "resetModelToFlash": item.get("capabilityId") == "worker.l4" or int(item.get("runIndex", 0)) > 0,
            "proModelName": self.pro_model,
        }
        job_file = run_dir / "job.json"
        job_file.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        cmd = [sys.executable, "-m", "G4W.agents.worker_runner", "--job", str(job_file)]
        main_dir = Path(__file__).resolve().parents[2]
        ga_app_dir = main_dir.parent / "app"
        inherited_path = os.environ.get("PYTHONPATH", "")
        python_path = os.pathsep.join(filter(None, (str(main_dir), str(ga_app_dir), inherited_path)))
        state_dir = self.root.resolve().parent
        runtime_dir = state_dir.parent
        workspace_root = runtime_dir.parent
        vector_index_dir = Path(os.environ.get("G4W_VECTOR_INDEX_DIR") or "runtime/G4W-vector-index")
        if not vector_index_dir.is_absolute():
            vector_index_dir = workspace_root / vector_index_dir
        env = {
            **os.environ,
            "PYTHONPATH": python_path,
            "G4W_WORKSPACE_ROOT": str(workspace_root),
            "G4W_STATE_DIR": str(state_dir),
            "G4W_RUNTIME_DIR": str(runtime_dir),
            "G4W_VECTOR_INDEX_DIR": str(vector_index_dir.resolve()),
        }
        kwargs = dict(cwd=str(main_dir), env=env)
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
        with self.lock:
            self.processes[worker_id] = process
        def mark(state):
            current = state["workers"][worker_id]
            current.update({
                "status": "running",
                "pid": process.pid,
                "task": task,
                "runIndex": run_index,
                "startedAt": time.time(),
                "updatedAt": time.time(),
                "review": {"runIndex": run_index, "state": "pending", "note": "", "updatedAt": time.time()},
                "lastProgressMilestone": 0,
                "modelEventLines": 0,
                "activeModelName": current.get("modelName", self.default_model),
                "currentRunDir": str(run_dir),
                "currentJobFile": str(job_file),
                "currentResultFile": str(job["resultFile"]),
                "currentReportFile": str(job["reportFile"]),
                "currentProgressFile": str(job["progressFile"]),
            })
        self.state.update(mark)
        threading.Thread(target=self._monitor, args=(worker_id, process, Path(job["resultFile"])), daemon=True).start()

    def _monitor(self, worker_id: str, process: subprocess.Popen, result_file: Path) -> None:
        try:
            process.wait(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            result = {"status": "failed", "summary": "Worker timed out"}
        else:
            try:
                result = json.loads(result_file.read_text(encoding="utf-8"))
            except Exception as error:
                result = {"status": "failed", "summary": f"Worker result missing: {error}"}
        item = self.get(worker_id) or {}
        if item.get("status") == "cancelled":
            with self.lock:
                self.processes.pop(worker_id, None)
            return
        next_status = "sleeping" if item.get("lifecycle") == "persistent" and result.get("status") == "completed" else result.get("status", "failed")
        def update(state):
            current = state.get("workers", {}).get(worker_id, {})
            current.update({
                "status": next_status,
                "pid": 0,
                "result": result,
                "updatedAt": time.time(),
                "lastError": result.get("summary", "") if result.get("status") == "failed" else "",
            })
        self.state.update(update)
        with self.lock:
            self.processes.pop(worker_id, None)
        run_index = int(item.get("runIndex", 0))
        self.event_store.enqueue(
            "worker.completed",
            item.get("bindingKey", ""),
            self.completion_report(worker_id),
            dedupe_key=f"worker.completed:{worker_id}:{run_index}",
        )

    def stop(self, worker_id: str) -> dict:
        item = self.get(worker_id)
        if not item:
            raise KeyError(f"worker not found: {worker_id}")
        with self.lock:
            process = self.processes.get(worker_id)
        if process and process.poll() is None:
            process.kill()
        def update(state):
            state["workers"][worker_id].update({"status": "cancelled", "pid": 0, "updatedAt": time.time()})
        self.state.update(update)
        return self.public(self.get(worker_id))

    def scan_stalled(self, stall_seconds: int) -> list[dict]:
        """Emit one durable warning per run when a live Worker stops reporting progress."""
        now = time.time()
        warnings = []
        for item in self.state.read().get("workers", {}).values():
            if item.get("status") != "running":
                continue
            run_index = int(item.get("runIndex", 0) or 0)
            if int(item.get("stallNotifiedRun", 0) or 0) == run_index:
                continue
            progress_file = self._progress_path(item)
            try:
                last_progress = progress_file.stat().st_mtime
            except OSError:
                last_progress = float(item.get("startedAt", now) or now)
            idle_seconds = max(0, int(now - last_progress))
            if idle_seconds < max(60, int(stall_seconds)):
                continue
            warning = {
                "workerId": item.get("id", ""),
                "runIndex": run_index,
                "idleSeconds": idle_seconds,
                "task": item.get("task", ""),
            }
            self.event_store.enqueue(
                "worker.stalled", item.get("bindingKey", ""), warning,
                dedupe_key=f"worker.stalled:{item.get('id', '')}:{run_index}",
            )
            def mark(state, worker_id=item.get("id", ""), current_run=run_index):
                state["workers"][worker_id]["stallNotifiedRun"] = current_run
                state["workers"][worker_id]["updatedAt"] = time.time()
            self.state.update(mark)
            warnings.append(warning)
        return warnings

    def scan_progress_milestones(self, step: int = 5) -> list[dict]:
        emitted = []
        for item in self.state.read().get("workers", {}).values():
            if item.get("status") != "running" or not item.get("progressReporting", True):
                continue
            try:
                progress = json.loads(self._progress_path(item).read_text(encoding="utf-8"))
            except Exception:
                continue
            turn = int(progress.get("turn", 0) or 0)
            interval = max(1, int(step))
            milestone = (turn // interval) * interval
            previous = int(item.get("lastProgressMilestone", 0) or 0)
            if milestone < step or milestone <= previous:
                continue
            for reached in range(previous + interval, milestone + 1, interval):
                payload = {
                    "workerId": item.get("id", ""), "runIndex": int(item.get("runIndex", 0) or 0),
                    "turn": turn, "milestone": reached, "summary": str(progress.get("summary") or "")[:1200],
                }
                self.event_store.enqueue(
                    "worker.progress_milestone", item.get("bindingKey", ""), payload,
                    dedupe_key=f"worker.progress:{item.get('id', '')}:{item.get('runIndex', 0)}:{reached}",
                )
                emitted.append(payload)
            def mark(state, worker_id=item.get("id", ""), value=milestone):
                state["workers"][worker_id]["lastProgressMilestone"] = value
            self.state.update(mark)
        return emitted

    def scan_model_switches(self) -> list[dict]:
        emitted = []
        for item in self.state.read().get("workers", {}).values():
            if item.get("status") != "running":
                continue
            run_dir_value = str(item.get("currentRunDir") or "").strip()
            if not run_dir_value:
                continue
            event_file = Path(run_dir_value) / "model-events.jsonl"
            if not event_file.is_file():
                continue
            lines = event_file.read_text(encoding="utf-8", errors="replace").splitlines()
            consumed = int(item.get("modelEventLines", 0) or 0)
            latest = {}
            for index, line in enumerate(lines[consumed:], start=consumed + 1):
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                latest = event
                payload = {
                    "workerId": item.get("id", ""),
                    "runIndex": int(item.get("runIndex", 0) or 0),
                    "topic": item.get("topic", ""),
                    **event,
                }
                self.event_store.enqueue(
                    "worker.model_switched", item.get("bindingKey", ""), payload,
                    dedupe_key=f"worker.model-switch:{item.get('id', '')}:{item.get('runIndex', 0)}:{index}",
                )
                emitted.append(payload)
            if len(lines) > consumed:
                def mark(state, worker_id=item.get("id", ""), count=len(lines), model=latest.get("to", "")):
                    current = state["workers"][worker_id]
                    current["modelEventLines"] = count
                    if model:
                        current["activeModelName"] = model
                    current["updatedAt"] = time.time()
                self.state.update(mark)
        return emitted

    def get(self, worker_id: str) -> dict | None:
        return self.state.read().get("workers", {}).get(worker_id)

    def detail(self, worker_id: str) -> dict:
        item = self.get(worker_id)
        if not item:
            raise KeyError(f"worker not found: {worker_id}")
        value = self.public(item)
        value["result"] = item.get("result") or {}
        value["review"] = item.get("review") or {}
        return value

    def completion_report(self, worker_id: str) -> dict:
        item = self.get(worker_id)
        if not item:
            raise KeyError(f"worker not found: {worker_id}")
        result = item.get("result") or {}
        serialized = json.dumps(result, ensure_ascii=False, default=str)
        run_index = int(item.get("runIndex", 0) or 0)
        result_file = Path(item.get("currentResultFile") or (Path(item.get("dir", "")) / f"result-{run_index}.json"))
        report_file = Path(item.get("currentReportFile") or (Path(item.get("dir", "")) / f"report-{run_index}.md"))
        report = {
            **self.public(item),
            "workerId": worker_id,
            "resultStatus": result.get("status", item.get("status", "")),
            "resultChars": len(serialized),
            "resultFile": str(result_file),
            "reportFile": str(report_file) if report_file.is_file() else "",
            "resultInline": len(serialized) <= INLINE_COMPLETION_RESULT_CHARS,
        }
        report["task"] = str(report.get("task") or "")[:2000]
        report["summary"] = str(report.get("summary") or "")[:1200]
        if report["resultInline"]:
            report["result"] = result
        else:
            report["resultPreview"] = {
                "status": result.get("status", ""),
                "summary": str(result.get("summary", ""))[:1200],
                "model": result.get("model", ""),
            }
        return report

    def review(self, worker_id: str, run_index: int, decision: str, note: str = "") -> dict:
        if decision not in ("accept", "revise", "needs_input", "reject"):
            raise ValueError("invalid review decision")
        item = self.get(worker_id)
        if not item:
            raise KeyError(f"worker not found: {worker_id}")
        current_run = int(item.get("runIndex", 0))
        if int(run_index or current_run) != current_run:
            raise RuntimeError(f"stale Worker review: expected runIndex={current_run}")
        existing = item.get("review") or {}
        if int(existing.get("runIndex", 0) or 0) == current_run and existing.get("state") == decision:
            return {
                "ok": True, "workerId": worker_id, "runIndex": current_run,
                "decision": decision, "idempotent": True,
                "message": "This Worker run already has the same review decision; do not review it again.",
            }
        def update(state):
            current = state["workers"][worker_id]
            current["review"] = {
                "runIndex": current_run,
                "state": decision,
                "note": str(note or "")[:2000],
                "updatedAt": time.time(),
            }
            current["updatedAt"] = time.time()
        self.state.update(update)
        if item.get("lifecycle") == "ephemeral" and decision in ("accept", "reject"):
            self._archive_ephemeral(worker_id)
        return {"ok": True, "workerId": worker_id, "runIndex": current_run, "decision": decision}

    def list_for(self, binding_key: str) -> list[dict]:
        items = [self.public(item) for item in self.state.read().get("workers", {}).values() if item.get("bindingKey") == binding_key]
        return sorted(items, key=lambda item: item.get("updatedAt", 0), reverse=True)

    def _find_persistent(self, sender_id: str, capability_id: str) -> dict | None:
        for item in self.state.read().get("workers", {}).values():
            if item.get("senderId") == sender_id and item.get("capabilityId") == capability_id and item.get("lifecycle") == "persistent" and item.get("status") not in ("cancelled", "archived"):
                return item
        return None

    def _consolidate_persistent_workers(self) -> None:
        groups = {}
        for item in self.state.read().get("workers", {}).values():
            if item.get("lifecycle") != "persistent":
                continue
            groups.setdefault((item.get("senderId", ""), item.get("capabilityId", "")), []).append(item)
        for items in groups.values():
            active = [item for item in items if item.get("status") not in ("cancelled", "archived")]
            keep = max(active or items, key=lambda item: float(item.get("updatedAt", 0) or 0))
            for item in items:
                if item.get("id") == keep.get("id") or item.get("status") == "archived":
                    continue
                self._archive_worker(item.get("id", ""), reason=f"consolidated into {keep.get('id', '')}")

    def _archive_finished_ephemeral_workers(self) -> None:
        for item in list(self.state.read().get("workers", {}).values()):
            if item.get("lifecycle") != "ephemeral" or item.get("status") == "archived":
                continue
            review_state = str((item.get("review") or {}).get("state") or "")
            finished = item.get("status") in ("completed", "failed", "cancelled")
            if finished and (review_state in ("accept", "reject") or item.get("status") == "cancelled"):
                self._archive_worker(item.get("id", ""), reason="migrated finished ephemeral Worker")

    def _progress_path(self, item: dict) -> Path:
        current = str(item.get("currentProgressFile") or "")
        return Path(current) if current else Path(item.get("dir", "")) / "progress.json"

    def _archive_ephemeral(self, worker_id: str) -> str:
        item = self.get(worker_id)
        if not item or item.get("archivePath"):
            return str((item or {}).get("archivePath") or "")
        return self._archive_worker(worker_id, reason="ephemeral run reviewed")

    def _archive_worker(self, worker_id: str, reason: str = "") -> str:
        item = self.get(worker_id)
        if not item or item.get("archivePath"):
            return str((item or {}).get("archivePath") or "")
        source_value = str(item.get("dir") or "").strip()
        if not source_value:
            return ""
        source = Path(source_value)
        if not source.is_dir():
            return ""
        when = datetime.fromtimestamp(float(item.get("updatedAt") or time.time()), SHANGHAI)
        archive_dir = self._conversation_workers_root(item.get("senderId", "")) / "archive" / when.strftime("%Y") / when.strftime("%m")
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive = archive_dir / f"{worker_folder_name(item.get('topic', ''), worker_id)}.zip"
        temporary = archive.with_suffix(".zip.tmp")
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(source.rglob("*")):
                if path.is_file():
                    bundle.write(path, path.relative_to(source))
            bundle.writestr("worker-metadata.json", json.dumps(item, ensure_ascii=False, indent=2, default=str) + "\n")
        os.replace(temporary, archive)
        shutil.rmtree(source)
        def update(state):
            current = state["workers"][worker_id]
            current.update({"status": "archived", "archivePath": str(archive), "archiveReason": reason, "dir": "", "archivedAt": time.time()})
        self.state.update(update)
        return str(archive)

    @staticmethod
    def public(item: dict | None) -> dict:
        if not item:
            return {}
        result = item.get("result") or {}
        progress = {}
        try:
            current = str(item.get("currentProgressFile") or "")
            progress_path = Path(current) if current else Path(item.get("dir", "")) / "progress.json"
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
        except Exception:
            pass
        review = item.get("review") or {}
        return {
            "id": item.get("id"),
            "capabilityId": item.get("capabilityId"),
            "lifecycle": item.get("lifecycle"),
            "status": item.get("status"),
            "task": item.get("task"),
            "topic": item.get("topic", ""),
            "summary": result.get("summary", ""),
            "updatedAt": item.get("updatedAt", 0),
            "startedAt": item.get("startedAt", 0),
            "runIndex": item.get("runIndex", 0),
            "reviewState": review.get("state", ""),
            "progress": progress.get("summary", ""),
            "progressTurn": int(progress.get("turn", 0) or 0),
            "modelTier": item.get("modelTier", "flash"),
            "modelName": item.get("modelName", ""),
            "reportFile": item.get("currentReportFile", ""),
            "archivePath": item.get("archivePath", ""),
        }
