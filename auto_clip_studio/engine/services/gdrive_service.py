import logging
import os
import re
import time
from pathlib import Path
from typing import Callable, Optional

import requests
import yt_dlp

from chopster.auto_clip_studio.engine.config import TEMP_DIR, UPLOADS_DIR, get_video_file_metadata, is_valid_mp4, logger


def is_google_drive_url(url: str) -> bool:
    """Checks whether the given URL is a Google Drive share or file link."""
    if not url:
        return False
    u = url.strip()
    return bool(re.search(r'(?:drive\.google\.com|docs\.google\.com|drive\.usercontent\.google\.com)', u))


def extract_google_drive_file_id(url: str) -> Optional[str]:
    """Extracts the Google Drive file ID from various URL formats."""
    if not url:
        return None
    u = url.strip()

    patterns = [
        r'/file/d/([a-zA-Z0-9_-]{20,})',
        r'[?&]id=([a-zA-Z0-9_-]{20,})',
        r'drive\.google\.com/uc\?.*id=([a-zA-Z0-9_-]{20,})',
        r'drive\.google\.com/open\?id=([a-zA-Z0-9_-]{20,})',
        r'docs\.google\.com/.*[?&]id=([a-zA-Z0-9_-]{20,})',
    ]
    for pattern in patterns:
        m = re.search(pattern, u)
        if m:
            return m.group(1)

    # In case the user pasted a raw Google Drive file ID
    if re.match(r'^[a-zA-Z0-9_-]{25,45}$', u):
        return u

    return None


