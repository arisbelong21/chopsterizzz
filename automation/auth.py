"""Platform authorization: desktop OAuth loopback flows + AuthManager.

Security rules (spec §6/§7):
  * Chopster never collects a platform password — only official consent pages.
  * Tokens live in automation_secrets.dpapi (Windows DPAPI), never in JSON,
    job rows, source code, or logs.
  * The UI only ever sees account metadata + status.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import secrets as pysecrets
import socket
import threading
import time
import urllib.parse
import weakref
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

import requests

from chopster.automation import events
from chopster.automation.models import (
    AuthRequiredError, PermissionDeniedError, PLATFORMS,
    UnsupportedOperationError, new_id,
)
from chopster.automation.secrets import AutomationSecrets
from chopster.automation.utils import redact

GRAPH = "https://graph.facebook.com/v21.0"
GRAPH_DIALOG = "https://www.facebook.com/v21.0/dialog/oauth"


def _log(app: Any, msg: str) -> None:
    try:
        app.logger.info("[Automation] %s", redact(msg))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Loopback OAuth 2.0 (installed-app pattern for desktop)
# --------------------------------------------------------------------------- #

class _CallbackHandler(BaseHTTPRequestHandler):
    server_version = "ChopsterOAuth/1.0"

    def log_message(self, *args: Any) -> None:   # keep tokens out of stderr
        pass

    def do_GET(self) -> None:                    # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        shared = getattr(self.server, "shared", None)
        if isinstance(shared, dict):
            # Dual-stack listeners (IPv4 + ::1) may both see the callback;
            # only the first one wins, then every server is shut down.
            if not shared:
                shared.update(params)
        else:                                     # pragma: no cover - legacy single server
            self.server.callback_params = params  # type: ignore[attr-defined]
        ok = "code" in params
        html = (
            "<!doctype html><meta charset='utf-8'><title>Chopster</title>"
            "<body style='font-family:Segoe UI,sans-serif;background:#101115;color:#F6F4EE;"
            "display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
            "<div style='text-align:center'><h2 style='color:#E3B653'>Chopster</h2>"
            + ("<p>Authorization complete. You can close this window and return to Chopster.</p>"
               if ok else
               "<p>Authorization was cancelled or denied. You can close this window.</p>")
            + "</div></body>"
        )
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        # Shut the server down right after the single callback.
        for srv in (getattr(self.server, "registry", None) or [self.server]):
            threading.Thread(target=srv.shutdown, daemon=True).start()


def _pkce_pair(hex_challenge: bool = False) -> tuple[str, str]:
    """PKCE verifier/challenge pair.

    ``hex_challenge`` encodes the SHA256 digest as lowercase hex, which is the
    encoding TikTok's desktop flow documents; the default base64url form
    (RFC 7636) is what Google and Meta expect.
    """
    verifier = pysecrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    if hex_challenge:
        challenge = digest.hex()
    else:
        challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


# --------------------------------------------------------------------------- #
# Loopback redirect URI — FIXED (not random) so it can be registered verbatim
# in each platform's developer console (Meta & TikTok reject unregistered
# redirect URIs; Google accepts any loopback port but a fixed one also works).
# --------------------------------------------------------------------------- #

DEFAULT_REDIRECT_URI = "http://localhost:51234/"
_LOOPBACK_EXACT = ("localhost", "127.0.0.1")


def validate_redirect_uri(value: str) -> tuple[bool, str]:
    """Validate a loopback callback URI. Returns (ok, normalized_or_reason)."""
    raw = str(value or "").strip()
    if not raw:
        return False, "Redirect URI kosong. Contoh yang benar: http://localhost:51234/"
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme != "http":
        return False, "Skema harus http:// — callback Chopster berjalan lokal (loopback), bukan HTTPS."
    host = (parsed.hostname or "").lower()
    if host not in _LOOPBACK_EXACT and not host.startswith("127."):
        return False, "Host harus localhost atau 127.0.0.1 — hanya alamat loopback yang diizinkan."
    if not parsed.port:
        return False, "Port wajib diisi, contoh: http://localhost:51234/"
    if not 1 <= int(parsed.port) <= 65535:
        return False, "Port tidak valid (harus 1-65535)."
    return True, f"http://{host}:{parsed.port}{parsed.path or '/'}"


def redirect_uri_for(app: Any) -> str:
    """Configured redirect URI, falling back to the default when unset/invalid."""
    try:
        stored = str(app.config.get("aut_oauth_redirect_uri") or "").strip()
    except Exception:
        stored = ""
    if not stored:
        return DEFAULT_REDIRECT_URI
    ok, value = validate_redirect_uri(stored)
    return value if ok else DEFAULT_REDIRECT_URI


class _V6HTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def _port_in_use(host: str, port: int) -> bool:
    """True when another process already LISTENS on host:port (Windows-safe)."""
    probe = socket.socket(socket.AF_INET if ":" not in host else socket.AF_INET6,
                          socket.SOCK_STREAM)
    try:
        probe.settimeout(0.4)
        return probe.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        try:
            probe.close()
        except Exception:
            pass


def _bind_servers(redirect_uri: str, shared: dict[str, Any]) -> list[Any]:
    """Bind the loopback callback listeners for redirect_uri.

    IPv4 (127.0.0.1) is mandatory — if it cannot be bound the port is owned by
    another program and we must fail loudly instead of leaking the OAuth code
    to a foreign listener. IPv6 (::1) is best-effort for dual-stack resolvers.
    """
    parsed = urllib.parse.urlparse(redirect_uri)
    host = (parsed.hostname or "localhost").lower()
    port = int(parsed.port or 0)
    if not port:
        raise AuthRequiredError(
            "Redirect URI tanpa port.",
            category="AUTH_REQUIRED",
            user_message=("Redirect URI harus memuat port, contoh: http://localhost:51234/ — "
                          "ubah di Automation → Accounts → Developer Configuration."))
    primary = "127.0.0.1" if host in _LOOPBACK_EXACT or host.startswith("127.") else host
    plan: list[tuple[str, Any]] = [(primary, ThreadingHTTPServer)]
    if host == "localhost":
        plan.append(("::1", _V6HTTPServer))

    def _busy(detail: str) -> AuthRequiredError:
        return AuthRequiredError(
            f"Callback port {port} gagal dipakai: {detail}",
            category="AUTH_REQUIRED",
            user_message=(
                f"Port {port} tidak bisa dipakai{detail}. Kemungkinan dipakai program lain — "
                f"tutup program tersebut atau ganti port di Automation → Accounts → "
                f"Developer Configuration (lalu perbarui redirect URI di konsol platform)."))

    servers: list[Any] = []
    for index, (bind_host, cls) in enumerate(plan):
        if index == 0 and _port_in_use(bind_host, port):
            raise _busy(" — port sedang didengarkan program lain")
        try:
            srv = cls((bind_host, port), _CallbackHandler)
        except OSError as exc:
            if index == 0:
                raise _busy(f" ({exc})") from exc
            continue                              # IPv6 optional
        srv.shared = shared                       # type: ignore[attr-defined]
        servers.append(srv)
    if not servers:                               # pragma: no cover - defensive
        raise _busy("")
    for srv in servers:
        srv.registry = servers                    # type: ignore[attr-defined]
    return servers


def run_desktop_oauth(
    *,
    auth_url: str,
    auth_params: dict[str, Any],
    token_url: str,
    client_id: str,
    client_secret: str = "",
    scope: str = "",
    use_pkce: bool = False,
    pkce_hex: bool = False,
    token_client_key: str = "client_id",
    timeout_sec: int = 300,
    redirect_uri: str = "",
    on_browser_open: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run a full desktop OAuth flow on the configured loopback URI.

    Raises AuthRequiredError when the user denies/closes the flow in time.
    """
    if not client_id:
        raise AuthRequiredError(
            "Client ID belum diisi. Buka Automation → Accounts → Developer Configuration dan isi "
            "developer credentials platform ini.",
            category="AUTH_REQUIRED", user_message="Developer Client ID belum dikonfigurasi.")
    ok, redirect = validate_redirect_uri(redirect_uri or DEFAULT_REDIRECT_URI)
    if not ok:
        raise AuthRequiredError(
            f"Redirect URI tidak valid: {redirect}",
            category="AUTH_REQUIRED",
            user_message=f"Redirect URI tidak valid: {redirect}")

    shared: dict[str, Any] = {}
    servers = _bind_servers(redirect, shared)
    state = pysecrets.token_urlsafe(16)
    verifier = ""
    for srv in servers:
        threading.Thread(target=srv.serve_forever, daemon=True, name="chopster-oauth").start()
    try:
        params: dict[str, Any] = dict(auth_params)
        params.update({"redirect_uri": redirect, "state": state})
        if scope:
            params["scope"] = scope
        if use_pkce:
            verifier, challenge = _pkce_pair(pkce_hex)
            params.update({"code_challenge": challenge, "code_challenge_method": "S256"})
        url = f"{auth_url}?{urllib.parse.urlencode(params)}"
        if on_browser_open:
            on_browser_open(url)
        opened = False
        try:
            opened = bool(webbrowser.open(url))
        except Exception:
            opened = False
        if not opened:
            startfile = getattr(os, "startfile", None)   # Windows fallback
            if startfile is not None:
                try:
                    startfile(url)
                    opened = True
                except Exception:
                    opened = False
        if not opened:
            # Caller may surface the URL so the user can open it manually.
            raise AuthRequiredError(
                "Browser tidak bisa dibuka otomatis.", category="AUTH_REQUIRED",
                user_message=f"Browser tidak terbuka otomatis. Salin URL berikut ke browser Anda: {url}")

        deadline = time.time() + max(30, int(timeout_sec))
        while time.time() < deadline:
            if shared:
                break
            if cancel_check is not None and cancel_check():
                raise AuthRequiredError(
                    "Otorisasi dibatalkan.", category="AUTH_REQUIRED",
                    user_message="Proses otorisasi dibatalkan. Coba lagi kapan saja.")
            time.sleep(0.25)
        else:
            raise AuthRequiredError(
                "Menunggu autorisasi habis (timeout).", category="AUTH_REQUIRED",
                user_message="Menunggu otorisasi melebihi batas waktu (5 menit). Coba lagi.")
        got = dict(shared)
        if got.get("error"):
            raise AuthRequiredError(
                f"OAuth error: {got.get('error')}", category="AUTH_REQUIRED",
                user_message="Otorisasi dibatalkan atau ditolak oleh platform.")
        if got.get("state") != state:
            raise AuthRequiredError(
                "OAuth state mismatch.", category="AUTH_REQUIRED",
                user_message="Respons otorisasi tidak cocok. Coba lagi.")
        code = got.get("code", "")
        if not code:
            raise AuthRequiredError("OAuth code kosong.", user_message="Otorisasi gagal: kode tidak diterima.")

        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect,
            token_client_key: client_id,
        }
        if client_secret:
            data["client_secret"] = client_secret
        if verifier:
            data["code_verifier"] = verifier
        resp = requests.post(token_url, data=data, timeout=60)
        if resp.status_code >= 400:
            detail = redact(resp.text)[:400]
            raise AuthRequiredError(
                f"Token exchange HTTP {resp.status_code}: {detail}",
                user_message=("Pertukaran token gagal. Periksa Client ID/Secret dan pastikan "
                              f"redirect URI terdaftar persis di konsol platform: {redirect}"))
        token = resp.json()
        if "access_token" not in token:
            raise AuthRequiredError(
                f"Token response tanpa access_token: {redact(str(token))[:300]}",
                user_message="Platform tidak mengembalikan access token.")
        expires_in = int(token.get("expires_in") or 0)
        token["expires_at"] = int(time.time()) + expires_in if expires_in else 0
        return token
    finally:
        for srv in servers:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass


