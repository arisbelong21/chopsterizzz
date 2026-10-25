"""YouTube payload carries approved audience/privacy and never fabricates."""
from __future__ import annotations

import json
import pytest

from chopster.automation import content
from chopster.automation.content import (
    audience_declared,
    map_for_platform,
    normalize_content_pack,
    validate_publish_payload,
)
from chopster.automation.models import MetadataInvalidError
from chopster.automation.publishers import PublishContext, YouTubePublisher


def _pack() -> content.NormalizedContent:
    return normalize_content_pack({
        "title": "Sate Padang Klasik",
        "description": "Rahasia bumbu sate Padang, dipanggang di atas arang, disajikan hangat.",
        "hashtags": ["#sate", "#padang"],
        "keywords": ["kuliner", "sate padang"],
    })


def test_payload_carries_approved_audience_and_privacy():
    payload = map_for_platform("youtube", _pack(),
                               privacy="unlisted", made_for_kids=False,
                               category_id="22")
    assert payload["privacy"] == "unlisted"
    assert payload["made_for_kids"] is False
    assert payload["audience"] == "not_kids"
    assert payload["category_id"] == "22"
    assert audience_declared(payload)
    assert validate_publish_payload("youtube", payload) == []


def test_payload_made_for_kids_true():
    payload = map_for_platform("youtube", _pack(), made_for_kids=True)
    assert payload["made_for_kids"] is True
    assert payload["audience"] == "kids"
    assert audience_declared(payload)


def test_validation_blocks_missing_values():
    norm = _pack()
    # audience never answered -> must block, never defaults to False
    p1 = map_for_platform("youtube", norm, made_for_kids=None)
    assert not audience_declared(p1)
    probs = validate_publish_payload("youtube", p1)
    assert any("audiens" in x.lower() for x in probs)

    # empty title -> block
    p2 = map_for_platform("youtube", normalize_content_pack({}), made_for_kids=False)
    probs2 = validate_publish_payload("youtube", p2)
    assert any("judul" in x.lower() for x in probs2)

    # over-limit title -> block (raw payload that skipped map_for_platform)
    probs3 = validate_publish_payload("youtube", {
        "platform": "youtube",
        "title": "x" * 101,
        "description": "d",
        "made_for_kids": False,
    })
    assert any("100" in x for x in probs3)


def test_queue_preflight_parks_job_when_audience_missing():
    """Pipeline-level: a YouTube run without the audience answer is parked as
    ACTION_REQUIRED with METADATA_INVALID - the adapter is never reached."""
    import tempfile
    import threading
    import types
    from pathlib import Path
    from unittest.mock import patch

    from chopster.automation.manager import AutomationManager
    from chopster.automation.queue import JobRunner
    from tests.test_automation import FakeApp, make_media

    with tempfile.TemporaryDirectory() as td:
        app = FakeApp(Path(td))
        mgr = AutomationManager(app)
        runner = JobRunner(mgr)
        runner.auth = types.SimpleNamespace(ensure_access_token=lambda aid: "fake-token")
        try:
            app.db.automation_upsert_account(
                "acc1", "youtube", display_name="YT", external_id="ch1",
                auth_mode="oauth", status="connected")
            media = make_media(Path(td))
            run = mgr.create_run(
                name="NoAudience", media_items=[{"path": str(media), "title": "T"}],
                platforms=[{"platform": "youtube"}],
                content_pack={"title": "T", "description": "D"})  # no overrides
            job_id = str(app.db.automation_list_jobs(run_id=str(run["id"]))[0]["id"])

            with patch("chopster.automation.queue.get_publisher") as pub:
                runner._execute(job_id, threading.Event())
                pub.assert_not_called()
            job = app.db.automation_get_job(job_id)
            assert str(job["status"]) == "ACTION_REQUIRED"
            assert str(job["error_code"]) == "METADATA_INVALID"
        finally:
            app.db.close()


