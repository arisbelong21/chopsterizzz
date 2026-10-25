"""Local-first persistent scheduler + startup reconciliation (spec §28, §64).

Responsibilities:
  * on start: recover jobs interrupted by an app restart without ever
    duplicating a post — jobs that never reached the platform are re-queued,
    while jobs interrupted *during* the upload are parked for the user because
    re-sending them could upload the same video twice,
  * recurring recipes → create runs at their local time,
  * import the legacy publish_queue.json staging list (migration §52).
"""
from __future__ import annotations

import threading
import traceback
from datetime import datetime
from typing import Any

from chopster.automation.models import JobStatus, RunStatus
from chopster.automation.utils import redact

IN_FLIGHT_STATUSES = [JobStatus.AUTH_CHECK.value, JobStatus.PREPARING.value, JobStatus.UPLOADING.value, JobStatus.PROCESSING.value, JobStatus.VERIFYING.value]

#: Run states that are settled history. Startup reconciliation must never rewrite
#: them: an emptied run (its last job deleted) has to stay closed instead of
#: being re-derived back into an empty Draft card.
TERMINAL_RUN_STATUSES = {
    RunStatus.COMPLETED.value, RunStatus.FAILED.value,
    RunStatus.CANCELLED.value, RunStatus.PARTIAL_SUCCESS.value,
}

#: Subset of ``IN_FLIGHT_STATUSES`` a job can only reach *after* the media was
#: handed to the platform. Whether that request landed is unknowable after a
#: crash, and re-running the whole job would upload the same video a second
#: time, so these are parked for the user instead of being re-queued.
UPLOAD_MAY_HAVE_LANDED = [JobStatus.UPLOADING.value, JobStatus.PROCESSING.value, JobStatus.VERIFYING.value]


class AutomationScheduler:
    def __init__(self, manager: Any):
        self.manager = manager
        self.app = manager.app
        self.db = manager.db
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._reconciled = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="automation-scheduler")
        self._thread.start()

    def stop(self, timeout: float = 4.0) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self._reconciled:
                    self.reconcile_startup()
                    self._reconciled = True
                self.manager.import_legacy_queue()
                self._run_due_recipes()
            except Exception:
                try:
                    self.app.logger.error("[Automation][scheduler] error: %s", redact(traceback.format_exc()[-1200:]))
                except Exception:
                    pass
            self._stop.wait(15.0)

    def reconcile_startup(self) -> None:
        """Recover in-flight jobs from a previous session without duplicating."""
        rows = self.db.automation_list_jobs(statuses=IN_FLIGHT_STATUSES, limit=200)
        for job in rows:
            job_id = str(job["id"])
            status = str(job.get("status") or "")
            attempt = int(job.get("attempt") or 0)
            try:
                max_attempts = max(1, int(self.app.config.get("aut_retry_limit") or 3))
            except Exception:
                max_attempts = 3
            if status in UPLOAD_MAY_HAVE_LANDED:
                # The upload was already handed to the platform. It may well have
                # been accepted before the app died, and running the job again
                # would send the same video a second time — hand it to the user.
                self.db.automation_update_job(
                    job_id, status=JobStatus.ACTION_REQUIRED.value,
                    error_code="INTERRUPTED", reason="verify_unknown",
                    error_message=("Aplikasi ditutup saat upload sedang berjalan. Cek dulu di aplikasi "
                                   "platform: kalau videonya sudah ada, jangan dijalankan ulang."),
                    last_activity="terputus saat upload — periksa platform dulu",
                )
            elif attempt < max_attempts:
                self.db.automation_update_job(job_id, status=JobStatus.QUEUED.value, scheduled_at="", last_activity="dipulihkan setelah restart aplikasi")
            else:
                self.db.automation_update_job(job_id, status=JobStatus.FAILED.value, error_code="INTERRUPTED", error_message="Terjeda saat aplikasi ditutup dan percobaan habis.")
            try:
                self.app.logger.info("[Automation] recovered job %s from %s (attempt=%s)", job_id, status, attempt)
            except Exception:
                pass
        # Recompute every non-terminal run so the UI is truthful after restart.
        # Settled history is left untouched, and a run with no jobs is skipped
        # rather than re-derived to DRAFT. Truthful also means a review run that
        # is still waiting for the user's Start stays READY: refresh_run applies
        # that rule, so reconciliation reuses it instead of deriving a bare
        # status that would auto-start the run.
        for run in self.db.automation_list_runs(limit=200):
            if str(run.get("status") or "") in TERMINAL_RUN_STATUSES:
                continue
            if not self.db.automation_job_statuses(str(run["id"])):
                continue
            self.manager.refresh_run(str(run["id"]))

    def _run_due_recipes(self) -> None:
        now = datetime.now()
        for recipe in self.db.automation_list_recipes():
            if not int(recipe.get("enabled") or 0):
                continue
            try:
                import json
                cfg = json.loads(recipe.get("config") or "{}")
            except Exception:
                continue
            repeat = str(cfg.get("repeat") or "")
            if repeat not in ("daily", "weekly"):
                continue
            want_time = str(cfg.get("time") or "19:00")
            try:
                hh, mm = want_time.split(":")
                due_minutes = int(hh) * 60 + int(mm)
            except Exception:
                due_minutes = -1
            if due_minutes < 0:
                continue
            now_minutes = now.hour * 60 + now.minute
            if now_minutes < due_minutes:
                continue
            last = str(recipe.get("last_run_at") or "")
            today = now.strftime("%Y-%m-%d")
            if last.startswith(today):
                continue
            if repeat == "weekly" and last:
                try:
                    last_dt = datetime.strptime(last[:10], "%Y-%m-%d")
                    if (now - last_dt).days < 7:
                        continue
                except Exception:
                    pass
            self.manager.run_recipe(str(recipe["id"]), trigger="schedule")
