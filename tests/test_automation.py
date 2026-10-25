"""Unit + integration tests for the Automation engine (spec §70)."""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
PARENT = ROOT.parent
for p in (str(PARENT), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from chopster.automation import auth as auth_mod  # noqa: E402
from chopster.automation import events as events_mod  # noqa: E402
from chopster.automation.auth import (  # noqa: E402
    DEFAULT_REDIRECT_URI, redirect_uri_for, run_desktop_oauth, token_is_fresh,
    validate_redirect_uri,
)
from chopster.automation.browser.actions import ActionValidator  # noqa: E402
from chopster.automation.content import (  # noqa: E402
    map_for_platform, normalize_content_pack, validate_publish_payload,
)
from chopster.automation.manager import AutomationManager  # noqa: E402
from chopster.automation.models import (  # noqa: E402
    AuthRequiredError, capabilities_for, derive_run_status, job_can_transition,
)
from chopster.automation.queue import JobRunner  # noqa: E402
from chopster.automation.utils import (  # noqa: E402
    backoff_seconds, idempotency_key, media_fingerprint, redact,
)
from chopster.app.configuration import Configuration  # noqa: E402
from chopster.app.database import Database  # noqa: E402


def make_media(tmp: Path, size: int = 100) -> Path:
    path = tmp / "clip.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + bytes(100) * max(1, size // 100))
    return path


class FakeApp:
    """Minimal Application stand-in backed by a temp directory."""

    def __init__(self, tmp: Path):
        self.data_dir = tmp
        self.config = Configuration(self.data_dir)
        self.db = Database(self.data_dir / "chopster.db")
        self.logger = logging.getLogger("test-automation")
        self.automation = None
        self.window = None


class TestStateMachine(unittest.TestCase):
    def test_job_transitions(self):
        self.assertTrue(job_can_transition("QUEUED", "AUTH_CHECK"))
        self.assertTrue(job_can_transition("UPLOADING", "PROCESSING"))
        self.assertTrue(job_can_transition("VERIFYING", "PUBLISHED"))
        self.assertTrue(job_can_transition("ACTION_REQUIRED", "CANCELLED"))
        self.assertTrue(job_can_transition("FAILED", "QUEUED"))
        self.assertFalse(job_can_transition("PUBLISHED", "QUEUED"))
        self.assertFalse(job_can_transition("CANCELLED", "RUNNING"))

    def test_run_derivation(self):
        self.assertEqual(derive_run_status(["PUBLISHED", "PUBLISHED"]), "COMPLETED")
        self.assertEqual(derive_run_status(["PUBLISHED", "SCHEDULED"]), "QUEUED")  # waiting for due time
        self.assertEqual(derive_run_status(["PUBLISHED", "FAILED"]), "PARTIAL_SUCCESS")
        self.assertEqual(derive_run_status(["FAILED", "FAILED"]), "FAILED")
        self.assertEqual(derive_run_status(["QUEUED", "QUEUED"]), "QUEUED")
        self.assertEqual(derive_run_status(["UPLOADING", "QUEUED"]), "RUNNING")
        self.assertEqual(derive_run_status(["ACTION_REQUIRED", "PUBLISHED"]), "ACTION_REQUIRED")
        self.assertEqual(derive_run_status(["CANCELLED", "CANCELLED"]), "CANCELLED")
        self.assertEqual(derive_run_status([]), "DRAFT")

    def test_capabilities(self):
        yt = capabilities_for("youtube")
        self.assertTrue(yt.can_schedule)
        ig_browser = capabilities_for("instagram", "browser")
        self.assertTrue(ig_browser.browser_fallback)
        self.assertEqual(ig_browser.auth_mode, "browser")


class TestContentMap(unittest.TestCase):
    def _pack(self) -> dict:
        return {
            "titles": ["Judul Bagus", "Judul Kedua"],
            "title": "Judul Bagus",
            "captions": ["Caption kuat"],
            "hashtags": ["#bisnis", "entrepreneur", "#bisnis", "#MOTIVASI"],
            "keywords": ["bisnis", "podcast"],
            "description_short": "Deskripsi pendek",
            "description_long": "Deskripsi panjang sekali untuk YouTube style.",
            "pinned_comment": "Komentar semat.",
        }

    def test_normalize(self):
        norm = normalize_content_pack(self._pack())
        self.assertEqual(norm.title, "Judul Bagus")
        self.assertIn("#motivasi", [t.lower() for t in norm.hashtags])
        self.assertEqual(norm.hashtags[0], "#bisnis")
        self.assertNotIn("entrepreneur", norm.hashtags)   # auto-prefixed & deduped list keeps prefixed form

    def test_youtube(self):
        payload = map_for_platform("youtube", normalize_content_pack(self._pack()), privacy="private")
        self.assertLessEqual(len(payload["title"]), 100)
        self.assertIn("#bisnis", payload["description"])
        self.assertLessEqual(len(payload["description"]), 5000)
        self.assertTrue(all(len(t) <= 30 for t in payload["tags"]))
        self.assertEqual(payload["privacy"], "private")

    def test_tiktok(self):
        payload = map_for_platform("tiktok", normalize_content_pack(self._pack()))
        self.assertIn("Caption kuat", payload["caption"])
        self.assertIn("#bisnis", payload["caption"])
        self.assertLessEqual(len(payload["caption"]), 2200)

    def test_tiktok_title_only_pack_is_not_blocked(self):
        """Regression: a manual import where the user fills only "Title" used to
        reach the queue as an empty TikTok payload and was parked with
        "Judul belum diisi" — the TikTok branch derived its text from the
        caption alone. The user's own title must be used instead."""
        pack = normalize_content_pack({
            "title": "about u the 1976",
            "titles": ["about u the 1976"],
            "description": "sing a song",
            "caption": "",
            "captions": [],
            "source": "manual",
        })
        payload = map_for_platform("tiktok", pack)
        self.assertEqual(payload["title"], "about u the 1976")
        self.assertEqual(payload["caption"], "about u the 1976")
        self.assertEqual(validate_publish_payload("tiktok", payload), [])

    def test_tiktok_empty_pack_is_still_blocked(self):
        """The fallback must never invent content: with neither title nor
        caption the pre-flight still refuses the upload."""
        payload = map_for_platform("tiktok", normalize_content_pack({}))
        self.assertEqual(payload["title"], "")
        self.assertEqual(payload["caption"], "")
        self.assertIn("Judul belum diisi",
                      validate_publish_payload("tiktok", payload))

    def test_tiktok_caption_still_wins_over_title(self):
        """Adding the title fallback must not change a pack that does have a
        caption (that text stays the single source for TikTok)."""
        payload = map_for_platform("tiktok", normalize_content_pack({
            "title": "Manual Title",
            "caption": "Caption kuat",
            "hashtags": ["#bisnis"],
        }))
        self.assertEqual(payload["title"], payload["caption"][:90])
        self.assertIn("Caption kuat", payload["caption"])
        self.assertNotIn("Manual Title", payload["caption"])

    def test_instagram(self):
        payload = map_for_platform("instagram", normalize_content_pack(self._pack()))
        self.assertIn("#bisnis", payload["caption"])
        self.assertLessEqual(len(payload["caption"]), 2200)

    def test_unknown_platform(self):
        with self.assertRaises(ValueError):
            map_for_platform("myspace", normalize_content_pack(self._pack()))


class TestIdempotency(unittest.TestCase):
    def test_fingerprint_stable(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "v.mp4"
            path.write_bytes(b"x" * 2048)
            a = media_fingerprint(path)
            b = media_fingerprint(path)
            self.assertEqual(a, b)
        self.assertTrue(a)

    def test_idempotency_key(self):
        k1 = idempotency_key("youtube", "acc", "fp", "publish")
        k2 = idempotency_key("youtube", "acc", "fp", "publish")
        k3 = idempotency_key("youtube", "acc", "fp", "publishX")
        self.assertEqual(k1, k2)
        self.assertNotEqual(k1, k3)


class TestSecretsRedaction(unittest.TestCase):
    def test_redact(self):
        line = "Authorization: Bearer abc123DEF456 access_token=ya29.secret&refresh_token=hello"
        out = redact(line)
        self.assertNotIn("ya29.secret", out)
        self.assertNotIn("abc123DEF456", out)
        self.assertIn("<redacted>", out)


class TestValidator(unittest.TestCase):
    def test_actions(self):
        with tempfile.TemporaryDirectory() as td:
            media = Path(td) / "upload.mp4"
            media.write_bytes(b"x" * 100)
            v = ActionValidator("instagram", allowed_files=[str(media)], current_url="https://www.instagram.com/")
            ok, _ = v.validate({"type": "CLICK", "target": {"role": "button", "name": "Create"}})
            self.assertTrue(ok)
            ok, _ = v.validate({"type": "UPLOAD_FILE", "target": {"selector": "input[type='file']"}, "value": str(media)})
            self.assertTrue(ok)
            ok, reason = v.validate({"type": "UPLOAD_FILE", "target": {}, "value": "C:/Windows/notepad.exe"})
            self.assertFalse(ok)
            self.assertIn("not associated", reason)
            ok, reason = v.validate({"type": "NAVIGATE", "target": None, "value": "https://evil.example.com"})
            self.assertFalse(ok)
            ok, reason = v.validate({"type": "CLICK", "target": {"coordinates": [10, 20]}})
            self.assertFalse(ok)
            ok, _ = v.validate({"type": "CLICK", "target": {"text": "Upload video"}})
            self.assertTrue(ok)


class TestManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="chopster-aut-")
        self.app = FakeApp(Path(self.tmp))
        self.mgr = AutomationManager(self.app)
        self.media = make_media(Path(self.tmp))

    def tearDown(self):
        try:
            self.mgr.stop()
        except Exception:
            pass
        try:
            self.app.db.close()
        except Exception:
            pass

    def _connect_account(self, platform: str) -> str:
        acc_id = f"test_{platform}_acc"
        self.app.db.automation_upsert_account(acc_id, platform, display_name=f"{platform.title()} Test", external_id="ext-1", auth_mode="oauth", status="connected")
        return acc_id

    def test_create_run(self):
        self._connect_account("youtube")
        self._connect_account("tiktok")
        run = self.mgr.create_run(name="Test Run", media_items=[{"path": str(self.media), "title": "clip"}], platforms=[{"platform": "youtube"}, {"platform": "tiktok"}], content_pack={"title": "Judul", "description": "Desc"})
        self.assertEqual(str(run.get("status")), "QUEUED")
        jobs = self.app.db.automation_list_jobs(run_id=str(run["id"]))
        self.assertEqual(len(jobs), 2)
        self.assertEqual(sorted(j["platform"] for j in jobs), ["tiktok", "youtube"])
        self.assertTrue(all(str(j["status"]) == "QUEUED" for j in jobs))
        self.assertTrue(all(j["media_fingerprint"] for j in jobs))
        self.assertTrue(all(j["idempotency_key"] for j in jobs))

    def test_create_run_missing_account(self):
        with self.assertRaises(Exception) as ctx:
            self.mgr.create_run(name="X", media_items=[{"path": str(self.media)}], platforms=[{"platform": "youtube"}])
        self.assertIn("youtube", str(getattr(ctx.exception, "user_message", "")))

    def test_run_requires_media(self):
        with self.assertRaises(Exception):
            self.mgr.create_run(name="X", media_items=[], platforms=[{"platform": "youtube"}])

    def test_ready_review_run(self):
        self._connect_account("youtube")
        run = self.mgr.create_run(name="Review", media_items=[{"path": str(self.media)}], platforms=[{"platform": "youtube"}], review=True)
        self.assertEqual(str(run.get("status")), "READY")
        self.assertTrue(self.mgr.start_ready_run(str(run["id"])))
        self.assertEqual(str(self.app.db.automation_get_run(str(run["id"]))["status"]), "QUEUED")

    def test_cancel_run(self):
        self._connect_account("youtube")
        run = self.mgr.create_run(name="Cancel", media_items=[{"path": str(self.media)}], platforms=[{"platform": "youtube"}])
        self.mgr.cancel_run(str(run["id"]))
        job = self.app.db.automation_list_jobs(run_id=str(run["id"]))[0]
        self.assertEqual(str(job["status"]), "CANCELLED")
        self.assertEqual(str(self.app.db.automation_get_run(str(run["id"]))["status"]), "CANCELLED")

    def test_retry_job(self):
        self._connect_account("youtube")
        run = self.mgr.create_run(name="Retry", media_items=[{"path": str(self.media)}], platforms=[{"platform": "youtube"}])
        job = self.app.db.automation_list_jobs(run_id=str(run["id"]))[0]
        self.app.db.automation_update_job(str(job["id"]), status="FAILED", error_code="UNKNOWN_RESULT")
        self.assertTrue(self.mgr.retry_job(str(job["id"])))
        self.assertEqual(str(self.app.db.automation_get_job(str(job["id"]))["status"]), "QUEUED")
        self.assertEqual(int(self.app.db.automation_get_job(str(job["id"]))["attempt"]), 0)

    def test_delete_job_only_for_finished_jobs(self):
        """A row may only be removed once it is finished — deleting a running
        job would orphan its worker (complaint: rows could only be cancelled)."""
        self._connect_account("youtube")
        run = self.mgr.create_run(name="Hapus",
                                  media_items=[{"path": str(self.media)}],
                                  platforms=[{"platform": "youtube"}])
        job = self.app.db.automation_list_jobs(run_id=str(run["id"]))[0]
        job_id = str(job["id"])

        # Still active -> refused with a user-facing message, row survives.
        with self.assertRaises(Exception) as ctx:
            self.mgr.delete_job(job_id)
        self.assertIn("batalkan", str(getattr(ctx.exception, "user_message", "")).lower())
        self.assertIsNotNone(self.app.db.automation_get_job(job_id))

        # Finished rows delete cleanly and disappear from Queue & History.
        self.app.db.automation_update_job(job_id, status="CANCELLED")
        self.assertTrue(self.mgr.delete_job(job_id))
        self.assertIsNone(self.app.db.automation_get_job(job_id))
        self.assertEqual(self.app.db.automation_list_jobs(run_id=str(run["id"])), [])
        # …and the emptied run is closed instead of lingering as an empty Draft.
        self.assertEqual(str(self.app.db.automation_get_run(str(run["id"]))["status"]),
                         "CANCELLED")
        self.assertFalse(self.mgr.delete_job("job_tidak_ada"))

    def test_duplicate_protection(self):
        self._connect_account("youtube")
        run = self.mgr.create_run(name="Dup", media_items=[{"path": str(self.media)}], platforms=[{"platform": "youtube"}])
        job = self.app.db.automation_list_jobs(run_id=str(run["id"]))[0]
        key = str(job["idempotency_key"])
        self.assertTrue(key)
        self.app.db.automation_update_job(str(job["id"]), status="UPLOADING")
        self.assertTrue(self.app.db.automation_find_idempotency(key))
        self.assertEqual(len(self.app.db.automation_find_idempotency(key, exclude_job_id=str(job["id"]))), 0)

    # -- creation-time duplicate guard (queue "bingung": jangan bikin baris baru) --

    def test_create_run_refuses_media_already_in_queue(self):
        from chopster.automation.models import DuplicateInQueueError
        self._connect_account("youtube")
        self.mgr.create_run(name="Pertama", media_items=[{"path": str(self.media)}],
                            platforms=[{"platform": "youtube"}])
        runs = len(self.app.db.automation_list_runs())
        jobs = len(self.app.db.automation_list_jobs())
        with self.assertRaises(DuplicateInQueueError) as ctx:
            self.mgr.create_run(name="Kedua", media_items=[{"path": str(self.media)}],
                                platforms=[{"platform": "youtube"}])
        self.assertIn("sudah ada di queue", str(ctx.exception.user_message).lower())
        # Nothing was written: no extra run, no extra job card.
        self.assertEqual(len(self.app.db.automation_list_runs()), runs)
        self.assertEqual(len(self.app.db.automation_list_jobs()), jobs)

    def test_create_run_skip_mode_returns_none_for_all_duplicates(self):
        self._connect_account("youtube")
        self.mgr.create_run(name="Pertama", media_items=[{"path": str(self.media)}],
                            platforms=[{"platform": "youtube"}])
        runs = len(self.app.db.automation_list_runs())
        out = self.mgr.create_run(name="Kedua", media_items=[{"path": str(self.media)}],
                                  platforms=[{"platform": "youtube"}], on_duplicate="skip")
        self.assertIsNone(out)
        self.assertEqual(len(self.app.db.automation_list_runs()), runs)

    def test_create_run_queues_only_the_new_media(self):
        self._connect_account("youtube")
        other = Path(self.tmp) / "other.mp4"
        other.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x11" * 4096)
        self.mgr.create_run(name="Pertama", media_items=[{"path": str(self.media)}],
                            platforms=[{"platform": "youtube"}])
        run = self.mgr.create_run(
            name="Campur",
            media_items=[{"path": str(self.media)}, {"path": str(other)}],
            platforms=[{"platform": "youtube"}])
        jobs = self.app.db.automation_list_jobs(run_id=str(run["id"]))
        self.assertEqual(len(jobs), 1)                       # hanya clip yang baru
        self.assertEqual(str(jobs[0]["media_path"]), str(other))
        meta = json.loads(str(self.app.db.automation_get_run(str(run["id"]))["metadata"] or "{}"))
        self.assertEqual(len(meta.get("skipped_duplicates") or []), 1)

    def test_create_run_allowed_after_previous_published(self):
        self._connect_account("youtube")
        first = self.mgr.create_run(name="Pertama", media_items=[{"path": str(self.media)}],
                                    platforms=[{"platform": "youtube"}])
        job = self.app.db.automation_list_jobs(run_id=str(first["id"]))[0]
        self.app.db.automation_update_job(str(job["id"]), status="PUBLISHED")
        second = self.mgr.create_run(name="Kedua", media_items=[{"path": str(self.media)}],
                                     platforms=[{"platform": "youtube"}])
        self.assertEqual(len(self.app.db.automation_list_jobs(run_id=str(second["id"]))), 1)

    def test_acs_handoff_event(self):
        payload = {"batch_id": "b1", "clip_index": 0, "clip_id": "b1:0", "output_path": str(self.media), "title": "ACS Clip"}
        events_mod.emit_render_completed(payload)
        rows = self.app.db.automation_list_media("ready")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "ACS Clip")
        self.assertEqual(rows[0]["source"], "acs")
        # idempotent
        events_mod.emit_render_completed(payload)
        self.assertEqual(len(self.app.db.automation_list_media("ready")), 1)

    def test_acs_handoff_disabled(self):
        self.app.config.set("aut_acs_handoff", False)
        events_mod.emit_render_completed({"output_path": str(self.media), "title": "T"})
        self.assertEqual(len(self.app.db.automation_list_media("ready")), 0)

    def test_recipes(self):
        self.mgr._seed_recipes()
        rid = self.mgr.save_recipe("Custom", {"platforms": ["youtube"], "mode": "now", "time": "19:00", "repeat": "daily", "max_per_run": 1})
        recipes = self.mgr.list_recipes()
        self.assertTrue(any(r["id"] == rid for r in recipes))
        self.mgr.delete_recipe(rid)
        self.assertFalse(any(r["id"] == rid for r in self.mgr.list_recipes()))
        self.assertEqual(len(self.mgr.list_recipes()), 4)  # builtins reseeded

    def test_recipe_forwards_the_template_audience(self):
        """Recipe/preset runs must carry the template's made-for-kids answer.

        Regression: ``run_recipe`` called ``create_run`` without ``overrides``,
        so every YouTube job created by a preset had ``made_for_kids = None``
        and was parked with "Deklarasi audiens YouTube belum dipilih" — even
        though the same template published fine from the ACS 1-click path.
        """
        from chopster.automation.content import (
            map_for_platform, normalize_content_pack, validate_publish_payload,
        )
        self._connect_account("youtube")
        self.app.db.automation_add_media("med_recipe", str(self.media), title="Clip",
                                         clip_id="c1")
        self.app.db.automation_update_media("med_recipe", "ready")
        rid = self.mgr.save_recipe("Post Terjadwal", {
            "platforms": ["youtube"], "mode": "now", "time": "19:00",
            "max_per_run": 1, "made_for_kids": False, "privacy": "public",
        })
        run = self.mgr.run_recipe(rid, trigger="manual")
        self.assertIsNotNone(run)
        job = self.app.db.automation_list_jobs(run_id=str(run["id"]))[0]
        packet = json.loads(job["content_pack"])
        self.assertIs(packet.get("made_for_kids"), False)
        self.assertEqual(str(packet.get("privacy")), "public")
        # The payload the YouTube adapter builds now passes the pre-flight.
        payload = map_for_platform(
            "youtube", normalize_content_pack(packet.get("pack") or {}),
            privacy=str(packet.get("privacy") or ""),
            made_for_kids=packet.get("made_for_kids"))
        self.assertEqual(validate_publish_payload("youtube", payload), [])

    def test_recipe_without_audience_stays_blocked(self):
        """A template that never answered must not be published as a guess."""
        self._connect_account("youtube")
        self.app.db.automation_add_media("med_recipe2", str(self.media), title="Clip")
        self.app.db.automation_update_media("med_recipe2", "ready")
        rid = self.mgr.save_recipe("Tanpa Audiens", {
            "platforms": ["youtube"], "mode": "now", "time": "19:00", "max_per_run": 1,
        })
        run = self.mgr.run_recipe(rid, trigger="manual")
        job = self.app.db.automation_list_jobs(run_id=str(run["id"]))[0]
        self.assertIsNone(json.loads(job["content_pack"]).get("made_for_kids"))

    def test_recipe_forwards_the_clips_pack_and_transcript_context(self):
        """Regression: preset jobs carried an empty pack, so the YouTube
        description was a second copy of the clip title."""
        from unittest.mock import patch
        self._connect_account("youtube")
        ctx = {"clip_start": 0.0, "clip_end": 8.0, "segments": [
            {"start": 0.0, "end": 4.0, "text": "Jadi ceritanya saya"},
            {"start": 4.0, "end": 8.0, "text": "berangkat ke Amerika dulu."},
        ]}
        self.app.db.automation_add_media(
            "med_recipe3", str(self.media), title="Clip Amerika", clip_id="c3",
            content_pack=json.dumps({"clip_context": ctx}))
        self.app.db.automation_update_media("med_recipe3", "ready")
        rid = self.mgr.save_recipe("Paket Per Clip", {
            "platforms": ["youtube"], "mode": "now", "time": "19:00",
            "max_per_run": 1, "made_for_kids": False, "ai_language": "id",
        })
        ai_pack = {"title": "Judul AI", "description": "Deskripsi AI dari transkrip."}
        with patch.object(self.mgr, "_generate_template_pack",
                          return_value=ai_pack) as gen:
            run = self.mgr.run_recipe(rid, trigger="schedule")
        self.assertTrue(gen.called)
        packet = json.loads(
            self.app.db.automation_list_jobs(run_id=str(run["id"]))[0]["content_pack"])
        self.assertEqual(packet.get("pack_source"), "per_media")
        self.assertEqual(packet["pack"]["title"], "Judul AI")
        self.assertEqual(packet["pack"]["description"], "Deskripsi AI dari transkrip.")
        self.assertEqual(packet["clip_context"]["segments"], ctx["segments"])

        from chopster.automation.content import map_for_platform, normalize_content_pack
        payload = map_for_platform(
            "youtube", normalize_content_pack(packet["pack"]),
            made_for_kids=packet.get("made_for_kids"),
            clip_context=packet.get("clip_context"))
        self.assertNotEqual(payload["description"].strip(), payload["title"])

    def test_recipe_keeps_an_existing_pack_without_calling_the_ai(self):
        from unittest.mock import patch
        self._connect_account("youtube")
        stored = {"title": "Judul Simpanan", "description": "Deskripsi simpanan."}
        self.app.db.automation_add_media(
            "med_recipe4", str(self.media), title="Clip Simpanan",
            content_pack=json.dumps({"pack": stored}))
        self.app.db.automation_update_media("med_recipe4", "ready")
        rid = self.mgr.save_recipe("Paket Tersimpan", {
            "platforms": ["youtube"], "mode": "now", "time": "19:00",
            "max_per_run": 1, "made_for_kids": False,
        })
        with patch.object(self.mgr, "_generate_template_pack") as gen:
            run = self.mgr.run_recipe(rid, trigger="manual")
        self.assertFalse(gen.called)
        packet = json.loads(
            self.app.db.automation_list_jobs(run_id=str(run["id"]))[0]["content_pack"])
        self.assertEqual(packet.get("pack_source"), "per_media")
        self.assertEqual(packet["pack"]["description"], "Deskripsi simpanan.")

    def test_manual_preset_run_does_not_call_the_ai_in_the_ui_thread(self):
        """A manual run happens on the UI thread; the pack is generated later by
        the queue worker, so the window never freezes."""
        from unittest.mock import patch
        self._connect_account("youtube")
        ctx = {"clip_start": 0.0, "clip_end": 8.0, "segments": [
            {"start": 0.0, "end": 8.0, "text": "Kalimat transkrip klip."},
        ]}
        self.app.db.automation_add_media(
            "med_recipe5", str(self.media), title="Clip Manual",
            content_pack=json.dumps({"clip_context": ctx}))
        self.app.db.automation_update_media("med_recipe5", "ready")
        rid = self.mgr.save_recipe("Preset Manual", {
            "platforms": ["youtube"], "mode": "now", "time": "19:00",
            "max_per_run": 1, "made_for_kids": False,
        })
        with patch.object(self.mgr, "_generate_template_pack") as gen:
            run = self.mgr.run_recipe(rid, trigger="manual")
        self.assertFalse(gen.called)
        packet = json.loads(
            self.app.db.automation_list_jobs(run_id=str(run["id"]))[0]["content_pack"])
        self.assertEqual(packet["clip_context"]["segments"], ctx["segments"])
        self.assertEqual(packet.get("pack_source"), "single")

    def test_legacy_queue_import(self):
        import chopster.clipper.scheduler as scheduler_mod
        legacy = Path(self.tmp) / "publish_queue.json"
        legacy.write_text(json.dumps([{"id": "leg1", "file": str(self.media), "platform": "youtube", "title": "Legacy", "scheduled_at": ""}]), encoding="utf-8")
        self._connect_account("youtube")
        scheduler_mod.PATH = legacy
        imported = self.mgr.import_legacy_queue()
        self.assertEqual(imported, 1)
        leftover = legacy.read_text(encoding="utf-8").strip() if legacy.exists() else ""
        self.assertTrue(leftover in ("[]", "", "[]\n"))

    def test_legacy_queue_import_archives_only_migrated_items(self):
        """Finding-4 regression: the archive must list the migrated rows only."""
        import chopster.clipper.scheduler as scheduler_mod
        legacy = Path(self.tmp) / "publish_queue.json"
        legacy.write_text(json.dumps([
            {"id": "bad", "file": str(self.media), "platform": "myspace", "title": "Bad"},
            {"id": "ok", "file": str(self.media), "platform": "youtube", "title": "Ok"},
        ]), encoding="utf-8")
        self._connect_account("youtube")
        scheduler_mod.PATH = legacy
        imported = self.mgr.import_legacy_queue()
        self.assertEqual(imported, 1)
        # Only the item that could not be migrated stays in the staging file…
        kept = json.loads(legacy.read_text(encoding="utf-8"))
        self.assertEqual([i["id"] for i in kept], ["bad"])
        # …and the archive records exactly the item that was migrated.
        archive = Path(self.tmp) / "publish_queue.imported.json"
        archived = json.loads(archive.read_text(encoding="utf-8"))
        self.assertEqual([i["id"] for i in archived], ["ok"])