def test_youtube_publisher_metadata_uses_snapshot_verbatim():
    payload = map_for_platform("youtube", _pack(),
                               privacy="private", made_for_kids=False)
    ctx = PublishContext(
        job={}, account={}, payload=payload, media_path=r"C:\x.mp4")
    meta = YouTubePublisher(None, None)._metadata(ctx)
    assert meta["snippet"]["title"] == "Sate Padang Klasik"
    assert meta["snippet"]["description"].startswith("Rahasia bumbu")
    assert meta["status"]["privacyStatus"] == "private"
    assert meta["status"]["selfDeclaredMadeForKids"] is False

    payload_mfk = map_for_platform("youtube", _pack(), made_for_kids=True)
    ctx_mfk = PublishContext(job={}, account={}, payload=payload_mfk,
                             media_path=r"C:\x.mp4")
    assert YouTubePublisher(None, None)._metadata(ctx_mfk)["status"]["selfDeclaredMadeForKids"] is True


def test_youtube_publisher_raises_metadata_invalid():
    publisher = YouTubePublisher(None, None)
    for payload in (
        map_for_platform("youtube", normalize_content_pack({}), made_for_kids=False),   # empty title
        map_for_platform("youtube", _pack(), made_for_kids=None),                        # audience missing
    ):
        ctx = PublishContext(job={}, account={}, payload=payload, media_path=r"C:\x.mp4")
        with pytest.raises(MetadataInvalidError):
            publisher._metadata(ctx)


# --------------------------------------------------------------------------- #
# Descriptions must not be a second copy of the title
# --------------------------------------------------------------------------- #

_CTX = {
    "clip_start": 0.0,
    "clip_end": 12.0,
    "segments": [
        {"start": 0.0, "end": 4.0, "text": "Jadi ceritanya begini,"},
        {"start": 4.0, "end": 9.0, "text": "saya berangkat ke Amerika dulu."},
    ],
}


def test_description_uses_the_clip_transcript_when_the_pack_has_none():
    payload = map_for_platform(
        "youtube", normalize_content_pack({"title": "Kisah Awal Chef"}),
        made_for_kids=False, clip_context=_CTX)
    assert payload["description"].strip() != payload["title"]
    assert "berangkat ke Amerika" in payload["description"]


def test_description_that_only_repeats_the_title_is_replaced():
    pack = normalize_content_pack({"title": "Judul Sama", "description": "Judul Sama"})
    payload = map_for_platform("youtube", pack, made_for_kids=False,
                               clip_context=_CTX)
    assert payload["description"].strip() != "Judul Sama"
    assert "berangkat ke Amerika" in payload["description"]


def test_explicit_description_override_is_never_replaced():
    pack = normalize_content_pack({"title": "Judul Sama", "description": "Judul Sama"})
    payload = map_for_platform("youtube", pack, made_for_kids=False,
                               description_override="Deskripsi pilihan saya.",
                               clip_context=_CTX)
    assert payload["description"].startswith("Deskripsi pilihan saya.")


def test_title_is_still_the_last_resort_without_any_context():
    payload = map_for_platform("youtube", normalize_content_pack({"title": "Judul"}),
                               made_for_kids=False)
    assert payload["description"].strip() == "Judul"


