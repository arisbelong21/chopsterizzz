"""AutomationManager — façade wiring auth, jobs, scheduler, recipes, events."""
from __future__ import annotations

import json
import sqlite3
import threading
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from chopster.automation import events as bus
from chopster.automation.auth import AuthManager
from chopster.automation.autopilot import AutoPilotEngine
from chopster.automation.content import normalize_content_pack
from chopster.automation.models import (
    AutomationError, DELETABLE_JOB_STATUSES, DuplicateInQueueError, JobStatus, PLATFORMS,
    RunStatus, derive_run_status, new_id, now_iso,
)
from chopster.automation.planner import PlanError, compute_staggered_slots
from chopster.automation.queue import JobRunner
from chopster.automation.scheduler import AutomationScheduler
from chopster.automation.utils import (
    idempotency_key, media_fingerprint, parse_local_time, redact, safe_json_loads,
    validate_media_file,
)

Listener = Callable[[str, dict[str, Any]], None]

BUILTIN_RECIPES = [
    {"name": "Publish Now", "config": {"platforms": [], "mode": "now", "repeat": "", "max_per_run": 1}},
    {"name": "Shorts Daily", "config": {"platforms": ["youtube", "tiktok"], "mode": "schedule", "time": "19:00", "repeat": "daily", "max_per_run": 1}},
    {"name": "Multi-Platform Daily", "config": {"platforms": ["youtube", "facebook", "tiktok", "instagram"], "mode": "schedule", "time": "19:00", "repeat": "daily", "max_per_run": 1}},
    {"name": "Manual Review Before Publish", "config": {"platforms": [], "mode": "now", "repeat": "", "review": True, "max_per_run": 3}},
]


