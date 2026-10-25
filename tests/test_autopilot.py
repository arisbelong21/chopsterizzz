"""Auto-Pilot, source identity, staggered scheduling and recovery tests.

Covers the Auto Publish Studio Pro work: ACS + CCA feeding ONE engine, real
staggered slot computation, persistent Auto-Pilot rules, readiness validation,
idempotent enqueue and restart recovery. No real upload happens here - platform
adapters are mocked, so mock success is never confused with production success.
"""
from __future__ import annotations

import json
import logging
import sys
import tempfile
import threading
import time
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
PARENT = ROOT.parent
for p in (str(PARENT), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from chopster.automation import events as events_mod  # noqa: E402
from chopster.automation.autopilot import (  # noqa: E402
    AutoPilotEngine, blocking_reasons, normalize_config, rule_view,
)
from chopster.automation.manager import AutomationManager  # noqa: E402
from chopster.automation.models import InvalidMediaError  # noqa: E402
from chopster.automation.planner import (  # noqa: E402
    ALL_DAYS, PlanError, compute_staggered_slots, describe_days, plan_rule_slot,
)
from chopster.automation.queue import JobRunner  # noqa: E402
from chopster.automation.utils import validate_media_file  # noqa: E402
from chopster.app.configuration import Configuration  # noqa: E402
from chopster.app.database import Database  # noqa: E402


def make_media(tmp: Path, name: str = "clip.mp4", body: bytes = b"\x00" * 512) -> Path:
    path = tmp / name
    path.write_bytes(body)
    return path


class FakeApp:
    """Minimal Application stand-in backed by a temp directory."""

    def __init__(self, tmp: Path):
        self.data_dir = tmp
        self.config = Configuration(self.data_dir)
        self.db = Database(self.data_dir / "chopster.db")
        self.logger = logging.getLogger("test-autopilot")
        self.automation = None
        self.window = None


class TestPlanner(unittest.TestCase):
    def test_staggered_count_interval_and_no_collisions(self):
        start = datetime(2026, 10, 9, 8, 0)
        slots = compute_staggered_slots(start=start, count=12, interval_minutes=120)
        self.assertEqual(len(slots), 12)
        self.assertEqual(len(set(slots)), 12)
        self.assertTrue(all((b - a) == timedelta(hours=2) for a, b in zip(slots, slots[1:])))

    def test_staggered_respects_operating_window(self):
        start = datetime(2026, 10, 9, 8, 0)
        slots = compute_staggered_slots(start=start, count=5, interval_minutes=120,
                                        active_hours=(10, 14))
        self.assertEqual(len(slots), 5)
        self.assertTrue(all(10 <= dt.hour < 14 for dt in slots))
        self.assertEqual(slots[0].hour, 10)
        self.assertEqual(slots[0].day, 9)
        self.assertEqual(slots[2].day, 10)

    def test_staggered_respects_active_days_and_keeps_time_of_day(self):
        start = datetime(2026, 10, 9, 8, 0)
        slots = compute_staggered_slots(start=start, count=3, interval_minutes=60,
                                        active_days=(0,))
        self.assertTrue(all(dt.weekday() == 0 for dt in slots))
        self.assertEqual([dt.hour for dt in slots], [8, 9, 10])

    def test_staggered_rejects_impossible_requests(self):
        start = datetime(2026, 10, 9, 8, 0)
        for kwargs in ({"count": 0}, {"count": 3, "interval_minutes": 0},
                       {"count": 3, "active_hours": (14, 10)},
                       {"count": 3, "active_days": (9,)}):
            with self.assertRaises(PlanError) as ctx:
                compute_staggered_slots(start=start, **kwargs)
            self.assertTrue(ctx.exception.user_message)

    def test_rule_slot_positions_and_daily_cap(self):
        now = datetime(2026, 10, 9, 7, 0)
        first = plan_rule_slot(now=now, start_time="10:00", interval_minutes=120,
                               used_today=0, per_day=2)
        second = plan_rule_slot(now=now, start_time="10:00", interval_minutes=120,
                                used_today=1, per_day=2)
        full = plan_rule_slot(now=now, start_time="10:00", interval_minutes=120,
                              used_today=2, per_day=2)
        self.assertEqual(first.hour, 10)
        self.assertEqual(second.hour, 12)
        self.assertEqual(full.date(), (now + timedelta(days=1)).date())
        self.assertEqual(full.hour, 10)

    def test_rule_slot_missed_policies(self):
        now = datetime(2026, 10, 9, 11, 0)
        rolled = plan_rule_slot(now=now, start_time="10:00", interval_minutes=120,
                                used_today=0, per_day=3, missed_policy="next_slot")
        self.assertGreater(rolled, now)
        immediate = plan_rule_slot(now=now, start_time="10:00", interval_minutes=120,
                                   used_today=0, per_day=3, missed_policy="run_now")
        self.assertEqual(immediate, now)
        with self.assertRaises(PlanError):
            plan_rule_slot(now=now, start_time="10:00", missed_policy="whenever")
        with self.assertRaises(PlanError):
            plan_rule_slot(now=now, start_time="25:99")

    def test_describe_days(self):
        self.assertEqual(describe_days(ALL_DAYS), "Setiap hari")
        self.assertEqual(describe_days((0, 2)), "Sen, Rab")


class TestAutoPilotConfig(unittest.TestCase):
    def test_normalize_defaults_and_coercion(self):
        cfg = normalize_config(None)
        self.assertEqual(cfg["per_day"], 1)
        self.assertEqual(cfg["approval"], "auto")
        self.assertEqual(cfg["active_days"], list(ALL_DAYS))
        dirty = normalize_config({"interval_minutes": "45", "per_day": "0",
                                  "active_hours": ["9", "17"], "active_days": [5, 1, 1],
                                  "approval": "REVIEW", "unknown": 1})
        self.assertEqual(dirty["interval_minutes"], 45)
        self.assertEqual(dirty["per_day"], 1)
        self.assertEqual(dirty["active_hours"], [9, 17])
        self.assertEqual(dirty["active_days"], [1, 5])
        self.assertEqual(dirty["approval"], "review")
        self.assertNotIn("unknown", dirty)

    def test_rule_view_decodes_json_columns(self):
        view = rule_view({"id": "r1", "name": "R", "enabled": 1, "sources": '["cca"]',
                          "platforms": '["youtube","bogus"]', "accounts": '["a1"]',
                          "config": '{"per_day":3}', "stats": '{"enabled_at_ts":5}'})
        self.assertTrue(view["enabled"])
        self.assertEqual(view["sources_list"], ["cca"])
        self.assertEqual(view["platforms_list"], ["youtube"])
        self.assertEqual(view["accounts_list"], ["a1"])
        self.assertEqual(view["cfg"]["per_day"], 3)
        self.assertEqual(view["stats"]["enabled_at_ts"], 5)


class TestMediaValidation(unittest.TestCase):
    def test_missing_unreadable_and_empty(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ok, reason = validate_media_file(tmp / "nope.mp4")
            self.assertFalse(ok)
            self.assertIn("tidak ditemukan", reason)
            empty = tmp / "empty.mp4"
            empty.write_bytes(b"")
            ok, reason = validate_media_file(empty)
            self.assertFalse(ok)
            self.assertIn("0 byte", reason)
            good = make_media(tmp, "good.mp4")
            self.assertEqual(validate_media_file(good), (True, ""))
            self.assertEqual(validate_media_file(""),
                             (False, "File hasil tidak ditemukan di disk."))


class TestSourceIdentity(unittest.TestCase):
    """ACS and CCA must both land in the same engine but keep their identity."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="chopster-ap-")
        self.app = FakeApp(Path(self.tmp))
        self.mgr = AutomationManager(self.app)

    def tearDown(self):
        try:
            self.mgr.stop()
        except Exception:
            pass
        try:
            self.app.db.close()
        except Exception:
            pass

    def test_acs_event_marks_source_acs(self):
        media = make_media(Path(self.tmp), "acs.mp4")
        events_mod.emit_render_completed({"output_path": str(media), "title": "ACS",
                                          "clip_id": "b1:0"})
        row = self.app.db.automation_media_by_path(str(media))
        self.assertIsNotNone(row)
        self.assertEqual(str(row["source"]), "acs")
        self.assertEqual(str(row["status"]), "ready")

    def test_cca_event_marks_source_cca(self):
        media = make_media(Path(self.tmp), "cca.mp4")
        events_mod.emit_clip_completed({"output_path": str(media), "title": "CCA",
                                        "clip_index": 3})
        row = self.app.db.automation_media_by_path(str(media))
        self.assertIsNotNone(row)
        self.assertEqual(str(row["source"]), "cca")

    def test_cca_handoff_can_be_disabled_independently(self):
        self.app.config.set("aut_cca_handoff", False)
        acs = make_media(Path(self.tmp), "a.mp4")
        cca = make_media(Path(self.tmp), "c.mp4")
        events_mod.emit_render_completed({"output_path": str(acs), "title": "A"})
        events_mod.emit_clip_completed({"output_path": str(cca), "title": "C"})
        self.assertIsNotNone(self.app.db.automation_media_by_path(str(acs)))
        self.assertIsNone(self.app.db.automation_media_by_path(str(cca)))

    def test_replayed_event_never_duplicates_media(self):
        media = make_media(Path(self.tmp), "again.mp4")
        payload = {"output_path": str(media), "title": "T"}
        for _ in range(3):
            events_mod.emit_render_completed(payload)
        self.assertEqual(len(self.app.db.automation_list_media("ready")), 1)

    def test_unreadable_content_is_rejected_with_reason(self):
        ghost = Path(self.tmp) / "never-written.mp4"
        self.mgr._on_content_finished({"output_path": str(ghost), "title": "G",
                                       "source": "cca"})
        row = self.app.db.automation_media_by_path(str(ghost))
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"]), "rejected")
        self.assertIn("tidak ditemukan", str(row["reject_reason"]))
        self.assertEqual(self.app.db.automation_list_media("ready"), [])
        self.assertEqual(len(self.mgr.rejected_media()), 1)

    def test_media_counts_are_real(self):
        self.mgr._on_content_finished(
            {"output_path": str(make_media(Path(self.tmp), "1.mp4")), "title": "1",
             "source": "acs"})
        self.mgr._on_content_finished(
            {"output_path": str(make_media(Path(self.tmp), "2.mp4")), "title": "2",
             "source": "cca"})
        counts = self.mgr.media_counts()
        self.assertEqual(counts["acs"], 1)
        self.assertEqual(counts["cca"], 1)
        self.assertEqual(counts["total"], 2)


class TestAutoPilotEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="chopster-rule-")
        self.app = FakeApp(Path(self.tmp))
        self.mgr = AutomationManager(self.app)
        self.engine: AutoPilotEngine = self.mgr.autopilot

    def tearDown(self):
        try:
            self.mgr.stop()
        except Exception:
            pass
        try:
            self.app.db.close()
        except Exception:
            pass

    def _connect(self, platform: str = "youtube") -> str:
        acc_id = f"acc_{platform}"
        self.app.db.automation_upsert_account(acc_id, platform, display_name=platform.title(),
                                              external_id="ext", auth_mode="oauth",
                                              status="connected")
        return acc_id

    def _make_rule(self, *, sources=("acs", "cca"), platforms=("youtube",), config=None,
                   enabled=True, accounts=None) -> str:
        cfg = {"per_day": 5, "start_time": "10:00", "interval_minutes": 60,
               "approval": "auto", "active_days": list(ALL_DAYS)}
        if config:
            cfg.update(config)
        return self.engine.save_rule(name="Daily Shorts", sources=list(sources),
                                     platforms=list(platforms), accounts=accounts,
                                     config=cfg, enabled=enabled)

    def _feed(self, name: str, source: str = "acs", body: bytes = b"\x00" * 512) -> Path:
        path = make_media(Path(self.tmp), name, body)
        self.mgr._on_content_finished({"output_path": str(path), "title": name,
                                       "source": source})
        return path

    def test_rule_cannot_enable_without_account(self):
        rule_id = self._make_rule(enabled=False)
        result = self.mgr.set_rule_enabled(rule_id, True)
        self.assertFalse(result["ok"])
        self.assertFalse(result["enabled"])
        self.assertTrue(any("akun" in r.lower() for r in result["blocked"]))
        self.assertFalse(self.engine.get_rule(rule_id)["enabled"])

    def test_rule_cannot_enable_without_platform(self):
        self._connect()
        rule_id = self._make_rule(platforms=(), enabled=False)
        blocked = blocking_reasons(self.engine.get_rule(rule_id),
                                   self.app.db.automation_list_accounts())
        self.assertTrue(any("platform" in r.lower() for r in blocked))

    def test_rule_cannot_enable_with_invalid_schedule(self):
        self._connect()
        rule_id = self._make_rule(enabled=False, config={"active_hours": [22, 6]})
        result = self.mgr.set_rule_enabled(rule_id, True)
        self.assertFalse(result["ok"])
        self.assertTrue(any("jam operasional" in r.lower() for r in result["blocked"]))

    def test_enable_records_activation_and_disable_keeps_jobs(self):
        self._connect()
        rule_id = self._make_rule()
        view = self.engine.get_rule(rule_id)
        self.assertGreater(int(view["stats"].get("enabled_at_ts") or 0), 0)
        self._feed("keep.mp4")
        self.assertEqual(len(self.app.db.automation_rule_jobs(rule_id)), 1)
        self.mgr.set_rule_enabled(rule_id, False)
        self.assertEqual(len(self.app.db.automation_rule_jobs(rule_id)), 1)
        self._feed("after-off.mp4")
        self.assertEqual(len(self.app.db.automation_rule_jobs(rule_id)), 1)

    def test_finished_cca_clip_becomes_a_real_job(self):
        self._connect()
        rule_id = self._make_rule(sources=("cca",))
        self._feed("clip-cca.mp4", source="cca")
        jobs = self.app.db.automation_rule_jobs(rule_id)
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(str(job["platform"]), "youtube")
        self.assertEqual(str(job["source"]), "autopilot")
        self.assertEqual(str(job["rule_id"]), rule_id)
        self.assertEqual(str(job["status"]), "SCHEDULED")
        self.assertTrue(str(job["scheduled_at"]))
        self.assertEqual(self.engine.status(rule_id)["content_today"], 1)

    def test_both_sources_share_one_engine(self):
        self._connect()
        rule_id = self._make_rule(sources=("acs", "cca"))
        # Distinct bytes on purpose: identical content must be de-duplicated by
        # fingerprint, so this test proves two *different* clips both flow in.
        self._feed("a.mp4", source="acs", body=b"ACS-content" * 60)
        self._feed("c.mp4", source="cca", body=b"CCA-content" * 60)
        jobs = self.app.db.automation_rule_jobs(rule_id)
        self.assertEqual(len(jobs), 2)
        media_sources = {str(r["source"]) for r in self.app.db.automation_list_media("ready")}
        self.assertEqual(media_sources, {"acs", "cca"})
        self.assertEqual(len({str(j["run_id"]) for j in jobs}), 2)
        self.assertTrue(all(str(j["rule_id"]) == rule_id for j in jobs))

    def test_replayed_content_does_not_duplicate_jobs(self):
        self._connect()
        rule_id = self._make_rule()
        body = b"identical-bytes" * 64
        self._feed("first.mp4", body=body)
        self._feed("second-copy.mp4", body=body)
        self.assertEqual(len(self.app.db.automation_rule_jobs(rule_id)), 1)

    def test_per_day_cap_is_enforced(self):
        self._connect()
        rule_id = self._make_rule(config={"per_day": 1})
        self._feed("one.mp4", body=b"aaa" * 100)
        self._feed("two.mp4", body=b"bbb" * 100)
        self.assertEqual(len(self.app.db.automation_rule_jobs(rule_id)), 1)
        self.assertIn("harian", self.engine.get_rule(rule_id)["last_error"].lower())

    def test_content_finished_before_activation_is_not_autopublished(self):
        self._connect()
        rule_id = self._make_rule()
        future = int(time.time()) + 3600
        self.app.db.automation_update_rule(rule_id,
                                           stats=json.dumps({"enabled_at_ts": future}))
        self._feed("old.mp4")
        self.assertEqual(self.app.db.automation_rule_jobs(rule_id), [])

    def test_manual_backfill_is_explicit(self):
        self._connect()
        rule_id = self._make_rule()
        future = int(time.time()) + 3600
        self.app.db.automation_update_rule(rule_id,
                                           stats=json.dumps({"enabled_at_ts": future}))
        self._feed("older.mp4")
        self.assertEqual(self.app.db.automation_rule_jobs(rule_id), [])
        result = self.mgr.run_rule_now(rule_id)
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.app.db.automation_rule_jobs(rule_id)), 1)

    def test_review_approval_creates_a_gated_run(self):
        self._connect()
        rule_id = self._make_rule(config={"approval": "review"})
        self._feed("review.mp4")
        run_id = str(self.app.db.automation_rule_jobs(rule_id)[0]["run_id"])
        self.assertEqual(str(self.app.db.automation_get_run(run_id)["status"]), "READY")
        self.assertTrue(self.mgr.start_ready_run(run_id))
        self.assertEqual(str(self.app.db.automation_get_run(run_id)["status"]), "QUEUED")

    def test_global_switch_suspends_automatic_intake_but_keeps_media_and_run_now(self):
        """The Settings kill switch must suspend unattended publishing only.

        The clip still becomes ready media (nothing is lost), "Run now" still
        works because it is a user action, and switching it back on resumes
        automatic scheduling for the next finished clip.
        """
        self._connect()
        rule_id = self._make_rule()
        self.app.config.set("aut_autopilot_enabled", False)
        self.assertFalse(self.engine.automatic_enabled())
        self._feed("suspended.mp4")
        self.assertEqual(self.app.db.automation_rule_jobs(rule_id), [])
        self.assertEqual(len(self.app.db.automation_list_media("ready")), 1)
        result = self.mgr.run_rule_now(rule_id)
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.app.db.automation_rule_jobs(rule_id)), 1)
        self.app.config.set("aut_autopilot_enabled", True)
        self._feed("resumed.mp4", body=b"bbb" * 100)
        self.assertEqual(len(self.app.db.automation_rule_jobs(rule_id)), 2)

    def test_rule_survives_restart_and_keeps_its_queue(self):
        self._connect()
        rule_id = self._make_rule()
        self._feed("persist.mp4")
        before = self.app.db.automation_rule_jobs(rule_id)
        mgr2 = AutomationManager(self.app)
        try:
            mgr2.scheduler.reconcile_startup()
            view = mgr2.autopilot.get_rule(rule_id)
            self.assertIsNotNone(view)
            self.assertTrue(view["enabled"])
            self.assertEqual(view["sources_list"], ["acs", "cca"])
            after = self.app.db.automation_rule_jobs(rule_id)
            self.assertEqual(len(after), len(before))
            self.assertEqual(str(after[0]["scheduled_at"]), str(before[0]["scheduled_at"]))
            self.assertEqual(str(after[0]["status"]), "SCHEDULED")
        finally:
            mgr2.stop()

    def test_duplicate_and_delete_rule(self):
        self._connect()
        rule_id = self._make_rule(enabled=False)
        copy_id = self.mgr.duplicate_rule(rule_id)
        self.assertIsNotNone(copy_id)
        self.assertFalse(self.engine.get_rule(copy_id)["enabled"])
        self.assertEqual(len(self.mgr.list_rules()), 2)
        self.mgr.delete_rule(copy_id)
        self.assertEqual(len(self.mgr.list_rules()), 1)


class TestStaggeredBatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="chopster-stagger-")
        self.app = FakeApp(Path(self.tmp))
        self.mgr = AutomationManager(self.app)
        self.app.db.automation_upsert_account("acc_yt", "youtube", display_name="YT",
                                             external_id="e", auth_mode="oauth",
                                             status="connected")

    def tearDown(self):
        try:
            self.mgr.stop()
        except Exception:
            pass
        try:
            self.app.db.close()
        except Exception:
            pass

    def _clips(self, count: int) -> list[dict]:
        tmp = Path(self.tmp)
        return [{"path": str(make_media(tmp, f"c{i}.mp4", bytes([i]) * 300)),
                 "title": f"clip {i}"} for i in range(count)]

    def test_stagger_assigns_one_distinct_slot_per_clip(self):
        start = datetime.now().replace(second=0, microsecond=0) + timedelta(hours=1)
        run = self.mgr.create_run(
            name="Staggered 12", media_items=self._clips(12),
            platforms=[{"platform": "youtube"}], mode="stagger",
            stagger={"start": start.strftime("%Y-%m-%d %H:%M"), "interval_minutes": 120})
        jobs = self.app.db.automation_list_jobs(run_id=str(run["id"]))
        self.assertEqual(len(jobs), 12)
        slots = sorted(str(j["scheduled_at"]) for j in jobs)
        self.assertEqual(len(set(slots)), 12)
        self.assertEqual(slots[0], start.strftime("%Y-%m-%d %H:%M"))
        for job in jobs:
            self.assertEqual(str(job["status"]), "SCHEDULED")
            self.assertTrue(str(job["scheduled_at"]))

    def test_stagger_validation_surfaces_a_readable_message(self):
        with self.assertRaises(Exception) as ctx:
            self.mgr.create_run(
                name="bad", media_items=self._clips(1),
                platforms=[{"platform": "youtube"}], mode="stagger",
                stagger={"start": datetime.now().strftime("%Y-%m-%d %H:%M"),
                         "interval_minutes": 0})
        self.assertIn("Interval", str(getattr(ctx.exception, "user_message", "")))

    def test_preview_matches_enqueued_schedule(self):
        start = datetime.now().replace(second=0, microsecond=0) + timedelta(hours=2)
        stagger = {"start": start.strftime("%Y-%m-%d %H:%M"), "interval_minutes": 60}
        preview = self.mgr.preview_stagger(stagger, 4)
        run = self.mgr.create_run(name="preview", media_items=self._clips(4),
                                  platforms=[{"platform": "youtube"}], mode="stagger",
                                  stagger=stagger)
        jobs = sorted(self.app.db.automation_list_jobs(run_id=str(run["id"])),
                      key=lambda j: str(j["scheduled_at"]))
        self.assertEqual([dt.strftime("%Y-%m-%d %H:%M") for dt in preview],
                         [str(j["scheduled_at"]) for j in jobs])


class TestPipelineIntegration(unittest.TestCase):
    """Worker/adapter behaviour with mocked publishers (no real uploads)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="chopster-pipe-")
        self.app = FakeApp(Path(self.tmp))
        self.mgr = AutomationManager(self.app)

    def tearDown(self):
        try:
            self.mgr.stop()
        except Exception:
            pass
        try:
            self.app.db.close()
        except Exception:
            pass

    def _account(self, platform: str) -> str:
        acc_id = f"acc_{platform}"
        self.app.db.automation_upsert_account(acc_id, platform, display_name=platform.title(),
                                              external_id="e", auth_mode="oauth",
                                              status="connected")
        return acc_id

    def _runner(self) -> JobRunner:
        runner = JobRunner(self.mgr)
        runner.auth = types.SimpleNamespace(ensure_access_token=lambda aid: "fake-token")
        return runner

    def test_worker_publishes_due_job_through_adapter(self):
        from chopster.automation.publishers import PublishResult, VerifyResult
        self._account("youtube")
        media = make_media(Path(self.tmp))
        run = self.mgr.create_run(name="E2E", media_items=[{"path": str(media), "title": "T"}],
                                  platforms=[{"platform": "youtube"}], source="cca",
                                  overrides={"made_for_kids": False})
        job = self.app.db.automation_list_jobs(run_id=str(run["id"]))[0]

        class FakePublisher:
            platform = "youtube"

            def publish(self, ctx):
                ctx.progress(60, "uploading")
                return PublishResult(ok=True, external_id="vid-9",
                                     external_url="https://youtu.be/vid-9")

            def verify(self, ctx, external_id):
                return VerifyResult(ok=True, external_id="vid-9",
                                    external_url="https://youtu.be/vid-9",
                                    verification_status="verified")

        with mock.patch("chopster.automation.queue.get_publisher",
                        return_value=FakePublisher()):
            self._runner()._execute(str(job["id"]), threading.Event())
        job = self.app.db.automation_get_job(str(job["id"]))
        self.assertEqual(str(job["status"]), "PUBLISHED")
        self.assertEqual(str(job["external_post_id"]), "vid-9")
        self.assertEqual(str(job["source"]), "cca")
        self.assertEqual(str(self.app.db.automation_get_run(str(run["id"]))["status"]),
                         "COMPLETED")

    def test_one_platform_failure_does_not_block_the_other(self):
        from chopster.automation.publishers import PublishResult, VerifyResult
        self._account("youtube")
        self._account("tiktok")
        media = make_media(Path(self.tmp))
        run = self.mgr.create_run(name="Mixed", media_items=[{"path": str(media), "title": "T"}],
                                  platforms=[{"platform": "youtube"}, {"platform": "tiktok"}],
                                  content_pack={"title": "T", "captions": ["TikTok caption"]},
                                  overrides={"made_for_kids": False})
        jobs = {str(j["platform"]): j for j in
                self.app.db.automation_list_jobs(run_id=str(run["id"]))}

        class FakePublisher:
            def __init__(self, platform):
                self.platform = platform

            def publish(self, ctx):
                if self.platform == "tiktok":
                    raise InvalidMediaError("tiktok rejected",
                                            user_message="Media ditolak TikTok.")
                return PublishResult(ok=True, external_id="vid-1", external_url="u")

            def verify(self, ctx, external_id):
                return VerifyResult(ok=True, external_id="vid-1", external_url="u")

        runner = self._runner()
        with mock.patch("chopster.automation.queue.get_publisher",
                        side_effect=lambda app, auth, platform, auth_mode="": FakePublisher(platform)):
            runner._execute(str(jobs["youtube"]["id"]), threading.Event())
            runner._execute(str(jobs["tiktok"]["id"]), threading.Event())
        self.assertEqual(
            str(self.app.db.automation_get_job(str(jobs["youtube"]["id"]))["status"]),
            "PUBLISHED")
        self.assertEqual(
            str(self.app.db.automation_get_job(str(jobs["tiktok"]["id"]))["status"]), "FAILED")
        self.assertEqual(str(self.app.db.automation_get_run(str(run["id"]))["status"]),
                         "PARTIAL_SUCCESS")

    def test_retry_then_success_creates_no_duplicate_job(self):
        from chopster.automation.publishers import PublishResult, VerifyResult
        self._account("youtube")
        media = make_media(Path(self.tmp))
        run = self.mgr.create_run(name="Retry", media_items=[{"path": str(media)}],
                                  platforms=[{"platform": "youtube"}],
                                  overrides={"made_for_kids": False})
        job_id = str(self.app.db.automation_list_jobs(run_id=str(run["id"]))[0]["id"])
        state = {"calls": 0}

        class FlakyPublisher:
            platform = "youtube"

            def publish(self, ctx):
                state["calls"] += 1
                if state["calls"] == 1:
                    raise ConnectionError("network drop")
                return PublishResult(ok=True, external_id="vid-2", external_url="u")

            def verify(self, ctx, external_id):
                return VerifyResult(ok=True, external_id="vid-2", external_url="u")

        runner = self._runner()
        with mock.patch("chopster.automation.queue.get_publisher",
                        return_value=FlakyPublisher()):
            runner._execute(job_id, threading.Event())
            self.assertEqual(str(self.app.db.automation_get_job(job_id)["status"]), "QUEUED")
            self.assertEqual(int(self.app.db.automation_get_job(job_id)["attempt"]), 1)
            self.app.db.automation_update_job(job_id, scheduled_at="")
            runner._execute(job_id, threading.Event())
        self.assertEqual(str(self.app.db.automation_get_job(job_id)["status"]), "PUBLISHED")
        self.assertEqual(len(self.app.db.automation_list_jobs(run_id=str(run["id"]))), 1)

    def test_scheduled_job_is_not_claimed_before_its_time(self):
        self._account("youtube")
        media = make_media(Path(self.tmp))
        future = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        run = self.mgr.create_run(name="Future", media_items=[{"path": str(media)}],
                                  platforms=[{"platform": "youtube"}], mode="schedule",
                                  scheduled_at=future)
        job_id = str(self.app.db.automation_list_jobs(run_id=str(run["id"]))[0]["id"])
        now_due = [str(d["id"]) for d in
                   self.app.db.automation_due_jobs(
                       datetime.now().strftime("%Y-%m-%d %H:%M:%S"))]
        self.assertNotIn(job_id, now_due)
        later_due = [str(d["id"]) for d in self.app.db.automation_due_jobs(future)]
        self.assertIn(job_id, later_due)

    def test_recovery_requeues_interrupted_but_not_successful_jobs(self):
        self._account("youtube")
        media = make_media(Path(self.tmp))
        run = self.mgr.create_run(name="Recovery", media_items=[{"path": str(media)}],
                                  platforms=[{"platform": "youtube"}])
        job_id = str(self.app.db.automation_list_jobs(run_id=str(run["id"]))[0]["id"])
        # Never reached the platform → safe to re-queue automatically.
        self.app.db.automation_update_job(job_id, status="AUTH_CHECK", attempt=0)
        self.mgr.scheduler.reconcile_startup()
        self.assertEqual(str(self.app.db.automation_get_job(job_id)["status"]), "QUEUED")
        # Interrupted *during* the upload → may already be on the platform, so it
        # is parked for the user instead of being uploaded a second time.
        self.app.db.automation_update_job(job_id, status="UPLOADING", attempt=0)
        self.mgr.scheduler.reconcile_startup()
        parked = self.app.db.automation_get_job(job_id)
        self.assertEqual(str(parked["status"]), "ACTION_REQUIRED")
        self.assertEqual(str(parked["reason"]), "verify_unknown")
        # A finished job is left exactly as it was.
        self.app.db.automation_update_job(job_id, status="PUBLISHED", reason="")
        self.mgr.scheduler.reconcile_startup()
        self.assertEqual(str(self.app.db.automation_get_job(job_id)["status"]), "PUBLISHED")


if __name__ == "__main__":
    unittest.main(verbosity=2)