def test_queue_generates_an_ai_description_when_the_job_has_no_pack():
    """A Studio publish without an AI pack gets one real description, and the
    generated pack is stored on the job so retries reuse it."""
    import tempfile
    import threading
    import types
    from pathlib import Path
    from unittest.mock import patch

    from chopster.automation.manager import AutomationManager
    from chopster.automation.publishers import PublishResult, VerifyResult
    from chopster.automation.queue import JobRunner
    from tests.test_automation import FakeApp, make_media

    with tempfile.TemporaryDirectory() as td:
        app = FakeApp(Path(td))
        mgr = AutomationManager(app)
        runner = JobRunner(mgr)
        runner.auth = types.SimpleNamespace(ensure_access_token=lambda aid: "fake-token")
        try:
            app.db.automation_upsert_account(
                "acc1", "youtube", display_name="YT", external_id="ch1",
                auth_mode="oauth", status="connected")
            media = make_media(Path(td))
            app.db.automation_add_media("med_desc", str(media), title="Judul Klip",
                                        content_pack=json.dumps({"clip_context": _CTX}))
            run = mgr.create_run(
                name="AI Desc", content_pack={},
                media_items=[{"path": str(media), "title": "Judul Klip"}],
                platforms=[{"platform": "youtube"}],
                overrides={"made_for_kids": False})
            job = app.db.automation_list_jobs(run_id=str(run["id"]))[0]

            seen = {}

            class FakePublisher:
                platform = "youtube"

                def publish(self, ctx):
                    seen["payload"] = ctx.payload
                    return PublishResult(ok=True, external_id="v1")

                def verify(self, ctx, external_id):
                    return VerifyResult(ok=True, external_id="v1",
                                        verification_status="verified")

            ai_pack = {"title": "Judul AI", "description": "Ringkasan AI dari transkrip."}
            with patch.object(mgr, "_generate_template_pack", return_value=ai_pack), \
                 patch("chopster.automation.queue.get_publisher",
                       return_value=FakePublisher()):
                runner._execute(str(job["id"]), threading.Event())

            assert seen["payload"]["description"].startswith("Ringkasan AI")
            stored = json.loads(app.db.automation_get_job(str(job["id"]))["content_pack"])
            assert stored["pack_source"] == "generated_at_publish"
            assert stored["pack"]["description"].startswith("Ringkasan AI")
        finally:
            app.db.close()


def test_queue_never_publishes_raw_speech_when_the_ai_is_down():
    """When the AI falls back, the job snapshot stays untouched (the mapper then
    uses the transcript instead of a fabricated pack)."""
    from chopster.automation.queue import JobRunner
    from unittest.mock import MagicMock

    runner = JobRunner(MagicMock())
    runner.db = MagicMock()
    runner.db.automation_media_by_path.return_value = {
        "media_path": "x.mp4",
        "content_pack": json.dumps({"clip_context": _CTX})}
    runner.manager = MagicMock()
    runner.manager._template_ai_config.return_value = {}
    runner.manager._generate_template_pack.return_value = {
        "title": "T", "description": "speech", "_fallback_reason": "AI gagal: 503"}
    pack_raw = {"pack": {"title": "T"}, "made_for_kids": False}
    assert runner._ensure_description({"id": "j", "media_path": "x.mp4"}, pack_raw) is pack_raw
    runner.db.automation_update_job.assert_not_called()


def test_queue_builds_a_transcript_description_for_a_packless_job():
    """Pipeline-level: a Studio/preset job without an AI pack must not upload
    the clip title as its description."""
    import tempfile
    import threading
    import types
    from pathlib import Path
    from unittest.mock import patch

    from chopster.automation.manager import AutomationManager
    from chopster.automation.publishers import PublishResult, VerifyResult
    from chopster.automation.queue import JobRunner
    from tests.test_automation import FakeApp, make_media

    with tempfile.TemporaryDirectory() as td:
        app = FakeApp(Path(td))
        mgr = AutomationManager(app)
        runner = JobRunner(mgr)
        runner.auth = types.SimpleNamespace(ensure_access_token=lambda aid: "fake-token")
        try:
            app.db.automation_upsert_account(
                "acc1", "youtube", display_name="YT", external_id="ch1",
                auth_mode="oauth", status="connected")
            media = make_media(Path(td))
            run = mgr.create_run(
                name="Desc", content_pack={},
                media_items=[{"path": str(media), "title": "Judul Klip",
                              "clip_context": _CTX}],
                platforms=[{"platform": "youtube"}],
                overrides={"made_for_kids": False})
            job = app.db.automation_list_jobs(run_id=str(run["id"]))[0]

            seen = {}

            class FakePublisher:
                platform = "youtube"

                def publish(self, ctx):
                    seen["payload"] = ctx.payload
                    return PublishResult(ok=True, external_id="v1")

                def verify(self, ctx, external_id):
                    return VerifyResult(ok=True, external_id="v1",
                                        verification_status="verified")

            with patch("chopster.automation.queue.get_publisher",
                       return_value=FakePublisher()):
                runner._execute(str(job["id"]), threading.Event())

            payload = seen["payload"]
            assert payload["title"] == "Judul Klip"
            assert payload["description"].strip() != payload["title"]
            assert "berangkat ke Amerika" in payload["description"]
        finally:
            app.db.close()