"""Persistent, isolated browser sessions for Browser Session mode (spec §8/§92).

Rules enforced here:
  * dedicated Chopster profile under user_data_dir()/automation/profiles/<platform>
    (never the user's main Chrome profile),
  * visible (headless=False) during setup/diagnostics,
  * no cookie export, no stealth, no proxy tricks,
  * profile lock with stale-lock recovery + clean shutdown,
  * clear error when the Playwright runtime is not installed (spec §96-§98).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from chopster.automation.models import NeedsActionError
from chopster.app.paths import user_data_dir

LOGIN_URLS = {
    "instagram": "https://www.instagram.com/",
    "facebook": "https://www.facebook.com/",
    "tiktok": "https://www.tiktok.com/login",
    "youtube": "https://accounts.google.com/",
}

COOKIE_MARKERS = {
    "instagram": "sessionid",
    "facebook": "c_user",
    "tiktok": "sessionid",
    "youtube": "SID",
}

INSTALL_HINT = ("Browser automation belum siap. Install paketnya sekali: pip install playwright. "
                "Chopster memakai Edge/Chrome yang sudah terpasang (Settings > Automation > "
                "Browser engine); bila mesin tidak punya keduanya, jalankan: "
                "python -m playwright install chromium")


def profiles_root() -> Path:
    return user_data_dir() / "automation" / "profiles"


def profile_dir(platform: str) -> Path:
    return profiles_root() / str(platform)


def account_profile_dir(platform: str, account_id: str) -> Path:
    """Per-account profile folder: two accounts on one platform stay isolated."""
    return profiles_root() / str(platform) / str(account_id)


def _migrate_legacy_profile(platform: str, account_id: str) -> None:
    """Move an existing per-platform profile into the per-account folder once.

    Only when the legacy folder exists, the target does not, and it actually
    holds data - so someone who already logged in never has to log in again.
    """
    try:
        aid = str(account_id or "").strip()
        if not aid:
            return
        legacy = profile_dir(platform)
        target = account_profile_dir(platform, aid)
        if not legacy.exists():
            return
        if target.exists() and any(target.iterdir()):
            return
        try:
            if not any(legacy.iterdir()):
                return
        except Exception:
            return
        # Create the target folder itself (the parent already exists); note the
        # target lives INSIDE the legacy folder, so it must be skipped below.
        target.mkdir(parents=True, exist_ok=True)
        # Move the children one by one: renaming the folder *into its own*
        # subdirectory is invalid on Windows and failed silently before.
        moved = 0
        for child in list(legacy.iterdir()):
            if child == target:
                continue
            try:
                child.rename(target / child.name)
                moved += 1
            except Exception:
                pass
        if moved:
            try:
                legacy.rmdir()
            except Exception:
                pass
    except Exception:
        pass


def logs_dir() -> Path:
    return user_data_dir() / "automation" / "logs"


def ensure_playwright() -> Any:
    """Return the sync playwright module or raise a user-actionable error."""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
        return sync_playwright
    except Exception as exc:
        raise NeedsActionError(f"playwright unavailable: {exc}", category="UNSUPPORTED_OPERATION", user_message=INSTALL_HINT) from exc


def playwright_installed() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("playwright") is not None
    except Exception:
        return False


def _dir_for(platform: str, account_id: str = "") -> Path:
    """Legacy per-platform folder when no account id is given (compatibility)."""
    aid = str(account_id or "").strip()
    return account_profile_dir(platform, aid) if aid else profile_dir(platform)


def _marker_path(platform: str, account_id: str = "") -> Path:
    return _dir_for(platform, account_id) / "chopster_session.json"


def _lock_path(platform: str, account_id: str = "") -> Path:
    return _dir_for(platform, account_id) / "chopster.lock"


def session_valid(platform: str, account_id: str = "") -> bool:
    marker = _marker_path(platform, account_id)
    if not marker.exists() or not _dir_for(platform, account_id).exists():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        return bool(data.get("verified_at"))
    except Exception:
        return False


def session_verified_at(platform: str, account_id: str = "") -> str:
    try:
        data = json.loads(_marker_path(platform, account_id).read_text(encoding="utf-8"))
        return str(data.get("verified_at") or "")
    except Exception:
        return ""


def _acquire_lock(platform: str, account_id: str = "") -> bool:
    lock = _lock_path(platform, account_id)
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists():
        try:
            age = time.time() - lock.stat().st_mtime
        except Exception:
            age = 0.0
        if age < 6 * 3600:
            return False
        try:
            lock.unlink()
        except Exception:
            return False
    try:
        lock.write_text(json.dumps({"pid": _own_pid(), "since": time.time()}), encoding="utf-8")
        return True
    except Exception:
        return False


def _release_lock(platform: str, account_id: str = "") -> None:
    try:
        _lock_path(platform, account_id).unlink(missing_ok=True)
    except Exception:
        pass


def _own_pid() -> int:
    import os
    try:
        return int(os.getpid())
    except Exception:
        return 0


def clear_session(platform: str, account_id: str = "") -> None:
    """Remove the Chopster-owned profile for this platform (§80/§92)."""
    import shutil
    target = _dir_for(platform, account_id)
    if not target.exists():
        return
    if not _acquire_lock(platform, account_id):
        raise NeedsActionError("profile locked", user_message="Session masih terbuka. Tutup jendela browser dulu lalu coba lagi.")
    try:
        shutil.rmtree(target, ignore_errors=True)
    finally:
        _release_lock(platform, account_id)


class BrowserSession:
    """Context manager around a Playwright persistent context."""

    def __init__(self, platform: str, headless: bool = False, nav_timeout_ms: int = 30000,
                 action_timeout_ms: int = 15000, channel: str = "", account_id: str = ""):
        self.platform = str(platform)
        self.headless = bool(headless)
        self.nav_timeout_ms = int(nav_timeout_ms)
        self.action_timeout_ms = int(action_timeout_ms)
        # "" / "bundled" = Playwright Chromium; otherwise a branded channel
        # ("msedge"/"chrome") launches the browser already installed on the PC,
        # so Chopster never has to download ~280 MB of Chromium.
        self.channel = str(channel or "")
        # Per-account profile: keeps two accounts on one platform isolated.
        self.account_id = str(account_id or "")
        self._playwright = None
        self.context: Any = None
        self.page: Any = None
        self._locked = False

    def __enter__(self) -> "BrowserSession":
        sync_playwright = ensure_playwright()
        if self.account_id:
            _migrate_legacy_profile(self.platform, self.account_id)
        if not _acquire_lock(self.platform, self.account_id):
            raise NeedsActionError("profile locked", user_message="Profil browser Chopster sedang dipakai sesi lain. Tutup sesi lama dulu.")
        self._locked = True
        profile = _dir_for(self.platform, self.account_id)
        profile.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        try:
            launch: dict[str, Any] = {
                "headless": self.headless,
                "viewport": {"width": 1280, "height": 860},
                # Only popup blocking: never weaken browser security just to make
                # automation easier (no --no-sandbox, no flags that lower it).
                "args": ["--disable-popup-blocking"],
                "locale": "en-US",
            }
            if self.channel and self.channel != "bundled":
                launch["channel"] = self.channel
            try:
                self.context = self._playwright.chromium.launch_persistent_context(str(profile), **launch)
            except Exception as exc:
                if launch.get("channel"):
                    raise NeedsActionError(
                        f"browser channel {launch['channel']} unavailable: {exc}",
                        user_message=("Tidak bisa menjalankan browser terpasang (" + str(launch["channel"]) +
                                      "). Pastikan Edge/Chrome terpasang, atau ubah Settings > Automation > "
                                      "Browser engine ke 'bundled' lalu jalankan: python -m playwright install chromium."),
                    ) from exc
                # Bundled Chromium missing (most common cause: the image was built
                # without `playwright install`). Give the exact fix instead of the
                # raw "Executable doesn't exist" traceback (spec §96-§98).
                raise NeedsActionError(
                    f"bundled chromium unavailable: {exc}",
                    user_message=("Chromium bawaan Playwright belum terpasang. Jalankan sekali di Command Prompt: "
                                  "python -m playwright install chromium — atau ubah Settings > Automation > "
                                  "Browser engine ke 'msedge'/'chrome' untuk memakai browser yang sudah ada."),
                ) from exc
            self.context.set_default_navigation_timeout(self.nav_timeout_ms)
            self.context.set_default_timeout(self.action_timeout_ms)
            if self.context.pages:
                self.page = self.context.pages[0]
            else:
                self.page = self.context.new_page()
            return self
        except Exception:
            self._cleanup()
            raise

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._cleanup()

    def _cleanup(self) -> None:
        try:
            if self.context is not None:
                self.context.close()
        except Exception:
            pass
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:
            pass
        self.context = None
        self.page = None
        if self._locked:
            _release_lock(self.platform, self.account_id)
            self._locked = False

    def cookies(self) -> list[dict[str, Any]]:
        try:
            return list(self.context.cookies()) if self.context else []
        except Exception:
            return []

    # -- window control (best effort; never fatal) -------------------------
    def _cdp(self) -> Any:
        try:
            return self.context.new_cdp_session(self.page) if (self.context and self.page) else None
        except Exception:
            return None

    def _set_window_state(self, state: str) -> bool:
        """Minimize/normalize the Chopster browser window via CDP.

        Only cosmetic: a failure (headless, CDP missing, platform quirk) must
        never break publishing, so it just returns False.
        """
        try:
            if self.headless:
                return False
            cdp = self._cdp()
            if cdp is None:
                return False
            info = cdp.send("Browser.getWindowForTarget")
            window_id = (info or {}).get("windowId")
            if window_id is None:
                return False
            cdp.send("Browser.setWindowBounds",
                     {"windowId": window_id, "bounds": {"windowState": state}})
            return True
        except Exception:
            return False

    def minimize_window(self) -> bool:
        return self._set_window_state("minimized")

    def restore_window(self) -> bool:
        ok = self._set_window_state("normal")
        try:
            if ok and self.page is not None:
                self.page.bring_to_front()
        except Exception:
            pass
        return ok

    def logged_in_marker_found(self) -> bool:
        want = COOKIE_MARKERS.get(self.platform, "")
        if not want:
            return False
        for c in self.cookies():
            if str(c.get("name") or "") == want and c.get("value"):
                return True
        return False

    def mark_verified(self) -> None:
        marker = _marker_path(self.platform, self.account_id)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"verified_at": time.strftime("%Y-%m-%d %H:%M:%S"), "platform": self.platform}), encoding="utf-8")

    def goto(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded")


def ensure_login(platform: str, timeout_sec: int = 600, channel: str = "", account_id: str = "") -> bool:
    """Open a visible Chopster browser for manual login (no password collected).

    Waits until the platform's login cookie appears (user completes login /
    MFA / CAPTCHA themselves) or the user closes the window.
    """
    if session_valid(platform, account_id):
        return True
    if not playwright_installed():
        raise NeedsActionError("playwright missing", category="UNSUPPORTED_OPERATION", user_message=INSTALL_HINT)
    with BrowserSession(platform, headless=False, channel=channel, account_id=account_id) as session:
        session.goto(LOGIN_URLS.get(platform, "https://www.google.com/"))
        deadline = time.time() + max(60, int(timeout_sec))
        while time.time() < deadline:
            try:
                if session.context is None or not session.context.pages:
                    break
                if session.logged_in_marker_found():
                    session.mark_verified()
                    return True
                time.sleep(1.5)
            except Exception:
                break
    return session_valid(platform, account_id)