def refresh_token_request(token_url: str, client_id: str, client_secret: str,
                          refresh_token: str,
                          client_key_param: str = "client_id") -> dict[str, Any]:
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        client_key_param: client_id,
    }
    if client_secret:
        data["client_secret"] = client_secret
    resp = requests.post(token_url, data=data, timeout=45)
    if resp.status_code >= 400:
        raise AuthRequiredError(
            f"Refresh HTTP {resp.status_code}: {redact(resp.text)[:300]}",
            category="AUTH_EXPIRED",
            user_message="Sesi kedaluwarsa. Sambungkan ulang akun.")
    payload = resp.json()
    expires_in = int(payload.get("expires_in") or 0)
    payload["expires_at"] = int(time.time()) + expires_in if expires_in else 0
    if payload.get("refresh_token"):
        payload["refresh_token"] = payload["refresh_token"]
    return payload


def token_is_fresh(token: dict[str, Any], skew_sec: int = 180) -> bool:
    expires_at = int(token.get("expires_at") or 0)
    if not expires_at:
        return True
    return time.time() < (expires_at - skew_sec)


# --------------------------------------------------------------------------- #
# Per-platform authorization
# --------------------------------------------------------------------------- #

class PlatformAuth:
    platform = ""

    def __init__(self, app: Any, secrets_store: AutomationSecrets):
        self.app = app
        self.secrets = secrets_store

    # -- config helpers ---------------------------------------------------
    def _cfg(self, key: str) -> str:
        try:
            return str(self.app.config.get(f"aut_{self.platform}_{key}") or "").strip()
        except Exception:
            return ""

    def _redirect_uri(self) -> str:
        """Registered loopback callback URI (shared across platforms)."""
        return redirect_uri_for(self.app)

    def _store_token(self, account_id: str, token: dict[str, Any]) -> None:
        self.secrets.set_json(self.secrets.account_key(account_id), token)

    def _load_token(self, account_id: str) -> dict[str, Any]:
        return self.secrets.get_json(self.secrets.account_key(account_id))

    def get_account(self, account_id: str) -> dict[str, Any]:
        acc = self.app.db.automation_get_account(account_id)
        if not acc:
            raise AuthRequiredError(f"Akun {account_id} tidak ditemui.",
                                    user_message="Akun tidak ditemukan. Sambungkan kembali.")
        return acc

    def connect(self, **kwargs: Any) -> dict[str, Any]:        # pragma: no cover - protocol
        raise NotImplementedError

    def ensure_access_token(self, account_id: str) -> str:     # pragma: no cover - protocol
        raise NotImplementedError

    def get_status(self, account_id: str) -> str:
        acc = self.app.db.automation_get_account(account_id)
        if not acc:
            return "missing"
        if acc.get("status") == "disconnected":
            return "disconnected"
        if str(acc.get("auth_mode") or "") == "browser":
            # Browser Session accounts have no token: the Chopster profile's
            # login cookie decides whether the session is usable.
            try:
                from chopster.automation.browser import session as browser_session
                return ("connected" if browser_session.session_valid(
                    str(acc.get("platform") or ""), str(acc.get("id") or ""))
                        else "expired")
            except Exception:
                return "expired"
        try:
            self.ensure_access_token(account_id)
            return "connected"
        except AuthRequiredError:
            return "expired"
        except Exception:
            return "error"