def split_media_content(raw: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(pack, clip_context)`` from a media row's persisted JSON.

    Handles both the wrapped shape (``{"pack": ..., "clip_context": ...}``)
    written by ``_on_content_finished`` and legacy rows where ``content_pack``
    was the raw pack dict itself.
    """
    if isinstance(raw, str):
        data = safe_json_loads(raw, {})
    else:
        data = raw if isinstance(raw, dict) else {}
    if not isinstance(data, dict):
        return {}, {}
    if "pack" in data or "clip_context" in data:
        pack = data.get("pack") if isinstance(data.get("pack"), dict) else {}
        ctx = data.get("clip_context") if isinstance(data.get("clip_context"), dict) else {}
        return pack, ctx
    return data, {}


class AutomationManager:
    def __init__(self, app: Any):
        self.app = app
        self.db = app.db
        self.auth = AuthManager(app)
        self.runner = JobRunner(self)
        self.scheduler = AutomationScheduler(self)
        self.autopilot = AutoPilotEngine(self)
        self.stopping = False
        self._started = False
        self._listeners: list[Listener] = []
        self._lock = threading.RLock()
        self._unsub_render = bus.subscribe(bus.RENDER_COMPLETED, self._on_render_completed)
        self._unsub_clip = bus.subscribe(bus.CLIP_COMPLETED, self._on_clip_completed)

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._started:
            return
        self.stopping = False
        self._seed_recipes()
        self.scheduler.start()
        self.runner.start()
        self._started = True
        try:
            self.app.logger.info("[Automation] manager started")
        except Exception:
            pass

    def stop(self) -> None:
        self.stopping = True
        # Always release the process-wide event subscriptions first. A manager
        # that was constructed but never started (or whose start() failed) must
        # not keep receiving render/clip events after its database is gone.
        try:
            self._unsub_render()
        except Exception:
            pass
        try:
            self._unsub_clip()
        except Exception:
            pass
        # The auth manager listens on the same bus to keep its token snapshot
        # fresh; release that subscription too.
        try:
            self.auth.close()
        except Exception:
            pass
        if not self._started:
            return
        try:
            self.scheduler.stop()
        except Exception:
            pass
        try:
            self.runner.stop()
        except Exception:
            pass
        self._started = False

    # -- events ------------------------------------------------------------
    def add_listener(self, fn: Listener) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(fn)

        def _remove() -> None:
            with self._lock:
                try:
                    self._listeners.remove(fn)
                except ValueError:
                    pass
        return _remove

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(event, dict(payload))
            except Exception:
                pass
        bus.emit(event, dict(payload))

    def notify_job(self, job_id: str, kind: str, extra: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"job_id": job_id, "kind": kind}
        if extra:
            payload.update(extra)
        if kind in ("done", "retry", "status") or kind == "progress":
            job = self.db.automation_get_job(job_id)
            if job:
                payload["status"] = str(job.get("status") or "")
                payload["platform"] = str(job.get("platform") or "")
                payload["run_id"] = str(job.get("run_id") or "")
        self._emit("job", payload)

    def notify_global(self, event: str, payload: dict[str, Any]) -> None:
        self._emit(event, payload)

    def on_job_finished(self, job_id: str) -> None:
        job = self.db.automation_get_job(job_id)
        if not job:
            return
        self.refresh_run(str(job["run_id"]))

    def refresh_run(self, run_id: str) -> None:
        run = self.db.automation_get_run(run_id)
        if not run:
            return
        statuses = self.db.automation_job_statuses(run_id)
        derived = derive_run_status(statuses)
        current = str(run.get("status") or "")
        if derived == RunStatus.QUEUED.value and current == RunStatus.READY.value:
            derived = RunStatus.READY.value        # review runs wait for human Start
        if derived != current:
            self.db.automation_update_run(run_id, status=derived, completed_at=(now_iso() if derived in (RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.PARTIAL_SUCCESS.value, RunStatus.CANCELLED.value) else ""))
        self._emit("run", {"run_id": run_id, "status": derived})

    # -- content handoff (spec 3.1 / 3.2) ----------------------------------
    def _on_render_completed(self, payload: dict[str, Any]) -> None:
        """Auto Clip Studio finished a clip MP4."""
        self._on_content_finished(payload, default_source="acs")

    def _on_clip_completed(self, payload: dict[str, Any]) -> None:
        """Content Clipper AI finished an exported clip MP4."""
        self._on_content_finished(payload, default_source="cca")

    def _handoff_gate_open(self, source: str) -> bool:
        """Per-source handoff switch so ACS and CCA can be toggled independently."""
        key = "aut_acs_handoff" if source == "acs" else "aut_cca_handoff"
        try:
            return bool(self.app.config.get(key, True))
        except Exception:
            return True

    def _on_content_finished(self, payload: dict[str, Any],
                             default_source: str = "acs") -> None:
        """Single intake for both content sources (spec 3.3 - one engine).

        Validates readiness *before* anything reaches the queue (spec 3.6),
        preserves the source identity on the media row (spec 3.4), and is
        idempotent so a replayed render/clip event can never duplicate content
        (spec 7.4). Failures here can never turn a successful render into a
        render failure (spec 29).
        """
        try:
            source = str(payload.get("source") or default_source).lower()
            if source not in ("acs", "cca", "manual"):
                source = default_source
            if not self._handoff_gate_open(source):
                return
            path = str(payload.get("output_path") or "")
            if not path:
                return
            if self.db.automation_media_by_path(path):
                return                      # replayed event: never duplicate media
            # Persist the clip context snapshot next to the pack so Studio can
            # prove which transcript the AI may use for THIS clip (spec 4.4).
            content: dict[str, Any] = {}
            pack = payload.get("content_pack")
            if isinstance(pack, dict) and pack:
                content["pack"] = pack
            clip_ctx = payload.get("clip_context")
            if isinstance(clip_ctx, dict) and clip_ctx:
                content["clip_context"] = dict(clip_ctx)
            pack_json = json.dumps(content, ensure_ascii=False) if content else "{}"
            media_id = new_id("med_")
            self.db.automation_add_media(
                media_id, path, source=source, title=str(payload.get("title") or ""),
                project_id=str(payload.get("project_id") or ""),
                clip_id=str(payload.get("clip_id") or ""),
                batch_id=str(payload.get("batch_id") or ""), content_pack=pack_json)
            ok, reason = validate_media_file(path)
            if not ok:
                self.db.automation_update_media(media_id, "rejected", reject_reason=reason)
                self._emit("media", {"media_id": media_id, "path": path, "source": source,
                                     "status": "rejected", "reason": reason})
                self.notify_global("media_rejected", {"media_id": media_id, "path": path,
                                                      "source": source, "reason": reason})
                return
            self.db.automation_update_media(media_id, "ready")
            self._emit("media", {"media_id": media_id, "path": path, "source": source,
                                 "status": "ready", "title": payload.get("title")})
            try:
                self.app.logger.info("[Automation] content handoff source=%s media=%s",
                                     source, media_id)
            except Exception:
                pass
            bus.emit(bus.MEDIA_READY, {"media_id": media_id, "source": source, "path": path})
            media_row = self.db.automation_media_by_path(path)
            if media_row:
                self.autopilot.on_media_ready(media_row)
        except sqlite3.ProgrammingError as exc:
            # Shutdown race: the event was already in flight when the database
            # was closed. No content was lost, so this is not a handoff failure.
            if "closed database" not in str(exc):
                try:
                    self.app.logger.error("[Automation] content handoff failed: %s",
                                          redact(str(exc)))
                except Exception:
                    pass
        except Exception:
            try:
                self.app.logger.error("[Automation] content handoff failed: %s",
                                      redact(traceback.format_exc()[-800:]))
            except Exception:
                pass

    def list_media(self, status: str = "ready", source: str = "") -> list[dict[str, Any]]:
        """Ready/rejected content, optionally narrowed to one source (spec 5.1)."""
        return self.db.automation_list_media(status=status, source=source)

    def media_counts(self) -> dict[str, int]:
        """Real per-source counts for the Studio filter (never hardcoded)."""
        counts: dict[str, int] = {"acs": 0, "cca": 0, "manual": 0, "total": 0,
                                  "rejected": 0}
        try:
            for row in self.db.automation_list_media("", limit=2000):
                source = str(row.get("source") or "acs")
                if str(row.get("status") or "") == "ready":
                    counts[source] = counts.get(source, 0) + 1
                    counts["total"] += 1
                elif str(row.get("status") or "") == "rejected":
                    counts["rejected"] += 1
        except Exception:
            pass
        return counts

    def rejected_media(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.db.automation_list_media("rejected", limit=limit)

    def register_media(self, path: str, title: str = "", source: str = "manual") -> str:
        """Manual import follows the exact same readiness gate + Auto-Pilot path."""
        self._on_content_finished({"output_path": str(path), "title": title,
                                   "source": source}, default_source=source)
        row = self.db.automation_media_by_path(str(path))
        return str(row["id"]) if row else ""

    # -- auto-pilot rules (spec 6) ----------------------------------------
    def list_rules(self) -> list[dict[str, Any]]:
        return self.autopilot.list_rules()

    def rule_status(self, rule_id: str) -> dict[str, Any]:
        return self.autopilot.status(rule_id)

    def save_rule(self, *, name: str, sources: list[str], platforms: list[str],
                  accounts: list[str] | None = None, config: dict[str, Any] | None = None,
                  enabled: bool = False, rule_id: str = "") -> str:
        rid = self.autopilot.save_rule(name=name, sources=sources, platforms=platforms,
                                       accounts=accounts, config=config, enabled=enabled,
                                       rule_id=rule_id)
        self._emit("rules", {"rule_id": rid, "kind": "saved"})
        return rid

    def set_rule_enabled(self, rule_id: str, enabled: bool) -> dict[str, Any]:
        result = self.autopilot.set_enabled(rule_id, enabled)
        self._emit("rules", {"rule_id": rule_id, "kind": "enabled" if enabled else "disabled",
                             "ok": result.get("ok")})
        return result

    def delete_rule(self, rule_id: str) -> None:
        self.autopilot.delete_rule(rule_id)
        self._emit("rules", {"rule_id": rule_id, "kind": "deleted"})

    def duplicate_rule(self, rule_id: str) -> str | None:
        new_rule = self.autopilot.duplicate_rule(rule_id)
        self._emit("rules", {"rule_id": new_rule, "kind": "duplicated"})
        return new_rule

    def run_rule_now(self, rule_id: str, limit: int = 20) -> dict[str, Any]:
        """Explicit manual/backfill trigger (spec 6.3 - only user action backfills)."""
        result = self.autopilot.run_rule_now(rule_id, limit=limit)
        self._emit("rules", {"rule_id": rule_id, "kind": "manual_run"})
        return result

    # -- accounts passthrough ---------------------------------------------
    def account_statuses(self) -> dict[str, dict[str, Any]]:
        return self.auth.statuses()

    def connect_platform(self, platform: str, **kwargs: Any) -> dict[str, Any]:
        result = self.auth.connect(platform, **kwargs)
        self._emit("auth", {"platform": platform, "status": "connected"})
        return result

    def disconnect_account(self, account_id: str) -> None:
        self.auth.disconnect(account_id)
        self._emit("auth", {"account_id": account_id, "status": "disconnected"})

    def capabilities(self, account_id: str) -> dict[str, Any]:
        return self.auth.capabilities(account_id)

    # -- runs / jobs --------------------------------------------------------
    def create_run(self, *, name: str, media_items: list[dict[str, Any]],
                   platforms: list[dict[str, Any]], mode: str = "now",
                   scheduled_at: str = "", content_pack: dict[str, Any] | None = None,
                   overrides: dict[str, Any] | None = None, recipe_id: str = "",
                   review: bool = False, source: str = "manual", rule_id: str = "",
                   intent: str = "publish", stagger: dict[str, Any] | None = None,
                   shared_pack: bool = False, on_duplicate: str = "raise") -> dict[str, Any] | None:
        """Create one run with platform child jobs (spec §12/§65).

        ``content_pack`` is only applied to *every* clip when ``shared_pack`` is
        set explicitly (spec §6.6/§7.7). Each clip's own ``content`` always wins,
        so one video's metadata can never silently become another video's title.

        A (media, platform, account) that is already waiting in the queue is
        skipped instead of queued a second time. When *nothing* is left to queue
        the run is not created at all: ``on_duplicate="raise"`` (default) raises
        :class:`DuplicateInQueueError` so the caller can tell the user, while
        ``on_duplicate="skip"`` returns ``None`` for background callers.
        """
        if not media_items:
            raise AutomationError("no media", user_message="Pilih minimal satu clip/media.")
        if not platforms:
            raise AutomationError("no platforms", user_message="Pilih minimal satu platform.")
        missing: list[str] = []
        resolved: list[dict[str, str]] = []
        accounts = self.db.automation_list_accounts()
        for entry in platforms:
            platform = str(entry.get("platform") or "").lower()
            if platform not in PLATFORMS:
                raise AutomationError(f"unknown platform {platform}", user_message="Platform tidak dikenal.")
            account_id = str(entry.get("account_id") or "")
            if not account_id:
                candidates = [a for a in accounts if a.get("platform") == platform and a.get("status") not in ("disconnected",)]
                connected = [a for a in candidates if a.get("status") == "connected"]
                pool = connected or candidates
                pick = pool[0] if pool else None
                # Browser Session accounts own one profile each, so a stale
                # duplicate can shadow the account the user actually logged into
                # (all of them share the same display name). Never auto-pick a
                # browser account without a live session while a sibling on the
                # same platform does have one.
                if pick is not None and str(pick.get("auth_mode") or "") == "browser":
                    try:
                        from chopster.automation.browser import session as bsession
                        if not bsession.session_valid(platform, str(pick.get("id") or "")):
                            live = [a for a in pool
                                    if str(a.get("auth_mode") or "") == "browser"
                                    and bsession.session_valid(platform, str(a.get("id") or ""))]
                            if live:
                                pick = live[0]
                    except Exception:
                        pass
                if pick is None:
                    missing.append(platform)
                    continue
                account_id = str(pick["id"])
            resolved.append({"platform": platform, "account_id": account_id, "privacy": str(entry.get("privacy") or "")})
        if missing:
            labels = ", ".join(missing)
            raise AutomationError(f"accounts missing: {labels}", user_message=f"Platform belum terhubung: {labels}. Sambungkan dulu di Accounts.")

        run_id = new_id("run_")
        requested = str(mode or "now").lower()
        if requested not in ("now", "schedule", "stagger"):
            requested = "now"
        job_schedule: dict[str, str] = {}
        if requested == "stagger":
            slots = self._stagger_slots(stagger or {}, len(media_items))
            for item, slot in zip(media_items, slots):
                job_schedule[str(item.get("path") or "")] = slot.strftime("%Y-%m-%d %H:%M")
            scheduled_at = slots[0].strftime("%Y-%m-%d %H:%M")
        elif requested == "schedule" and not scheduled_at:
            requested = "now"
        run_mode = "schedule" if requested in ("schedule", "stagger") else "now"
        scheduled = bool(run_mode == "schedule")
        status = RunStatus.READY.value if review else RunStatus.QUEUED.value
        pack_norm = normalize_content_pack(content_pack or {})
        meta = {"job_count": len(media_items) * len(resolved), "platforms": [r["platform"] for r in resolved], "media": [str(m.get("title") or Path(str(m.get("path"))).name) for m in media_items], "source": source, "review": bool(review)}
        meta["rule_id"] = rule_id
        meta["stagger"] = bool(job_schedule)
        self.db.automation_create_run(run_id, name=name or "Automation", status=status, schedule_mode=run_mode, scheduled_at=scheduled_at if scheduled else "", repeat_rule="", recipe_id=recipe_id, metadata=json.dumps(meta, ensure_ascii=False))

        ov = overrides or {}
        audience = ov.get("made_for_kids")
        pack_payload = {
            "pack": pack_norm.to_dict(),
            "privacy": ov.get("privacy", ""),
            "title_override": ov.get("title_override", ""),
            "description_override": ov.get("description_override", ""),
            "caption_override": ov.get("caption_override", ""),
            "extra": ov.get("extra", {}),
            # YouTube audience declaration travels with the job snapshot so the
            # adapter never falls back to a silent default (spec §9).
            "made_for_kids": audience if isinstance(audience, bool) else None,
            "category_id": str(ov.get("category_id") or ""),
            "shared_pack": bool(shared_pack),
        }
        skipped_duplicates: list[dict[str, Any]] = []
        created_jobs = 0
        for media in media_items:
            path = str(media.get("path") or "")
            if not path:
                continue
            title = str(media.get("title") or Path(path).name)
            clip_id = str(media.get("clip_id") or "")
            try:
                fp = media_fingerprint(path)
            except Exception:
                fp = ""
            job_pack = dict(pack_payload)
            per_media = media.get("content") if isinstance(media.get("content"), dict) else None
            if per_media:
                job_pack["pack"] = normalize_content_pack(per_media).to_dict()
                job_pack["pack_source"] = "per_media"
            elif len(media_items) > 1 and not shared_pack:
                # Several different videos selected but no per-video metadata:
                # do NOT stamp one title/description onto all of them.
                job_pack["pack"] = normalize_content_pack({}).to_dict()
                job_pack["pack_source"] = "missing"
            else:
                job_pack["pack_source"] = "shared" if len(media_items) > 1 else "single"
            # Every job may always carry its own clip's title: that is the
            # selected clip's identity, never another video's metadata, so it is
            # safe even when shared_pack is off (pack_source == "missing").
            # Legacy runs without any pack then still fail the pre-flight
            # instead of silently publishing an empty title (spec §8.4).
            pack_for_job = job_pack.get("pack")
            if isinstance(pack_for_job, dict) and not str(pack_for_job.get("title") or "").strip():
                pack_for_job["title"] = title
            clip_ctx = media.get("clip_context")
            if isinstance(clip_ctx, dict) and clip_ctx:
                job_pack["clip_context"] = clip_ctx
            media_slot = job_schedule.get(path, scheduled_at if scheduled else "")
            for target in resolved:
                job_id = new_id("job_")
                ikey = (idempotency_key(target["platform"], target["account_id"], fp, intent)
                        if fp else "")
                if ikey and self.db.automation_find_pending(ikey):
                    # The same media is already waiting for this platform+account
                    # (queued, uploading, or parked for the user). A second card
                    # would only duplicate it — skip it and report below.
                    skipped_duplicates.append({
                        "platform": target["platform"],
                        "file": Path(path).name,
                        "title": title,
                    })
                    continue
                self.db.automation_add_job(job_id, run_id, path, target["platform"], clip_id=clip_id, media_title=title, media_fingerprint=fp, account_id=target["account_id"], publish_mode=run_mode, scheduled_at=media_slot, content_pack=json.dumps(job_pack, ensure_ascii=False), status=(JobStatus.SCHEDULED.value if scheduled else JobStatus.QUEUED.value), max_attempts=max(1, int(self.app.config.get("aut_retry_limit") or 3)), source=source, idempotency_key=ikey, rule_id=rule_id)
                created_jobs += 1
        if not created_jobs:
            # Nothing new to queue: never leave an empty run card behind.
            self.db.automation_delete_run(run_id)
            if str(on_duplicate) == "skip":
                return None
            raise DuplicateInQueueError(
                f"semua {len(skipped_duplicates)} job sudah ada di queue",
                user_message=DuplicateInQueueError.user_message)
        if skipped_duplicates:
            meta["skipped_duplicates"] = skipped_duplicates
            try:
                self.db.automation_update_run(
                    run_id, metadata=json.dumps(meta, ensure_ascii=False))
            except Exception:
                pass
        self.refresh_run(run_id)
        self._emit("run", {"run_id": run_id, "kind": "created", "status": status})
        bus.emit(bus.AUTOMATION_CREATED, {"run_id": run_id})
        if status == RunStatus.QUEUED.value:
            bus.emit(bus.AUTOMATION_STARTED, {"run_id": run_id})
        return self.db.automation_get_run(run_id) or {}

    def _stagger_slots(self, stagger: dict[str, Any], count: int) -> list[datetime]:
        """One publish time per clip (spec 5.6C) — the single source of truth.

        The UI preview calls the same planner, so what the user sees before
        pressing Start is exactly what gets written to the queue.
        """
        start = parse_local_time(str(stagger.get("start") or "")) or datetime.now()
        hours = stagger.get("active_hours") or None
        # An explicitly supplied interval is validated by the planner instead of
        # being silently replaced, so a bad value can never fake a valid plan.
        raw_interval = stagger.get("interval_minutes")
        interval = 120 if raw_interval in (None, "") else int(raw_interval)
        try:
            return compute_staggered_slots(
                start=start,
                count=max(1, int(count)),
                interval_minutes=interval,
                active_hours=(int(hours[0]), int(hours[1])) if hours else None,
                active_days=stagger.get("active_days") or None,
            )
        except PlanError as exc:
            raise AutomationError(f"stagger plan failed: {exc}",
                                  user_message=exc.user_message) from exc

    def preview_stagger(self, stagger: dict[str, Any], count: int) -> list[datetime]:
        """Public wrapper used by the Studio UI to preview computed slots."""
        return self._stagger_slots(stagger, count)

    def start_ready_run(self, run_id: str) -> bool:
        run = self.db.automation_get_run(run_id)
        if not run or str(run.get("status")) != RunStatus.READY.value:
            return False
        self.db.automation_update_run(run_id, status=RunStatus.QUEUED.value)
        self._emit("run", {"run_id": run_id, "kind": "started", "status": RunStatus.QUEUED.value})
        bus.emit(bus.AUTOMATION_STARTED, {"run_id": run_id})
        return True

    def cancel_run(self, run_id: str) -> None:
        run = self.db.automation_get_run(run_id)
        if not run:
            return
        for job in self.db.automation_list_jobs(run_id=run_id, limit=1000):
            status = str(job.get("status") or "")
            if status in (JobStatus.PUBLISHED.value, JobStatus.CANCELLED.value, JobStatus.FAILED.value):
                continue
            job_id = str(job["id"])
            in_flight = status in (JobStatus.AUTH_CHECK.value, JobStatus.PREPARING.value, JobStatus.UPLOADING.value, JobStatus.PROCESSING.value, JobStatus.VERIFYING.value)
            if in_flight and self.runner.is_active(job_id):
                # A worker still owns this row: signal it and let it settle the
                # row as CANCELLED at its next checkpoint (same contract as
                # cancel_job). Writing CANCELLED here would race the worker's own
                # final status write and could show a cancelled job as PUBLISHED.
                self.runner.cancel_job(job_id)
                self.db.automation_update_job(job_id, last_activity="membatalkan…")
                continue
            if in_flight or status in (JobStatus.QUEUED.value, JobStatus.SCHEDULED.value, JobStatus.PAUSED.value, JobStatus.RETRYING.value, JobStatus.ACTION_REQUIRED.value):
                self.db.automation_update_job(job_id, status=JobStatus.CANCELLED.value, last_activity="dibatalkan")
                self.notify_job(job_id, "done", {"status": JobStatus.CANCELLED.value})
        if str(run.get("status")) != RunStatus.CANCELLED.value:
            self.db.automation_update_run(run_id, status=RunStatus.CANCELLED.value, completed_at=now_iso())
        self._emit("run", {"run_id": run_id, "kind": "cancelled", "status": RunStatus.CANCELLED.value})

    def cancel_job(self, job_id: str) -> None:
        job = self.db.automation_get_job(job_id)
        if not job:
            return
        status = str(job.get("status") or "")
        if status in (JobStatus.PUBLISHED.value, JobStatus.CANCELLED.value):
            return
        if status in (JobStatus.AUTH_CHECK.value, JobStatus.PREPARING.value, JobStatus.UPLOADING.value, JobStatus.PROCESSING.value, JobStatus.VERIFYING.value):
            self.runner.cancel_job(job_id)
            # Show the intent immediately — the worker stops at its next
            # checkpoint, so the row would otherwise stay "Running" for a while.
            self.db.automation_update_job(job_id, last_activity="membatalkan…")
            return
        self.db.automation_update_job(job_id, status=JobStatus.CANCELLED.value, last_activity="dibatalkan")
        self.notify_job(job_id, "done", {"status": JobStatus.CANCELLED.value})
        self.refresh_run(str(job.get("run_id") or ""))

    def retry_job(self, job_id: str) -> bool:
        job = self.db.automation_get_job(job_id)
        if not job:
            return False
        if str(job.get("status")) not in (JobStatus.FAILED.value, JobStatus.ACTION_REQUIRED.value, JobStatus.CANCELLED.value):
            return False
        if str(job.get("verification_status") or "") == "DRAFT_READY":
            # A draft was already delivered to the platform inbox. Re-running the
            # job would create a duplicate draft, so ask the user to finish (or
            # delete) that draft first.
            self.db.automation_update_job(
                job_id, status=JobStatus.ACTION_REQUIRED.value, error_code="DRAFT_READY",
                reason="draft_ready",
                error_message=("Draft sudah dikirim ke aplikasi platform. Publish dari aplikasi; "
                               "untuk mengirim ulang, hapus draft itu dulu."),
                last_activity="draft sudah dikirim",
            )
            self.notify_job(job_id, "status")
            return False
        self.db.automation_update_job(job_id, status=JobStatus.QUEUED.value, scheduled_at="", attempt=0, error_code="", error_message="", reason="", last_activity="manual retry")
        self.notify_job(job_id, "status")
        run_id = str(job.get("run_id") or "")
        run = self.db.automation_get_run(run_id)
        if run and str(run.get("status")) in (RunStatus.FAILED.value, RunStatus.ACTION_REQUIRED.value, RunStatus.CANCELLED.value, RunStatus.PARTIAL_SUCCESS.value):
            self.db.automation_update_run(run_id, status=RunStatus.QUEUED.value, completed_at="")
        self.refresh_run(run_id)
        return True

    def pause_job(self, job_id: str) -> bool:
        return self.db.automation_update_job(job_id, status=JobStatus.PAUSED.value, last_activity="paused")

    def resume_job(self, job_id: str) -> bool:
        job = self.db.automation_get_job(job_id)
        if not job or str(job.get("status")) != JobStatus.PAUSED.value:
            return False
        # Browser-lane jobs only resume when the session can actually continue;
        # a blind resume would hand the worker a logged-out page.
        try:
            acct = next((a for a in self.db.automation_list_accounts()
                         if str(a.get("id")) == str(job.get("account_id") or "")), None)
            if acct and str(acct.get("auth_mode") or "") == "browser":
                from chopster.automation.browser.session import session_valid
                if not session_valid(str(job.get("platform") or ""),
                                     str(job.get("account_id") or "")):
                    self.db.automation_update_job(
                        job_id, status=JobStatus.ACTION_REQUIRED.value, error_code="AUTH_REQUIRED",
                        reason="login_required",
                        error_message=("Sesi browser belum login. Buka Automation > Accounts > Login, "
                                       "lalu klik Lanjutkan lagi."),
                        last_activity="menunggu login browser",
                    )
                    self.notify_job(job_id, "status")
                    return False
        except Exception:
            # Fail open: if the session probe itself errors we still let the job
            # resume; the AUTH_CHECK gate before publishing will park it safely.
            pass
        return self.db.automation_update_job(job_id, status=JobStatus.QUEUED.value, last_activity="resumed")

    def verify_and_resume_job(self, job_id: str) -> tuple[bool, str]:
        """Actionable ``Cek & Lanjut`` for a job parked as ACTION_REQUIRED.

        Returns ``(ok, message)`` for the UI. It never re-sends a video blindly:
        an already-delivered draft is refused, a browser session is re-checked
        first, and everything else is handed back to :meth:`retry_job`.
        """
        job = self.db.automation_get_job(job_id)
        if not job:
            return False, "Job tidak ditemukan."
        if str(job.get("status") or "") != JobStatus.ACTION_REQUIRED.value:
            return False, "Job ini tidak sedang menunggu tindakan."
        if str(job.get("verification_status") or "") == "DRAFT_READY":
            return False, ("Draft sudah dikirim ke aplikasi platform. Selesaikan publish di aplikasi; "
                           "untuk mengirim ulang, hapus draft itu dulu.")
        try:
            acct = next((a for a in self.db.automation_list_accounts()
                         if str(a.get("id")) == str(job.get("account_id") or "")), None)
        except Exception:
            acct = None
        if acct and str(acct.get("auth_mode") or "") == "browser":
            try:
                from chopster.automation.browser import session as bsession
                if not bsession.playwright_installed():
                    return False, ("Browser automation belum siap. Install paketnya: pip install playwright, "
                                   "lalu jalankan python -m playwright install chromium.")
                platform = str(job.get("platform") or "")
                account_id = str(job.get("account_id") or "")
                if not bsession.session_valid(platform, account_id):
                    return False, ("Sesi browser belum login. Buka Automation → Accounts → Kelola → "
                                   "'Login / Verifikasi browser', selesaikan di jendela Chopster, "
                                   "lalu klik 'Cek & Lanjut' lagi.")
            except Exception:
                pass
        if self.retry_job(job_id):
            return True, "Job dimasukkan kembali ke antrean."
        return False, "Job tidak bisa dilanjutkan otomatis. Periksa detail error di kolom Detail."

    # -- diagnostics --------------------------------------------------------
    def _adapter_label(self, platform: str, tiktok_mode: str) -> str:
        if platform == "youtube":
            return "API resmi (YouTube Data API v3)"
        if platform == "facebook":
            return "API resmi (Meta Graph) / Browser Session (fallback)"
        if platform == "instagram":
            return "API resmi (Instagram Graph) / Browser Session (fallback)"
        if platform == "tiktok":
            mode = "Inbox draft resmi" if str(tiktok_mode) == "inbox" else "Direct Post resmi"
            return f"API resmi TikTok ({mode}) / Browser Session (fallback)"
        return "Tidak diketahui"

    def _last_platform_failure(self, platform: str) -> dict[str, Any]:
        try:
            rows = self.db.automation_list_jobs(
                statuses=[JobStatus.ACTION_REQUIRED.value, JobStatus.FAILED.value], limit=200)
        except Exception:
            return {}
        for row in rows:
            if str(row.get("platform") or "") != platform:
                continue
            return {
                "job_id": str(row.get("id") or ""),
                "status": str(row.get("status") or ""),
                "category": str(row.get("error_code") or ""),
                "reason": str(row.get("reason") or ""),
                "message": redact(str(row.get("error_message") or ""))[:300],
                "at": str(row.get("updated_at") or ""),
            }
        return {}

    def diagnostics(self) -> dict[str, Any]:
        """Read-only readiness report for the Automation → Diagnostics panel.

        Never opens a browser and never posts: it only reports Playwright
        availability, the configured channel, the recorded policy review, the
        per-account session state and the last failure per platform (spec §9).
        """
        def _cfg(key: str, default: Any = None) -> Any:
            try:
                return self.app.config.get(key)
            except Exception:
                return default

        review = _cfg("aut_policy_review") or {}
        if not isinstance(review, dict):
            review = {}
        bsession = None
        try:
            from chopster.automation.browser import session as bsession  # noqa: F811
            playwright = bool(bsession.playwright_installed())
        except Exception:
            bsession = None
            playwright = False
        tiktok_mode = str(_cfg("aut_tiktok_mode") or "inbox")
        try:
            soft_cap = int(_cfg("aut_browser_soft_cap") or 0)
        except Exception:
            soft_cap = 0
        platforms: list[dict[str, Any]] = []
        for platform in PLATFORMS:
            accounts = self.db.automation_list_accounts(platform)
            entry: dict[str, Any] = {
                "platform": platform,
                "adapter": self._adapter_label(platform, tiktok_mode),
                "accounts": len(accounts),
                "policy_reviewed": review.get(platform) or {},
                "last_failure": self._last_platform_failure(platform),
                "sessions": [],
            }
            for a in accounts:
                if str(a.get("auth_mode") or "oauth") != "browser":
                    continue
                valid: bool | None = None
                if playwright and bsession is not None:
                    try:
                        valid = bool(bsession.session_valid(platform, str(a.get("id") or "")))
                    except Exception:
                        valid = None
                used = 0
                if soft_cap > 0:
                    try:
                        since = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
                        used = int(self.db.automation_count_account_jobs_since(
                            str(a.get("id") or ""), since) or 0)
                    except Exception:
                        used = 0
                entry["sessions"].append({
                    "account_id": a.get("id"),
                    "display_name": a.get("display_name") or "",
                    "session": "siap" if valid is True else ("belum login" if valid is False else "tidak dapat diperiksa"),
                    "used_24h": used,
                    "cap_remaining": (max(0, soft_cap - used) if soft_cap > 0 else None),
                })
            platforms.append(entry)
        profiles = ""
        try:
            if bsession is not None:
                profiles = str(bsession.profiles_root())
        except Exception:
            profiles = ""
        return {
            "playwright": playwright,
            "channel": str(_cfg("aut_browser_channel") or ""),
            "headless": bool(_cfg("aut_browser_headless")),
            "test_mode": bool(_cfg("aut_browser_test_mode")),
            "soft_cap": soft_cap,
            "tiktok_mode": tiktok_mode,
            "profiles_dir": profiles,
            "platforms": platforms,
        }

    def delete_job(self, job_id: str) -> bool:
        """Delete a finished job record from the queue and History.

        In-flight jobs are refused with a user-facing message so the worker's
        state can never be orphaned — cancel first, then delete.
        """
        job = self.db.automation_get_job(job_id)
        if not job:
            return False
        status = str(job.get("status") or "").upper()
        if status not in DELETABLE_JOB_STATUSES:
            raise AutomationError(
                f"job {job_id} still active ({status})",
                user_message="Job ini masih berjalan — batalkan dulu dengan ✕ Batal "
                              "sebelum menghapusnya.")
        ok = bool(self.db.automation_delete_job(job_id))
        run_id = str(job.get("run_id") or "")
        if run_id:
            # Without jobs there is nothing left to derive a status from, so the
            # emptied run is closed instead of resurfacing as an empty Draft
            # card under "Active automations".
            if self.db.automation_list_jobs(run_id=run_id, limit=1):
                self.refresh_run(run_id)
            else:
                self.db.automation_update_run(run_id, status=RunStatus.CANCELLED.value,
                                              completed_at=now_iso())
                self._emit("run", {"run_id": run_id, "kind": "emptied",
                                   "status": RunStatus.CANCELLED.value})
        if ok:
            self._emit("run", {"run_id": run_id, "kind": "deleted",
                               "job_id": job_id})
        return ok

    def job_counts(self) -> dict[str, int]:
        return self.db.automation_job_counts()

    def history(self, limit: int = 300) -> list[dict[str, Any]]:
        return self.db.automation_history(limit)

    # -- recipes ------------------------------------------------------------
    def _seed_recipes(self) -> None:
        if self.db.automation_list_recipes():
            return
        for item in BUILTIN_RECIPES:
            self.db.automation_save_recipe(new_id("rcp_"), item["name"], json.dumps(item["config"], ensure_ascii=False), enabled=1)

    def list_recipes(self) -> list[dict[str, Any]]:
        out = []
        for r in self.db.automation_list_recipes():
            row = dict(r)
            row["config_obj"] = safe_json_loads(row.get("config"), {})
            out.append(row)
        return out

    def save_recipe(self, name: str, config: dict[str, Any], recipe_id: str = "") -> str:
        rid = recipe_id or new_id("rcp_")
        self.db.automation_save_recipe(rid, name or "Recipe", json.dumps(config, ensure_ascii=False), enabled=1)
        return rid

    def delete_recipe(self, recipe_id: str) -> None:
        self.db.automation_delete_recipe(recipe_id)

    def set_recipe_enabled(self, recipe_id: str, enabled: bool) -> None:
        self.db.automation_set_recipe_enabled(recipe_id, 1 if enabled else 0)

    def run_recipe(self, recipe_id: str, trigger: str = "manual") -> dict[str, Any] | None:
        recipe = self.db.automation_get_recipe(recipe_id)
        if not recipe:
            return None
        cfg = safe_json_loads(recipe.get("config"), {})
        media = self.db.automation_list_media(status="ready", limit=50)
        # Drop rows whose file vanished.
        media = [m for m in media if Path(str(m.get("media_path") or "")).exists()]
        if not media:
            try:
                self.app.logger.info("[Automation] recipe '%s' skipped: no ready media", recipe.get("name"))
            except Exception:
                pass
            if trigger == "manual":
                raise AutomationError("no media", user_message="Belum ada clip siap publish. Render dulu atau tambah media.")
            return None
        # max_per_run <= 0 means "every ready clip" (the template's "Semua").
        max_per = int(cfg.get("max_per_run") or 0)
        chosen = media if max_per <= 0 else media[:max_per]
        platforms = [{"platform": p} for p in (cfg.get("platforms") or []) if p in PLATFORMS]
        if not platforms:
            # empty platform list = every connected platform
            platforms = [{"platform": p} for p in PLATFORMS if any(a.get("platform") == p and a.get("status") == "connected" for a in self.db.automation_list_accounts())]
        if not platforms:
            if trigger == "manual":
                raise AutomationError("no platforms", user_message="Tidak ada platform terhubung.")
            return None
        # Only publish to platforms that actually have a connected account.
        # A recipe listing platforms without accounts should skip cleanly
        # (like "no ready media") instead of raising a scheduler traceback.
        try:
            connected = {str(a.get("platform")) for a in self.db.automation_list_accounts()
                         if a.get("status") == "connected"}
        except Exception:
            connected = set()
        avail = [p for p in platforms if p.get("platform") in connected]
        if not avail:
            if trigger == "manual":
                raise AutomationError(
                    "no platforms",
                    user_message="Tidak ada platform terhubung untuk preset ini. Sambungkan akun dulu di Accounts.")
            try:
                self.app.logger.info(
                    "[Automation] recipe '%s' skipped: no connected account for %s",
                    recipe.get("name"),
                    ", ".join(str(p.get("platform") or "?") for p in platforms))
            except Exception:
                pass
            return None
        platforms = avail
        review = bool(cfg.get("review"))
        # A scheduled preset stores its wall-clock "time": run it at the next
        # occurrence instead of silently falling back to "now". When the
        # Scheduler itself fires the recipe, the time has already arrived — the
        # upload must happen now, not be deferred to tomorrow.
        _sched = str(cfg.get("scheduled_at") or "")
        if (str(cfg.get("mode") or "") == "schedule" and not _sched
                and str(trigger) != "schedule"):
            _sched = self._next_occurrence(str(cfg.get("time") or "19:00"))
        # The preset owns the publish settings, so forward them to the jobs the
        # preset creates. Without this the YouTube audience answer from the
        # template was silently dropped: every recipe job carried
        # made_for_kids=None and was parked with "Deklarasi audiens YouTube
        # belum dipilih" — while the very same template published fine from the
        # ACS 1-click path (which always forwarded the overrides).
        _audience = cfg.get("made_for_kids")
        # Each preset clip keeps its OWN metadata: the stored pack when the user
        # generated one, otherwise a fresh AI pack (same generator as the ACS
        # one-click template) and the clip's transcript context. Without this a
        # preset job ran with an empty pack and YouTube received the clip title
        # as its description.
        ai_auto = bool(cfg.get("ai_auto", True))
        cfg_base = self._template_ai_config() if ai_auto else {}
        ai_language = str(cfg.get("ai_language") or "id")
        media_items: list[dict[str, Any]] = []
        for m in chosen:
            pack, ctx = split_media_content(m.get("content_pack"))
            item: dict[str, Any] = {
                "path": str(m["media_path"]),
                "title": str(m.get("title") or ""),
                "clip_id": str(m.get("clip_id") or ""),
            }
            if ctx:
                item["clip_context"] = ctx
            if not str(pack.get("title") or pack.get("titles")
                       or pack.get("description_long") or pack.get("description") or "").strip():
                # No usable metadata yet. Only ask the AI when this clip has its
                # own transcript, so a preset run never starts an unexpected
                # transcription (and tests/offline runs stay instant). A manual
                # run is triggered from the UI thread, so it skips the AI here;
                # the queue generates the same pack in its worker thread.
                if (ai_auto and str(trigger) != "manual"
                        and isinstance(ctx.get("segments"), list) and ctx.get("segments")):
                    try:
                        pack = self._generate_template_pack(
                            dict(m), cfg_base, language=ai_language)
                    except Exception as exc:
                        try:
                            self.app.logger.warning(
                                "[Automation] recipe pack gagal untuk %s: %s",
                                item["title"], str(exc)[:120])
                        except Exception:
                            pass
                        pack = {}
            if isinstance(pack, dict) and pack:
                item["content"] = pack
            media_items.append(item)
        run = self.create_run(
            name=str(recipe.get("name") or "Recipe"),
            media_items=media_items,
            platforms=platforms,
            mode=str(cfg.get("mode") or "now"),
            scheduled_at=_sched,
            content_pack={},
            recipe_id=recipe_id,
            review=review,
            source="recipe",
            overrides={
                "privacy": str(cfg.get("privacy") or ""),
                "category_id": str(cfg.get("category_id") or ""),
                # Only a recorded answer travels; we never invent the audience.
                "made_for_kids": _audience if isinstance(_audience, bool) else None,
            },
            stagger=(cfg.get("stagger") if str(cfg.get("mode") or "") == "stagger"
                     and isinstance(cfg.get("stagger"), dict) else None),
            on_duplicate="skip",
        )
        self.db.automation_set_recipe_enabled(recipe_id, 1, last_run_at=now_iso())
        return run

    # -- ACS one-click template publish -----------------------------------
    def list_acs_templates(self) -> list[dict[str, Any]]:
        """Presets flagged ``acs_publish`` — the templates ACS can publish with."""
        out: list[dict[str, Any]] = []
        try:
            for recipe in self.list_recipes():
                cfg = recipe.get("config_obj") or {}
                if not bool(cfg.get("acs_publish")):
                    continue
                platforms = [p for p in (cfg.get("platforms") or []) if p in PLATFORMS]
                # Human-readable route summary so ACS can show how the template
                # will publish (API vs browser draft) before the user clicks.
                modes = self._template_route_summary(platforms or list(PLATFORMS))
                out.append({
                    "id": str(recipe.get("id") or ""),
                    "name": str(recipe.get("name") or "Template"),
                    "summary": (" + ".join(platforms) if platforms else "Semua platform terhubung"),
                    "modes": modes,
                })
        except Exception:
            pass
        return out

    def _template_route_summary(self, platforms: list[str]) -> str:
        """e.g. "youtube: API · tiktok: draft · instagram: Browser"."""
        try:
            accounts = {str(a.get("platform")): a for a in self.db.automation_list_accounts()}
        except Exception:
            accounts = {}
        try:
            tiktok_mode = str(self.app.config.get("aut_tiktok_mode") or "inbox")
        except Exception:
            tiktok_mode = "inbox"
        parts: list[str] = []
        for plat in platforms:
            acc = accounts.get(plat)
            if not acc or str(acc.get("status") or "") == "disconnected":
                parts.append(f"{plat}: belum tersambung")
                continue
            if plat == "tiktok":
                parts.append(f"{plat}: {'draft' if tiktok_mode == 'inbox' else 'API'}")
            elif str(acc.get("auth_mode") or "") == "browser":
                parts.append(f"{plat}: Browser")
            else:
                parts.append(f"{plat}: API")
        return " · ".join(parts)

    def _template_ai_config(self) -> dict[str, Any]:
        """AI settings for background pack generation (page-free replica)."""
        cfg: dict[str, Any] = {}
        try:
            cfg = dict(self.app.config.as_dict() or {})
        except Exception:
            cfg = {}
        cfg["ai_provider"] = self.app.config.get("ai_provider") or "none"
        cfg["ai_model"] = self.app.config.get("ai_model") or ""
        cfg["ai_vision_model"] = self.app.config.get("ai_vision_model") or ""
        cfg["ai_endpoint"] = self.app.config.get("ai_endpoint") or ""
        cfg["ai_api_key"] = self.app.config.get("ai_api_key") or ""
        cfg["ai_profiles"] = self.app.config.get("ai_profiles", []) or []
        cfg["ai_auto_model"] = bool(self.app.config.get("ai_auto_model", True))
        cfg["ai_auto_visual"] = bool(self.app.config.get("ai_auto_visual", True))
        cfg["ai_timeout"] = int(self.app.config.get("ai_timeout") or 90)
        cfg["ai_temperature"] = float(self.app.config.get("ai_temperature") or 0.2)
        cfg["ai_instruction"] = ""
        return cfg

    def _generate_template_pack(self, media_row: dict[str, Any],
                                cfg_base: dict[str, Any],
                                language: str = "id") -> dict[str, Any]:
        """One AI Content Pack per ready ACS clip — same evidence rules as the
        Automation Create tab, without any page dependency."""
        from copy import deepcopy
        from chopster.automation.context import (
            ClipContext, caption_context, focused_transcript,
        )
        from chopster.clipper.caption_generator import generate_captions

        path = str(media_row.get("media_path") or "")
        name = str(media_row.get("title") or Path(path).name)
        _, raw_ctx = split_media_content(media_row.get("content_pack"))
        ctx = ClipContext.from_dict(raw_ctx) or None
        if ctx is None or not ctx.segments:
            try:
                from chopster.automation.local_transcribe import media_context_from_file
                ctx = media_context_from_file(
                    path, title=name, config=self.app.config,
                    progress_cb=None, cancel_check=lambda: False)
            except Exception:
                ctx = None
        if ctx is None or not ctx.segments:
            # No usable transcript: publish anyway with a text fallback so the
            # one-click flow never stalls on a missing transcript.
            return self._fallback_pack(name, "", "tidak ada transkrip")
        cfg2 = deepcopy(cfg_base)
        cfg2.update(caption_context(ctx))
        cfg2["source_title"] = name
        cfg2["working_title"] = name
        # Title/description/hashtag language: template choice, or the clip's own
        # language when the template asks to follow the video.
        lang = str(language or "id").strip().lower() or "id"
        if lang in ("auto", "video", "follow"):
            lang = str(getattr(ctx, "language", "") or "").strip().lower()
            if not lang or lang == "auto":
                from chopster.automation.context import focused_transcript as _ft
                focus_hint = _ft(ctx)
                lang = str(getattr(focus_hint, "language", "") or "").strip().lower()
            if not lang or lang == "auto":
                lang = "id"
        focus = focused_transcript(ctx)
        if focus is None or not focus.segments:
            return self._fallback_pack(name, ctx.excerpt, "transkrip kosong")
        try:
            pack = generate_captions(focus, cfg2, platform="youtube",
                                     language=lang, count=5, style="hooks")
        except Exception as exc:
            # AI provider down/quota/offline: publish with a text fallback
            # instead of failing the whole one-click template.
            return self._fallback_pack(name, ctx.excerpt, f"AI gagal: {str(exc)[:80]}")
        if isinstance(pack, dict) and (pack.get("title") or pack.get("titles")):
            pack["_clip_source"] = str(getattr(ctx, "source", "") or "")
            return pack
        return self._fallback_pack(name, ctx.excerpt, "hasil AI kosong")

    def _fallback_pack(self, title: str, excerpt: str, reason: str) -> dict[str, Any]:
        """Deterministic, human-readable content when AI is unavailable."""
        clean_excerpt = " ".join(str(excerpt or "").split())
        description = (clean_excerpt[:400].strip()
                       or f"{title}. Dipublikasikan otomatis lewat template Chopster.")
        try:
            self.app.logger.warning("[Automation][template] fallback pack untuk «%s» (%s)",
                                    title, reason)
        except Exception:
            pass
        return {
            "title": title or "Clip",
            "description": description,
            "hashtags": [],
            "source": "template_fallback",
            "_fallback_reason": reason,
        }

    @staticmethod
    def _next_occurrence(hhmm: str) -> str:
        """Next ``HH:MM`` wall-clock moment (today or tomorrow)."""
        try:
            hours, minutes = (int(x) for x in str(hhmm).split(":"))
        except Exception:
            hours, minutes = 19, 0
        now = datetime.now()
        when = now.replace(hour=hours % 24, minute=minutes % 60, second=0, microsecond=0)
        if when <= now:
            when += timedelta(days=1)
        return when.strftime("%Y-%m-%d %H:%M")

    def run_template_acs(self, template_id: str,
                         status_cb: Callable[[str], None] | None = None) -> dict[str, Any]:
        """One-click template publish from ACS: every ready ACS render gets a
        fresh AI pack, then runs + uploads through the existing queue/runner."""
        recipe = self.db.automation_get_recipe(str(template_id or ""))
        if not recipe:
            return {"ok": False, "message": "Template tidak ditemukan."}
        cfg = safe_json_loads(recipe.get("config"), {})
        if not bool(cfg.get("acs_publish")):
            return {"ok": False, "message": "Preset ini belum diaktifkan sebagai template ACS."}
        def _log(msg: str) -> None:
            try:
                self.app.logger.info("[Automation][template] %s", msg)
            except Exception:
                pass
        _log(f"start template='{recipe.get('name')}'")
        try:
            media = self.db.automation_list_media(status="ready", limit=100)
        except Exception:
            media = []
        ready = [m for m in media
                 if str(m.get("source") or "").lower() == "acs"
                 and (m.get("media_path") and Path(str(m.get("media_path"))).exists())]
        if not ready:
            _log("no ready ACS media")
            return {"ok": False, "message": "Belum ada hasil render ACS yang siap publish."}

        platforms = [{"platform": p} for p in (cfg.get("platforms") or []) if p in PLATFORMS]
        if not platforms:
            platforms = [{"platform": p} for p in PLATFORMS
                         if any(a.get("platform") == p and a.get("status") == "connected"
                                for a in self.db.automation_list_accounts())]
        try:
            connected = {str(a.get("platform")) for a in self.db.automation_list_accounts()
                         if a.get("status") == "connected"}
        except Exception:
            connected = set()
        platforms = [p for p in platforms if p.get("platform") in connected]
        if not platforms:
            _log("no connected account for template platforms")
            return {"ok": False,
                    "message": "Tidak ada akun terhubung untuk platform template ini. Sambungkan dulu di Accounts."}

        cfg_base = self._template_ai_config()
        # max_per_run <= 0 means "every ready clip" (the template's "Semua").
        max_per = int(cfg.get("max_per_run") or 0)
        ai_language = str(cfg.get("ai_language") or "id")
        chosen = ready if max_per <= 0 else ready[:max_per]
        _log(f"{len(chosen)} media akan diproses untuk platform {[p.get('platform') for p in platforms]} (bahasa AI: {ai_language})")
        media_items: list[dict[str, Any]] = []
        errors: list[str] = []
        for i, row in enumerate(chosen, start=1):
            path = str(row.get("media_path") or "")
            name = str(row.get("title") or Path(path).name)
            try:
                if status_cb is not None:
                    status_cb(f"Generate AI judul/deskripsi/hashtag ({i}/{len(chosen)}): {name}…")
                _log(f"pack {i}/{len(chosen)}: {name}")
                pack = self._generate_template_pack(row, cfg_base, language=ai_language)
                media_items.append({"path": path, "title": name,
                                    "clip_id": str(row.get("clip_id") or ""),
                                    "content": pack})
            except Exception as exc:
                _log(f"pack gagal {name}: {str(exc)[:120]}")
                errors.append(f"{name}: {str(exc)[:120]}")
        if not media_items:
            reason = "; ".join(errors[:3]) if errors else "gagal membuat konten AI"
            return {"ok": False, "message": f"Tidak ada video berhasil disiapkan ({reason})."}

        mode = str(cfg.get("mode") or "now")
        scheduled_at = ""
        stagger: dict[str, Any] | None = None
        if mode == "stagger":
            # Honour the template's staggered spacing (Waktu → Staggered:
            # interval + operating hours) instead of dumping every clip at once.
            _st = cfg.get("stagger") if isinstance(cfg.get("stagger"), dict) else {}
            stagger = dict(_st) if _st else {}
            stagger.setdefault(
                "start",
                datetime.now().replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M"))
            if not stagger.get("interval_minutes"):
                stagger["interval_minutes"] = int(cfg.get("stagger_interval") or 120)
            if not stagger.get("active_hours"):
                stagger["active_hours"] = [10, 23]
            _log(f"stagger tiap {int(stagger.get('interval_minutes') or 120)} menit")
        elif mode == "schedule":
            scheduled_at = self._next_occurrence(str(cfg.get("time") or "19:00"))
        try:
            run = self.create_run(
                name=str(recipe.get("name") or "Template ACS"),
                media_items=media_items,
                platforms=platforms,
                mode=mode,
                scheduled_at=scheduled_at,
                content_pack={},
                overrides={
                    "privacy": str(cfg.get("privacy") or "public"),
                    "made_for_kids": bool(cfg.get("made_for_kids", False)),
                    "category_id": str(cfg.get("category_id") or ""),
                },
                recipe_id=str(recipe.get("id") or ""),
                review=bool(cfg.get("review", False)),
                source="acs_template",
                shared_pack=False,
                stagger=stagger,
            )
        except Exception as exc:
            _log(f"create_run gagal: {str(exc)[:120]}")
            return {"ok": False, "message": str(getattr(exc, "user_message", "") or f"Gagal membuat antrean: {exc}")}
        platforms_lbl = " + ".join(str(p.get("platform")) for p in platforms)
        if stagger is not None:
            when = f"terjadwal bertahap tiap {int(stagger.get('interval_minutes') or 120)} menit"
        elif scheduled_at:
            when = f"terjadwal {scheduled_at}"
        else:
            when = "langsung upload"
        _log(f"run dibuat {run.get('id')}: {len(media_items)} video -> {len(platforms)} platform, {when}")
        return {"ok": True, "run_id": str(run.get("id") or ""),
                "message": f"{len(media_items)} video → {len(media_items) * len(platforms)} job ({platforms_lbl}), {when}. Cek tab Queue di Automation."}

    # -- legacy publish_queue.json bridge (spec §52) ------------------------
    def import_legacy_queue(self) -> int:
        try:
            from chopster.clipper.scheduler import PATH as LEGACY_PATH
        except Exception:
            return 0
        path = Path(LEGACY_PATH)
        if not path.exists():
            return 0
        try:
            items = json.loads(path.read_text(encoding="utf-8")) if path.stat().st_size else []
        except Exception:
            return 0
        if not isinstance(items, list) or not items:
            return 0
        imported = 0
        try:
            accounts = self.db.automation_list_accounts()
            connected: dict[str, str] = {}
            for a in accounts:
                if a.get("status") == "connected":
                    connected.setdefault(str(a.get("platform")), str(a["id"]))
            pending = []
            migrated = []
            for item in items:
                platform = str(item.get("platform") or "").lower()
                fpath = str(item.get("file") or "")
                if platform not in PLATFORMS or not fpath:
                    pending.append(item)
                    continue
                account_id = connected.get(platform, "")
                if not account_id:
                    pending.append(item)     # keep until user connects the platform
                    continue
                pack = {"title": str(item.get("title") or ""), "description": str(item.get("description") or ""), "hashtags": item.get("hashtags") or []}
                sched = str(item.get("scheduled_at") or "")
                mode = "schedule" if sched else "now"
                run = self.create_run(name="Imported Publish Queue", media_items=[{"path": fpath, "title": str(item.get("title") or Path(fpath).name)}], platforms=[{"platform": platform, "account_id": account_id}], mode=mode, scheduled_at=sched, content_pack=pack, source="legacy", on_duplicate="skip")
                if run is None:
                    pending.append(item)     # already queued: keep it, never duplicate
                    continue
                imported += 1
                migrated.append(item)
                try:
                    self.app.logger.info("[Automation] imported legacy job %s -> run %s", item.get("id"), run.get("id"))
                except Exception:
                    pass
            # Preserve whatever could not be migrated (never delete blindly).
            if len(pending) == len(items):
                return 0
            kept = pending
            archive = path.with_suffix(".imported.json")
            try:
                old = json.loads(archive.read_text(encoding="utf-8")) if archive.exists() else []
            except Exception:
                old = []
            if isinstance(old, list):
                # Archive exactly the items that were migrated. Slicing the head
                # of the original list (the old bug) recorded the wrong rows
                # whenever a pending item was not at the end.
                old.extend(migrated)
                archive.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")
            path.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            try:
                self.app.logger.error("[Automation] legacy import failed: %s", redact(traceback.format_exc()[-800:]))
            except Exception:
                pass
        return imported
