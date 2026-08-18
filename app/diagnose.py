"""Background runner for the on-demand PM2 diagnostic.

``run_diagnostic`` (app/diagnostics.py) can take ~1 minute (10 HTTP probes
with 1s sleeps + iostat). Running it synchronously in an HTTP handler would
block the event loop, so it runs in a daemon thread writing to a temp file and
the page polls that file until the job completes.
"""
from __future__ import annotations

import tempfile
import threading
import time
import uuid


class DiagnoseJob:
    """Runs ``fn(app, out=<file>)`` in a daemon thread, streaming to a file."""

    def __init__(self, app_name: str, fn, args: tuple = (),
                 kwargs: dict | None = None):
        self.id = uuid.uuid4().hex[:8]
        self.app_name = app_name
        self.out_path = tempfile.mktemp(prefix="pm2-diag-", suffix=".txt")
        self.done = False
        self.returncode: int | None = None
        self.started = time.time()
        self.thread = threading.Thread(
            target=self._run, args=(fn, args, kwargs or {}), daemon=True
        )

    def _run(self, fn, args: tuple, kwargs: dict) -> None:
        try:
            with open(self.out_path, "w", encoding="utf-8", errors="replace") as f:
                fn(*args, out=f, **kwargs)
            self.returncode = 0
        except Exception as exc:  # pragma: no cover - defensive
            try:
                with open(self.out_path, "a", encoding="utf-8") as f:
                    f.write(f"\nERROR running diagnostic: {exc}\n")
            except OSError:
                pass
            self.returncode = 1
        finally:
            self.done = True

    def start(self) -> "DiagnoseJob":
        self.thread.start()
        return self

    def read(self) -> str:
        try:
            with open(self.out_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return ""


class DiagnoseRegistry:
    """Process-local registry of running/completed diagnostic jobs."""

    def __init__(self):
        self._jobs: dict[str, DiagnoseJob] = {}
        self._lock = threading.Lock()

    def start(self, app_name: str, fn, args: tuple = (),
              kwargs: dict | None = None) -> DiagnoseJob:
        job = DiagnoseJob(app_name, fn, args, kwargs)
        with self._lock:
            self._jobs[job.id] = job
        job.start()
        self._prune()
        return job

    def get(self, job_id: str) -> DiagnoseJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _prune(self) -> None:
        # Drop finished jobs older than 30 minutes to bound memory.
        now = time.time()
        with self._lock:
            for job_id in list(self._jobs):
                job = self._jobs[job_id]
                if job.done and now - job.started > 1800:
                    del self._jobs[job_id]


registry = DiagnoseRegistry()