class TestRunnerRetry(unittest.TestCase):
    def test_backoff(self):
        self.assertEqual(backoff_seconds(0), 30)
        self.assertEqual(backoff_seconds(1), 60)
        self.assertEqual(backoff_seconds(10), 900)   # capped

    def test_handle_error_retry_and_fail(self):
        with tempfile.TemporaryDirectory() as td:
            app = FakeApp(Path(td))
            mgr = AutomationManager(app)
            app.db.automation_create_run("run1", name="R", status="QUEUED")
            app.db.automation_add_job("job1", "run1", str(Path(td) / "m.mp4"), "youtube", content_pack="{}", max_attempts=3)
            runner = JobRunner(mgr)
            from chopster.automation.models import AutomationError
            exc = AutomationError("boom", category="NETWORK_ERROR", retryable=True, user_message="jaringan")
            runner._handle_error("job1", exc)
            job = app.db.automation_get_job("job1")
            self.assertEqual(str(job["status"]), "QUEUED")
            self.assertEqual(int(job["attempt"]), 1)
            self.assertTrue(job["scheduled_at"])
            # exhaust attempts → FAILED
            for _ in range(3):
                runner._handle_error("job1", exc)
            job = app.db.automation_get_job("job1")
            self.assertEqual(str(job["status"]), "FAILED")
            # auth errors → ACTION_REQUIRED
            app.db.automation_add_job("job2", "run1", str(Path(td) / "n.mp4"), "youtube", max_attempts=3)
            from chopster.automation.models import AuthRequiredError
            runner._handle_error("job2", AuthRequiredError("x", user_message="reconnect"))
            self.assertEqual(str(app.db.automation_get_job("job2")["status"]), "ACTION_REQUIRED")
            app.db.close()


