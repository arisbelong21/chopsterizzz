"""Regression tests for the Google Drive video identity contract.

The stored file name carries a random download tag (``gdrive_<id>_<tag>.mp4``),
but the value returned as ``video_id`` must stay ``gdrive_<id>``: the web UI
derives the Drive link and the history cache key from it, so a tag in the
identity produced broken links and unreadable history entries.
"""

import pytest

from chopster.auto_clip_studio.engine.routers import media
from chopster.auto_clip_studio.engine.services.gdrive_service import (
    extract_google_drive_file_id,
    gdrive_video_id,
)

DRIVE_ID = "0ByeS4oOUV-49Zzh4R1J6R09zazQ"
DRIVE_URL = f"https://drive.google.com/file/d/{DRIVE_ID}/view"
STORAGE_TAG = "2cd930697ac8"


def test_video_id_has_no_storage_tag_for_share_link():
    assert gdrive_video_id(DRIVE_URL) == f"gdrive_{DRIVE_ID}"


@pytest.mark.parametrize(
    "url",
    [
        DRIVE_URL,
        f"https://drive.google.com/open?id={DRIVE_ID}",
        f"https://drive.google.com/uc?export=download&id={DRIVE_ID}",
        f"https://drive.google.com/file/d/{DRIVE_ID}/preview",
    ],
)
def test_video_id_matches_the_file_id_extracted_from_the_link(url):
    assert gdrive_video_id(url) == f"gdrive_{extract_google_drive_file_id(url)}"


def test_video_id_is_stable_across_downloads_with_different_tags():
    first = gdrive_video_id(DRIVE_URL, f"gdrive_{DRIVE_ID}_aaaaaaaaaaaa")
    second = gdrive_video_id(DRIVE_URL, f"gdrive_{DRIVE_ID}_bbbbbbbbbbbb")
    assert first == second == f"gdrive_{DRIVE_ID}"


def test_video_id_falls_back_to_a_tag_free_stem():
    assert gdrive_video_id("", f"gdrive_{DRIVE_ID}_{STORAGE_TAG}") == f"gdrive_{DRIVE_ID}"
    # Already tag-free (or not a Drive stem): keep it untouched.
    assert gdrive_video_id("", f"gdrive_{DRIVE_ID}") == f"gdrive_{DRIVE_ID}"
    assert gdrive_video_id("", "upload_abc123XYZ") == "upload_abc123XYZ"


def test_history_cache_key_parses_the_duration_from_the_video_id():
    """Mirrors refreshHistory() in App.tsx: suffix = rest.substring(video_id.length + 1)."""
    video_id = gdrive_video_id(DRIVE_URL)
    key = f"cheat_clip_cache_{video_id}_30s_model_gemini_clips_3"
    rest = key[len("cheat_clip_cache_"):]

    suffix = rest[len(video_id) + 1:]
    assert suffix.split("_")[0] == "30s"
    # "Load from history" rebuilds the same key from the parsed suffix.
    assert f"cheat_clip_cache_{video_id}_{suffix}" == key


def test_media_route_resolves_the_tagged_file_from_the_stable_id(tmp_path, monkeypatch):
    uploads = tmp_path / "uploads"
    temp = tmp_path / "temp"
    exports = tmp_path / "exports"
    for folder in (uploads, temp, exports):
        folder.mkdir()

    stored = uploads / f"gdrive_{DRIVE_ID}_{STORAGE_TAG}.mp4"
    stored.write_bytes(b"\x00" * 2048)

    monkeypatch.setattr(media, "UPLOADS_DIR", uploads)
    monkeypatch.setattr(media, "TEMP_DIR", temp)
    monkeypatch.setattr(media, "EXPORTS_DIR", exports)

    video_id = gdrive_video_id(DRIVE_URL)

    # /api/video/<video_id> (downloads.py, render fallback) must find the file.
    assert media._find_video_file_on_disk(video_id) == stored
    # /api/video/<real filename> (video_url returned by the analyzer) keeps working.
    assert media._find_video_file_on_disk(stored.name) == stored
