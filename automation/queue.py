"""Bounded-concurrency job runner with idempotency, retries, verification.

Execution model (spec §26-§29, §66-§69):
  * dispatcher thread claims due jobs with a CAS UPDATE (no double runs),
  * bounded worker threads (aut_max_concurrent, default 2),
  * cooperative cancellation via per-job Event,
  * idempotency check before publish (duplicate → verify/cancel, never blind retry),
  * exponential backoff for safe failures only, bounded attempts,
  * every terminal transition updates DB + notifies the manager.
"""
from __future__ import annotations

import json
import threading
import traceback
from datetime import datetime, timedelta
from typing import Any

import requests

from chopster.automation.content import (
    map_for_platform, normalize_content_pack, validate_publish_payload,
)
from chopster.automation.models import (
    AuthRequiredError, AutomationError, CancelledError, DuplicateSuspectedError,
    InvalidMediaError, JobStatus, MetadataInvalidError, NeedsActionError,
    RetryableError, VerificationFailedError, now_iso,
)
from chopster.automation.publishers import PublishContext, get_publisher
from chopster.automation.utils import backoff_seconds, idempotency_key, media_fingerprint, redact, safe_json_loads

IN_FLIGHT = [JobStatus.AUTH_CHECK.value, JobStatus.PREPARING.value, JobStatus.UPLOADING.value, JobStatus.PROCESSING.value, JobStatus.VERIFYING.value]

SAFE_RETRY_CATEGORIES = {"NETWORK_ERROR", "RATE_LIMITED", "UPLOAD_FAILED", "PROCESSING_FAILED", "VERIFICATION_FAILED", "UNKNOWN_RESULT"}
# Failures the user must fix themselves. Retrying them unchanged would only
# reproduce the same failure, so they park the job instead of looping.
USER_CATEGORIES = {"USER_ACTION_REQUIRED", "AUTH_REQUIRED", "AUTH_EXPIRED", "PERMISSION_DENIED", "UNSUPPORTED_OPERATION", "PLATFORM_UI_CHANGED", "METADATA_INVALID"}


