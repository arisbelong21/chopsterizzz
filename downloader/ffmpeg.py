"""FFmpeg discovery helper — handles frozen & dev layout."""
from __future__ import annotations

import shutil
from pathlib import Path

from chopster.app.paths import app_dir


def find_ffmpeg() -> str | None:
    """Return path to ffmpeg executable if found, else None."""
    # Check in _internal folder for PyInstaller onedir builds (chopster.exe next to _internal)
    import sys as _sys
    if getattr(_sys, "frozen", False):
        exe_dir = Path(_sys.executable).parent
        for cand in [exe_dir / "ffmpeg.exe", exe_dir / "_internal" / "ffmpeg.exe", exe_dir / "ffmpeg"]:
            if cand.is_file():
                return str(cand)
    # bundled exe next to the main entry point (dev)
    for cand in [app_dir() / "ffmpeg.exe", app_dir() / "ffmpeg"]:
        if cand.is_file():
            return str(cand)
    # system PATH
    found = shutil.which("ffmpeg")
    return found


def find_ffprobe() -> str | None:
    """Return the ffprobe paired with FFmpeg when available."""
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        ffmpeg_path = Path(ffmpeg)
        sibling = ffmpeg_path.with_name(
            "ffprobe.exe" if ffmpeg_path.suffix.lower() == ".exe" else "ffprobe"
        )
        if sibling.is_file():
            return str(sibling)
    return shutil.which("ffprobe")


def ffmpeg_available() -> bool:
    return find_ffmpeg() is not None and find_ffprobe() is not None