def download_google_drive_video(
    url_or_id: str,
    on_progress: Optional[Callable[[str, str, int], None]] = None
) -> Path:
    """Download a bounded, validated Google Drive video into UPLOADS_DIR."""
    max_bytes = 4 * 1024 * 1024 * 1024  # Match the local video upload limit.
    total_timeout = 20 * 60
    file_id = extract_google_drive_file_id(url_or_id)
    if not file_id:
        raise ValueError("Could not extract a valid Google Drive file ID from the link.")

    for cached in UPLOADS_DIR.glob(f"gdrive_{file_id}_*.*"):
        try:
            if cached.is_file() and 1024 * 1024 < cached.stat().st_size <= max_bytes and is_valid_mp4(cached):
                if on_progress:
                    on_progress("Video Found in Cache", f"Using cached Google Drive video: {cached.name}", 100)
                return cached
        except OSError:
            continue

    import uuid
    from urllib.parse import urlsplit

    unique_tag = uuid.uuid4().hex[:12]
    file_prefix = f"gdrive_{file_id}_{unique_tag}"
    out_path = UPLOADS_DIR / f"{file_prefix}.mp4"
    if on_progress:
        on_progress("Connecting to Google Drive", "Requesting video stream from Google Drive...", 10)

    def cleanup_partial():
        for partial in UPLOADS_DIR.glob(f"{file_prefix}*"):
            try:
                if partial.is_file():
                    partial.unlink()
            except OSError:
                logger.warning("Could not remove partial Google Drive download: %s", partial)

    # yt-dlp is the preferred path; max_filesize also rejects known oversized files.
    try:
        ytdlp_started = time.monotonic()

        def ydl_hook(data):
            if data.get("status") != "downloading":
                return
            if time.monotonic() - ytdlp_started > total_timeout:
                raise TimeoutError("Google Drive download exceeded the 20 minute time limit")
            downloaded = int(data.get("downloaded_bytes") or 0)
            if downloaded > max_bytes:
                raise RuntimeError("Google Drive video exceeds the 4 GB download limit")
            total = int(data.get("total_bytes") or data.get("total_bytes_estimate") or 0)
            if on_progress:
                if total > 0:
                    pct = int(min(90, 10 + downloaded / total * 80))
                    on_progress("Downloading Video", f"Downloading: {downloaded / 1048576:.1f} MB / {total / 1048576:.1f} MB ({pct}%)", pct)
                else:
                    on_progress("Downloading Video", f"Downloading: {downloaded / 1048576:.1f} MB...", 40)

        template = str(UPLOADS_DIR / f"{file_prefix}_%(title).50B.%(ext)s")
        ydl_opts = {
            "outtmpl": template, "format": "bestvideo+bestaudio/best",
            "merge_output_format": "mp4", "max_filesize": max_bytes,
            "socket_timeout": 35, "progress_hooks": [ydl_hook],
            "quiet": True, "no_warnings": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([f"https://drive.google.com/file/d/{file_id}/view"])
        for candidate in UPLOADS_DIR.glob(f"{file_prefix}*"):
            if candidate.is_file() and 512 * 1024 < candidate.stat().st_size <= max_bytes and is_valid_mp4(candidate):
                if candidate != out_path:
                    candidate.replace(out_path)
                if on_progress:
                    on_progress("Download Complete", f"Google Drive video ready: {out_path.name}", 100)
                return out_path
    except Exception as exc:
        logger.warning("yt-dlp Google Drive download failed; trying bounded HTTP fallback: %s", exc)
    cleanup_partial()

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    })
    response = None
    deadline = time.monotonic() + total_timeout

    def safe_get(url: str):
        resp = session.get(url, stream=True, timeout=(10, 35), allow_redirects=True)
        final_host = (urlsplit(resp.url).hostname or "").lower()
        if not (final_host == "google.com" or final_host.endswith(".google.com") or
                final_host == "googleusercontent.com" or final_host.endswith(".googleusercontent.com")):
            resp.close()
            raise RuntimeError("Google Drive redirected to an unsupported host")
        try:
            resp.raise_for_status()
        except requests.RequestException:
            resp.close()
            raise
        return resp

    try:
        if on_progress:
            on_progress("Direct Streaming", "Downloading video file directly from Google Drive...", 20)
        response = safe_get(f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t")
        content_type = (response.headers.get("Content-Type") or "").lower()
        if "text/html" in content_type:
            parts, size = [], 0
            for chunk in response.iter_content(65536):
                if chunk:
                    parts.append(chunk)
                    size += len(chunk)
                    if size >= 1024 * 1024:
                        break
            html = b"".join(parts).decode("utf-8", errors="ignore")
            response.close()
            token = next((v for k, v in session.cookies.items() if k.startswith("download_warning")), None)
            if not token:
                match = re.search(r"confirm=([0-9A-Za-z_]+)", html)
                token = match.group(1) if match else None
            confirm_url = (
                f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm={token}"
                if token else f"https://docs.google.com/uc?export=download&id={file_id}&confirm=t"
            )
            response = safe_get(confirm_url)
            content_type = (response.headers.get("Content-Type") or "").lower()

        allowed_types = {"application/octet-stream", "binary/octet-stream", "application/mp4"}
        if not (content_type.startswith("video/") or content_type in allowed_types):
            raise RuntimeError(f"Google Drive returned unsupported content type: {content_type or 'unknown'}")
        content_length = int(response.headers.get("Content-Length") or 0)
        if content_length > max_bytes:
            raise RuntimeError("Google Drive video exceeds the 4 GB download limit")

        downloaded = 0
        with out_path.open("wb") as target:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if time.monotonic() > deadline:
                    raise TimeoutError("Google Drive download exceeded the 20 minute time limit")
                if not chunk:
                    continue
                downloaded += len(chunk)
                if downloaded > max_bytes:
                    raise RuntimeError("Google Drive video exceeds the 4 GB download limit")
                target.write(chunk)
                if on_progress:
                    pct = int(min(95, 20 + downloaded / content_length * 75)) if content_length else 45
                    total_text = f" / {content_length / 1048576:.1f} MB" if content_length else ""
                    on_progress("Downloading Video", f"Downloading: {downloaded / 1048576:.1f} MB{total_text}", pct)
        if downloaded < 512 * 1024 or not is_valid_mp4(out_path):
            raise RuntimeError("The downloaded file is empty, too small, or is not a valid video")
        if on_progress:
            on_progress("Download Complete", f"Google Drive video ready: {out_path.name}", 100)
        return out_path
    except Exception as exc:
        cleanup_partial()
        raise RuntimeError(
            "Failed to download Google Drive video. Confirm that the link is shared with access and points to a video. "
            f"Details: {exc}"
        ) from exc
    finally:
        if response is not None:
            response.close()
        session.close()