class YouTubeAuth(PlatformAuth):
    platform = "youtube"
    SCOPE = ("https://www.googleapis.com/auth/youtube.upload "
             "https://www.googleapis.com/auth/youtube.readonly")
    AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_URL = "https://oauth2.googleapis.com/token"

    def connect(self, **kwargs: Any) -> dict[str, Any]:
        client_id = self._cfg("client_id")
        client_secret = self._cfg("client_secret")
        token = run_desktop_oauth(
            auth_url=self.AUTH_URL,
            auth_params={"client_id": client_id, "response_type": "code",
                         "access_type": "offline", "prompt": "consent"},
            token_url=self.TOKEN_URL,
            client_id=client_id, client_secret=client_secret,
            scope=self.SCOPE, use_pkce=True, timeout_sec=300,
            redirect_uri=self._redirect_uri(),
            on_browser_open=kwargs.get("on_browser_open"),
            cancel_check=kwargs.get("cancel_check"),
        )
        headers = {"Authorization": f"Bearer {token['access_token']}"}
        resp = requests.get(
            "https://www.googleapis.com/youtube/v3/channels",
            params={"part": "snippet,contentDetails", "mine": "true"},
            headers=headers, timeout=45)
        if resp.status_code >= 400:
            raise AuthRequiredError(
                f"Channel lookup HTTP {resp.status_code}: {redact(resp.text)[:300]}",
                user_message="Berhasil login tetapi channel tidak bisa dibaca. Periksa izin YouTube.")
        items = (resp.json() or {}).get("items") or []
        channel = items[0] if items else {}
        account_id = new_id("acc_yt_")
        title = (channel.get("snippet") or {}).get("title") or "YouTube Channel"
        self._store_token(account_id, token)
        self.app.db.automation_upsert_account(
            account_id, "youtube", display_name=title,
            external_id=channel.get("id") or "", auth_mode="oauth",
            status="connected", credential_ref=self.secrets.account_key(account_id),
            metadata=json.dumps({"subscribers": ((channel.get("statistics") or {}).get("subscriberCount"))},
                                ensure_ascii=False))
        events.emit(events.AUTH_STATE_CHANGED, {"platform": "youtube", "status": "connected"})
        _log(self.app, f"YouTube connected channel={title}")
        return self.app.db.automation_get_account(account_id) or {}

    def ensure_access_token(self, account_id: str) -> str:
        token = self._load_token(account_id)
        if not token.get("access_token"):
            raise AuthRequiredError("Token YouTube hilang.", user_message="Sambungkan ulang YouTube.")
        if token_is_fresh(token):
            return str(token["access_token"])
        if not token.get("refresh_token"):
            raise AuthRequiredError("Token YouTube kedaluwarsa tanpa refresh token.",
                                    category="AUTH_EXPIRED",
                                    user_message="Sesi YouTube kedaluwarsa. Sambungkan ulang.")
        fresh = refresh_token_request(self.TOKEN_URL, self._cfg("client_id"),
                                      self._cfg("client_secret"), token["refresh_token"])
        merged = dict(token)
        merged.update(fresh)
        if not merged.get("refresh_token"):
            merged["refresh_token"] = token["refresh_token"]
        self._store_token(account_id, merged)
        return str(merged["access_token"])