class TestAuthHelpers(unittest.TestCase):
    def test_token_freshness(self):
        fresh = {"access_token": "t", "expires_at": int(time.time()) + 3600}
        stale = {"access_token": "t", "expires_at": int(time.time()) - 5}
        self.assertTrue(token_is_fresh(fresh))
        self.assertFalse(token_is_fresh(stale))
        self.assertTrue(token_is_fresh({"access_token": "t"}))


class TestRunnerEndToEnd(unittest.TestCase):
    """Pipeline integration: QUEUED → AUTH_CHECK → PREPARING → UPLOADING → VERIFYING → PUBLISHED."""

    def test_full_publish_with_fake_publisher(self):
        import types
        from unittest.mock import patch
        from chopster.automation.publishers import PublishResult, VerifyResult

        with tempfile.TemporaryDirectory() as td:
            app = FakeApp(Path(td))
            mgr = AutomationManager(app)
            runner = JobRunner(mgr)
            runner.auth = types.SimpleNamespace(ensure_access_token=lambda aid: "fake-token")
            media = make_media(Path(td))
            app.db.automation_upsert_account("acc1", "youtube", display_name="YT", external_id="ch1", auth_mode="oauth", status="connected")
            # YouTube requires the audience question to be answered; the pre-flight
            # would otherwise park the job as ACTION_REQUIRED (spec §8.4).
            run = mgr.create_run(name="E2E", media_items=[{"path": str(media), "title": "clip"}], platforms=[{"platform": "youtube"}], content_pack={"title": "T", "description": "D"}, overrides={"made_for_kids": False})
            job = app.db.automation_list_jobs(run_id=str(run["id"]))[0]

            class FakePublisher:
                platform = "youtube"

                def publish(self, ctx):
                    ctx.progress(50, "uploading")
                    return PublishResult(ok=True, external_id="vid-1", external_url="https://youtu.be/vid-1")

                def verify(self, ctx, external_id):
                    return VerifyResult(ok=True, external_id="vid-1", external_url="https://youtu.be/vid-1", verification_status="verified")

            with patch("chopster.automation.queue.get_publisher", return_value=FakePublisher()):
                runner._execute(str(job["id"]), threading.Event())
            job = app.db.automation_get_job(str(job["id"]))
            self.assertEqual(str(job["status"]), "PUBLISHED")
            self.assertEqual(str(job["external_post_id"]), "vid-1")
            self.assertEqual(str(job["external_url"]), "https://youtu.be/vid-1")
            run = app.db.automation_get_run(str(run["id"]))
            self.assertEqual(str(run["status"]), "COMPLETED")
            app.db.close()


