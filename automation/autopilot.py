"""Auto-Pilot rule engine (spec 6).

Auto-Pilot is the event-driven *front end* of the single publishing pipeline. A
finished ACS render or CCA clip arrives, matching enabled rules pick accounts,
a publish time is computed, and jobs are created through
``AutomationManager.create_run`` - the same queue, scheduler, workers, retry
policy, status model and History used by manual batches. There is deliberately
no second queue, scheduler or worker in this module.

Rules persist in the ``automation_rules`` table, so configuration survives an
app restart (spec 6.2 forbids UI-only state).
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from chopster.automation.models import PLATFORMS, new_id, now_iso
from chopster.automation.planner import PlanError, describe_days, plan_rule_slot
from chopster.automation.utils import safe_json_loads

SOURCES = ("acs", "cca", "manual")
SOURCE_LABELS = {
    "acs": "Auto Clip Studio",
    "cca": "Content Clipper AI",
    "manual": "Manual Import",
}
APPROVAL_MODES = ("auto", "review")
MISSED_POLICIES = ("next_slot", "run_now")
ACCOUNT_POLICIES = ("hold", "skip")

DEFAULT_CONFIG: dict[str, Any] = {
    "start_time": "10:00",
    "interval_minutes": 120,
    "per_day": 1,
    "active_hours": [],              # [] or [start_hour, end_hour]
    "active_days": [0, 1, 2, 3, 4, 5, 6],
    "approval": "auto",              # auto | review
    "missed_policy": "next_slot",    # next_slot | run_now
    "when_account_missing": "hold",  # hold | skip
    "privacy": "",
    "made_for_kids": None,           # YouTube audience; None = not answered yet
    "title_prefix": "",
    "title_suffix": "",
}


def normalize_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Merge stored config over defaults and coerce every field type."""
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(raw if isinstance(raw, dict) else {})
    try:
        cfg["interval_minutes"] = max(1, int(cfg.get("interval_minutes") or 120))
    except Exception:
        cfg["interval_minutes"] = 120
    try:
        cfg["per_day"] = max(1, int(cfg.get("per_day") or 1))
    except Exception:
        cfg["per_day"] = 1
    hours = cfg.get("active_hours") or []
    if isinstance(hours, (list, tuple)) and len(hours) >= 2:
        try:
            cfg["active_hours"] = [int(hours[0]), int(hours[1])]
        except Exception:
            cfg["active_hours"] = []
    else:
        cfg["active_hours"] = []
    days = cfg.get("active_days")
    if isinstance(days, (list, tuple)) and days:
        try:
            cfg["active_days"] = sorted({int(d) for d in days if 0 <= int(d) <= 6})
        except Exception:
            cfg["active_days"] = list(DEFAULT_CONFIG["active_days"])
    else:
        cfg["active_days"] = list(DEFAULT_CONFIG["active_days"])
    cfg["approval"] = str(cfg.get("approval") or "auto").lower()
    cfg["missed_policy"] = str(cfg.get("missed_policy") or "next_slot").lower()
    cfg["when_account_missing"] = str(cfg.get("when_account_missing") or "hold").lower()
    cfg["start_time"] = str(cfg.get("start_time") or "10:00")
    cfg["privacy"] = str(cfg.get("privacy") or "")
    mfk = cfg.get("made_for_kids")
    cfg["made_for_kids"] = mfk if isinstance(mfk, bool) else None
    cfg["title_prefix"] = str(cfg.get("title_prefix") or "")
    cfg["title_suffix"] = str(cfg.get("title_suffix") or "")
    if not cfg["active_days"]:
        cfg["active_days"] = list(DEFAULT_CONFIG["active_days"])
    # Unknown keys are dropped so a rule config can never smuggle stale or
    # misleading settings into the engine.
    return {key: value for key, value in cfg.items() if key in DEFAULT_CONFIG}