class FacebookAuth(PlatformAuth):
    platform = "facebook"
    SCOPE = ("pages_show_list,pages_read_engagement,pages_manage_posts,"
             "pages_read_user_content,instagram_basic,instagram_content_publish")
    DIALOG = GRAPH_DIALOG
    TOKEN_URL = f"{GRAPH}/oauth/access_token"

    def connect(self, **kwargs: Any) -> dict[str, Any]:
        client_id = self._cfg("client_id")
        client_secret = self._cfg("client_secret")
        token = run_desktop_oauth(
            auth_url=self.DIALOG,
            auth_params={"client_id": client_id, "response_type": "code"},
            token_url=self.TOKEN_URL,
            client_id=client_id, client_secret=client_secret,
            scope=self.SCOPE, timeout_sec=300,
            redirect_uri=self._redirect_uri(),
            on_browser_open=kwargs.get("on_browser_open"),
            cancel_check=kwargs.get("cancel_check"),
        )
        # Long-lived user token then page discovery (spec §6.2).
        token = self._exchange_long_lived(token)
        pages = self._list_pages(token["access_token"])
        account_id = new_id("acc_fb_")
        self._store_token(account_id, {"user_token": token, "page_token": {}})
        if not pages:
            self.app.db.automation_upsert_account(
                account_id, "facebook", display_name="Facebook", auth_mode="oauth",
                status="page_selection_required", credential_ref=self.secrets.account_key(account_id),
                metadata=json.dumps({"pages": []}))
            events.emit(events.AUTH_STATE_CHANGED, {"platform": "facebook",
                                                    "status": "page_selection_required"})
            return {"account": self.app.db.automation_get_account(account_id), "pages": []}
        # Single page → auto-select; multiple → let UI choose (spec §6.2 states).
        return {"account": self._register_account(account_id, token, pages[0]),
                "pages": pages, "needs_page_selection": len(pages) > 1, "user_token": token}

    def _exchange_long_lived(self, token: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = requests.get(f"{GRAPH}/oauth/access_token", params={
                "grant_type": "fb_exchange_token",
                "client_id": self._cfg("client_id"),
                "client_secret": self._cfg("client_secret"),
                "fb_exchange_token": token.get("access_token", ""),
            }, timeout=45)
            if resp.status_code < 400:
                data = resp.json()
                data.setdefault("expires_at", 0)   # long-lived: no daily expiry
                if token.get("refresh_token"):
                    data["refresh_token"] = token["refresh_token"]
                return data
        except Exception:
            pass
        return token

    def _list_pages(self, user_token: str) -> list[dict[str, Any]]:
        resp = requests.get(f"{GRAPH}/me/accounts", params={
            "fields": "id,name,access_token,tasks",
            "access_token": user_token,
        }, timeout=45)
        if resp.status_code >= 400:
            raise PermissionDeniedError(
                f"Page discovery HTTP {resp.status_code}: {redact(resp.text)[:300]}",
                user_message="Facebook tidak memberi izin daftar Page. Periksa permintaan izin app.")
        return list((resp.json() or {}).get("data") or [])

    def _register_account(self, account_id: str, user_token: dict[str, Any],
                          page: dict[str, Any]) -> dict[str, Any]:
        page_token = page.get("access_token") or ""
        stored = self._load_token(account_id) or {}
        stored["user_token"] = user_token
        stored["page_token"] = {page.get("id", ""): page_token}
        self._store_token(account_id, stored)
        self.app.db.automation_upsert_account(
            account_id, "facebook", display_name=str(page.get("name") or "Facebook Page"),
            external_id=str(page.get("id") or ""), auth_mode="oauth", status="connected",
            credential_ref=self.secrets.account_key(account_id),
            metadata=json.dumps({"tasks": page.get("tasks") or []}, ensure_ascii=False))
        events.emit(events.AUTH_STATE_CHANGED, {"platform": "facebook", "status": "connected"})
        _log(self.app, f"Facebook page connected id={page.get('id')}")
        return self.app.db.automation_get_account(account_id) or {}

    def select_page(self, account_id: str, page_id: str) -> dict[str, Any]:
        stored = self._load_token(account_id)
        user_token = (stored.get("user_token") or {}).get("access_token") or ""
        if not user_token:
            raise AuthRequiredError("Sesi Facebook hilang.", user_message="Sambungkan ulang Facebook.")
        pages = self._list_pages(user_token)
        page = next((p for p in pages if str(p.get("id")) == str(page_id)), None)
        if page is None:
            raise PermissionDeniedError("Page tidak ditemukan.", user_message="Page tidak ditemukan di akun ini.")
        return self._register_account(account_id, {"access_token": user_token}, page)

    def list_pages(self, account_id: str) -> list[dict[str, Any]]:
        stored = self._load_token(account_id)
        user_token = (stored.get("user_token") or {}).get("access_token") or ""
        if not user_token:
            return []
        return self._list_pages(user_token)

    def ensure_access_token(self, account_id: str) -> str:
        """Returns the long-lived *page* token used for publishing."""
        stored = self._load_token(account_id)
        acc = self.get_account(account_id)
        page_id = str(acc.get("external_id") or "")
        page_tokens = stored.get("page_token") or {}
        token = page_tokens.get(page_id) or ""
        if token:
            return str(token)
        user_token = (stored.get("user_token") or {}).get("access_token") or ""
        if not user_token:
            raise AuthRequiredError("Token Facebook hilang.", user_message="Sambungkan ulang Facebook.")
        if page_id:
            resp = requests.get(f"{GRAPH}/{page_id}",
                                params={"fields": "access_token", "access_token": user_token},
                                timeout=45)
            if resp.status_code < 400:
                token = str((resp.json() or {}).get("access_token") or "")
                if token:
                    page_tokens[page_id] = token
                    stored["page_token"] = page_tokens
                    self._store_token(account_id, stored)
                    return token
        raise AuthRequiredError("Page token tidak tersedia.",
                                user_message="Token Page tidak ada. Sambungkan ulang Facebook.")


class TikTokAuth(PlatformAuth):
    platform = "tiktok"
    #: Scope set for the default (inbox/draft) publish path. ``_scopes()`` picks
    #: the set that matches the configured path, since asking for a scope the
    #: endpoint does not accept yields a token the publisher cannot use.
    SCOPE = "user.info.basic,video.upload"
    AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
    TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"

    def _scopes(self) -> str:
        """Scope set matching the configured TikTok publish path.

        Inbox drafts POST to ``/v2/post/publish/inbox/video/init/`` and require
        ``video.upload``; Direct Post requires ``video.publish``. TikTok grants
        exactly what the authorize request asks for, so requesting the other
        path's scope leaves the granted token unable to call the endpoint.
        """
        mode = self._cfg("mode").lower() or "inbox"
        if mode == "direct":
            return "user.info.basic,video.publish"
        return "user.info.basic,video.upload"

    def connect(self, **kwargs: Any) -> dict[str, Any]:
        client_id = self._cfg("client_id")
        client_secret = self._cfg("client_secret")
        if not client_id:
            raise AuthRequiredError(
                "TikTok client key belum diisi.", category="AUTH_REQUIRED",
                user_message="Isi TikTok Client Key/Secret di Accounts → Advanced dulu.")
        token = run_desktop_oauth(
            auth_url=self.AUTH_URL,
            auth_params={"client_key": client_id, "response_type": "code"},
            token_url=self.TOKEN_URL,
            client_id=client_id, client_secret=client_secret,
            scope=self._scopes(), timeout_sec=300,
            # TikTok's desktop flow requires PKCE, and it documents the
            # challenge as the *hex* SHA256 of the verifier (unlike Google/Meta,
            # which use base64url). The token endpoint also names the credential
            # client_key, not client_id.
            use_pkce=True, pkce_hex=True, token_client_key="client_key",
            redirect_uri=self._redirect_uri(),
            on_browser_open=kwargs.get("on_browser_open"),
            cancel_check=kwargs.get("cancel_check"),
        )
        # The token endpoint names the credential client_key (token_client_key
        # above); from here on we only need the access token.
        info = self._user_info(token["access_token"])
        account_id = new_id("acc_tt_")
        self._store_token(account_id, token)
        name = str(info.get("display_name") or "").strip() or "TikTok"
        self.app.db.automation_upsert_account(
            account_id, "tiktok", display_name=name,
            external_id=str(info.get("open_id") or ""), auth_mode="oauth",
            status="connected", credential_ref=self.secrets.account_key(account_id),
            metadata=json.dumps({"display_name": name}, ensure_ascii=False))
        events.emit(events.AUTH_STATE_CHANGED, {"platform": "tiktok", "status": "connected"})
        _log(self.app, f"TikTok connected user={name}")
        return self.app.db.automation_get_account(account_id) or {}

    def _user_info(self, access_token: str) -> dict[str, Any]:
        """Read the authorized account's basic profile (Display API v2).

        ``/v2/user/info/`` is a **GET** endpoint — POSTing to it makes TikTok's
        gateway answer ``404 path(Janus)`` — and it only accepts fields the
        granted scopes cover. ``user.info.basic`` allows ``open_id`` and
        ``display_name``; ``username`` needs ``user.info.profile`` (not
        requested here), so the account is shown by its display name.
        """
        resp = requests.get(
            "https://open.tiktokapis.com/v2/user/info/",
            params={"fields": "open_id,display_name"},
            headers={"Authorization": f"Bearer {access_token}"}, timeout=45)
        if resp.status_code >= 400:
            raise AuthRequiredError(
                f"TikTok user info HTTP {resp.status_code}: {redact(resp.text)[:300]}",
                user_message="Info akun TikTok gagal dibaca. Periksa scope user.info.basic.")
        payload = resp.json() or {}
        error = payload.get("error") or {}
        if error.get("code") not in (None, "", "ok"):
            raise AuthRequiredError(
                f"TikTok user info error: {redact(str(error))[:300]}",
                user_message=(f"TikTok menolak permintaan info akun "
                              f"({error.get('code')}). Periksa scope user.info.basic."))
        data = payload.get("data") or {}
        return data.get("user") or {}

    def ensure_access_token(self, account_id: str) -> str:
        token = self._load_token(account_id)
        if not token.get("access_token"):
            raise AuthRequiredError("Token TikTok hilang.", user_message="Sambungkan ulang TikTok.")
        if token_is_fresh(token):
            return str(token["access_token"])
        if not token.get("refresh_token"):
            raise AuthRequiredError("Token TikTok kedaluwarsa.", category="AUTH_EXPIRED",
                                    user_message="Sesi TikTok kedaluwarsa. Sambungkan ulang.")
        fresh = refresh_token_request(self.TOKEN_URL, self._cfg("client_id"),
                                      self._cfg("client_secret"), token["refresh_token"],
                                      client_key_param="client_key")
        merged = dict(token)
        merged.update(fresh)
        merged.setdefault("refresh_token", token["refresh_token"])
        self._store_token(account_id, merged)
        return str(merged["access_token"])


class InstagramAuth(PlatformAuth):
    """Hybrid Instagram: Official Graph API (via linked FB Page) or Browser Session."""
    platform = "instagram"

    def connect_api(self, facebook_account_id: str) -> dict[str, Any]:
        fb_auth = FacebookAuth(self.app, self.secrets)
        page_token = fb_auth.ensure_access_token(facebook_account_id)
        fb_acc = self.get_account(facebook_account_id)
        page_id = str(fb_acc.get("external_id") or "")
        resp = requests.get(f"{GRAPH}/{page_id}/instagram_accounts",
                            params={"fields": "id,username", "access_token": page_token},
                            timeout=45)
        if resp.status_code >= 400:
            raise PermissionDeniedError(
                f"IG discovery HTTP {resp.status_code}: {redact(resp.text)[:300]}",
                user_message="Halaman ini tidak punya Instagram Professional terhubung. "
                             "Hubungkan IG Professional ke Page dulu di Meta.")
        accounts = list((resp.json() or {}).get("data") or [])
        if not accounts:
            raise PermissionDeniedError(
                "No linked Instagram Professional account.",
                user_message="Tidak ada akun Instagram Professional terhubung ke Page ini.")
        ig = accounts[0]
        account_id = new_id("acc_ig_")
        self._store_token(account_id, {"page_token": page_token, "page_id": page_id})
        self.app.db.automation_upsert_account(
            account_id, "instagram",
            display_name=f"@{ig.get('username') or 'instagram'}",
            external_id=str(ig.get("id") or ""), auth_mode="oauth", status="connected",
            credential_ref=self.secrets.account_key(account_id),
            metadata=json.dumps({"page_id": page_id, "facebook_account": facebook_account_id}))
        events.emit(events.AUTH_STATE_CHANGED, {"platform": "instagram", "status": "connected"})
        return self.app.db.automation_get_account(account_id) or {}

    def connect_browser(self) -> dict[str, Any]:
        """Browser Session mode — no password ever collected by Chopster (§6.4)."""
        from chopster.automation.browser import session as browser_session
        account_id = new_id("acc_igbr_")
        try:
            channel = str(self.app.config.get("aut_browser_channel") or "")
        except Exception:
            channel = ""
        ok = browser_session.ensure_login("instagram", channel=channel, account_id=account_id)
        status = "connected" if ok else "session_required"
        self.app.db.automation_upsert_account(
            account_id, "instagram", display_name="Instagram (Browser Session)",
            external_id="", auth_mode="browser", status=status,
            credential_ref="browser:instagram",
            metadata=json.dumps({"profile": "instagram"}))
        events.emit(events.AUTH_STATE_CHANGED, {"platform": "instagram", "status": status})
        return self.app.db.automation_get_account(account_id) or {}

    def ensure_access_token(self, account_id: str) -> str:
        acc = self.get_account(account_id)
        if acc.get("auth_mode") == "browser":
            from chopster.automation.browser import session as browser_session
            if browser_session.session_valid("instagram", str(acc.get("id") or "")):
                return "browser-session"
            raise AuthRequiredError(
                "Instagram browser session belum login.",
                category="AUTH_REQUIRED",
                user_message="Buka Instagram Login di Automation → Accounts, login manual, lalu coba lagi.")
        stored = self._load_token(account_id)
        token = str(stored.get("page_token") or "")
        if not token:
            raise AuthRequiredError("Token Instagram hilang.", user_message="Sambungkan ulang Instagram.")
        return token


# --------------------------------------------------------------------------- #
# AuthManager — façade over the per-platform auth classes
# --------------------------------------------------------------------------- #

# Per-platform "where do I get credentials" guidance surfaced in the connect
# popup when Developer Configuration is still empty (spec §6: never vague errors).
_SETUP_HINTS: dict[str, tuple[str, str]] = {
    "youtube": (
        "YouTube",
        "console.cloud.google.com → APIs & Services → Library: aktifkan 'YouTube Data API v3' → "
        "Credentials → Create credentials → OAuth client ID → tipe 'Desktop app'. "
        "Salin Client ID dan Client Secret.",
    ),
    "facebook": (
        "Facebook",
        "developers.facebook.com → My Apps → Create App → tambah produk 'Facebook Login' → "
        "Facebook Login → Settings: isi 'Valid OAuth Redirect URIs' dengan redirect URI di bawah → "
        "Settings → Basic: salin App ID (isi sebagai Client ID) dan App Secret.",
    ),
    "tiktok": (
        "TikTok",
        "developers.tiktok.com → Create app → produk 'Content Posting API' → OAuth: daftarkan "
        "'Redirect URL' persis seperti redirect URI di bawah → salin Client Key (isi sebagai "
        "Client ID) dan Client Secret.",
    ),
}


#: ``statuses()`` probes every connected account: a DPAPI decrypt per account
#: plus, when a token is stale, a real OAuth refresh round-trip. Measured at
#: ~250 ms per account on a normal machine, and the Automation page asks for the
#: snapshot five times per rebuild plus once per 8 s tick — which is what made
#: the page feel like it hung on every visit. The last probe is therefore reused
#: for a short window; connect/disconnect and any AUTH_STATE_CHANGED event clear
#: it immediately, so the UI never shows a state older than the user's click.
STATUS_CACHE_TTL_SEC = 30.0


class AuthManager:
    def __init__(self, app: Any):
        self.app = app
        self.secrets = AutomationSecrets()
        self._by_platform: dict[str, PlatformAuth] = {
            "youtube": YouTubeAuth(app, self.secrets),
            "facebook": FacebookAuth(app, self.secrets),
            "tiktok": TikTokAuth(app, self.secrets),
            "instagram": InstagramAuth(app, self.secrets),
        }
        self._status_cache: dict[str, dict[str, Any]] | None = None
        self._status_cached_at = 0.0
        # Every platform emits AUTH_STATE_CHANGED on connect/login/disconnect,
        # so one listener keeps the cache honest. The reference is weak: the
        # event bus is process-wide and must not keep managers alive.
        _manager = weakref.ref(self)

        def _drop_cache(_payload: dict[str, Any] | None = None) -> None:
            target = _manager()
            if target is not None:
                target.invalidate_status_cache()

        self._status_listener = _drop_cache
        self._unsub_auth_state: Any = None
        try:
            self._unsub_auth_state = events.subscribe(
                events.AUTH_STATE_CHANGED, self._status_listener)
        except Exception:
            pass

    def close(self) -> None:
        """Release the process-wide ``AUTH_STATE_CHANGED`` subscription.

        The event bus outlives every manager, so the listener must be removed
        explicitly when the manager stops (``AutomationManager.stop``).
        """
        unsub = self._unsub_auth_state
        self._unsub_auth_state = None
        self._status_listener = None
        if unsub is not None:
            try:
                unsub()
            except Exception:
                pass
        self.invalidate_status_cache()

    def invalidate_status_cache(self) -> None:
        """Forget the memoised ``statuses()`` snapshot (call after auth changes)."""
        self._status_cache = None
        self._status_cached_at = 0.0

    def for_platform(self, platform: str) -> PlatformAuth:
        auth = self._by_platform.get(str(platform or "").lower())
        if auth is None:
            raise UnsupportedOperationError(f"Platform {platform} tidak didukung.",
                                            user_message="Platform tidak didukung.")
        return auth

    def require_client_credentials(self, platform: str) -> None:
        """Preflight: fail fast with actionable steps before opening a browser."""
        hint = _SETUP_HINTS.get(platform)
        if hint is None:
            return
        label, steps = hint
        cid = str(self.app.config.get(f"aut_{platform}_client_id") or "").strip()
        csec = str(self.app.config.get(f"aut_{platform}_client_secret") or "").strip()
        missing = [name for name, value in (("Client ID", cid), ("Client Secret", csec)) if not value]
        if not missing:
            return
        redirect = redirect_uri_for(self.app)
        raise AuthRequiredError(
            f"{platform}: {', '.join(missing)} belum diisi.",
            category="AUTH_REQUIRED",
            user_message=(
                f"{label}: {', '.join(missing)} belum diisi.\n\n"
                "1. Buka Automation → Accounts → Developer Configuration.\n"
                "2. Isi kredensial lalu klik 'Simpan Developer Config'.\n"
                f"3. Cara mendapatkannya: {steps}\n\n"
                f"Redirect URI yang harus didaftarkan di konsol {label}: {redirect}"
            ),
        )

    def _connect_browser(self, platform: str) -> dict[str, Any]:
        """Register a Browser Session account for *platform* (no password).

        Opens Chopster's dedicated browser profile and waits for the user to
        finish login / MFA / CAPTCHA themselves (spec §6.4).
        """
        from chopster.automation.browser import session as browser_session
        try:
            channel = str(self.app.config.get("aut_browser_channel") or "")
        except Exception:
            channel = ""
        # Reuse an existing Browser Session account for this platform instead of
        # minting a fresh id on every click. Each account owns its own profile
        # folder, so creating a new one each time left orphan duplicates that
        # shadowed the profile the user had actually logged into.
        existing = [a for a in self.app.db.automation_list_accounts(platform)
                    if str(a.get("auth_mode") or "") == "browser"]
        live = [a for a in existing if browser_session.session_valid(platform, str(a.get("id") or ""))]
        active = [a for a in existing if str(a.get("status") or "") != "disconnected"]
        reuse = live or active or existing
        account_id = str(reuse[0]["id"]) if reuse else new_id(f"acc_{platform}br_")
        # The account id exists before login so the browser profile folder
        # matches this account (keeps multiple accounts on one platform isolated).
        ok = browser_session.ensure_login(platform, channel=channel, account_id=account_id)
        status = "connected" if ok else "session_required"
        self.app.db.automation_upsert_account(
            account_id, platform,
            display_name=f"{str(platform).title()} (Browser Session)",
            external_id="", auth_mode="browser", status=status,
            credential_ref=f"browser:{platform}",
            metadata=json.dumps({"profile": platform}))
        events.emit(events.AUTH_STATE_CHANGED, {"platform": platform, "status": status})
        return self.app.db.automation_get_account(account_id) or {}

    def connect(self, platform: str, **kwargs: Any) -> dict[str, Any]:
        platform = str(platform or "").lower()
        if platform not in PLATFORMS:
            raise UnsupportedOperationError(f"Platform {platform} tidak didukung.")
        # Browser Session mode works for any platform that advertises
        # browser_fallback (Instagram/TikTok/Facebook). The user logs in manually
        # in Chopster's own browser profile; no password is ever collected.
        if str(kwargs.get("mode") or "api") == "browser":
            try:
                return {"account": self._connect_browser(platform)}
            except ImportError as exc:
                raise AuthRequiredError(
                    f"Playwright belum terpasang: {exc}", category="AUTH_REQUIRED",
                    user_message=("Browser Session butuh Playwright. Jalankan sekali: "
                                  "pip install playwright (Chopster memakai Edge/Chrome terpasang; "
                                  "bila tidak ada, jalankan python -m playwright install chromium)."))
        if platform == "instagram":
            mode = str(kwargs.get("mode") or "api")
            if mode == "browser":
                try:
                    return {"account": self.for_platform("instagram").connect_browser()}
                except ImportError as exc:
                    raise AuthRequiredError(
                        f"Playwright belum terpasang: {exc}", category="AUTH_REQUIRED",
                        user_message=("Instagram Browser Session butuh Playwright. Jalankan sekali: "
                                      "pip install playwright && python -m playwright install chromium "
                                      "lalu coba lagi."))
            fb_id = str(kwargs.get("facebook_account_id") or "")
            if not fb_id:
                fb = self.app.db.automation_list_accounts("facebook")
                fb_ok = [a for a in fb if a.get("status") == "connected"]
                if not fb_ok:
                    raise AuthRequiredError(
                        "Instagram API butuh Facebook Page terhubung dulu.",
                        user_message="Sambungkan Facebook Page dulu, lalu hubungkan Instagram (API).")
                fb_id = str(fb_ok[0]["id"])
            account = self.for_platform("instagram").connect_api(fb_id)
            self.invalidate_status_cache()
            return {"account": account}
        self.require_client_credentials(platform)
        account = self.for_platform(platform).connect(**kwargs)
        self.invalidate_status_cache()
        return account

    def statuses(self, *, force: bool = False) -> dict[str, dict[str, Any]]:
        """Per-platform connection state, memoised for ``STATUS_CACHE_TTL_SEC``.

        Pass ``force=True`` to re-probe (used by explicit user refresh actions).
        The cached snapshot is returned as a copy so no caller can mutate it.
        """
        now = time.time()
        cached = self._status_cache
        if (not force and cached is not None
                and (now - self._status_cached_at) < STATUS_CACHE_TTL_SEC):
            return copy.deepcopy(cached)
        out = self._probe_statuses()
        self._status_cache = copy.deepcopy(out)
        self._status_cached_at = now
        return out

    def _probe_statuses(self) -> dict[str, dict[str, Any]]:
        """Real probe: token check (and refresh) for every connected account."""
        out: dict[str, dict[str, Any]] = {}
        accounts = self.app.db.automation_list_accounts()
        for platform in PLATFORMS:
            rows = [a for a in accounts if a.get("platform") == platform and a.get("status") != "disconnected"]
            if not rows:
                out[platform] = {"state": "not_connected", "label": "Not connected", "accounts": []}
                continue
            states = []
            for acc in rows:
                st = acc.get("status") or "connected"
                if st == "connected":
                    try:
                        # Route through the manager so Browser Session accounts
                        # (TikTok/Facebook/Instagram) are validated against their
                        # persistent profile instead of an API token they do not
                        # have — otherwise a valid session would read as expired.
                        self.ensure_access_token(str(acc["id"]))
                    except Exception as exc:
                        if isinstance(exc, AuthRequiredError):
                            st = "session_required" if acc.get("auth_mode") == "browser" else "expired"
                        else:
                            st = "error"
                states.append(st)
            if any(s == "connected" for s in states):
                state = "connected"
            elif any(s in ("session_required",) for s in states):
                state = "session_required"
            elif any(s in ("page_selection_required",) for s in states):
                state = "page_selection_required"
            elif any(s == "expired" for s in states):
                state = "expired"
            else:
                state = states[0] if states else "error"
            label = {
                "connected": "Connected",
                "expired": "Expired — reauthorize",
                "session_required": "Browser session requires login",
                "page_selection_required": "Choose a Page",
                "error": "Error",
            }.get(state, state)
            out[platform] = {"state": state, "label": label, "accounts": rows}
        return out

    def ensure_access_token(self, account_id: str) -> str:
        acc = self.app.db.automation_get_account(account_id)
        if not acc:
            raise AuthRequiredError("Akun tidak ditemui.", user_message="Akun tidak ditemukan.")
        if acc.get("status") == "disconnected":
            raise AuthRequiredError("Akun diputus.", user_message="Akun sudah diputus. Sambungkan ulang.")
        # Browser Session accounts authenticate through Chopster's own persistent
        # profile, not an API token. Their credential is the login cookie inside
        # the per-account profile, so the API token gate must not apply here —
        # otherwise every job for a TikTok/Facebook browser account would fail
        # at AUTH_CHECK even while the session is perfectly valid.
        if str(acc.get("auth_mode") or "") == "browser":
            platform = str(acc.get("platform") or "")
            if self._browser_session_valid(platform, account_id):
                return "browser-session"
            raise AuthRequiredError(
                f"{platform} browser session belum login.",
                category="AUTH_REQUIRED",
                user_message=("Sesi browser belum login. Buka Automation > Accounts > "
                              "+ Sambungkan Akun > Browser Session (atau Login/Verifikasi), "
                              "login manual di jendela browser Chopster, lalu lanjutkan."),
            )
        return self.for_platform(str(acc["platform"])).ensure_access_token(account_id)

    @staticmethod
    def _browser_session_valid(platform: str, account_id: str) -> bool:
        """True when the per-account Chopster browser profile is logged in.

        Kept defensive: a missing Playwright package or an unreadable marker
        simply counts as "not logged in" instead of crashing the status probe.
        """
        try:
            from chopster.automation.browser import session as browser_session
            return bool(browser_session.session_valid(platform, account_id))
        except Exception:
            return False

    def disconnect(self, account_id: str) -> None:
        """Remove credentials but preserve history rows (spec §80)."""
        acc = self.app.db.automation_get_account(account_id)
        if not acc:
            return
        self.secrets.delete(self.secrets.account_key(account_id))
        if acc.get("auth_mode") == "browser":
            try:
                from chopster.automation.browser import session as browser_session
                browser_session.clear_session(str(acc.get("platform") or "instagram"),
                                              str(acc.get("id") or ""))
            except Exception:
                pass
        self.app.db.automation_update_account(
            account_id, status="disconnected", credential_ref="")
        self.invalidate_status_cache()
        events.emit(events.AUTH_STATE_CHANGED,
                    {"platform": acc.get("platform"), "status": "disconnected"})
        _log(self.app, f"Account disconnected platform={acc.get('platform')} id={account_id}")

    def capabilities(self, account_id: str) -> dict[str, Any]:
        from chopster.automation.models import capabilities_for
        acc = self.app.db.automation_get_account(account_id)
        if not acc:
            return capabilities_for("unknown").to_dict()
        caps = capabilities_for(str(acc.get("platform")), str(acc.get("auth_mode") or "oauth"))
        data = caps.to_dict()
        data["auth_valid"] = acc.get("status") == "connected"
        return data
