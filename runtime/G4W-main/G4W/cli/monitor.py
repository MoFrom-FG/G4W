import time
from pathlib import Path

from .main import _pid_is_running, _read_pid
from ..agents.round_log import ROUND_END, latest_output


class ModelLogMonitor:
    """Tail the real GA Conductor output files, including silent/tool turns."""

    def __init__(self, state_dir: Path, pid_file: Path):
        self.state_dir = Path(state_dir).resolve()
        self.pid_file = Path(pid_file).resolve()
        self.output_dir = self.state_dir / "memory" / "conversations"
        self.active_file: Path | None = None
        self.last_rendered = ""
        self.round_closed = False

    def initialize(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        # Match the original shared-open observer: render the newest GA output
        # once on attachment, then tail subsequent deltas/files.
        self.active_file = None
        self.last_rendered = ""
        self.round_closed = False

    def drain(self) -> None:
        current = latest_output(self.output_dir)
        if not current:
            return
        if self.active_file != current:
            self.active_file = current
            self.last_rendered = ""
            self.round_closed = False
            print("", flush=True)
        try:
            value = current.read_text(encoding="utf-8")
        except Exception:
            return
        if value == self.last_rendered:
            return
        delta = value[len(self.last_rendered):] if value.startswith(self.last_rendered) else value
        self.last_rendered = value
        if delta:
            print(delta, end="", flush=True)
        if ROUND_END in value and not self.round_closed:
            self.round_closed = True
            print(">", flush=True)

    def run(self) -> int:
        print(f"Connected to G4W real GA output stream ({self.output_dir})", flush=True)
        print("Silent rounds, tools and non-delivered model actions are shown. Ctrl+C to exit.", flush=True)
        self.initialize()
        deadline = time.time() + 30
        pid = 0
        while time.time() < deadline:
            pid = _read_pid(self.pid_file)
            if pid and _pid_is_running(pid):
                break
            time.sleep(0.25)
        if not pid or not _pid_is_running(pid):
            print("G4W service did not start within 30 seconds.", flush=True)
            return 1
        print(">", flush=True)
        while _pid_is_running(pid):
            self.drain()
            time.sleep(0.25)
        self.drain()
        if (self.state_dir / "G4W.stop-requested").is_file():
            print("\nG4W service stopped; observer exited normally.", flush=True)
        else:
            print("\nG4W service exited; observer stopped.", flush=True)
        return 0