def rule_view(row: dict[str, Any]) -> dict[str, Any]:
    """Decode one DB row into a UI/engine-friendly view."""
    sources = [str(s) for s in (safe_json_loads(row.get("sources"), []) or [])
               if str(s) in SOURCES]
    platforms = [str(p) for p in (safe_json_loads(row.get("platforms"), []) or [])
                 if str(p) in PLATFORMS]
    accounts = [str(a) for a in (safe_json_loads(row.get("accounts"), []) or [])]
    stats = safe_json_loads(row.get("stats"), {}) or {}
    return {
        "id": str(row.get("id") or ""),
        "name": str(row.get("name") or "Rule"),
        "enabled": bool(int(row.get("enabled") or 0)),
        "sources_list": sources or list(SOURCES[:2]),
        "platforms_list": platforms,
        "accounts_list": accounts,
        "cfg": normalize_config(safe_json_loads(row.get("config"), {})),
        "stats": stats if isinstance(stats, dict) else {},
        "last_run_at": str(row.get("last_run_at") or ""),
        "next_run_at": str(row.get("next_run_at") or ""),
        "last_error": str(row.get("last_error") or ""),
        "raw": dict(row),
    }


def blocking_reasons(view: dict[str, Any], accounts: list[dict[str, Any]]) -> list[str]:
    """Everything that would stop this rule from actually running (spec 6.5)."""
    reasons: list[str] = []
    cfg = view["cfg"]
    if not view["name"].strip():
        reasons.append("Rule belum punya nama.")
    if not view["sources_list"]:
        reasons.append("Pilih minimal satu sumber konten (ACS dan/atau CCA).")
    if not view["platforms_list"]:
        reasons.append("Pilih minimal satu platform tujuan.")
    if cfg.get("approval") not in APPROVAL_MODES:
        reasons.append("Mode approval tidak dikenal.")
    if cfg.get("missed_policy") not in MISSED_POLICIES:
        reasons.append("Kebijakan jadwal terlewat tidak dikenal.")
    if cfg.get("when_account_missing") not in ACCOUNT_POLICIES:
        reasons.append("Perilaku saat akun tidak tersedia tidak dikenal.")
    try:
        plan_rule_slot(
            now=datetime.now(),
            start_time=cfg["start_time"],
            interval_minutes=cfg["interval_minutes"],
            used_today=0,
            per_day=cfg["per_day"],
            active_hours=cfg["active_hours"] or None,
            active_days=cfg["active_days"],
            missed_policy=cfg["missed_policy"],
        )
    except PlanError as exc:
        reasons.append(exc.user_message)
    if view["platforms_list"]:
        usable = [a for a in accounts
                  if str(a.get("platform")) in view["platforms_list"]
                  and str(a.get("status")) not in ("disconnected",)]
        if view["accounts_list"]:
            usable = [a for a in usable if str(a.get("id")) in view["accounts_list"]]
        if not usable:
            labels = ", ".join(view["platforms_list"])
            reasons.append(f"Belum ada akun terhubung untuk: {labels}.")
    return reasons


