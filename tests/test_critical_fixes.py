"""Regression tests for the verified critical/high-severity fixes.

Guards:
  * clipper.clip_renderer.export_batch parallel branch — the out-of-scope `c`
    raised NameError for every successful clip (fake failures + Automation never
    notified). We now map futures back to their clips.
  * automation.publishers YouTube resumable upload — HTTP 308 must honour the
    server `Range` header, and HTTP 401 retries must be bounded.
  * auto_clip_studio analyze endpoint — only app-managed media paths may be
    analysed.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from chopster.automation import content
from chopster.automation.content import map_for_platform
from chopster.automation.models import AuthRequiredError
from chopster.automation.publishers import (
    PublishContext,
    PublishResult,
    YouTubePublisher,
    _parse_resumable_range,
)
from chopster.clipper import clip_renderer
from chopster.clipper.clip_renderer import ExportResult


# --------------------------------------------------------------------------- #
# clipper.clip_renderer — parallel export must not raise NameError
# --------------------------------------------------------------------------- #

def test_parallel_export_reports_success_and_notifies_automation(tmp_path: Path) -> None:
    notified: list[dict] = []

    def fake_export_clip(source, start, end, out_path, **kwargs):
        Path(out_path).write_bytes(b"stub")
        return ExportResult(0, True, str(out_path))

    clips = [
        {"index": 1, "title": "Klip Satu", "start": 10.0, "end": 25.0},
        {"index": 2, "title": "Klip Dua", "start": 30.0, "end": 45.0},
        {"index": 3, "title": "Klip Tiga", "start": 50.0, "end": 65.0},
    ]

    with patch.object(clip_renderer, "export_clip", side_effect=fake_export_clip), \
            patch.object(clip_renderer, "_notify_automation",
                         side_effect=lambda *a, **k: notified.append(k)):
        ok, fail = clip_renderer.export_batch(
            source=str(tmp_path / "src.mp4"),
            clips=clips,
            out_dir=str(tmp_path / "out"),
            parallel=2,
        )

    assert (ok, fail) == (3, 0)
    assert len(notified) == 3
    # The notified window must come from the clip that produced the future.
    assert sorted((k["clip_start"], k["clip_end"]) for k in notified) == [
        (10.0, 25.0), (30.0, 45.0), (50.0, 65.0),
    ]


# --------------------------------------------------------------------------- #
# automation.publishers — resumable upload Range / 401 handling
# --------------------------------------------------------------------------- #

class _FakeResponse:
    def __init__(self, status_code: int, headers: dict | None = None,
                 payload: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


class _FakeAuth:
    def ensure_access_token(self, account_id: str) -> str:
        return "token-1"


def _youtube_ctx(media: Path) -> PublishContext:
    pack = content.normalize_content_pack({
        "title": "Uji Upload Resumable",
        "description": "Deskripsi uji untuk upload resumable YouTube.",
        "hashtags": ["#uji"],
    })
    payload = map_for_platform("youtube", pack, privacy="private", made_for_kids=False)
    return PublishContext(
        job={}, account={"id": "acc1"}, payload=payload,
        media_path=str(media), progress=lambda pct, label: None,
    )


def test_parse_resumable_range() -> None:
    assert _parse_resumable_range("bytes=0-8388607") == 8388608
    assert _parse_resumable_range("bytes=0-0") == 1
    assert _parse_resumable_range("bytes=0-5/1234") == 6
    assert _parse_resumable_range(None) is None
    assert _parse_resumable_range("") is None
    assert _parse_resumable_range("garbage") is None


def test_youtube_upload_resumes_from_server_reported_range(tmp_path: Path) -> None:
    size = 20 * 1024 * 1024
    media = tmp_path / "media.mp4"
    media.write_bytes(b"\x00" * size)
    ctx = _youtube_ctx(media)
    ranges: list[str] = []

    def fake_put(url, headers=None, data=None, timeout=None):
        content_range = (headers or {}).get("Content-Range", "")
        ranges.append(content_range)
        start = int(content_range.split("bytes ")[1].split("-")[0])
        end = int(content_range.split("-")[1].split("/")[0])
        if start == 0:
            # The server persisted only the first half of this chunk.
            return _FakeResponse(308, {"Range": "bytes=0-4194303"})
        if end < size - 1:
            return _FakeResponse(308, {"Range": f"bytes=0-{end}"})
        return _FakeResponse(200, payload={"id": "yt-abc123"})

    with patch("chopster.automation.publishers.requests.put", side_effect=fake_put), \
            patch("chopster.automation.publishers.requests.post",
                  return_value=_FakeResponse(200, {"Location": "https://upload.example/session"})):
        result = YouTubePublisher(None, _FakeAuth()).publish(ctx)

    assert isinstance(result, PublishResult)
    assert result.ok and result.external_id == "yt-abc123"
    chunk = 8 * 1024 * 1024
    assert ranges[0] == f"bytes 0-{chunk - 1}/{size}"
    # Must resume from the byte the server actually persisted (4194304), not from
    # the end of the first chunk (8388608) — otherwise bytes go missing and a
    # truncated upload is reported as success.
    assert ranges[1].startswith("bytes 4194304-")


def test_youtube_401_retry_is_bounded(tmp_path: Path) -> None:
    media = tmp_path / "media.mp4"
    media.write_bytes(b"\x00" * (1024 * 1024))
    ctx = _youtube_ctx(media)
    calls: list[dict] = []

    def fake_put(url, headers=None, data=None, timeout=None):
        calls.append(headers or {})
        return _FakeResponse(401, text="unauthorized")

    with patch("chopster.automation.publishers.requests.put", side_effect=fake_put), \
            patch("chopster.automation.publishers.requests.post",
                  return_value=_FakeResponse(200, {"Location": "https://upload.example/session"})):
        with pytest.raises(AuthRequiredError):
            YouTubePublisher(None, _FakeAuth()).publish(ctx)

    # Initial attempt + 2 refreshes, then it must give up instead of hanging.
    assert len(calls) == 3


# --------------------------------------------------------------------------- #
# auto_clip_studio.analyze — local path hardening
# --------------------------------------------------------------------------- #

def test_analyze_path_guard_rejects_paths_outside_media_dirs(tmp_path: Path) -> None:
    from chopster.auto_clip_studio.engine.config import TEMP_DIR, UPLOADS_DIR
    from chopster.auto_clip_studio.engine.routers.analyze import _is_allowed_analyze_path

    outside = tmp_path / "secret.txt"
    outside.write_text("secret")

    assert _is_allowed_analyze_path(str(outside)) is False
    assert _is_allowed_analyze_path(str(UPLOADS_DIR / "clip.mp4")) is True
    assert _is_allowed_analyze_path(str(TEMP_DIR / "clip.mp4")) is True
    assert _is_allowed_analyze_path(
        str(UPLOADS_DIR / ".." / ".." / ".." / "Windows" / "win.ini")) is False