class JobRunner:
    def __init__(self, manager: Any):
        self.manager = manager
        self.app = manager.app
        self.db = manager.db
        self.auth = manager.auth
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._active: dict[str, threading.Event] = {}
        self._lock = threading.RLock()

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="automation-runner")
        self._thread.start()

    def stop(self, timeout: float = 6.0) -> None:
        self._stop.set()
        with self._lock:
            for ev in self._active.values():
                ev.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=timeout)

    def cancel_job(self, job_id: str) -> None:
        with self._lock:
            ev = self._active.get(job_id)
        if ev is not None:
            ev.set()

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def is_active(self, job_id: str) -> bool:
        """True while a worker thread currently owns this job's row."""
        with self._lock:
            return job_id in self._active

    # -- dispatcher -------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                try:
                    self.app.logger.error("[Automation][runner] tick error: %s", redact(traceback.format_exc()[-1500:]))
                except Exception:
                    pass
            self._stop.wait(2.0)

    def _tick(self) -> None:
        if getattr(self.manager, "stopping", False):
            return
        try:
            maxc = max(1, int(self.app.config.get("aut_max_concurrent") or 2))
        except Exception:
            maxc = 2
        with self._lock:
            active = len(self._active)
        capacity = maxc - active
        if capacity <= 0:
            return
        due = self.db.automation_due_jobs(now_iso(), limit=capacity + 4)
        for job in due:
            with self._lock:
                if len(self._active) >= maxc:
                    break
            job_id = str(job["id"])
            with self._lock:
                if job_id in self._active:
                    continue
            run = self.db.automation_get_run(str(job["run_id"]))
            if run:
                run_status = str(run.get("status") or "")
                if run_status in ("READY", "CANCELLED"):
                    continue           # waiting for Start, or cancelled run
            if self.db.automation_claim_job(job_id, [JobStatus.QUEUED.value, JobStatus.SCHEDULED.value], JobStatus.AUTH_CHECK.value):
                ev = threading.Event()
                with self._lock:
                    self._active[job_id] = ev
                t = threading.Thread(target=self._execute, args=(job_id, ev), daemon=True, name=f"aut-job-{job_id[-8:]}")
                t.start()

    # -- execution --------------------------------------------------------
    def _ensure_description(self, job: dict[str, Any],
                            pack_raw: dict[str, Any]) -> dict[str, Any]:
        """Generate a real AI description for a job that never got a pack.

        Studio/preset publishes without an AI Content Pack used to send the clip
        title as the YouTube description. The pack is created once, stored on
        the job and reused by retries. When the AI is unavailable nothing is
        invented here: the snapshot stays as-is (the mapper then falls back to
        the clip transcript, never to a fabricated claim).
        """
        if str(pack_raw.get("description_override") or "").strip():
            return pack_raw
        body = pack_raw.get("pack") if isinstance(pack_raw.get("pack"), dict) else {}
        if str(body.get("description_long") or body.get("description") or "").strip():
            return pack_raw
        try:
            media_row = self.db.automation_media_by_path(str(job.get("media_path") or ""))
        except Exception:
            media_row = None
        if not media_row:
            return pack_raw
        raw_ctx = safe_json_loads(media_row.get("content_pack"), {})
        ctx = raw_ctx.get("clip_context") if isinstance(raw_ctx, dict) else {}
        if not isinstance(ctx, dict) or not isinstance(ctx.get("segments"), list) or not ctx.get("segments"):
            # No ready transcript on the media row: never start a surprise
            # transcription inside the upload worker.
            return pack_raw
        try:
            pack = self.manager._generate_template_pack(
                media_row, self.manager._template_ai_config(), language="auto")
        except Exception as exc:
            self._log(f"pack otomatis gagal: {str(exc)[:120]}")
            return pack_raw
        if not isinstance(pack, dict) or not pack or pack.get("_fallback_reason"):
            if isinstance(pack, dict) and pack.get("_fallback_reason"):
                self._log(f"pack otomatis dilewati: {pack.get('_fallback_reason')}")
            return pack_raw
        merged = dict(pack_raw)
        merged["pack"] = normalize_content_pack(pack).to_dict()
        merged["pack_source"] = "generated_at_publish"
        try:
            self.db.automation_update_job(str(job.get("id") or ""),
                                          content_pack=json.dumps(merged, ensure_ascii=False))
        except Exception:
            pass
        self._log("deskripsi belum ada: AI pack dibuat saat publish")
        return merged

    def _execute(self, job_id: str, cancel_event: threading.Event) -> None:
        try:
            self._run_job(job_id, cancel_event)
        except CancelledError as exc:
            self._finish(job_id, JobStatus.CANCELLED.value, error_code="CANCELLED", error_msg=exc.user_message or "Dibatalkan.")
        except AutomationError as exc:
            self._handle_error(job_id, exc)
        except requests.RequestException as exc:
            self._handle_error(job_id, RetryableError(redact(str(exc)), user_message="Gangguan jaringan. Akan dicoba lagi."))
        except Exception as exc:
            self._handle_error(job_id, RetryableError(f"{type(exc).__name__}: {redact(str(exc))}", user_message="Galat tak terduga. Akan dicoba lagi."))
        finally:
            with self._lock:
                self._active.pop(job_id, None)
            try:
                self.manager.on_job_finished(job_id)
            except Exception:
                pass

    def _run_job(self, job_id: str, cancel_event: threading.Event) -> None:
        db = self.db
        job = db.automation_get_job(job_id)
        if not job:
            return
        platform = str(job.get("platform") or "")
        account_id = str(job.get("account_id") or "")

        def cancel_check() -> bool:
            return cancel_event.is_set() or self._stop.is_set() or bool(getattr(self.manager, "stopping", False))

        # 1) media must exist
        path = str(job.get("media_path") or "")
        import os as _os
        if not _os.path.isfile(path):
            raise InvalidMediaError(f"missing media: {path}", user_message="File media tidak ditemukan.")

        # 2) account
        account = db.automation_get_account(account_id) if account_id else None
        if not account or str(account.get("status") or "") == "disconnected":
            raise AuthRequiredError(f"account unavailable: {account_id}", user_message="Akun tidak terhubung. Sambungkan kembali di Accounts.")

        # 3) idempotency / duplicate protection (spec §26)
        fp = str(job.get("media_fingerprint") or "")
        if not fp:
            fp = media_fingerprint(path)
            db.automation_update_job(job_id, media_fingerprint=fp)
        key = str(job.get("idempotency_key") or "")
        if not key:
            key = idempotency_key(platform, account_id, fp)
            db.automation_update_job(job_id, idempotency_key=key)
        dups = db.automation_find_idempotency(key, exclude_job_id=job_id)
        if dups:
            statuses = [str(d.get("status") or "") for d in dups]
            if any(s in ("PUBLISHED", "SCHEDULED") for s in statuses):
                raise DuplicateSuspectedError("same media+platform already published", user_message="Publikasi sama sudah pernah dilakukan — job dibatalkan untuk mencegah duplikat.")
            attempt = int(job.get("attempt") or 0) + 1
            if attempt >= 3:
                raise NeedsActionError("duplicate suspected", user_message="Job serupa sedang berjalan lama. Periksa Queue.")
            defer_at = (datetime.now() + timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")
            db.automation_update_job(job_id, attempt=attempt, status=JobStatus.QUEUED.value, scheduled_at=defer_at, last_activity="menunggu job serupa selesai")
            return

        # 4) auth check (fresh token / session)
        self.auth.ensure_access_token(account_id)
        db.automation_update_job(job_id, status=JobStatus.PREPARING.value, last_activity="auth ok")
        self.manager.notify_job(job_id, "status")

        # 5) content mapping (reuse the AI Content Pack — spec §14)
        pack_raw = safe_json_loads(job.get("content_pack"), {})
        pack_raw = self._ensure_description(job, pack_raw)
        pack_body = pack_raw.get("pack") if isinstance(pack_raw.get("pack"), dict) else pack_raw
        norm = normalize_content_pack(pack_body)
        # Audience/extra overrides are part of the job snapshot, so the payload
        # is built from persisted job data only - never from live UI state.
        audience_raw = pack_raw.get("made_for_kids")
        made_for_kids = audience_raw if isinstance(audience_raw, bool) else None
        payload = map_for_platform(platform, norm,
                                   privacy=str(pack_raw.get("privacy") or ""),
                                   title_override=str(pack_raw.get("title_override") or ""),
                                   description_override=str(pack_raw.get("description_override") or ""),
                                   caption_override=str(pack_raw.get("caption_override") or ""),
                                   made_for_kids=made_for_kids,
                                   category_id=str(pack_raw.get("category_id") or ""),
                                   clip_context=pack_raw.get("clip_context"))
        if isinstance(pack_raw.get("extra"), dict):
            payload["extra"] = pack_raw["extra"]

        # 5b) pre-flight: block the request when required metadata is missing
        # instead of silently uploading an empty title/description (spec §8.4).
        problems = validate_publish_payload(platform, payload)
        if problems:
            raise MetadataInvalidError(
                "metadata preflight failed: " + "; ".join(problems),
                user_message="Upload dihentikan karena metadata belum lengkap: "
                             + "; ".join(problems))

        # 6) upload
        last_pct = {"v": -1}
        last_label = {"v": ""}

        def progress(pct: int, label: str) -> None:
            if cancel_check():
                raise CancelledError("cancelled", user_message="Dibatalkan.")
            pct = max(0, min(100, int(pct)))
            if pct >= last_pct["v"] + 3 or label != last_label["v"]:
                last_pct["v"] = pct
                last_label["v"] = label
                self.db.automation_update_job(job_id, progress=pct, last_activity=label)
                self.manager.notify_job(job_id, "progress", {"progress": pct, "label": label})

        auth_mode = str(account.get("auth_mode") or "oauth")
        # Browser-lane guard rails (Diagnostics): Test Mode never posts for real,
        # and the optional conservative soft cap stops runaway UI automation.
        # Checked before the "uploading" status so a parked simulation is honest.
        if auth_mode == "browser":
            if bool(self.app.config.get("aut_browser_test_mode")):
                raise NeedsActionError(
                    "browser test mode active", reason="test_mode",
                    user_message="Test Mode aktif: tidak ada posting nyata. Job ini hanya simulasi. "
                                 "Matikan Test Mode di Automation → Diagnostics untuk publish sungguhan.")
            self._enforce_browser_soft_cap(account_id, platform)
        db.automation_update_job(job_id, status=JobStatus.UPLOADING.value, progress=2, last_activity="mulai upload")
        self.manager.notify_job(job_id, "status")
        publisher = get_publisher(self.app, self.auth, platform, auth_mode=auth_mode)
        ctx = PublishContext(job=job, account=account, payload=payload, media_path=path, progress=progress, cancel_check=cancel_check)
        result = publisher.publish(ctx)
        if not result.ok:
            raise VerificationFailedError(result.message or "publish not confirmed", user_message=result.message or "Publish tidak terkonfirmasi.")

        # 7) verification (spec §27)
        db.automation_update_job(job_id, status=JobStatus.VERIFYING.value, last_activity="verifying")
        self.manager.notify_job(job_id, "status")
        try:
            vres = publisher.verify(ctx, result.external_id)
        except VerificationFailedError as exc:
            # Verification blew up (HTTP error / network). The upload itself was
            # already accepted by publish(), so the media is on the platform and
            # a retry would send it a second time.
            raise self._verify_inconclusive(exc) from exc
        if not vres.ok:
            if getattr(publisher, "verify_unknown_is_action", False):
                # Draft-first providers (e.g. TikTok official inbox) must never
                # auto-retry: re-sending could create a duplicate draft. Hand it
                # to the user instead of retrying blindly.
                raise NeedsActionError(
                    vres.message or "verification pending",
                    reason="verify_unknown",
                    user_message=(vres.message or "Hasil publish belum dapat dipastikan.")
                    + " Periksa di aplikasi platform dulu sebelum mencoba lagi.",
                )
            # Any other provider: publish() already succeeded, so an unclear
            # verification must never become an automatic re-upload. Park it.
            raise self._verify_inconclusive(
                NeedsActionError(vres.message or "verification pending",
                                 user_message=vres.message or "Hasil publish belum dapat dipastikan.")
            )
        final_status = vres.status or result.status or JobStatus.PUBLISHED.value
        last_label = "published"
        reason = ""
        if str(final_status) == JobStatus.ACTION_REQUIRED.value:
            if str(vres.verification_status or result.verification_status or "") == "DRAFT_READY":
                last_label = "draft siap - publish manual di aplikasi platform"
                reason = "draft_ready"
            else:
                last_label = "perlu tindakanmu"
                reason = "verify_unknown"
        elif str(final_status) == JobStatus.SCHEDULED.value:
            # The media is already uploaded; the platform will publish it at the
            # scheduled time. Mark it so the dispatcher can tell this terminal
            # "live later" state apart from a job that is still waiting for its
            # own slot (see Database.automation_due_jobs).
            last_label = "terjadwal di platform (sudah diupload)"
            reason = "scheduled_live"
        db.automation_update_job(job_id, status=final_status, progress=100, external_post_id=vres.external_id or result.external_id, external_url=vres.external_url or result.external_url, verification_status=vres.verification_status or result.verification_status, error_code="", error_message="", reason=reason, last_activity=last_label)
        try:
            media_row = db.automation_media_by_path(path)
            if media_row:
                db.automation_update_media(str(media_row["id"]), "published")
        except Exception:
            pass
        self.manager.notify_job(job_id, "done", {"status": final_status})
        self._log(f"job {job_id} {platform} -> {final_status} id={vres.external_id or result.external_id}")
        if str(final_status) in (JobStatus.PUBLISHED.value, JobStatus.SCHEDULED.value):
            # The media is now on the platform. Any twin still waiting in the
            # queue is moot — and would read as a duplicate — so retire it.
            self._cancel_superseded_twins(job_id, str(job.get("idempotency_key") or ""))

    def _cancel_superseded_twins(self, job_id: str, key: str) -> None:
        """Cancel still-pending jobs that share this job's media fingerprint.

        After a job is published (or scheduled live), a twin parked as "needs
        your login" or sitting QUEUED would keep showing up in the Queue
        forever, which reads as a duplicate post. It can never publish anyway
        (the duplicate guard blocks a second upload), so retire it honestly.
        """
        if not key:
            return
        try:
            twins = self.app.db.automation_find_pending(key, exclude_job_id=job_id)
        except Exception:
            return
        for twin in twins:
            twin_id = str(twin.get("id") or "")
            if not twin_id:
                continue
            try:
                self.app.db.automation_update_job(
                    twin_id, status=JobStatus.CANCELLED.value,
                    last_activity="dibatalkan otomatis: video sudah terbit")
                self.manager.notify_job(twin_id, "done", {"status": JobStatus.CANCELLED.value})
                self._log(f"job {twin_id} dibatalkan otomatis (kembaran {job_id} sudah terbit)")
            except Exception:
                pass

    def _log(self, msg: str) -> None:
        try:
            self.app.logger.info("[Automation][runner] %s", redact(msg))
        except Exception:
            pass

    @staticmethod
    def _verify_inconclusive(exc: AutomationError) -> NeedsActionError:
        """Turn an unclear verification into a user decision, never a re-send.

        Once ``publish()`` has returned ok, the media already sits on the
        platform. Re-running the job at that point would duplicate the post, so
        a failed or inconclusive verification always parks the job as
        ACTION_REQUIRED (reason ``verify_unknown``) for a human check. The user
        keeps full control: ``Cek & Lanjut`` / ``Ulangi`` still work if they
        confirm nothing was uploaded.
        """
        base = str(getattr(exc, "user_message", "") or exc or "Verifikasi belum berhasil.")
        return NeedsActionError(
            str(exc) or "verification pending",
            reason="verify_unknown",
            user_message=base + " Periksa di aplikasi platform dulu sebelum mencoba lagi.",
        )

    def _enforce_browser_soft_cap(self, account_id: str, platform: str) -> None:
        """Optional conservative guard for Browser Session automation.

        Disabled by default (``aut_browser_soft_cap`` <= 0). When enabled it
        parks further browser submissions once the account has already made that
        many real attempts in the last 24h, so a runaway loop cannot hammer the
        platform UI. The user can raise the cap or continue explicitly.
        """
        try:
            cap = int(self.app.config.get("aut_browser_soft_cap") or 0)
        except Exception:
            cap = 0
        if cap <= 0:
            return
        try:
            since = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
            used = int(self.db.automation_count_account_jobs_since(account_id, since) or 0)
        except Exception:
            return
        if used >= cap:
            raise NeedsActionError(
                f"browser soft cap reached ({used}/{cap})", reason="soft_cap",
                user_message=(f"Soft cap Browser Session tercapai ({used}/{cap} dalam 24 jam). "
                              "Ini pengaman konservatif agar otomasi UI tidak berjalan berlebihan. "
                              "Naikkan Soft cap di Automation → Diagnostics, atau lanjutkan manual."))

    # -- outcomes ---------------------------------------------------------
    def _finish(self, job_id: str, status: str, error_code: str = "", error_msg: str = "",
                reason: str = "") -> None:
        self.db.automation_update_job(job_id, status=status, error_code=error_code, error_message=redact(error_msg), reason=str(reason or ""), last_activity=(error_msg or status)[:180])
        self.manager.notify_job(job_id, "done", {"status": status, "error": error_msg, "reason": reason})
        self._log(f"job {job_id} -> {status} {error_code}")

    def _handle_error(self, job_id: str, exc: AutomationError) -> None:
        job = self.db.automation_get_job(job_id)
        if not job:
            return
        category = str(getattr(exc, "category", "UNKNOWN_RESULT"))
        user_msg = str(getattr(exc, "user_message", "") or exc)
        reason = str(getattr(exc, "reason", "") or "")
        if not reason and category in ("AUTH_REQUIRED", "AUTH_EXPIRED"):
            reason = "login_required"
        retryable = bool(getattr(exc, "retryable", False)) or category in SAFE_RETRY_CATEGORIES
        # Browser-lane jobs never auto-retry: re-running the UI flow can post the
        # same video twice. The user reviews the result and retries explicitly.
        if retryable:
            try:
                acct = next((a for a in self.db.automation_list_accounts()
                             if str(a.get("id")) == str(job.get("account_id") or "")), None)
                if acct and str(acct.get("auth_mode") or "") == "browser":
                    retryable = False
            except Exception:
                pass
        if isinstance(exc, CancelledError) or category == "CANCELLED":
            self._finish(job_id, JobStatus.CANCELLED.value, "CANCELLED", user_msg, reason)
            return
        if isinstance(exc, DuplicateSuspectedError) or category == "DUPLICATE_SUSPECTED":
            self._finish(job_id, JobStatus.CANCELLED.value, "DUPLICATE_SUSPECTED", user_msg, reason)
            return
        if isinstance(exc, InvalidMediaError) or category == "INVALID_MEDIA":
            self._finish(job_id, JobStatus.FAILED.value, "INVALID_MEDIA", user_msg, reason)
            self.manager.notify_global("publish_failed", {"job_id": job_id, "category": category, "message": user_msg})
            return
        if category in USER_CATEGORIES:
            self._finish(job_id, JobStatus.ACTION_REQUIRED.value, category, user_msg, reason)
            self.manager.notify_global("action_required", {"job_id": job_id, "platform": job.get("platform"), "category": category, "reason": reason, "message": user_msg})
            return
        if category == "QUOTA_EXCEEDED":
            self._finish(job_id, JobStatus.FAILED.value, category, user_msg, reason)
            self.manager.notify_global("publish_failed", {"job_id": job_id, "category": category, "message": user_msg})
            return
        attempt = int(job.get("attempt") or 0)
        max_attempts = int(job.get("max_attempts") or 3)
        try:
            max_attempts = max(1, int(self.app.config.get("aut_retry_limit") or max_attempts))
        except Exception:
            pass
        if retryable and attempt + 1 < max_attempts:
            attempt += 1
            delay = backoff_seconds(attempt)
            ready_at = (datetime.now() + timedelta(seconds=delay)).strftime("%Y-%m-%d %H:%M:%S")
            self.db.automation_update_job(job_id, attempt=attempt, status=JobStatus.QUEUED.value, scheduled_at=ready_at, error_code=category, error_message=redact(user_msg), reason="", last_activity=f"retry {attempt}/{max_attempts - 1} dalam {delay}s")
            self.manager.notify_job(job_id, "retry", {"attempt": attempt, "delay": delay, "error": user_msg})
            self._log(f"job {job_id} retry {attempt}/{max_attempts - 1} in {delay}s category={category}")
            return
        self._finish(job_id, JobStatus.FAILED.value, category, user_msg, reason)
        self.manager.notify_global("publish_failed", {"job_id": job_id, "category": category, "message": user_msg})