class AutoPilotEngine:
    """Evaluates rules against finished content and enqueues through the manager."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager
        self.app = manager.app
        self.db = manager.db

    def automatic_enabled(self) -> bool:
        """Global Auto-Pilot switch (Settings -> Automation).

        It only gates *automatic* intake. "Run now" stays available on purpose:
        that is an explicit user action, not unattended publishing.
        """
        try:
            return bool(self.app.config.get("aut_autopilot_enabled", True))
        except Exception:
            return True

    # -- rule management --------------------------------------------------
    def list_rules(self) -> list[dict[str, Any]]:
        return [rule_view(row) for row in self.db.automation_list_rules()]

    def get_rule(self, rule_id: str) -> dict[str, Any] | None:
        row = self.db.automation_get_rule(rule_id)
        return rule_view(row) if row else None

    def save_rule(self, *, name: str, sources: list[str], platforms: list[str],
                  accounts: list[str] | None = None, config: dict[str, Any] | None = None,
                  enabled: bool = False, rule_id: str = "") -> str:
        rid = rule_id or new_id("rule_")
        existing = self.db.automation_get_rule(rid) if rule_id else None
        cfg = normalize_config(config)
        cfg_json = json.dumps(cfg, ensure_ascii=False)
        if existing is None:
            self.db.automation_save_rule(
                rid, name or "Rule", enabled=0,
                sources=json.dumps([s for s in sources if s in SOURCES] or list(SOURCES[:2])),
                platforms=json.dumps([p for p in platforms if p in PLATFORMS]),
                accounts=json.dumps([str(a) for a in (accounts or [])]),
                config=cfg_json, stats="{}")
        else:
            stats = safe_json_loads(existing.get("stats"), {}) or {}
            self.db.automation_save_rule(
                rid, name or "Rule", enabled=int(existing.get("enabled") or 0),
                sources=json.dumps([s for s in sources if s in SOURCES] or list(SOURCES[:2])),
                platforms=json.dumps([p for p in platforms if p in PLATFORMS]),
                accounts=json.dumps([str(a) for a in (accounts or [])]),
                config=cfg_json, stats=json.dumps(stats, ensure_ascii=False),
                last_run_at=str(existing.get("last_run_at") or ""),
                next_run_at=str(existing.get("next_run_at") or ""),
                last_error=str(existing.get("last_error") or ""))
        if enabled:
            self.set_enabled(rid, True)
        return rid

    def duplicate_rule(self, rule_id: str) -> str | None:
        view = self.get_rule(rule_id)
        if not view:
            return None
        return self.save_rule(name=f"{view['name']} (copy)", sources=view["sources_list"],
                              platforms=view["platforms_list"],
                              accounts=view["accounts_list"], config=view["cfg"],
                              enabled=False)

    def set_enabled(self, rule_id: str, enabled: bool) -> dict[str, Any]:
        """Enable/disable a rule; enabling validates first (spec 6.5).

        Disabling never touches jobs that are already queued or scheduled
        (spec 13), it only stops new jobs from being created.
        """
        view = self.get_rule(rule_id)
        if not view:
            return {"ok": False, "enabled": False, "blocked": ["Rule tidak ditemukan."]}
        if not enabled:
            self.db.automation_update_rule(rule_id, enabled=0)
            return {"ok": True, "enabled": False, "blocked": []}
        reasons = blocking_reasons(view, self.db.automation_list_accounts())
        if reasons:
            self.db.automation_update_rule(rule_id, enabled=0,
                                           last_error="; ".join(reasons)[:400])
            return {"ok": False, "enabled": False, "blocked": reasons}
        stats = dict(view["stats"])
        stats["enabled_at"] = now_iso()
        stats["enabled_at_ts"] = int(time.time())
        self.db.automation_update_rule(rule_id, enabled=1, last_error="",
                                       stats=json.dumps(stats, ensure_ascii=False))
        return {"ok": True, "enabled": True, "blocked": []}

    def delete_rule(self, rule_id: str) -> None:
        self.db.automation_delete_rule(rule_id)

    def status(self, rule_id: str) -> dict[str, Any]:
        view = self.get_rule(rule_id)
        if not view:
            return {}
        accounts = self.db.automation_list_accounts()
        today = datetime.now().strftime("%Y-%m-%d")
        return {
            "enabled": view["enabled"],
            "blocked": blocking_reasons(view, accounts),
            "last_execution": view["last_run_at"],
            "next_execution": view["next_run_at"],
            "jobs_today": self.db.automation_rule_jobs_today(rule_id, today),
            "content_today": self.db.automation_rule_media_today(rule_id, today),
            "last_error": view["last_error"],
            "schedule_label": self.describe_schedule(view),
            "automatic_enabled": self.automatic_enabled(),
        }

    def describe_schedule(self, view: dict[str, Any]) -> str:
        cfg = view["cfg"]
        hours = cfg.get("active_hours") or []
        window = f"{int(hours[0]):02d}:00-{int(hours[1]):02d}:00" if len(hours) >= 2 else "tanpa batas jam"
        return (f"{cfg['per_day']}x/hari, mulai {cfg['start_time']}, "
                f"setiap {cfg['interval_minutes']} menit, {window}, "
                f"{describe_days(cfg['active_days'])}")

    # -- targeting --------------------------------------------------------
    def _targets_for(self, view: dict[str, Any]) -> list[dict[str, str]]:
        accounts = self.db.automation_list_accounts()
        explicit = set(view["accounts_list"])
        targets: list[dict[str, str]] = []
        for platform in view["platforms_list"]:
            if platform not in PLATFORMS:
                continue
            candidates = [a for a in accounts
                          if str(a.get("platform")) == platform
                          and str(a.get("status")) not in ("disconnected",)]
            if explicit:
                candidates = [a for a in candidates if str(a.get("id")) in explicit]
            connected = [a for a in candidates if str(a.get("status")) == "connected"]
            pick = connected[0] if connected else (candidates[0] if candidates else None)
            if pick is None:
                continue
            targets.append({"platform": platform, "account_id": str(pick["id"]),
                            "privacy": str(view["cfg"].get("privacy") or "")})
        return targets

    def _already_queued(self, rule_id: str, fingerprint: str, platform: str,
                        account_id: str) -> bool:
        return bool(self.db.automation_rule_has_job(rule_id, fingerprint, platform, account_id))

    # -- triggering -------------------------------------------------------
    def on_media_ready(self, media_row: dict[str, Any] | None) -> list[dict[str, Any]]:
        """Called for every finished ACS/CCA clip. Returns the runs it created."""
        created: list[dict[str, Any]] = []
        if not media_row:
            return created
        if str(media_row.get("status") or "") != "ready":
            return created
        if not str(media_row.get("media_path") or ""):
            return created
        if not self.automatic_enabled():
            # Global kill switch: the clip is still registered as ready media
            # (visible in Studio), only unattended scheduling is suspended.
            return created
        source = str(media_row.get("source") or "acs")
        media_ts = int(media_row.get("ts") or 0)
        for view in self.list_rules():
            if not view["enabled"]:
                continue
            if source not in view["sources_list"]:
                continue
            enabled_ts = int(view["stats"].get("enabled_at_ts") or 0)
            if enabled_ts and media_ts and media_ts < enabled_ts:
                # Only content finished after activation (spec 6.3); old clips
                # require an explicit backfill via run_rule_now().
                continue
            try:
                run = self._enqueue(view, [media_row])
            except Exception as exc:  # noqa: BLE001 - one bad rule must not stop others
                self._record_error(view["id"], str(exc))
                continue
            if run:
                created.append(run)
        return created

    def run_rule_now(self, rule_id: str, media_ids: list[str] | None = None,
                     limit: int = 20) -> dict[str, Any]:
        """Explicit manual/backfill trigger — the only path that accepts old clips."""
        view = self.get_rule(rule_id)
        if not view:
            return {"ok": False, "message": "Rule tidak ditemukan.", "runs": []}
        rows = self.db.automation_list_media("ready", limit=max(1, int(limit)))
        rows = [r for r in rows if str(r.get("source") or "acs") in view["sources_list"]]
        if media_ids:
            wanted = {str(m) for m in media_ids}
            rows = [r for r in rows if str(r.get("id")) in wanted]
        if not rows:
            return {"ok": False, "message": "Tidak ada media siap publish untuk sumber rule ini.",
                    "runs": []}
        try:
            run = self._enqueue(view, rows)
        except Exception as exc:  # noqa: BLE001
            self._record_error(rule_id, str(exc))
            return {"ok": False, "message": str(exc), "runs": []}
        if not run:
            return {"ok": False, "runs": [],
                    "message": self.get_rule(rule_id)["last_error"] or "Tidak ada job yang dibuat."}
        return {"ok": True, "runs": [run], "message": f"Run dibuat: {run.get('name')}"}

    # -- internals --------------------------------------------------------
    def _enqueue(self, view: dict[str, Any], media_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
        rule_id = view["id"]
        cfg = view["cfg"]
        targets = self._targets_for(view)
        if not targets:
            reason = "Tidak ada akun terhubung untuk platform rule ini."
            self._record_error(rule_id, reason)
            if cfg["when_account_missing"] == "skip":
                return None
            return None

        today = datetime.now().strftime("%Y-%m-%d")
        used_today = self.db.automation_rule_media_today(rule_id, today)

        accepted: list[dict[str, Any]] = []
        accepted_targets: list[dict[str, str]] = []
        skipped = 0
        from chopster.automation.utils import media_fingerprint
        for media in media_rows:
            if used_today + len(accepted) >= int(cfg["per_day"]):
                break
            path = str(media.get("media_path") or "")
            try:
                fingerprint = media_fingerprint(path)
            except Exception:
                fingerprint = ""
            remaining = [t for t in targets
                         if not self._already_queued(rule_id, fingerprint, t["platform"],
                                                     t["account_id"])]
            if not remaining:
                skipped += 1
                continue
            item: dict[str, Any] = {
                "path": path,
                "title": str(media.get("title") or ""),
                "clip_id": str(media.get("clip_id") or ""),
            }
            raw_content = (safe_json_loads(media.get("content_pack"), {})
                           if media.get("content_pack") else {})
            if isinstance(raw_content, dict) and ("pack" in raw_content
                                                  or "clip_context" in raw_content):
                from chopster.automation.manager import split_media_content
                pack, clip_ctx = split_media_content(raw_content)
                if pack:
                    item["content"] = pack
                if clip_ctx:
                    item["clip_context"] = clip_ctx
            elif isinstance(raw_content, dict) and raw_content:
                item["content"] = raw_content
            accepted.append(item)
            if not accepted_targets:
                accepted_targets = remaining

        if not accepted:
            reason = ("Batas harian tercapai." if used_today >= int(cfg["per_day"])
                      else "Konten sudah masuk antrean untuk rule ini.")
            self._record_error(rule_id, reason)
            return None

        try:
            slot = plan_rule_slot(
                now=datetime.now(), start_time=cfg["start_time"],
                interval_minutes=cfg["interval_minutes"], used_today=used_today,
                per_day=cfg["per_day"], active_hours=cfg["active_hours"] or None,
                active_days=cfg["active_days"], missed_policy=cfg["missed_policy"],
            )
            slot_text = slot.strftime("%Y-%m-%d %H:%M")
        except PlanError as exc:
            self._record_error(rule_id, exc.user_message)
            return None

        pack: dict[str, Any] = {}
        first = accepted[0]
        if isinstance(first.get("content"), dict) and first["content"]:
            pack = dict(first["content"])
        title = str(first.get("title") or "")
        prefix, suffix = cfg.get("title_prefix") or "", cfg.get("title_suffix") or ""
        overrides: dict[str, Any] = {
            "privacy": cfg.get("privacy") or "",
            # Answer travels with the run snapshot so YouTube's adapter can use
            # it verbatim (never a silent default).
            "made_for_kids": cfg.get("made_for_kids"),
        }
        if title and (prefix or suffix):
            overrides["title_override"] = f"{prefix}{title}{suffix}"

        run = self.manager.create_run(
            name=f"{view['name']} — {len(accepted)} clip",
            media_items=accepted,
            platforms=accepted_targets,
            mode="schedule",
            scheduled_at=slot_text,
            content_pack=pack,
            overrides=overrides,
            review=(cfg["approval"] == "review"),
            source="autopilot",
            rule_id=rule_id,
            intent=f"autopilot:{rule_id}",
            on_duplicate="skip",
        )
        if not run:
            # Every accepted clip is already waiting in the queue.
            self._record_error(rule_id, "Semua clip sudah ada di antrean.")
            return None

        stats = dict(view["stats"])
        stats["last_created"] = len(accepted)
        stats["last_slot"] = slot_text
        next_slot = ""
        try:
            next_slot = plan_rule_slot(
                now=datetime.now(), start_time=cfg["start_time"],
                interval_minutes=cfg["interval_minutes"], used_today=used_today + len(accepted),
                per_day=cfg["per_day"], active_hours=cfg["active_hours"] or None,
                active_days=cfg["active_days"], missed_policy=cfg["missed_policy"],
            ).strftime("%Y-%m-%d %H:%M")
        except PlanError:
            next_slot = ""
        self.db.automation_update_rule(
            rule_id, last_run_at=now_iso(), next_run_at=next_slot, last_error="",
            stats=json.dumps(stats, ensure_ascii=False))
        self.manager.notify_global("autopilot", {
            "rule_id": rule_id, "rule_name": view["name"], "run_id": str(run.get("id") or ""),
            "created": len(accepted), "skipped": skipped, "slot": slot_text,
        })
        try:
            self.app.logger.info(
                "[Automation][autopilot] rule=%s created=%s skipped=%s slot=%s",
                view["name"], len(accepted), skipped, slot_text)
        except Exception:
            pass
        return run

    def _record_error(self, rule_id: str, message: str) -> None:
        try:
            self.db.automation_update_rule(rule_id, last_error=str(message)[:400])
        except Exception:
            pass