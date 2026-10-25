"""Lightweight process-wide event bus.

The renderer only needs to *emit* RENDER_COMPLETED; Automation subscribes.
Listener failures are always swallowed so publishing can never break rendering
(spec §29: "Automation failure must never cause an already-successful render
to become a render failure").
"""
from __future__ import annotations

import threading
from typing import Any, Callable

RENDER_COMPLETED = "render_completed"
CLIP_COMPLETED = "clip_completed"
MEDIA_READY = "media_ready"
CONTENT_PACK_READY = "content_pack_ready"
AUTOMATION_CREATED = "automation_created"
AUTOMATION_STARTED = "automation_started"
PUBLISH_STARTED = "publish_started"
PUBLISH_PROGRESS = "publish_progress"
PUBLISH_COMPLETED = "publish_completed"
PUBLISH_FAILED = "publish_failed"
ACTION_REQUIRED = "action_required"
AUTH_STATE_CHANGED = "auth_state_changed"
PLATFORM_UI_CHANGED = "platform_ui_changed"
AUTOMATION_COMPLETED = "automation_completed"

_LOCK = threading.RLock()
_LISTENERS: dict[str, list[Callable[[dict[str, Any]], None]]] = {}


def subscribe(event: str, listener: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
    with _LOCK:
        _LISTENERS.setdefault(event, []).append(listener)

    def _unsubscribe() -> None:
        with _LOCK:
            try:
                _LISTENERS.get(event, []).remove(listener)
            except ValueError:
                pass

    return _unsubscribe


def emit(event: str, payload: dict[str, Any] | None = None) -> None:
    with _LOCK:
        listeners = list(_LISTENERS.get(event, []))
    for fn in listeners:
        try:
            fn(payload or {})
        except Exception:
            # Events must never propagate into the emitter's call stack.
            pass


def emit_render_completed(payload: dict[str, Any]) -> None:
    """Called by Auto Clip Studio after a clip MP4 has been verified on disk."""
    emit(RENDER_COMPLETED, dict(payload))


def emit_clip_completed(payload: dict[str, Any]) -> None:
    """Called by Content Clipper AI after an exported clip MP4 is verified on disk.

    The payload is shape-compatible with emit_render_completed; only the
    ``source`` key differs ("cca"). Both feed the *same* Automation Engine —
    there is never a second queue or scheduler per source (spec §3.3).
    """
    body = dict(payload)
    body.setdefault("source", "cca")
    emit(CLIP_COMPLETED, body)
