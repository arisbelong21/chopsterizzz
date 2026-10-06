"""Hide child console windows on Windows.

Chopster is a windowed (GUI) application. On Windows, any CLI child it spawns
(curl, ffmpeg, yt-dlp, node, npm, powershell, taskkill, ...) is a console
application, and without CREATE_NO_WINDOW each spawn flashes a console window
(for example C:\\Windows\\system32\\curl.exe during Master Analysis when the AI
request falls back to curl.exe). This installs a thin, guarded, idempotent
patch over subprocess.Popen so that, on Windows, a child is created with
CREATE_NO_WINDOW unless the caller explicitly requested a window
(for example CREATE_NEW_CONSOLE in dev-mode tools). Nothing else about process
creation or the application flow is changed.
"""
from __future__ import annotations

import os
import subprocess as _subprocess

_CREATE_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW

_original_popen_init = _subprocess.Popen.__init__
_patched = False


def install() -> None:
    """Idempotent patch: inject CREATE_NO_WINDOW on Windows only."""
    global _patched
    if _patched or os.name != "nt":
        return

    def _popen_init(self, *args, **kwargs):
        flags = kwargs.get("creationflags", 0)
        if not flags:
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        _original_popen_init(self, *args, **kwargs)

    _subprocess.Popen.__init__ = _popen_init
    _patched = True


install()
