"""Action schema + validator for the AI Browser Agent (spec §18-§20, §62-§63)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

ALLOWED_ACTIONS = {
    "CLICK", "TYPE", "SELECT", "UPLOAD_FILE", "SCROLL", "WAIT",
    "NAVIGATE", "PRESS_KEY", "FOCUS", "READ", "VERIFY",
    "REQUEST_USER_ACTION", "STOP",
}

MAX_TEXT_LEN = 4000
MAX_WAIT_SEC = 20
ALLOWED_MEDIA_EXT = {".mp4", ".mov", ".webm", ".m4v"}

DOMAIN_ALLOWLIST = {
    "instagram": ["instagram.com", "cdninstagram.com", "facebook.com", "fbcdn.net"],
    "facebook": ["facebook.com", "fbcdn.net", "messenger.com"],
    "tiktok": ["tiktok.com", "tiktokcdn.com"],
    "youtube": ["youtube.com", "google.com", "googleapis.com", "ytimg.com", "youtu.be"],
}

CAPTCHA_MARKERS = (
    "captcha", "recaptcha", "/challenge", "two-factor", "2fa", "checkpoint",
    "security check", "confirm it's you", "confirm your identity",
    "verify it's you", "suspicious login", "verify your identity",
)


def looks_like_security_challenge(url: str, visible_text: str) -> str:
    blob = f"{url} {visible_text[:2000]}".lower()
    for marker in CAPTCHA_MARKERS:
        if marker in blob:
            return marker
    return ""


class ActionValidator:
    """Deterministic gate between model output and the browser engine."""

    def __init__(self, platform: str, *, allowed_files: list[str] | None = None,
                 current_url: str = "", task: str = "publish_short_video"):
        self.platform = str(platform)
        self.allowed_files = [str(Path(f).resolve()) for f in (allowed_files or [])]
        self.current_url = current_url
        self.task = task

    def _domain_allowed(self, url: str) -> bool:
        allowed = DOMAIN_ALLOWLIST.get(self.platform, [])
        low = str(url).lower()
        if low.startswith(("about:blank", "data:", "javascript:")):
            return False
        if not allowed:
            return low.startswith("https://")
        return any(dom in low for dom in allowed)

    def validate(self, action: Any) -> tuple[bool, str]:
        if not isinstance(action, dict):
            return False, "action must be an object"
        atype = str(action.get("type") or "").upper()
        if atype not in ALLOWED_ACTIONS:
            return False, f"action type not allowed: {atype}"
        target = action.get("target")
        value = action.get("value")
        if atype in ("CLICK", "FOCUS") and not target:
            return False, "target required for CLICK/FOCUS"
        if atype == "TYPE" and (not target or value is None):
            return False, "target and value required for TYPE"
        if atype == "TYPE" and len(str(value)) > MAX_TEXT_LEN:
            return False, "text too long"
        if atype == "SELECT" and (not target or value is None):
            return False, "target and value required for SELECT"
        if atype == "WAIT":
            try:
                secs = float(value or 1)
            except Exception:
                return False, "WAIT value must be seconds"
            if secs <= 0 or secs > MAX_WAIT_SEC:
                return False, f"WAIT must be 0-{MAX_WAIT_SEC}s"
        if atype == "NAVIGATE":
            if not value or not self._domain_allowed(str(value)):
                return False, "navigation outside platform domain allowlist"
        if atype == "UPLOAD_FILE":
            if not value:
                return False, "UPLOAD_FILE requires file path"
            path = str(Path(str(value)).resolve())
            if self.allowed_files and path not in self.allowed_files:
                return False, "file not associated with this automation job"
            if not os.path.isfile(path):
                return False, "file does not exist"
            if Path(path).suffix.lower() not in ALLOWED_MEDIA_EXT:
                return False, "file type not allowed"
        if atype == "VERIFY" and not value:
            return False, "VERIFY requires expected text/state"
        if atype == "REQUEST_USER_ACTION" and not value:
            return False, "REQUEST_USER_ACTION requires reason"
        if isinstance(target, dict) and "coordinates" in target:
            return False, "coordinate targeting is not allowed"
        return True, "ok"

    def validate_model_output(self, raw: Any) -> tuple[dict[str, Any] | None, str]:
        """Validate the full model decision envelope."""
        if not isinstance(raw, dict):
            return None, "model output must be a JSON object"
        action = raw.get("action")
        ok, why = self.validate(action)
        if not ok:
            return None, why
        envelope = {"state": str(raw.get("state") or ""), "goal": str(raw.get("goal") or ""), "confidence": float(raw.get("confidence") or 0), "action": action, "expected_change": str(raw.get("expected_change") or "")}
        return envelope, "ok"
