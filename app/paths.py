"""Centralised path resolution — works in dev and PyInstaller frozen mode."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def app_dir() -> Path:
    """Directory that contains the executable / source entry point."""
    if is_frozen():
        return Path(sys.executable).parent
    # chopster/app/paths.py -> chopster/ (package root == project root for this layout)
    return Path(__file__).resolve().parent.parent


def user_data_dir() -> Path:
    """Writable user data directory (AppData on Windows)."""
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        p = Path(base) / "ChopsterByAris"
    else:
        p = Path.home() / ".chopster_by_aris"
    p.mkdir(parents=True, exist_ok=True)
    return p


def resource_path(relative: str) -> Path:
    """Resolve bundled resource (handles PyInstaller _MEIPASS).

    The spec mirrors the source layout under the ``chopster`` package root:
    on disk ``resources`` -> ``_internal\\chopster\\resources``, which matches
    the source path ``chopster/resources/`` returned by app_dir() in dev mode.
    """
    if is_frozen() and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "chopster" / relative
    return app_dir() / relative


def ensure_dirs() -> dict[str, Path]:
    data = user_data_dir()
    paths = {
        "data": data,
        "logs": data / "logs",
        "cache": data / "cache",
        "projects": data / "projects",
        "thumbnails": data / "thumbnails",
    }
    for p in paths.values():
        p.mkdir(parents=True, exist_ok=True)
    return paths