class TestAgent(unittest.TestCase):
    def test_agent_missing_runtime(self):
        from chopster.automation.browser import session as bsession
        saved = bsession.playwright_installed
        bsession.playwright_installed = lambda: False
        try:
            from chopster.automation.browser.agent import publish_via_agent
            outcome = publish_via_agent(app=object(), platform="instagram", media_path="x.mp4", caption="c", privacy="public", progress=lambda p, l: None, cancel_check=lambda: False)
            self.assertFalse(outcome["ok"])
            self.assertEqual(outcome["category"], "UNSUPPORTED_OPERATION")
        finally:
            bsession.playwright_installed = saved


class TestBrowserSessionAuth(unittest.TestCase):
    """Auth gate for Browser Session accounts (TikTok/Facebook/Instagram).

    Regression: the job runner calls ``ensure_access_token`` before selecting a
    publisher. A browser account has no API token, so the gate must accept the
    persistent-profile session — otherwise every browser publish fails at
    AUTH_CHECK even while the session is valid.
    """

    def _app_with_browser_account(self, td: Path, platform: str):
        app = FakeApp(td)
        from chopster.automation.auth import AuthManager
        auth = AuthManager(app)
        aid = f"acc_{platform}br_test"
        app.db.automation_upsert_account(
            aid, platform, display_name=f"{platform} browser",
            auth_mode="browser", status="connected",
            credential_ref=f"browser:{platform}")
        return app, auth, aid

    def test_browser_account_uses_profile_session(self):
        from chopster.automation.browser import session as bsession
        for platform in ("instagram", "tiktok", "facebook"):
            with self.subTest(platform=platform):
                with tempfile.TemporaryDirectory() as td:
                    tmp = Path(td)
                    with mock.patch.object(bsession, "profiles_root",
                                           lambda: tmp / "profiles"):
                        app, auth, aid = self._app_with_browser_account(tmp, platform)
                        # Valid session: marker exists.
                        marker = bsession._marker_path(platform, aid)
                        marker.parent.mkdir(parents=True, exist_ok=True)
                        marker.write_text('{"verified_at": "2026-01-01 00:00:00"}',
                                          encoding="utf-8")
                        self.assertTrue(bsession.session_valid(platform, aid))
                        self.assertEqual(auth.ensure_access_token(aid), "browser-session")
                        state = auth.statuses(force=True)[platform]["state"]
                        self.assertEqual(state, "connected")

                        # Logged out: marker removed -> actionable AUTH_REQUIRED.
                        marker.unlink()
                        with self.assertRaises(AuthRequiredError):
                            auth.ensure_access_token(aid)
                        state = auth.statuses(force=True)[platform]["state"]
                        self.assertEqual(state, "session_required")
                        app.db.close()

    def test_probe_statuses_direct_platform_call_ok(self):
        """Sanity: API-mode accounts still probe through the platform auth."""
        with tempfile.TemporaryDirectory() as td:
            app = FakeApp(Path(td))
            from chopster.automation.auth import AuthManager
            auth = AuthManager(app)
            app.db.automation_upsert_account(
                "acc_yt_api", "youtube", display_name="YT", auth_mode="oauth",
                status="connected", credential_ref="x")
            # No token stored -> the probe must report expired, not crash.
            state = auth.statuses(force=True)["youtube"]["state"]
            self.assertEqual(state, "expired")
            app.db.close()


class TestBrowserAccountReuse(unittest.TestCase):
    """Connecting Browser Session twice must not mint duplicate accounts.

    Regression: ``_connect_browser`` used ``new_id()`` unconditionally, so every
    click created another account row with an identical display name and a fresh
    empty profile — the profile the user had actually logged into then lost the
    auto-pick because duplicates shadowed it.
    """

    def _auth(self, tmp: Path):
        app = FakeApp(tmp)
        from chopster.automation.auth import AuthManager
        return app, AuthManager(app)

    def test_repeat_connect_reuses_existing_account(self):
        from chopster.automation.browser import session as bsession
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            app, auth = self._auth(tmp)
            try:
                with mock.patch.object(bsession, "profiles_root", lambda: tmp / "profiles"), \
                        mock.patch.object(bsession, "ensure_login", lambda *a, **k: True):
                    first = auth.connect("tiktok", mode="browser")["account"]
                    second = auth.connect("tiktok", mode="browser")["account"]
                self.assertEqual(first["id"], second["id"])
                self.assertEqual(len(app.db.automation_list_accounts("tiktok")), 1)
            finally:
                app.db.close()

    def test_connect_reuses_account_with_live_session(self):
        from chopster.automation.browser import session as bsession
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            app, auth = self._auth(tmp)
            try:
                with mock.patch.object(bsession, "profiles_root", lambda: tmp / "profiles"):
                    app.db.automation_upsert_account(
                        "acc_stale", "tiktok", display_name="Tiktok (Browser Session)",
                        auth_mode="browser", status="connected", credential_ref="browser:tiktok")
                    marker = bsession._marker_path("tiktok", "acc_live")
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    marker.write_text('{"verified_at": "2026-01-01 00:00:00"}', encoding="utf-8")
                    app.db.automation_upsert_account(
                        "acc_live", "tiktok", display_name="Tiktok (Browser Session)",
                        auth_mode="browser", status="connected", credential_ref="browser:tiktok")
                    with mock.patch.object(bsession, "ensure_login", lambda *a, **k: True):
                        account = auth.connect("tiktok", mode="browser")["account"]
                self.assertEqual(account["id"], "acc_live")
                self.assertEqual(len(app.db.automation_list_accounts("tiktok")), 2)
            finally:
                app.db.close()


class TestBrowserAccountAutoPick(unittest.TestCase):
    """create_run must not silently pick a sessionless browser duplicate.

    All Browser Session accounts on a platform share one display name, so the
    old status-only pick could land on an orphan row (no profile) while the
    account the user logged into sat right next to it.
    """

    def test_prefers_browser_account_with_live_session(self):
        from chopster.automation.browser import session as bsession
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            app = FakeApp(tmp)
            mgr = AutomationManager(app)
            media = make_media(tmp)
            try:
                with mock.patch.object(bsession, "profiles_root", lambda: tmp / "profiles"):
                    # Inserted first, so the old "first connected" pick chose it.
                    app.db.automation_upsert_account(
                        "acc_stale", "tiktok", display_name="Tiktok (Browser Session)",
                        auth_mode="browser", status="connected", credential_ref="browser:tiktok")
                    marker = bsession._marker_path("tiktok", "acc_live")
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    marker.write_text('{"verified_at": "2026-01-01 00:00:00"}', encoding="utf-8")
                    app.db.automation_upsert_account(
                        "acc_live", "tiktok", display_name="Tiktok (Browser Session)",
                        auth_mode="browser", status="connected", credential_ref="browser:tiktok")
                    run = mgr.create_run(
                        name="Pick", media_items=[{"path": str(media), "title": "clip"}],
                        platforms=[{"platform": "tiktok"}],
                        content_pack={"title": "T", "description": "D"})
                    jobs = app.db.automation_list_jobs(run_id=str(run["id"]))
                self.assertEqual([j["account_id"] for j in jobs], ["acc_live"])
            finally:
                try:
                    mgr.stop()
                except Exception:
                    pass
                app.db.close()

    def test_falls_back_when_no_browser_session_exists(self):
        from chopster.automation.browser import session as bsession
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            app = FakeApp(tmp)
            mgr = AutomationManager(app)
            media = make_media(tmp)
            try:
                with mock.patch.object(bsession, "profiles_root", lambda: tmp / "profiles"):
                    app.db.automation_upsert_account(
                        "acc_stale", "tiktok", display_name="Tiktok (Browser Session)",
                        auth_mode="browser", status="connected", credential_ref="browser:tiktok")
                    run = mgr.create_run(
                        name="Pick", media_items=[{"path": str(media), "title": "clip"}],
                        platforms=[{"platform": "tiktok"}],
                        content_pack={"title": "T", "description": "D"})
                    jobs = app.db.automation_list_jobs(run_id=str(run["id"]))
                self.assertEqual([j["account_id"] for j in jobs], ["acc_stale"])
            finally:
                try:
                    mgr.stop()
                except Exception:
                    pass
                app.db.close()


