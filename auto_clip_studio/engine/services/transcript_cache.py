"""Persistent transcript cache for Auto Clip Studio.

One successful transcription per (video, language) is enough. Subtitle tiers can be
rate-limited (HTTP 429 / IP blocked), and local Whisper needs minutes on long videos,
so re-running the same video used to pay that cost again on every attempt.

The cache stores exactly what the analysis stage consumes — timestamped dialogue
lines — and never stores AI results (clip count/duration/mode change per run).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable, Optional

CACHE_VERSION = 1
_SIGNATURE_CHUNK = 1024 * 1024


def cache_root() -> Path:
    """Writable cache directory (AppData on Windows, home dir elsewhere)."""
    try:
        from chopster.app.paths import user_data_dir

        base = user_data_dir()
    except Exception:
        base = Path(os.environ.get("APPDATA") or Path.home()) / "ChopsterByAris"
    d = base / "cache" / "acs_transcripts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def file_signature(path: str | Path) -> str:
    """Content identity of a media file: size + 3 sampled 1 MiB chunks.

    Uses sampling instead of hashing the whole file so a 1 GB video still costs a
    few milliseconds. Copying/renaming/moving the file keeps the same signature,
    while a different video almost never collides.
    """
    p = Path(path)
    size = p.stat().st_size
    digest = hashlib.sha256()
    with p.open("rb") as fh:
        if size <= _SIGNATURE_CHUNK * 3:
            digest.update(fh.read())
        else:
            positions = (0, max(0, size // 2 - _SIGNATURE_CHUNK // 2), max(0, size - _SIGNATURE_CHUNK))
            for pos in positions:
                fh.seek(pos)
                digest.update(fh.read(_SIGNATURE_CHUNK))
    return hashlib.sha256(f"{size}|{digest.hexdigest()}".encode()).hexdigest()[:32]


def subject_for_video(video_id: str) -> str:
    """Cache subject for an online video (YouTube id or stable Drive id)."""
    return f"video:{(video_id or '').strip().lower()}"


def subject_for_file(path: str | Path, video_id: str = "") -> str:
    """Cache subject for a local/uploaded file.

    Drive downloads get a stable id, so they can skip content hashing entirely.
    Plain uploads are keyed by content, because the stored filename changes on
    every upload even when the user picks the same video again.
    """
    if video_id and str(video_id).lower().startswith("gdrive_"):
        return f"drive:{str(video_id).lower()}"
    try:
        return f"file:{file_signature(path)}"
    except Exception:
        return f"path:{Path(path)}"


def _cache_path(subject: str, lang: str | None = None) -> Path:
    raw = f"{CACHE_VERSION}|{subject}|{(lang or 'auto').lower()}"
    name = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return cache_root() / f"{name}.json"


def _clean_segments(segments: Iterable[dict]) -> list[dict]:
    cleaned: list[dict] = []
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(seg.get("start", 0.0) or 0.0)
            dur = float(seg.get("duration", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if start < 0.0:
            continue
        cleaned.append({"text": text, "start": round(start, 3), "duration": max(0.1, round(dur, 3))})
    return cleaned


def get_cached_transcript(subject: str, lang: str | None = None) -> list[dict] | None:
    """Return cached timestamped lines for this subject/language, or None."""
    if not subject:
        return None
    path = _cache_path(subject, lang)
    try:
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if int(payload.get("cache_version") or 0) != CACHE_VERSION:
            return None
        if str(payload.get("subject") or "") != subject:
            return None
        if str(payload.get("lang") or "auto").lower() != (lang or "auto").lower():
            return None
        segments = _clean_segments(payload.get("segments") or [])
        return segments or None
    except Exception:
        return None


def save_cached_transcript(
    subject: str,
    lang: str | None,
    segments: Iterable[dict],
    source: str = "",
    duration: float | None = None,
) -> Optional[Path]:
    """Persist transcript lines. Returns the cache file, or None when nothing usable."""
    if not subject:
        return None
    cleaned = _clean_segments(segments)
    if not cleaned:
        return None
    payload = {
        "cache_version": CACHE_VERSION,
        "subject": subject,
        "lang": (lang or "auto").lower(),
        "source": str(source or "unknown"),
        "duration": round(float(duration), 2) if duration else None,
        "created_at": time.time(),
        "segments": cleaned,
    }
    path = _cache_path(subject, lang)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path


def clear_cache(subject: str | None = None) -> int:
    """Delete one subject's cache entry (or every entry when subject is None)."""
    if subject:
        removed = 0
        for lang in _known_langs(subject):
            try:
                _cache_path(subject, lang).unlink(missing_ok=True)
                removed += 1
            except Exception:
                pass
        return removed
    removed = 0
    for item in cache_root().glob("*.json"):
        try:
            item.unlink()
            removed += 1
        except Exception:
            pass
    return removed


def _known_langs(subject: str) -> list[str]:
    """Every language variant stored for a subject (best effort, for cleanup)."""
    found: set[str] = set()
    for item in cache_root().glob("*.json"):
        try:
            payload = json.loads(item.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(payload.get("subject") or "") == subject:
            found.add(str(payload.get("lang") or "auto"))
    return sorted(found)


def cache_stats() -> dict[str, Any]:
    """Small summary used for logging/diagnostics."""
    files = list(cache_root().glob("*.json"))
    total = 0
    for item in files:
        try:
            total += item.stat().st_size
        except Exception:
            pass
    return {"entries": len(files), "bytes": total}
