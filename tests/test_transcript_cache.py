"""Transcript cache contract for Auto Clip Studio and Content Clipper AI.

ACS used to fetch subtitles (or run Whisper) on every single run of the same
video. Subtitle endpoints can answer 429 for hours and Whisper needs minutes on
long podcasts, so the transcript is now persisted per (subject, language).

CCA already cached Whisper output, but keyed by file path + mtime; a
re-downloaded copy missed and retranscribed. The cache is now content based.
"""

import json

import pytest

from chopster.auto_clip_studio.engine.services import transcript_cache as acs_cache
from chopster.clipper import transcript_manager as cca_manager


LINES = [
    {"text": "Halo semuanya", "start": 0.0, "duration": 2.5},
    {"text": "Kita mulai dari sini", "start": 2.5, "duration": 3.0},
]


@pytest.fixture(autouse=True)
def isolated_appdata(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    yield tmp_path


# ── ACS cache ────────────────────────────────────────────────────────────────


def test_acs_cache_roundtrip_returns_same_lines():
    subject = acs_cache.subject_for_video("2F1KCji6e3Y")
    assert acs_cache.get_cached_transcript(subject, "id") is None

    path = acs_cache.save_cached_transcript(subject, "id", LINES, source="supadata", duration=100.0)
    assert path and path.exists()

    loaded = acs_cache.get_cached_transcript(subject, "id")
    assert loaded == LINES


def test_acs_cache_separates_languages():
    subject = acs_cache.subject_for_video("abc12345678")
    acs_cache.save_cached_transcript(subject, "id", LINES)

    assert acs_cache.get_cached_transcript(subject, "id") == LINES
    assert acs_cache.get_cached_transcript(subject, "en") is None


def test_acs_cache_separates_videos():
    first = acs_cache.subject_for_video("aaaaaaaaaaa")
    second = acs_cache.subject_for_video("bbbbbbbbbbb")
    acs_cache.save_cached_transcript(first, "", LINES)

    assert acs_cache.get_cached_transcript(first, "") == LINES
    assert acs_cache.get_cached_transcript(second, "") is None


def test_acs_cache_handles_automatic_language_as_its_own_bucket():
    subject = acs_cache.subject_for_video("ccccccccccc")
    acs_cache.save_cached_transcript(subject, "", LINES)

    assert acs_cache.get_cached_transcript(subject, None) == LINES
    assert acs_cache.get_cached_transcript(subject, "") == LINES


def test_acs_cache_ignores_empty_or_invalid_segments():
    subject = acs_cache.subject_for_video("ddddddddddd")
    assert acs_cache.save_cached_transcript(subject, "", []) is None
    assert acs_cache.save_cached_transcript(subject, "", [{"text": "   ", "start": 0}]) is None
    assert acs_cache.get_cached_transcript(subject, "") is None

    mixed = [{"text": "valid", "start": 1.0, "duration": 1.0}, {"text": "", "start": 2.0}, {"text": "no start", "start": "x"}]
    acs_cache.save_cached_transcript(subject, "", mixed)
    assert acs_cache.get_cached_transcript(subject, "") == [
        {"text": "valid", "start": 1.0, "duration": 1.0}
    ]


def test_acs_cache_rejects_corrupt_file():
    subject = acs_cache.subject_for_video("eeeeeeeeeee")
    path = acs_cache.save_cached_transcript(subject, "", LINES)
    path.write_text("{not json", encoding="utf-8")

    assert acs_cache.get_cached_transcript(subject, "") is None


def test_acs_cache_rejects_payload_from_another_version():
    subject = acs_cache.subject_for_video("fffffffffff")
    path = acs_cache.save_cached_transcript(subject, "", LINES)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["cache_version"] = 999
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert acs_cache.get_cached_transcript(subject, "") is None


def test_acs_file_signature_survives_copy_and_rename(tmp_path):
    original = tmp_path / "original.mp4"
    original.write_bytes(b"chopster-test-payload" * 1000)
    copy = tmp_path / "renamed-copy.mp4"
    copy.write_bytes(original.read_bytes())

    assert acs_cache.file_signature(original) == acs_cache.file_signature(copy)

    subject = acs_cache.subject_for_file(original)
    acs_cache.save_cached_transcript(subject, "id", LINES)
    assert acs_cache.get_cached_transcript(acs_cache.subject_for_file(copy), "id") == LINES


def test_acs_file_signature_differs_for_different_content(tmp_path):
    first = tmp_path / "a.mp4"
    second = tmp_path / "b.mp4"
    first.write_bytes(b"a" * 5000)
    second.write_bytes(b"b" * 5000)

    assert acs_cache.file_signature(first) != acs_cache.file_signature(second)


def test_acs_drive_subject_uses_stable_id_not_file_path(tmp_path):
    first = tmp_path / "gdrive_DRIVEID_tag1.mp4"
    second = tmp_path / "gdrive_DRIVEID_tag2.mp4"
    first.write_bytes(b"content")
    second.write_bytes(b"content")

    assert acs_cache.subject_for_file(first, video_id="gdrive_DRIVEID") == "drive:gdrive_driveid"
    assert acs_cache.subject_for_file(first, video_id="gdrive_DRIVEID") == acs_cache.subject_for_file(
        second, video_id="gdrive_DRIVEID"
    )


def test_acs_clear_cache_removes_all_languages_of_one_subject():
    subject = acs_cache.subject_for_video("ggggggggggg")
    other = acs_cache.subject_for_video("hhhhhhhhhhh")
    acs_cache.save_cached_transcript(subject, "id", LINES)
    acs_cache.save_cached_transcript(subject, "", LINES)
    acs_cache.save_cached_transcript(other, "", LINES)

    acs_cache.clear_cache(subject)

    assert acs_cache.get_cached_transcript(subject, "id") is None
    assert acs_cache.get_cached_transcript(subject, "") is None
    assert acs_cache.get_cached_transcript(other, "") == LINES


# ── CCA cache ────────────────────────────────────────────────────────────────


def _transcript() -> "cca_manager.Transcript":
    return cca_manager.Transcript(
        language="id",
        segments=[
            cca_manager.Segment(start=0.0, end=2.5, text="Halo semuanya"),
            cca_manager.Segment(start=2.5, end=5.5, text="Kita mulai dari sini"),
        ],
    )


def test_cca_cache_hit_for_same_path(tmp_path):
    media = tmp_path / "video.mp4"
    media.write_bytes(b"cca-payload" * 5000)

    cca_manager.save_cached_transcript(media, "small", "id", False, _transcript())
    loaded = cca_manager.load_cached_transcript(media, "small", "id", False)

    assert loaded is not None
    assert [s.text for s in loaded.segments] == ["Halo semuanya", "Kita mulai dari sini"]


def test_cca_cache_survives_redownload_at_new_path_with_new_mtime(tmp_path):
    original = tmp_path / "download-1.mp4"
    original.write_bytes(b"same-video-bytes" * 4000)
    cca_manager.save_cached_transcript(original, "small", "id", False, _transcript())

    redownloaded = tmp_path / "download-2.mp4"
    redownloaded.write_bytes(original.read_bytes())

    assert cca_manager.load_cached_transcript(redownloaded, "small", "id", False) is not None


def test_cca_cache_still_separates_transcription_settings(tmp_path):
    media = tmp_path / "video.mp4"
    media.write_bytes(b"settings-payload" * 3000)
    cca_manager.save_cached_transcript(media, "small", "id", False, _transcript())

    assert cca_manager.load_cached_transcript(media, "small", "id", False) is not None
    assert cca_manager.load_cached_transcript(media, "large-v3", "id", False) is None
    assert cca_manager.load_cached_transcript(media, "small", "en", False) is None
    assert cca_manager.load_cached_transcript(media, "small", "id", True) is None


def test_cca_delete_cache_removes_content_entry(tmp_path):
    media = tmp_path / "video.mp4"
    media.write_bytes(b"delete-payload" * 2000)
    cca_manager.save_cached_transcript(media, "small", "id", False, _transcript())

    assert cca_manager.delete_cached_transcript(media, "small", "id", False) is True
    assert cca_manager.load_cached_transcript(media, "small", "id", False) is None


def test_cca_legacy_path_cache_is_still_readable(tmp_path):
    media = tmp_path / "legacy.mp4"
    media.write_bytes(b"legacy-payload" * 1500)

    legacy = cca_manager.transcript_cache_path(media, "small", "id", False)
    legacy.write_text(json.dumps(_transcript().to_dict(), ensure_ascii=False), encoding="utf-8")

    loaded = cca_manager.load_cached_transcript(media, "small", "id", False)
    assert loaded is not None
    assert len(loaded.segments) == 2


# ── ACS endpoint wiring ──────────────────────────────────────────────────────


async def _collect_sse(generator):
    import json as _json

    events = []
    async for chunk in generator:
        text = str(chunk).strip()
        if not text.startswith("data: "):
            continue
        events.append(_json.loads(text[len("data: "):].strip()))
    return events


async def _run_analysis(request):
    from chopster.auto_clip_studio.engine.routers import analyze as analyze_mod

    response = await analyze_mod.analyze_video(request)
    return await _collect_sse(response.body_iterator)


def test_acs_youtube_run_uses_cache_and_skips_subtitle_tiers(monkeypatch):
    """A cached transcript must bypass subtitle tiers, audio download and Whisper."""
    import asyncio

    from chopster.auto_clip_studio.engine.routers import analyze as analyze_mod
    from chopster.auto_clip_studio.engine.schemas.analyze import AnalyzeRequest

    video_id = "2F1KCji6e3Y"
    acs_cache.save_cached_transcript(
        acs_cache.subject_for_video(video_id),
        "",
        LINES,
        source="supadata",
        duration=8.0,
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("transcript acquisition must not run when the cache is warm")

    monkeypatch.setattr(analyze_mod, "fetch_transcript", forbidden)
    monkeypatch.setattr(analyze_mod, "download_youtube_audio_for_transcription", forbidden)
    monkeypatch.setattr(analyze_mod, "transcribe_local_video_file", forbidden)
    monkeypatch.setattr(analyze_mod, "fetch_video_metadata", lambda *a, **k: {
        "title": "Cached video",
        "channel": "Test",
        "duration": 8.0,
        "heatmap": [],
        "is_live": False,
        "live_status": "not_live",
        "audio_language": "id",
    })

    # Bypass the AI key gate and the network call: this test is about the
    # transcript stage, and a local fake keeps it offline and deterministic.
    from chopster.auto_clip_studio.engine import gateway_ai

    ai_payload = {
        "summary": "Ringkasan uji cache transkrip. #uji",
        "clips": [{
            "title": "Uji Cache Transkrip",
            "start_time": 0.0,
            "end_time": 5.5,
            "hook_time": 0.0,
            "virality_score": 80,
            "key_quotes": ["Halo semuanya"],
            "title_suggestion": "Uji Cache",
            "caption_suggestion": "Caption uji",
            "hashtag_suggestion": "#uji",
        }],
    }
    monkeypatch.setattr(
        analyze_mod,
        "resolve_ai_source",
        lambda *a, **k: {
            "source": "gateway",
            "gemini_key": "",
            "gateway": {"provider": "gateway", "model": "test-model", "timeout": 5},
            "is_mock": False,
        },
    )
    monkeypatch.setattr(
        gateway_ai,
        "run_gateway_analysis",
        lambda *a, **k: gateway_ai.GatewayResponse(text=json.dumps(ai_payload)),
    )
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    events = asyncio.run(
        _run_analysis(
            AnalyzeRequest(url=f"https://www.youtube.com/watch?v={video_id}", duration="30s")
        )
    )

    stages = [e.get("stage") for e in events if e.get("stage")]
    blob = json.dumps(events, ensure_ascii=False).lower()

    # The transcript stage must be served from cache ...
    assert "Subtitles Ready" in stages
    assert "cache" in blob
    assert "Downloading Audio for Local Transcription" not in stages
    assert "Transcribing with Local Whisper" not in stages
    assert "Transcribing with Whisper" not in stages

    # ... and the run reaches the AI stage without touching subtitles/Whisper.
    assert "Subtitles Ready" in stages
    assert "Downloading Audio for Local Transcription" not in stages
    assert "Transcribing with Local Whisper" not in stages
    assert "Transcribing with Whisper" not in stages
