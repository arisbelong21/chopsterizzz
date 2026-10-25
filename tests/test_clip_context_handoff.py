"""Handoff wiring: renderers emit clip_context, Automation persists it."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from chopster.automation import events as events_mod
from chopster.automation.context import (
    ClipContext,
    context_status,
    segments_from_words,
)
from chopster.automation.manager import AutomationManager, split_media_content
from tests.test_automation import FakeApp


def _media(tmp: Path, name: str) -> Path:
    p = tmp / name
    p.write_bytes(b"\x00\x00\x00\x18ftypmp42" + bytes(100) * 4)
    return p


def test_segments_from_words_groups_spoken_lines():
    words = [
        {"word": "Sate", "start": 0.0, "end": 0.4},
        {"word": "Padang", "start": 0.4, "end": 0.8},
        {"word": "lezat", "start": 3.0, "end": 3.5},   # pause > 0.7 -> new line
        {"word": "sekali", "start": 3.5, "end": 4.0},
    ]
    lines = segments_from_words(words)
    assert len(lines) == 2
    assert lines[0]["text"] == "Sate Padang"
    assert lines[1]["text"] == "lezat sekali"
    assert lines[0]["start"] == 0.0 and lines[1]["end"] == 4.0


def test_manager_persists_clip_context_from_event():
    with tempfile.TemporaryDirectory() as td:
        app = FakeApp(Path(td))
        mgr = AutomationManager(app)
        try:
            media = _media(Path(td), "clip.mp4")
            ctx = {
                "version": 1,
                "source": "acs",
                "source_asset_id": "b1:0",
                "project_id": "proj1",
                "media_path": str(media),
                "title": "Sate Padang",
                "language": "id",
                "clip_start": 0.0,
                "clip_end": 5.0,
                "evidence": "transcript analisis",
                "segments": [{"start": 0.0, "end": 5.0, "text": "cara membuat sate padang"},
                             {"start": 5.0, "end": 9.0, "text": "bumbu kuning yang khas"}],
            }
            events_mod.emit_render_completed({
                "output_path": str(media), "title": "Sate Padang",
                "clip_id": "b1:0", "clip_context": ctx, "source": "acs",
            })
            row = app.db.automation_media_by_path(str(media))
            assert row is not None
            pack, stored_ctx = split_media_content(row.get("content_pack"))
            assert stored_ctx == ctx
            restored = ClipContext.from_dict(stored_ctx)
            assert restored is not None
            assert restored.source_asset_id == "b1:0"
            ok, _ = context_status(restored)
            assert ok
        finally:
            app.db.close()


def test_create_run_attaches_clip_context_to_job_snapshot():
    with tempfile.TemporaryDirectory() as td:
        app = FakeApp(Path(td))
        mgr = AutomationManager(app)
        try:
            app.db.automation_upsert_account(
                "acc1", "youtube", display_name="YT", external_id="ch1",
                auth_mode="oauth", status="connected")
            media = _media(Path(td), "a.mp4")
            ctx = {"source": "cca", "source_asset_id": "cca:1",
                   "media_path": str(media), "title": "T", "clip_start": 1.0,
                   "clip_end": 4.0,
                   "segments": [{"start": 0.5, "end": 4.5, "text": "GPU tensor autograd untuk model besar"}]}
            run = mgr.create_run(
                name="Ctx", media_items=[{"path": str(media), "title": "T", "clip_context": ctx}],
                platforms=[{"platform": "youtube"}], overrides={"made_for_kids": False})
            job = app.db.automation_list_jobs(run_id=str(run["id"]))[0]
            pack_raw = json.loads(str(job["content_pack"]))
            assert pack_raw["clip_context"]["source_asset_id"] == "cca:1"
        finally:
            app.db.close()


def test_autopilot_builds_job_snapshot_from_wrapped_media_content():
    """Auto-Pilot must read the wrapped {pack, clip_context} media content and
    carry the rule's audience answer into the job snapshot."""
    with tempfile.TemporaryDirectory() as td:
        app = FakeApp(Path(td))
        mgr = AutomationManager(app)
        try:
            app.db.automation_upsert_account(
                "acc_yt", "youtube", display_name="YT", external_id="ch1",
                auth_mode="oauth", status="connected")
            rule_id = mgr.save_rule(
                name="Ctx Rule", sources=["acs"], platforms=["youtube"],
                config={"made_for_kids": False, "privacy": "unlisted"})
            mgr.set_rule_enabled(rule_id, True)
            media = _media(Path(td), "rule-clip.mp4")
            ctx = {"source": "acs", "source_asset_id": "b1:0",
                   "media_path": str(media), "title": "Rule Clip",
                   "clip_start": 0.0, "clip_end": 8.0,
                   "segments": [{"start": 0.0, "end": 8.0,
                                 "text": "cara membuat sate padang bumbu kuning"}]}
            events_mod.emit_render_completed({
                "output_path": str(media), "title": "Rule Clip",
                "clip_id": "b1:0", "clip_context": ctx, "source": "acs",
            })
            jobs = app.db.automation_rule_jobs(rule_id)
            assert len(jobs) == 1
            pack_raw = json.loads(str(jobs[0]["content_pack"]))
            assert pack_raw["clip_context"]["source_asset_id"] == "b1:0"
            assert pack_raw["made_for_kids"] is False
            assert pack_raw["privacy"] == "unlisted"
        finally:
            app.db.close()