class TestTikTokChunkPlan(unittest.TestCase):
    """TikTok Content Posting API chunking (official media transfer guide).

    Every chunk must be 5-64 MB, ``total_chunk_count = floor(size / chunk_size)``
    and the trailing chunk may exceed chunk_size (up to 128 MB). Declaring
    chunk_size == video_size (the previous behaviour) was only accepted for files
    that happened to land inside 5-64 MB.
    """

    def test_empty_and_tiny(self):
        from chopster.automation.publishers import tiktok_chunk_plan
        self.assertEqual(tiktok_chunk_plan(0), (0, 0))
        self.assertEqual(tiktok_chunk_plan(1_000_000), (1_000_000, 1))

    def test_single_chunk_up_to_64mb(self):
        from chopster.automation.publishers import TIKTOK_MAX_CHUNK, tiktok_chunk_plan
        self.assertEqual(tiktok_chunk_plan(5_000_000), (5_000_000, 1))
        self.assertEqual(tiktok_chunk_plan(TIKTOK_MAX_CHUNK), (TIKTOK_MAX_CHUNK, 1))

    def test_large_file_is_split(self):
        from chopster.automation.publishers import (
            TIKTOK_MIN_CHUNK, TIKTOK_PREFERRED_CHUNK, tiktok_chunk_plan,
        )
        size = 70_000_000
        chunk, total = tiktok_chunk_plan(size)
        self.assertEqual(chunk, TIKTOK_PREFERRED_CHUNK)
        self.assertEqual(total, size // chunk)          # floor(size/chunk)
        last_size = size - (total - 1) * chunk
        self.assertGreaterEqual(last_size, TIKTOK_MIN_CHUNK)
        self.assertLessEqual(last_size, 2 * chunk)

    def test_upload_ranges_match_the_declared_plan(self):
        """The PUT Content-Range boundaries must follow chunk_size/count exactly."""
        with tempfile.TemporaryDirectory() as td:
            app = FakeApp(Path(td))
            from chopster.automation.auth import AuthManager
            from chopster.automation.publishers import PublishContext, TikTokPublisher
            pub = TikTokPublisher(app, AuthManager(app))
            size = 25_000
            media = Path(td) / "clip.mp4"
            media.write_bytes(b"\0" * size)
            ctx = PublishContext(
                job={"id": "j"}, account={"id": "a"}, payload={},
                media_path=str(media), progress=lambda p, l: None,
                cancel_check=lambda: False)
            seen: list[tuple[int, int]] = []

            class _Resp:
                status_code = 200
                text = ""

            def _fake_put(url, headers=None, data=None, timeout=None):
                span = headers["Content-Range"].split()[1].split("/")[0]
                start, end = (int(x) for x in span.split("-"))
                seen.append((start, end))
                return _Resp()

            with mock.patch("chopster.automation.publishers.requests.put", _fake_put):
                pub._tiktok_upload("http://u", size, ctx, 10_000, 3)

            self.assertEqual(seen, [(0, 9_999), (10_000, 19_999), (20_000, 24_999)])
            app.db.close()

    def test_transient_failure_retries_the_same_chunk(self):
        """A dropped connection must not consume the next chunk index."""
        import requests
        with tempfile.TemporaryDirectory() as td:
            app = FakeApp(Path(td))
            from chopster.automation.auth import AuthManager
            from chopster.automation.publishers import PublishContext, TikTokPublisher
            pub = TikTokPublisher(app, AuthManager(app))
            size = 25_000
            media = Path(td) / "clip.mp4"
            media.write_bytes(b"\0" * size)
            ctx = PublishContext(
                job={"id": "j"}, account={"id": "a"}, payload={},
                media_path=str(media), progress=lambda p, l: None,
                cancel_check=lambda: False)
            seen: list[tuple[int, int]] = []

            class _Resp:
                status_code = 200
                text = ""

            def _flaky_put(url, headers=None, data=None, timeout=None):
                span = headers["Content-Range"].split()[1].split("/")[0]
                start, end = (int(x) for x in span.split("-"))
                if len(seen) == 0:
                    seen.append((start, end))
                    raise requests.ConnectionError("reset")
                seen.append((start, end))
                return _Resp()

            with mock.patch("chopster.automation.publishers.requests.put", _flaky_put), \
                    mock.patch("chopster.automation.publishers.time.sleep", lambda s: None):
                pub._tiktok_upload("http://u", size, ctx, 10_000, 3)

            # First attempt failed, so chunk 0 is sent twice and the ranges stay aligned.
            self.assertEqual(seen, [(0, 9_999), (0, 9_999),
                                    (10_000, 19_999), (20_000, 24_999)])
            app.db.close()

    def _tiktok_inbox_ctx(self, app, td):
        from chopster.automation.publishers import PublishContext
        return PublishContext(
            job={"id": "j"}, account={"id": "a"}, payload={"caption": "c"},
            media_path=str(make_media(Path(td))), progress=lambda p, l: None,
            cancel_check=lambda: False)

    def test_inbox_poll_accepts_send_to_user_inbox(self):
        """``SEND_TO_USER_INBOX`` is the documented inbox terminal status.

        Waiting only for ``SEND_TO_USER`` made the poll burn its whole budget and
        report a failure for drafts TikTok had already delivered to the inbox.
        """
        with tempfile.TemporaryDirectory() as td:
            app = FakeApp(Path(td))
            from chopster.automation.auth import AuthManager
            from chopster.automation.publishers import TikTokPublisher
            pub = TikTokPublisher(app, AuthManager(app))
            ctx = self._tiktok_inbox_ctx(app, td)

            class _Resp:
                status_code = 200

                def json(self):
                    return {"data": {"status": "SEND_TO_USER_INBOX"}, "error": {"code": "ok"}}

            calls = {"n": 0}

            def _fake_post(*a, **k):
                calls["n"] += 1
                return _Resp()

            with mock.patch("chopster.automation.publishers.requests.post", _fake_post), \
                    mock.patch("chopster.automation.publishers.time.sleep", lambda s: None):
                status = pub._poll_status("tok", "pub_1", ctx)

            self.assertEqual(status, "SEND_TO_USER_INBOX")
            self.assertEqual(calls["n"], 1)        # terminal on the first answer
            app.db.close()

    def test_inbox_publish_reports_draft_ready(self):
        """A delivered inbox draft ends as ACTION_REQUIRED/DRAFT_READY, never PUBLISHED."""
        with tempfile.TemporaryDirectory() as td:
            app = FakeApp(Path(td))
            from chopster.automation.auth import AuthManager
            from chopster.automation.publishers import TikTokPublisher
            pub = TikTokPublisher(app, AuthManager(app))
            ctx = self._tiktok_inbox_ctx(app, td)

            class _Resp:
                def __init__(self, payload, status_code=200):
                    self._payload = payload
                    self.status_code = status_code
                    self.text = ""

                def json(self):
                    return self._payload

            def _fake_post(url, headers=None, data=None, json=None, timeout=None):
                if url.endswith("/inbox/video/init/"):
                    return _Resp({"data": {"publish_id": "pub_9", "upload_url": "http://upload"},
                                  "error": {"code": "ok"}})
                return _Resp({"data": {"status": "SEND_TO_USER_INBOX"}, "error": {"code": "ok"}})

            with mock.patch("chopster.automation.publishers.requests.post", _fake_post), \
                    mock.patch("chopster.automation.publishers.requests.put",
                               lambda *a, **k: _Resp({}, status_code=206)), \
                    mock.patch.object(pub.auth, "ensure_access_token", return_value="tok"), \
                    mock.patch("chopster.automation.publishers.time.sleep", lambda s: None):
                result = pub.publish(ctx)

            self.assertTrue(result.ok)
            self.assertEqual(result.status, "ACTION_REQUIRED")
            self.assertEqual(result.verification_status, "DRAFT_READY")
            self.assertEqual(result.external_id, "pub_9")
            self.assertTrue(pub.verify_unknown_is_action)   # inbox never auto-retries
            app.db.close()

    def test_inbox_publish_pending_status_asks_the_user(self):
        """A still-processing upload must not be reported as FAILED.

        The draft may already be in the inbox, so the job goes to the user for a
        check instead of claiming a failure (and never an automatic re-send).
        """
        with tempfile.TemporaryDirectory() as td:
            app = FakeApp(Path(td))
            from chopster.automation.auth import AuthManager
            from chopster.automation.models import NeedsActionError
            from chopster.automation.publishers import TikTokPublisher
            pub = TikTokPublisher(app, AuthManager(app))
            ctx = self._tiktok_inbox_ctx(app, td)
            polls = {"n": 0}

            class _Resp:
                def __init__(self, payload, status_code=200):
                    self._payload = payload
                    self.status_code = status_code
                    self.text = ""

                def json(self):
                    return self._payload

            def _fake_post(url, headers=None, data=None, json=None, timeout=None):
                if url.endswith("/inbox/video/init/"):
                    return _Resp({"data": {"publish_id": "pub_9", "upload_url": "http://upload"},
                                  "error": {"code": "ok"}})
                polls["n"] += 1
                return _Resp({"data": {"status": "PROCESSING_UPLOAD"}, "error": {"code": "ok"}})

            with mock.patch("chopster.automation.publishers.requests.post", _fake_post), \
                    mock.patch("chopster.automation.publishers.requests.put",
                               lambda *a, **k: _Resp({}, status_code=206)), \
                    mock.patch.object(pub.auth, "ensure_access_token", return_value="tok"), \
                    mock.patch("chopster.automation.publishers.time.sleep", lambda s: None):
                with self.assertRaises(NeedsActionError) as caught:
                    pub.publish(ctx)

            self.assertEqual(caught.exception.reason, "verify_unknown")
            self.assertIn("Inbox TikTok", caught.exception.user_message)
            self.assertGreater(polls["n"], 1)       # polled more than once, then gave up politely
            app.db.close()


class TestReasonCodes(unittest.TestCase):
    """Machine-readable reason codes flow error → job row → diagnostics."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="chopster-reason-")
        self.app = FakeApp(Path(self.tmp))
        self.mgr = AutomationManager(self.app)
        self.media = make_media(Path(self.tmp))

    def tearDown(self):
        try:
            self.mgr.stop()
        except Exception:
            pass
        try:
            self.app.db.close()
        except Exception:
            pass

    def _connect_account(self, platform: str, auth_mode: str = "oauth") -> str:
        acc_id = f"reason_{platform}_{auth_mode}"
        self.app.db.automation_upsert_account(
            acc_id, platform, display_name=f"{platform} {auth_mode}",
            external_id="ext-1", auth_mode=auth_mode, status="connected",
            credential_ref="x")
        return acc_id

    def _make_job(self, platform: str = "youtube", auth_mode: str = "oauth") -> str:
        self._connect_account(platform, auth_mode)
        run = self.mgr.create_run(
            name="reason run", media_items=[{"path": str(self.media), "title": "clip"}],
            platforms=[{"platform": platform}],
            content_pack={"title": "T", "description": "D"})
        return str(self.app.db.automation_list_jobs(run_id=str(run["id"]))[0]["id"])

    def test_error_carries_reason(self):
        from chopster.automation.models import AuthRequiredError, NeedsActionError
        self.assertEqual(AuthRequiredError("x", reason="login_required").reason, "login_required")
        self.assertEqual(NeedsActionError("x", reason="captcha").reason, "captcha")
        self.assertEqual(NeedsActionError("x").reason, "")

    def test_reason_column_round_trips(self):
        job_id = self._make_job()
        self.app.db.automation_update_job(job_id, reason="policy_blocked")
        self.assertEqual(str(self.app.db.automation_get_job(job_id)["reason"]), "policy_blocked")

    def test_handle_error_marks_login_required(self):
        from chopster.automation.models import AuthRequiredError
        job_id = self._make_job()
        self.mgr.runner._handle_error(job_id, AuthRequiredError("nope", user_message="login dulu"))
        job = self.app.db.automation_get_job(job_id)
        self.assertEqual(str(job["status"]), "ACTION_REQUIRED")
        self.assertEqual(str(job["reason"]), "login_required")

    def test_handle_error_keeps_explicit_reason(self):
        from chopster.automation.models import NeedsActionError
        job_id = self._make_job()
        self.mgr.runner._handle_error(
            job_id, NeedsActionError("challenge", reason="captcha", user_message="captcha"))
        job = self.app.db.automation_get_job(job_id)
        self.assertEqual(str(job["status"]), "ACTION_REQUIRED")
        self.assertEqual(str(job["reason"]), "captcha")

    def test_retry_clears_reason(self):
        job_id = self._make_job()
        self.app.db.automation_update_job(
            job_id, status="FAILED", error_code="UNKNOWN_RESULT", reason="verify_unknown")
        self.assertTrue(self.mgr.retry_job(job_id))
        job = self.app.db.automation_get_job(job_id)
        self.assertEqual(str(job["status"]), "QUEUED")
        self.assertEqual(str(job["reason"]), "")

    def test_verify_and_resume_refuses_draft(self):
        job_id = self._make_job("tiktok")
        self.app.db.automation_update_job(
            job_id, status="ACTION_REQUIRED", verification_status="DRAFT_READY",
            error_code="DRAFT_READY", reason="draft_ready")
        ok, msg = self.mgr.verify_and_resume_job(job_id)
        self.assertFalse(ok)
        self.assertIn("Draft", msg)
        self.assertEqual(str(self.app.db.automation_get_job(job_id)["status"]), "ACTION_REQUIRED")

    def test_verify_and_resume_requeues_action_required(self):
        job_id = self._make_job("youtube")
        self.app.db.automation_update_job(
            job_id, status="ACTION_REQUIRED", error_code="PERMISSION_DENIED", reason="")
        ok, msg = self.mgr.verify_and_resume_job(job_id)
        self.assertTrue(ok)
        self.assertIn("antrean", msg)
        self.assertEqual(str(self.app.db.automation_get_job(job_id)["status"]), "QUEUED")

    def test_browser_soft_cap_parks_jobs(self):
        from chopster.automation.models import NeedsActionError
        acc_id = self._connect_account("instagram", "browser")
        self.app.db.automation_add_job(
            "job_cap_1", "run_cap", str(self.media), "instagram",
            account_id=acc_id, status="PUBLISHED")
        self.app.config.update({"aut_browser_soft_cap": 1})
        with self.assertRaises(NeedsActionError) as ctx:
            self.mgr.runner._enforce_browser_soft_cap(acc_id, "instagram")
        self.assertEqual(getattr(ctx.exception, "reason", ""), "soft_cap")
        # Disabled (0) must never block.
        self.app.config.update({"aut_browser_soft_cap": 0})
        self.mgr.runner._enforce_browser_soft_cap(acc_id, "instagram")

    def test_test_mode_jobs_do_not_consume_soft_cap(self):
        """A simulation never posts, so it must not count as a real submission."""
        from datetime import datetime, timedelta
        acc_id = self._connect_account("instagram", "browser")
        self.app.db.automation_add_job(
            "job_cap_sim", "run_cap", str(self.media), "instagram",
            account_id=acc_id, status="ACTION_REQUIRED")
        self.app.db.automation_update_job("job_cap_sim", reason="test_mode")
        since = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        self.assertEqual(self.app.db.automation_count_account_jobs_since(acc_id, since), 0)
        self.app.db.automation_update_job("job_cap_sim", reason="login_required")
        self.assertEqual(self.app.db.automation_count_account_jobs_since(acc_id, since), 1)

    def test_test_mode_parks_browser_job_without_posting(self):
        """Test Mode is a dry-run guard: the job parks, nothing is published."""
        job_id = self._make_job("instagram", "browser")
        self.app.config.update({"aut_browser_test_mode": True})
        with mock.patch.object(self.mgr.auth, "ensure_access_token", return_value="browser"):
            self.mgr.runner._execute(job_id, threading.Event())
        job = self.app.db.automation_get_job(job_id)
        self.assertEqual(str(job["status"]), "ACTION_REQUIRED")
        self.assertEqual(str(job["reason"]), "test_mode")

    def test_diagnostics_shape(self):
        from chopster.automation.models import PLATFORMS
        self._connect_account("youtube")
        data = self.mgr.diagnostics()
        for key in ("playwright", "channel", "test_mode", "soft_cap", "platforms"):
            self.assertIn(key, data)
        self.assertEqual(sorted(p["platform"] for p in data["platforms"]), sorted(PLATFORMS))
        for entry in data["platforms"]:
            self.assertIn("adapter", entry)
            self.assertIn("policy_reviewed", entry)
            self.assertIn("last_failure", entry)
            self.assertIn("sessions", entry)

    def test_policy_review_is_reported(self):
        self._connect_account("tiktok")
        self.app.config.update({"aut_policy_review": {"tiktok": {"date": "2026-01-01"}}})
        data = self.mgr.diagnostics()
        entry = next(p for p in data["platforms"] if p["platform"] == "tiktok")
        self.assertEqual(entry["policy_reviewed"].get("date"), "2026-01-01")


class TestSchedulerReconcile(unittest.TestCase):
    def test_reconcile(self):
        with tempfile.TemporaryDirectory() as td:
            app = FakeApp(Path(td))
            mgr = AutomationManager(app)
            app.db.automation_create_run("run1", name="R", status="RUNNING")
            app.db.automation_add_job("j1", "run1", str(Path(td) / "m.mp4"), "youtube", status="UPLOADING", attempt=0, max_attempts=3)
            app.db.automation_add_job("j2", "run1", str(Path(td) / "n.mp4"), "youtube", status="AUTH_CHECK", attempt=3, max_attempts=3)
            mgr.scheduler.reconcile_startup()
            # j1 was interrupted *during* the upload: it may already exist on the
            # platform, so it is parked for the user instead of being re-sent.
            j1 = app.db.automation_get_job("j1")
            self.assertEqual(str(j1["status"]), "ACTION_REQUIRED")
            self.assertEqual(str(j1["reason"]), "verify_unknown")
            self.assertEqual(str(j1["error_code"]), "INTERRUPTED")
            # j2 never reached the platform but ran out of attempts → FAILED.
            self.assertEqual(str(app.db.automation_get_job("j2")["status"]), "FAILED")
            run = app.db.automation_get_run("run1")
            self.assertEqual(str(run["status"]), "ACTION_REQUIRED")
            app.db.close()

    def test_reconcile_keeps_emptied_run_closed(self):
        """Finding-3 regression: an emptied run must not resurface as a Draft."""
        with tempfile.TemporaryDirectory() as td:
            app = FakeApp(Path(td))
            mgr = AutomationManager(app)
            app.db.automation_create_run("run1", name="R", status="ACTION_REQUIRED")
            app.db.automation_add_job("j1", "run1", str(Path(td) / "m.mp4"), "youtube",
                                      status="PUBLISHED")
            self.assertTrue(mgr.delete_job("j1"))      # closes the emptied run
            self.assertEqual(str(app.db.automation_get_run("run1")["status"]), "CANCELLED")
            mgr.scheduler.reconcile_startup()          # must not resurrect it
            self.assertEqual(str(app.db.automation_get_run("run1")["status"]), "CANCELLED")
            app.db.close()

    def test_reconcile_does_not_autostart_a_review_run(self):
        """A READY run waits for the user's Start — even across a restart."""
        with tempfile.TemporaryDirectory() as td:
            app = FakeApp(Path(td))
            mgr = AutomationManager(app)
            app.db.automation_create_run("run1", name="R", status="READY")
            app.db.automation_add_job("j1", "run1", str(Path(td) / "m.mp4"), "youtube",
                                      status="QUEUED")
            mgr.scheduler.reconcile_startup()
            self.assertEqual(str(app.db.automation_get_run("run1")["status"]), "READY")
            app.db.close()


class TestCancelRunSemantics(unittest.TestCase):
    """Finding-2 regression: cancel_run must not fake-cancel a live upload."""

    @staticmethod
    def _run_with_jobs(app, td):
        app.db.automation_create_run("run1", name="R", status="QUEUED")
        app.db.automation_add_job("j_up", "run1", str(Path(td) / "m.mp4"), "youtube",
                                  status="UPLOADING", reason="")
        app.db.automation_add_job("j_q", "run1", str(Path(td) / "n.mp4"), "youtube",
                                  status="QUEUED", reason="")

    def test_inflight_job_is_signalled_not_precancelled(self):
        with tempfile.TemporaryDirectory() as td:
            app = FakeApp(Path(td))
            mgr = AutomationManager(app)
            self._run_with_jobs(app, td)
            # Simulate a live worker owning the in-flight row.
            ev = threading.Event()
            mgr.runner._active["j_up"] = ev
            mgr.cancel_run("run1")
            up = app.db.automation_get_job("j_up")
            # The row is not falsely marked CANCELLED while it is still uploading…
            self.assertEqual(str(up["status"]), "UPLOADING")
            self.assertEqual(str(up["last_activity"]), "membatalkan…")
            # …but the worker was actually asked to stop.
            self.assertTrue(ev.is_set())
            # A queued sibling is cancelled right away.
            self.assertEqual(str(app.db.automation_get_job("j_q")["status"]), "CANCELLED")
            self.assertEqual(str(app.db.automation_get_run("run1")["status"]), "CANCELLED")
            app.db.close()

    def test_stale_inflight_job_without_worker_is_cancelled(self):
        with tempfile.TemporaryDirectory() as td:
            app = FakeApp(Path(td))
            mgr = AutomationManager(app)
            self._run_with_jobs(app, td)
            mgr.cancel_run("run1")                     # no worker registered
            self.assertEqual(str(app.db.automation_get_job("j_up")["status"]), "CANCELLED")
            app.db.close()


class TestDuplicatePublishGuards(unittest.TestCase):
    """Regression guards for the duplicate-upload holes found in audit.

    Every test here locks one rule whose whole purpose is to stop the same
    video from being sent to the same platform twice.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="chopster-dupguard-")
        self.app = FakeApp(Path(self.tmp))
        self.mgr = AutomationManager(self.app)
        self.media = make_media(Path(self.tmp))

    def tearDown(self):
        try:
            self.mgr.stop()
        except Exception:
            pass
        try:
            self.app.db.close()
        except Exception:
            pass

    def _job(self, **fields) -> str:
        self.app.db.automation_upsert_account(
            "acc_yt_dup", "youtube", display_name="YT", external_id="ext-1",
            auth_mode="oauth", status="connected")
        run = self.mgr.create_run(
            name="dup", media_items=[{"path": str(self.media), "title": "clip"}],
            platforms=[{"platform": "youtube"}],
            content_pack={"title": "T", "description": "D"},
            overrides={"made_for_kids": False})
        job_id = str(self.app.db.automation_list_jobs(run_id=str(run["id"]))[0]["id"])
        if fields:
            self.app.db.automation_update_job(job_id, **fields)
        return job_id

    def _runner(self) -> JobRunner:
        import types
        runner = JobRunner(self.mgr)
        runner.auth = types.SimpleNamespace(ensure_access_token=lambda aid: "fake-token")
        return runner

    def _twin_job(self, src_id: str) -> str:
        """Queue a second job for the same media+platform+account (same key)."""
        src = self.app.db.automation_get_job(src_id)
        twin_id = "job_twin_dup_1"
        self.app.db.automation_add_job(
            twin_id, str(src["run_id"]), str(src["media_path"]), str(src["platform"]),
            account_id=str(src.get("account_id") or ""),
            media_fingerprint=str(src.get("media_fingerprint") or ""),
            idempotency_key=str(src.get("idempotency_key") or ""),
            content_pack=str(src.get("content_pack") or ""),
            status="QUEUED")
        return twin_id

    # -- hole 1: a restart mid-upload must never re-send -------------------
    def test_interrupted_upload_is_parked_and_still_retryable(self):
        job_id = self._job()
        self.app.db.automation_update_job(job_id, status="UPLOADING", attempt=0)
        self.mgr.scheduler.reconcile_startup()
        job = self.app.db.automation_get_job(job_id)
        self.assertEqual(str(job["status"]), "ACTION_REQUIRED")
        self.assertEqual(str(job["reason"]), "verify_unknown")
        # The user keeps control: after checking the platform they can push it.
        self.assertTrue(self.mgr.retry_job(job_id))
        self.assertEqual(str(self.app.db.automation_get_job(job_id)["status"]), "QUEUED")

    # -- hole 2: an unclear verification must never re-upload --------------
    def test_failed_verification_parks_instead_of_reuploading(self):
        from chopster.automation.publishers import PublishResult, VerifyResult

        calls = {"publish": 0}

        class VerifierFails:
            platform = "youtube"

            def publish(self, ctx):
                calls["publish"] += 1
                return PublishResult(ok=True, external_id="vid-1", external_url="u")

            def verify(self, ctx, external_id):
                return VerifyResult(ok=False, external_id="vid-1",
                                    message="Video belum muncul di API.")

        job_id = self._job()
        runner = self._runner()
        with mock.patch("chopster.automation.queue.get_publisher", return_value=VerifierFails()):
            runner._execute(job_id, threading.Event())

        job = self.app.db.automation_get_job(job_id)
        self.assertEqual(str(job["status"]), "ACTION_REQUIRED")
        self.assertEqual(str(job["reason"]), "verify_unknown")
        self.assertEqual(calls["publish"], 1)         # uploaded exactly once
        self.assertIn("Periksa", str(job["error_message"]))

    def test_verify_exception_parks_instead_of_reuploading(self):
        from chopster.automation.models import VerificationFailedError
        from chopster.automation.publishers import PublishResult

        calls = {"publish": 0}

        class VerifyRaises:
            platform = "youtube"

            def publish(self, ctx):
                calls["publish"] += 1
                return PublishResult(ok=True, external_id="vid-1", external_url="u")

            def verify(self, ctx, external_id):
                raise VerificationFailedError("HTTP 500", user_message="Verifikasi gagal dihubungi.")

        job_id = self._job()
        runner = self._runner()
        with mock.patch("chopster.automation.queue.get_publisher", return_value=VerifyRaises()):
            runner._execute(job_id, threading.Event())

        job = self.app.db.automation_get_job(job_id)
        self.assertEqual(str(job["status"]), "ACTION_REQUIRED")
        self.assertEqual(str(job["reason"]), "verify_unknown")
        self.assertEqual(calls["publish"], 1)         # never re-sent

    # -- hole 3: the idempotency window covers the in-flight claim states ---
    def test_idempotency_detects_a_twin_already_in_flight(self):
        job_id = self._job()
        key = str(self.app.db.automation_get_job(job_id)["idempotency_key"])
        self.assertTrue(key)
        # A claimed job sits in AUTH_CHECK and then PREPARING before any upload,
        # so those must count as "same media already on its way" (old hole: they
        # did not, and two twins claimed in one tick both uploaded).
        for status in ("AUTH_CHECK", "PREPARING", "UPLOADING", "PROCESSING",
                       "VERIFYING", "PUBLISHED", "SCHEDULED"):
            self.app.db.automation_update_job(job_id, status=status)
            found = self.app.db.automation_find_idempotency(key, exclude_job_id="job_lain")
            self.assertEqual([str(d["id"]) for d in found], [job_id],
                             f"status {status} tidak terdeteksi sebagai kembar")
        # A twin that merely stepped aside (QUEUED) must NOT count as duplicate,
        # otherwise two twins would defer to each other forever (livelock).
        for status in ("QUEUED", "RETRYING", "FAILED"):
            self.app.db.automation_update_job(job_id, status=status, reason="")
            self.assertEqual(
                self.app.db.automation_find_idempotency(key, exclude_job_id="job_lain"), [],
                f"status {status} tidak boleh dianggap duplikat")
        # ACTION_REQUIRED counts only when the park reason means the media may
        # already have reached the platform; a plain "user must fix this" park
        # must not block a fresh twin.
        self.app.db.automation_update_job(job_id, status="ACTION_REQUIRED", reason="login_required")
        self.assertEqual(
            self.app.db.automation_find_idempotency(key, exclude_job_id="job_lain"), [],
            "ACTION_REQUIRED/login_required tidak boleh dianggap duplikat")
        for reason in ("verify_unknown", "draft_ready"):
            self.app.db.automation_update_job(job_id, status="ACTION_REQUIRED", reason=reason)
            found = self.app.db.automation_find_idempotency(key, exclude_job_id="job_lain")
            self.assertEqual([str(d["id"]) for d in found], [job_id],
                             f"ACTION_REQUIRED/{reason} harus dianggap duplikat")

        # A job never counts as its own duplicate.
        self.app.db.automation_update_job(job_id, status="UPLOADING", reason="")
        self.assertEqual(self.app.db.automation_find_idempotency(key, exclude_job_id=job_id), [])

    def test_twin_job_steps_aside_instead_of_uploading_in_parallel(self):
        """The real finding-3 scenario: a twin is in flight when we start."""
        from chopster.automation.publishers import PublishResult, VerifyResult

        first_id = self._job()
        twin_id = self._twin_job(first_id)
        calls = {"publish": 0}

        class Publisher:
            platform = "youtube"

            def publish(self, ctx):
                calls["publish"] += 1
                return PublishResult(ok=True, external_id="vid-1", external_url="u")

            def verify(self, ctx, external_id):
                return VerifyResult(ok=True, external_id="vid-1", external_url="u",
                                    status="PUBLISHED")

        # The dispatcher claimed the first twin; it is in flight (AUTH_CHECK).
        self.app.db.automation_update_job(first_id, status="AUTH_CHECK")
        runner = self._runner()
        with mock.patch("chopster.automation.queue.get_publisher", return_value=Publisher()):
            runner._execute(twin_id, threading.Event())
        # It must step aside rather than upload the same media a second time.
        self.assertEqual(str(self.app.db.automation_get_job(twin_id)["status"]), "QUEUED")
        self.assertEqual(calls["publish"], 0)
        # Once the first twin is published, the second cancels as a duplicate.
        self.app.db.automation_update_job(first_id, status="PUBLISHED")
        with mock.patch("chopster.automation.queue.get_publisher", return_value=Publisher()):
            runner._execute(twin_id, threading.Event())
        self.assertEqual(str(self.app.db.automation_get_job(twin_id)["status"]), "CANCELLED")
        self.assertEqual(calls["publish"], 0)      # never uploaded twice

    def test_twin_blocked_when_partner_parked_after_upload(self):
        """Finding-1 regression: a parked "may have landed" job is a duplicate.

        The first twin uploaded and then parked as ACTION_REQUIRED because its
        verification was inconclusive. The second twin must step aside instead of
        re-uploading the same video — which is exactly what used to happen.
        """
        from chopster.automation.publishers import PublishResult, VerifyResult

        first_id = self._job()
        twin_id = self._twin_job(first_id)
        self.app.db.automation_update_job(
            first_id, status="ACTION_REQUIRED", reason="verify_unknown",
            verification_status="UNKNOWN")
        calls = {"publish": 0}

        class Publisher:
            platform = "youtube"

            def publish(self, ctx):
                calls["publish"] += 1
                return PublishResult(ok=True, external_id="vid-1", external_url="u")

            def verify(self, ctx, external_id):
                return VerifyResult(ok=True, external_id="vid-1", external_url="u",
                                    status="PUBLISHED")

        runner = self._runner()
        with mock.patch("chopster.automation.queue.get_publisher", return_value=Publisher()):
            runner._execute(twin_id, threading.Event())

        job = self.app.db.automation_get_job(twin_id)
        self.assertEqual(calls["publish"], 0)          # the media is never re-sent
        self.assertEqual(str(job["status"]), "QUEUED")  # it stepped aside instead
        self.assertEqual(int(job["attempt"]), 1)

    def test_published_twin_retires_the_parked_partner(self):
        """A parked "perlu login" twin is cancelled once its partner publishes.

        This is the queue-cleaning half of the duplicate story: without it the
        parked card lingers in the Queue forever after the video is already live.
        """
        from chopster.automation.publishers import PublishResult, VerifyResult

        parked_id = self._job()
        twin_id = self._twin_job(parked_id)
        self.app.db.automation_update_job(
            parked_id, status="ACTION_REQUIRED", reason="login_required",
            last_activity="perlu tindakanmu")
        self.app.db.automation_update_job(twin_id, status="QUEUED")

        class Publisher:
            platform = "youtube"

            def publish(self, ctx):
                return PublishResult(ok=True, external_id="vid-1", external_url="u")

            def verify(self, ctx, external_id):
                return VerifyResult(ok=True, external_id="vid-1", external_url="u",
                                    status="PUBLISHED")

        runner = self._runner()
        with mock.patch("chopster.automation.queue.get_publisher", return_value=Publisher()):
            runner._execute(twin_id, threading.Event())

        self.assertEqual(str(self.app.db.automation_get_job(twin_id)["status"]), "PUBLISHED")
        parked = self.app.db.automation_get_job(parked_id)
        self.assertEqual(str(parked["status"]), "CANCELLED")
        self.assertIn("otomatis", str(parked["last_activity"]))

    # -- hole 4: a platform-scheduled job is terminal, never "due" ---------
    def test_platform_scheduled_job_is_not_claimed_again(self):
        from datetime import datetime
        job_id = self._job(status="SCHEDULED", scheduled_at="", reason="")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # A run waiting for its own slot is still claimable…
        self.assertIn(job_id, [str(d["id"]) for d in self.app.db.automation_due_jobs(now)])
        # …but once the platform owns it, it must never be picked up again.
        self.app.db.automation_update_job(job_id, reason="scheduled_live")
        self.assertNotIn(job_id, [str(d["id"]) for d in self.app.db.automation_due_jobs(now)])


class TestJobsPersistence(unittest.TestCase):
    def test_token_never_in_db(self):
        with tempfile.TemporaryDirectory() as td:
            app = FakeApp(Path(td))
            app.db.automation_create_run("r1", name="R")
            app.db.automation_add_job("j", "r1", "m.mp4", "youtube", content_pack=json.dumps({"pack": {"title": "x"}, "privacy": ""}))
            raw = (Path(td) / "chopster.db").read_bytes().decode("utf-8", errors="ignore")
            self.assertNotIn("ya29.", raw)
            self.assertNotIn("refresh_token", raw)
            app.db.close()


class TestRedirectUri(unittest.TestCase):
    """Fixed loopback redirect URI — must be registrable verbatim at Meta/TikTok."""

    def test_default_valid(self):
        ok, uri = validate_redirect_uri(DEFAULT_REDIRECT_URI)
        self.assertTrue(ok)
        self.assertEqual(uri, "http://localhost:51234/")

    def test_normalizes_path(self):
        ok, uri = validate_redirect_uri("http://127.0.0.1:8080")
        self.assertTrue(ok)
        self.assertEqual(uri, "http://127.0.0.1:8080/")

    def test_rejects_https(self):
        ok, msg = validate_redirect_uri("https://example.com/cb")
        self.assertFalse(ok)
        self.assertIn("http", msg)

    def test_rejects_non_loopback(self):
        ok, _ = validate_redirect_uri("http://example.com:8080/cb")
        self.assertFalse(ok)

    def test_rejects_missing_port(self):
        ok, _ = validate_redirect_uri("http://localhost/")
        self.assertFalse(ok)

    def test_config_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            app = FakeApp(Path(td))
            self.assertEqual(redirect_uri_for(app), DEFAULT_REDIRECT_URI)
            app.config.update({"aut_oauth_redirect_uri": "http://localhost:59999/cb"})
            self.assertEqual(redirect_uri_for(app), "http://localhost:59999/cb")
            app.config.update({"aut_oauth_redirect_uri": "http://evil.example/cb"})
            self.assertEqual(redirect_uri_for(app), DEFAULT_REDIRECT_URI)
            app.db.close()


class TestConnectPreflight(unittest.TestCase):
    """Connect must fail fast with actionable setup steps, never a vague error."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="chopster-pre-")
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

    def test_youtube_missing_credentials(self):
        with self.assertRaises(AuthRequiredError) as ctx:
            self.mgr.auth.connect("youtube")
        self.assertIn("Client ID", str(ctx.exception))
        user_msg = ctx.exception.user_message
        self.assertIn("Developer Configuration", user_msg)
        self.assertIn(DEFAULT_REDIRECT_URI, user_msg)

    def test_youtube_missing_secret(self):
        self.app.config.update({"aut_youtube_client_id": "cid-only"})
        with self.assertRaises(AuthRequiredError) as ctx:
            self.mgr.auth.connect("youtube")
        self.assertIn("Client Secret", str(ctx.exception))

    def test_tiktok_hint_mentions_console(self):
        with self.assertRaises(AuthRequiredError) as ctx:
            self.mgr.auth.connect("tiktok")
        self.assertIn("Content Posting API", ctx.exception.user_message)

    def test_facebook_hint_mentions_redirect_registration(self):
        with self.assertRaises(AuthRequiredError) as ctx:
            self.mgr.auth.connect("facebook")
        self.assertIn("Valid OAuth Redirect URIs", ctx.exception.user_message)

    def test_tiktok_user_info_uses_get_and_basic_fields(self):
        """Display API ``/v2/user/info/`` is a GET endpoint.

        Regression: the code POSTed to it, which makes TikTok's gateway answer
        ``404 Unsupported path(Janus)`` right after a successful authorization.
        Only fields covered by ``user.info.basic`` may be requested.
        """
        auth = self.mgr.auth.for_platform("tiktok")
        seen: dict = {}

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {"data": {"user": {"open_id": "oid-1", "display_name": "Aris"}},
                        "error": {"code": "ok", "message": ""}}

        def fake_get(url, **kw):
            seen["method"] = "GET"
            seen["url"] = url
            seen["params"] = dict(kw.get("params") or {})
            return _Resp()

        def fake_post(*_a, **_kw):
            raise AssertionError("user/info must not be requested with POST")

        with mock.patch.object(auth_mod.requests, "get", side_effect=fake_get), \
                mock.patch.object(auth_mod.requests, "post", side_effect=fake_post):
            info = auth._user_info("tok")
        self.assertEqual(seen["method"], "GET")
        self.assertTrue(seen["url"].endswith("/v2/user/info/"))
        self.assertEqual(sorted(seen["params"].get("fields", "").split(",")),
                         ["display_name", "open_id"])
        self.assertEqual(info.get("display_name"), "Aris")


class TestLoopbackOAuth(unittest.TestCase):
    """Full local loopback flow with a fake token endpoint + simulated browser."""

    @staticmethod
    def _free_port() -> int:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
        probe.close()
        return port

    def test_local_authorization_and_token_exchange(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        received: dict = {}

        class TokenHandler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                received["body"] = self.rfile.read(length).decode("utf-8", "replace")
                body = json.dumps({"access_token": "tok-123", "expires_in": 3600,
                                   "refresh_token": "ref-1"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        token_srv = ThreadingHTTPServer(("127.0.0.1", 0), TokenHandler)
        threading.Thread(target=token_srv.serve_forever, daemon=True).start()
        token_url = f"http://127.0.0.1:{token_srv.server_address[1]}/token"
        redirect = f"http://localhost:{self._free_port()}/"
        auth_seen: dict = {}

        def simulate_browser(url: str) -> None:
            auth_seen["url"] = url

            def hit() -> None:
                time.sleep(0.4)
                query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                state = query["state"][0]
                cb = urllib.parse.urlparse(query["redirect_uri"][0])
                target = f"http://127.0.0.1:{cb.port}{cb.path}?code=code-1&state={state}"
                urllib.request.urlopen(target, timeout=5).read()

            threading.Thread(target=hit, daemon=True).start()

        try:
            with mock.patch.object(auth_mod.webbrowser, "open", return_value=True):
                token = run_desktop_oauth(
                    auth_url="https://example.invalid/auth",
                    auth_params={"client_id": "cid", "response_type": "code"},
                    token_url=token_url,
                    client_id="cid",
                    client_secret="sec",
                    scope="scope.a",
                    use_pkce=True,
                    timeout_sec=30,
                    redirect_uri=redirect,
                    on_browser_open=simulate_browser,
                )
            self.assertEqual(token.get("access_token"), "tok-123")
            # The auth request must carry the exact registrable redirect_uri.
            auth_query = urllib.parse.parse_qs(urllib.parse.urlparse(auth_seen["url"]).query)
            self.assertEqual(auth_query.get("redirect_uri"), [redirect])
            self.assertTrue(auth_query.get("code_challenge"))
            # Token exchange must reuse the same redirect_uri + PKCE verifier.
            parsed = urllib.parse.parse_qs(received.get("body", ""))
            self.assertEqual(parsed.get("grant_type"), ["authorization_code"])
            self.assertEqual(parsed.get("code"), ["code-1"])
            self.assertEqual(parsed.get("redirect_uri"), [redirect])
            self.assertTrue(parsed.get("code_verifier"))
        finally:
            try:
                token_srv.shutdown()
                token_srv.server_close()
            except Exception:
                pass

    def test_tiktok_flow_uses_pkce_hex_and_client_key(self):
        """TikTok desktop needs PKCE with a hex S256 challenge and client_key.

        Regression: without PKCE the authorize page bounced with "code
        challenge", and the token exchange must name the credential
        ``client_key`` (not ``client_id``).
        """
        import hashlib
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        received: dict = {}

        class TokenHandler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                received["body"] = self.rfile.read(length).decode("utf-8", "replace")
                body = json.dumps({"access_token": "tok-tt", "expires_in": 3600}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        token_srv = ThreadingHTTPServer(("127.0.0.1", 0), TokenHandler)
        threading.Thread(target=token_srv.serve_forever, daemon=True).start()
        token_url = f"http://127.0.0.1:{token_srv.server_address[1]}/token"
        redirect = f"http://localhost:{self._free_port()}/"
        auth_seen: dict = {}

        def simulate_browser(url: str) -> None:
            auth_seen["url"] = url

            def hit() -> None:
                time.sleep(0.4)
                query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                state = query["state"][0]
                cb = urllib.parse.urlparse(query["redirect_uri"][0])
                target = f"http://127.0.0.1:{cb.port}{cb.path}?code=code-1&state={state}"
                urllib.request.urlopen(target, timeout=5).read()

            threading.Thread(target=hit, daemon=True).start()

        try:
            with mock.patch.object(auth_mod.webbrowser, "open", return_value=True):
                run_desktop_oauth(
                    auth_url="https://www.tiktok.com/v2/auth/authorize/",
                    auth_params={"client_key": "ck", "response_type": "code"},
                    token_url=token_url,
                    client_id="ck",
                    client_secret="cs",
                    scope="user.info.basic,video.upload",
                    use_pkce=True, pkce_hex=True, token_client_key="client_key",
                    timeout_sec=30,
                    redirect_uri=redirect,
                    on_browser_open=simulate_browser,
                )
            auth_query = urllib.parse.parse_qs(urllib.parse.urlparse(auth_seen["url"]).query)
            challenge = auth_query["code_challenge"][0]
            self.assertEqual(auth_query.get("code_challenge_method"), ["S256"])
            # Hex SHA256, never base64url (TikTok rejects the latter).
            self.assertEqual(len(challenge), 64)
            self.assertTrue(all(c in "0123456789abcdef" for c in challenge))
            body = urllib.parse.parse_qs(received.get("body", ""))
            verifier = body["code_verifier"][0]
            self.assertEqual(challenge, hashlib.sha256(verifier.encode()).hexdigest())
            self.assertEqual(body.get("client_key"), ["ck"])
            self.assertNotIn("client_id", body)
        finally:
            try:
                token_srv.shutdown()
                token_srv.server_close()
            except Exception:
                pass

    def test_cancel_check_aborts_the_wait(self):
        """Closing Chopster must break the OAuth wait instead of hanging."""
        redirect = f"http://localhost:{self._free_port()}/"
        state = {"checks": 0}

        def cancel_check() -> bool:
            state["checks"] += 1
            return state["checks"] >= 3

        started = time.time()
        with mock.patch.object(auth_mod.webbrowser, "open", return_value=True):
            with self.assertRaises(AuthRequiredError) as ctx:
                run_desktop_oauth(
                    auth_url="https://example.invalid/auth",
                    auth_params={"client_id": "cid"},
                    token_url="https://example.invalid/token",
                    client_id="cid",
                    timeout_sec=300,
                    redirect_uri=redirect,
                    cancel_check=cancel_check,
                )
        self.assertLess(time.time() - started, 10)
        self.assertIn("dibatalkan", ctx.exception.user_message.lower())

    def test_busy_port_gives_clear_error(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = int(blocker.getsockname()[1])
        try:
            with mock.patch.object(auth_mod.webbrowser, "open",
                                   side_effect=AssertionError("browser must not open")):
                with self.assertRaises(AuthRequiredError) as ctx:
                    run_desktop_oauth(
                        auth_url="https://example.invalid/auth",
                        auth_params={"client_id": "cid"},
                        token_url="https://example.invalid/token",
                        client_id="cid",
                        timeout_sec=5,
                        redirect_uri=f"http://localhost:{port}/",
                    )
            self.assertIn(str(port), ctx.exception.user_message)
        finally:
            blocker.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
