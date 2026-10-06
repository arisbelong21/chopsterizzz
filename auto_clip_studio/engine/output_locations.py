"""Output-directory helpers for Auto Clip Studio exports and downloads."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from chopster.auto_clip_studio.engine.video_engine import EXPORTS_DIR

_lock = threading.RLock()
_known_output_dirs: set[Path] = {Path(EXPORTS_DIR).resolve()}


def default_output_directory() -> Path:
    return Path(EXPORTS_DIR).expanduser().resolve()


def resolve_output_directory(value: Optional[str] = None) -> Path:
    """Create and register a selected output directory; blank uses the legacy exports folder."""
    candidate = Path(value).expanduser() if value and value.strip() else default_output_directory()
    resolved = candidate.resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    if not resolved.is_dir():
        raise ValueError("The selected output location is not a directory.")
    with _lock:
        _known_output_dirs.add(resolved)
    return resolved


def locate_output_file(filename: str) -> Optional[Path]:
    """Find a generated file by basename in the default or a registered output folder."""
    safe_name = Path(filename).name
    with _lock:
        directories = [default_output_directory(), *_known_output_dirs]
    for directory in directories:
        candidate = directory / safe_name
        if candidate.is_file():
            return candidate
    return None
