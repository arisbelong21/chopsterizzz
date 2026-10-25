"""Official API publishers + capability registry (spec §48/§49).

All publishers never log tokens (redaction applied), surface real platform
errors (never fake success), report progress through a callback, honor
cooperative cancellation, and return PublishResult for the job runner.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

from chopster.automation.auth import GRAPH, AuthManager
from chopster.automation.content import audience_declared
from chopster.automation.models import (
    AuthRequiredError, AutomationError, CancelledError, InvalidMediaError,
    MetadataInvalidError, NeedsActionError, PermissionDeniedError,
    QuotaExceededError, RetryableError, UnsupportedOperationError,
    VerificationFailedError,
)
from chopster.automation.utils import redact

ProgressCb = Callable[[int, str], None]
CancelCheck = Callable[[], bool]


def _parse_resumable_range(header: Any) -> int | None:
    """Translate a resumable-upload ``Range`` header into the next byte offset.

    Google's resumable protocol replies to an accepted chunk with HTTP 308 and
    a ``Range: bytes=0-<last>`` header describing how many bytes were actually
    persisted. Returning ``last + 1`` lets the uploader resume from the true
    server offset instead of assuming the whole chunk landed. ``None`` means the
    header was absent/unparsable, so the caller falls back to its old behavior.
    """
    if not header:
        return None
    text = str(header).strip().lower()
    if not text.startswith("bytes="):
        return None
    spec = text[len("bytes="):].split("/", 1)[0].strip()
    span = spec.split("-", 1)
    if len(span) != 2 or not span[1].strip():
        return None
    try:
        last = int(span[1].strip())
    except (TypeError, ValueError):
        return None
    if last < 0:
        return None
    return last + 1


# TikTok Content Posting API chunking rules
# (https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide):
#   * every chunk must be >= 5 MB and <= 64 MB,
#   * the *final* chunk may exceed chunk_size (up to 128 MB),
#   * total_chunk_count = floor(video_size / chunk_size),
#   * a file smaller than 5 MB is sent as a single chunk (chunk_size == size).
# Declaring chunk_size == video_size (the previous behaviour) was only accepted
# for files that happened to land inside 5-64 MB; smaller clips and larger
# exports were rejected by TikTok with an opaque init error.
TIKTOK_MIN_CHUNK = 5 * 1000 * 1000
TIKTOK_MAX_CHUNK = 64 * 1000 * 1000
TIKTOK_PREFERRED_CHUNK = 10 * 1000 * 1000
TIKTOK_MAX_FINAL_CHUNK = 128 * 1000 * 1000


def tiktok_chunk_plan(size: int) -> tuple[int, int]:
    """Return ``(chunk_size, total_chunk_count)`` for a TikTok file upload."""
    size = int(size or 0)
    if size <= 0:
        return 0, 0
    if size <= TIKTOK_MAX_CHUNK:
        # Fits in one chunk. Sub-5 MB files are sent whole (chunk_size == size)
        # and 5-64 MB files use chunk_size == size, which is inside the allowed
        # 5-64 MB window.
        return size, 1
    chunk = TIKTOK_PREFERRED_CHUNK
    total = max(1, size // chunk)
    # Defensive: keep the trailing chunk inside TikTok's 128 MB ceiling by
    # splitting further when a pathological size would overflow it.
    while total > 1 and (size - (total - 1) * chunk) > TIKTOK_MAX_FINAL_CHUNK:
        total += 1
    return chunk, total


@dataclass
class PublishContext:
    job: dict[str, Any]
    account: dict[str, Any]
    payload: dict[str, Any]
    media_path: str
    progress: ProgressCb = lambda pct, label: None
    cancel_check: CancelCheck = lambda: False


@dataclass
class PublishResult:
    ok: bool
    status: str = "PUBLISHED"
    external_id: str = ""
    external_url: str = ""
    verification_status: str = ""
    message: str = ""
    error: BaseException | None = None


@dataclass
class VerifyResult:
    ok: bool
    external_id: str = ""
    external_url: str = ""
    verification_status: str = "verified"
    status: str = "PUBLISHED"
    message: str = ""


def _check_cancel(ctx: PublishContext) -> None:
    try:
        if ctx.cancel_check():
            raise CancelledError("Job dibatalkan pengguna.", user_message="Dibatalkan.")
    except CancelledError:
        raise
    except Exception:
        pass


def _media_size(path: str) -> int:
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise InvalidMediaError(f"Media tidak ditemukan: {path}", user_message="File media tidak ditemukan. Render ulang atau pilih file lain.")
    size = p.stat().st_size
    if size <= 0:
        raise InvalidMediaError("Media kosong.", user_message="File media kosong/tidak valid.")
    return size


def _graph_error(resp: requests.Response, default: str) -> AutomationError:
    msg = default
    code = 0
    sub = ""
    try:
        data = resp.json() or {}
        err = data.get("error") or {}
        msg = str(err.get("message") or default)
        code = int(err.get("code") or 0)
        sub = str(err.get("error_subcode") or "")
    except Exception:
        msg = str(resp.text or default)[:300]
        code = resp.status_code
    if resp.status_code == 401:
        return AuthRequiredError(f"HTTP 401: {redact(msg)}", user_message="Sesi kedaluwarsa. Sambungkan ulang akun.")
    if resp.status_code == 403 or code in (10, 200, 102, 104):
        return PermissionDeniedError(f"HTTP {resp.status_code} code={code}: {redact(msg)}", user_message=f"Izin platform ditolak: {msg[:160]}")
    if resp.status_code == 429:
        return AutomationError(f"Rate limited: {redact(msg)}", category="RATE_LIMITED", retryable=True, user_message="Rate limit platform. Akan dicoba lagi.")
    if resp.status_code >= 500:
        return RetryableError(f"HTTP {resp.status_code}: {redact(msg)}", user_message="Server platform bermasalah. Akan dicoba lagi.")
    return AutomationError(f"HTTP {resp.status_code} code={code} sub={sub}: {redact(msg)}", category="PROCESSING_FAILED", user_message=f"Platform menolak: {msg[:180]}")


class BasePublisher:
    platform = ""

    def __init__(self, app: Any, auth: AuthManager):
        self.app = app
        self.auth = auth

    def _log(self, msg: str) -> None:
        try:
            self.app.logger.info("[Automation][%s] %s", self.platform, redact(msg))
        except Exception:
            pass

    def publish(self, ctx: PublishContext) -> PublishResult:
        raise NotImplementedError

    def verify(self, ctx: PublishContext, external_id: str) -> VerifyResult:
        return VerifyResult(ok=True, external_id=external_id, verification_status="accepted")


# --------------------------------------------------------------------------- #
# YouTube — resumable videos.insert (official Data API v3)
# --------------------------------------------------------------------------- #

class YouTubePublisher(BasePublisher):
    platform = "youtube"
    UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
    API_URL = "https://www.googleapis.com/youtube/v3/videos"

    def _metadata(self, ctx: PublishContext) -> dict[str, Any]:
        payload = ctx.payload
        privacy = str(payload.get("privacy") or "public")
        if privacy not in ("public", "unlisted", "private"):
            privacy = "public"
        title = str(payload.get("title") or "").strip()
        description = str(payload.get("description") or "")
        # Never fabricate metadata: an empty title used to be replaced with a
        # placeholder and uploaded anyway, which is exactly the "title kosong di
        # YouTube" bug. Fail loudly instead (spec §8.4).
        problems: list[str] = []
        if not title:
            problems.append("Judul video kosong")
        if not description.strip():
            problems.append("Deskripsi video kosong")
        made_for_kids = payload.get("made_for_kids")
        if not audience_declared(payload):
            problems.append(
                "Deklarasi audiens YouTube belum dipilih "
                "(apakah video ini dibuat untuk anak-anak?)")
        if problems:
            raise MetadataInvalidError(
                "missing required YouTube metadata: " + "; ".join(problems),
                user_message="Metadata belum lengkap: " + "; ".join(problems)
                             + ". Lengkapi di Studio lalu jalankan ulang.")
        snippet = {
            "title": title[:100],
            # Budget the description in automation.content (hashtags included);
            # the API limit is 5000, so do not truncate below that here.
            "description": description[:5000],
            "tags": [str(t)[:30] for t in (payload.get("tags") or [])][:40],
            "categoryId": str(payload.get("category_id") or "22"),
        }
        status: dict[str, Any] = {"privacyStatus": privacy}
        # The audience answer is transmitted verbatim only when it was recorded;
        # otherwise we raised above and never let the API default silently.
        status["selfDeclaredMadeForKids"] = bool(made_for_kids)
        return {"snippet": snippet, "status": status}

    def _authorized_headers(self, ctx: PublishContext) -> dict[str, str]:
        token = self.auth.ensure_access_token(str(ctx.account["id"]))
        return {"Authorization": f"Bearer {token}"}

    def publish(self, ctx: PublishContext) -> PublishResult:
        size = _media_size(ctx.media_path)
        metadata = self._metadata(ctx)
        _check_cancel(ctx)
        ctx.progress(3, "Memulai upload resumable (YouTube)…")
        self._log(f"resumable init size={size} title={metadata['snippet']['title']!r}")
        headers = self._authorized_headers(ctx)
        headers.update({"Content-Type": "application/json; charset=UTF-8", "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)})
        init_url = f"{self.UPLOAD_URL}?uploadType=resumable&part=snippet,status"
        init = requests.post(init_url, headers=headers, data=json.dumps(metadata).encode("utf-8"), timeout=60)
        if init.status_code == 401:
            self._force_refresh(str(ctx.account["id"]))
            headers["Authorization"] = f"Bearer {self.auth.ensure_access_token(str(ctx.account['id']))}"
            init = requests.post(init_url, headers=headers, data=json.dumps(metadata).encode("utf-8"), timeout=60)
        if init.status_code >= 400:
            raise self._yt_error(init)
        session_url = init.headers.get("Location") or init.headers.get("location") or ""
        if not session_url:
            raise AutomationError("YouTube resumable session tanpa Location header.", category="UPLOAD_FAILED", user_message="YouTube tidak memberi sesi upload. Coba lagi.")
        uploaded = 0
        chunk = 8 * 1024 * 1024
        attempts = 0
        auth_attempts = 0
        with open(ctx.media_path, "rb") as fh:
            while uploaded < size:
                _check_cancel(ctx)
                fh.seek(uploaded)
                data = fh.read(min(chunk, size - uploaded))
                if not data:
                    break
                end = uploaded + len(data) - 1
                headers_up = {"Content-Length": str(len(data)), "Content-Range": f"bytes {uploaded}-{end}/{size}"}
                try:
                    resp = requests.put(session_url, headers=headers_up, data=data, timeout=300)
                except (requests.ConnectionError, requests.Timeout) as exc:
                    attempts += 1
                    if attempts > 5:
                        raise RetryableError(f"Upload terputus: {exc}", user_message="Koneksi upload terputus. Akan dicoba lagi.")
                    time.sleep(2 * attempts)
                    continue
                if resp.status_code == 308:
                    attempts = 0
                    auth_attempts = 0
                    # A 308 carries the server's real persisted progress in its
                    # Range header. Honor it: assuming the whole chunk landed
                    # skips still-missing bytes and silently truncates the upload.
                    next_byte = _parse_resumable_range(resp.headers.get("Range"))
                    if next_byte is None or next_byte <= uploaded:
                        next_byte = end + 1
                    uploaded = min(next_byte, size)
                    ctx.progress(min(92, int(uploaded * 85 / max(1, size)) + 5), "Mengupload video…")
                    continue
                if resp.status_code in (200, 201):
                    try:
                        video_id = str((resp.json() or {}).get("id") or "")
                    except Exception:
                        video_id = ""
                    if not video_id:
                        raise VerificationFailedError("Upload selesai tetapi tanpa video ID.", user_message="YouTube tidak mengembalikan ID video.")
                    ctx.progress(95, "Upload selesai — memverifikasi…")
                    self._log(f"upload done video_id={video_id}")
                    return PublishResult(ok=True, external_id=video_id, external_url=f"https://youtu.be/{video_id}", message="upload accepted")
                if resp.status_code == 401:
                    # A persistently rejected token must fail the job instead of
                    # re-sending the same chunk forever (which previously hung the
                    # job with no bound on retries).
                    auth_attempts += 1
                    if auth_attempts > 2:
                        raise AuthRequiredError(
                            "HTTP 401 selama upload resumable.",
                            user_message="Sesi YouTube ditolak saat upload. Login ulang akun YouTube lalu jalankan ulang.",
                        )
                    self._force_refresh(str(ctx.account["id"]))
                    headers["Authorization"] = f"Bearer {self.auth.ensure_access_token(str(ctx.account['id']))}"
                    continue
                if resp.status_code >= 500:
                    attempts += 1
                    if attempts <= 5:
                        time.sleep(2 * attempts)
                        continue
                raise self._yt_error(resp)
        raise VerificationFailedError("Upload berakhir tanpa respons sukses.", user_message="Upload YouTube gagal tanpa konfirmasi.")

    def _force_refresh(self, account_id: str) -> None:
        try:
            store = self.auth.secrets
            key = store.account_key(account_id)
            token = store.get_json(key)
            token["expires_at"] = 0
            store.set_json(key, token)
        except Exception:
            pass

    def _yt_error(self, resp: requests.Response) -> AutomationError:
        msg = str(resp.text or "")[:200]
        reason = ""
        try:
            err = (resp.json() or {}).get("error") or {}
            msg = str(err.get("message") or msg)
            for entry in (err.get("errors") or []):
                r = str(entry.get("reason") or "")
                if r:
                    reason = r
                    break
        except Exception:
            pass
        if resp.status_code == 401:
            return AuthRequiredError(f"HTTP 401 {redact(msg)}", user_message="Sesi YouTube kedaluwarsa.")
        if resp.status_code == 403 and "quota" in reason.lower():
            return QuotaExceededError(f"quota: {redact(msg)}", user_message="Kuota YouTube API harian habis. Coba lagi nanti atau naikkan kuota project Google Cloud.")
        if resp.status_code == 403:
            return PermissionDeniedError(f"403 {reason} {redact(msg)}", user_message="Izin ditolak. Periksa audit/verification app Google Cloud — upload butuh app yang lolos audit.")
        if "uploadLimitExceeded" in reason:
            return AutomationError("uploadLimitExceeded", category="QUOTA_EXCEEDED", user_message="Batas upload YouTube tercapai.")
        if resp.status_code >= 500:
            return RetryableError(f"HTTP {resp.status_code}", user_message="Server YouTube bermasalah.")
        return AutomationError(f"HTTP {resp.status_code} {reason}: {redact(msg)}", category="UPLOAD_FAILED", user_message=f"YouTube menolak upload: {msg[:160]}")

    def verify(self, ctx: PublishContext, external_id: str) -> VerifyResult:
        headers = self._authorized_headers(ctx)
        resp = requests.get(self.API_URL, params={"part": "status,snippet", "id": external_id}, headers=headers, timeout=45)
        if resp.status_code >= 400:
            raise VerificationFailedError(f"verify HTTP {resp.status_code}", user_message="Verifikasi YouTube gagal dihubungi.")
        items = (resp.json() or {}).get("items") or []
        if not items:
            return VerifyResult(ok=False, external_id=external_id, verification_status="not_found", message="Video belum muncul di API (proses biasanya sebentar).")
        status = (items[0].get("status") or {})
        upload_status = str(status.get("uploadStatus") or "")
        privacy = str(status.get("privacyStatus") or "")
        if upload_status == "rejected":
            return VerifyResult(ok=False, external_id=external_id, verification_status="rejected", message="YouTube menolak video (policy/processing).")
        scheduled = bool(status.get("publishAt"))
        return VerifyResult(ok=True, external_id=external_id, external_url=f"https://youtu.be/{external_id}", verification_status=f"uploadStatus={upload_status or 'unknown'} privacy={privacy}", status="SCHEDULED" if scheduled and privacy == "private" else "PUBLISHED")


# --------------------------------------------------------------------------- #
# Facebook — Meta Graph API page video publishing (resumable start/offset/finish)
# --------------------------------------------------------------------------- #

class FacebookPublisher(BasePublisher):
    platform = "facebook"

    def _page_id(self, ctx: PublishContext) -> str:
        page_id = str(ctx.account.get("external_id") or "")
        if not page_id:
            raise PermissionDeniedError("Page ID tidak ada.", user_message="Pilih Page dulu di Automation → Accounts.")
        return page_id

    def publish(self, ctx: PublishContext) -> PublishResult:
        size = _media_size(ctx.media_path)
        token = self.auth.ensure_access_token(str(ctx.account["id"]))
        page_id = self._page_id(ctx)
        description = str(ctx.payload.get("description") or ctx.payload.get("caption") or "")[:4000]
        title = str(ctx.payload.get("title") or "")[:255]
        _check_cancel(ctx)
        ctx.progress(4, "Mengupload video ke Page (Meta)…")
        self._log(f"page video upload page={page_id} size={size}")
        start = requests.post(f"{GRAPH}/{page_id}/videos", data={"upload_phase": "start", "file_size": str(size), "access_token": token}, timeout=60)
        if start.status_code >= 400:
            return self._multipart_publish(ctx, page_id, token, title, description)
        try:
            start_data = start.json() or {}
        except Exception:
            return self._multipart_publish(ctx, page_id, token, title, description)
        video_id = str(start_data.get("video_id") or "")
        if not video_id:
            return self._multipart_publish(ctx, page_id, token, title, description)
        offset = int(start_data.get("start_offset") or 0)
        with open(ctx.media_path, "rb") as fh:
            while offset < size:
                _check_cancel(ctx)
                fh.seek(offset)
                chunk = fh.read(min(64 * 1024 * 1024, size - offset))
                if not chunk:
                    break
                try:
                    part = requests.post(f"{GRAPH}/{page_id}/videos", params={"upload_phase": "offset", "video_id": video_id, "start_offset": str(offset), "access_token": token}, headers={"Content-Type": "application/octet-stream"}, data=chunk, timeout=300)
                except (requests.ConnectionError, requests.Timeout) as exc:
                    raise RetryableError(f"Offset upload terputus: {exc}", user_message="Koneksi upload Meta terputus. Akan dicoba lagi.")
                if part.status_code >= 400:
                    raise _graph_error(part, "offset upload failed")
                try:
                    pdata = part.json() or {}
                    offset = int(pdata.get("start_offset") or (offset + len(chunk)))
                except Exception:
                    offset += len(chunk)
                ctx.progress(min(90, int(offset * 85 / max(1, size)) + 6), "Mengupload video (Meta)…")
        finish = requests.post(f"{GRAPH}/{page_id}/videos", data={"upload_phase": "finish", "video_id": video_id, "title": title, "description": description, "access_token": token}, timeout=90)
        if finish.status_code >= 400:
            raise _graph_error(finish, "finish upload failed")
        try:
            post_id = str((finish.json() or {}).get("id") or "") or video_id
        except Exception:
            post_id = video_id
        ctx.progress(96, "Upload selesai — memverifikasi…")
        self._log(f"page video uploaded video_id={video_id} post_id={post_id}")
        return PublishResult(ok=True, external_id=post_id or video_id, message="graph upload ok")

    def _multipart_publish(self, ctx: PublishContext, page_id: str, token: str, title: str, description: str) -> PublishResult:
        self._log("resumable start failed — falling back to multipart upload")
        _check_cancel(ctx)
        with open(ctx.media_path, "rb") as fh:
            try:
                resp = requests.post(f"{GRAPH}/{page_id}/videos", data={"title": title, "description": description, "access_token": token}, files={"source": (Path(ctx.media_path).name, fh, "video/mp4")}, timeout=600)
            except (requests.ConnectionError, requests.Timeout) as exc:
                raise RetryableError(f"Multipart terputus: {exc}", user_message="Koneksi upload Meta terputus. Akan dicoba lagi.")
        if resp.status_code >= 400:
            raise _graph_error(resp, "multipart video upload failed")
        try:
            post_id = str((resp.json() or {}).get("id") or "")
        except Exception:
            post_id = ""
        if not post_id:
            raise VerificationFailedError("Meta tidak mengembalikan ID video.", user_message="Upload Meta gagal dikonfirmasi.")
        ctx.progress(96, "Upload selesai — memverifikasi…")
        return PublishResult(ok=True, external_id=post_id, message="multipart upload ok")

    def verify(self, ctx: PublishContext, external_id: str) -> VerifyResult:
        token = self.auth.ensure_access_token(str(ctx.account["id"]))
        try:
            resp = requests.get(f"{GRAPH}/{external_id}", params={"fields": "id,permalink_link,title", "access_token": token}, timeout=45)
        except requests.RequestException as exc:
            raise VerificationFailedError(str(exc), user_message="Verifikasi Meta gagal dihubungi.")
        if resp.status_code < 400:
            try:
                data = resp.json() or {}
            except Exception:
                data = {}
            return VerifyResult(ok=True, external_id=str(data.get("id") or external_id), external_url=str(data.get("permalink_link") or ""), verification_status="verified")
        return VerifyResult(ok=True, external_id=external_id, verification_status="accepted_post_upload_ok", message="Upload diterima; objek tersembunyi dari pembacaan API.")


# --------------------------------------------------------------------------- #
# TikTok — Content Posting API Direct Post (current flow, spec §6.3/§139)
# --------------------------------------------------------------------------- #

#: Values returned by ``/v2/post/publish/status/fetch/`` (official "Get Post
#: Status"). The two flows finish on *different* strings: Direct Post ends as
#: ``PUBLISH_COMPLETE``, while the inbox/upload flow ends as
#: ``SEND_TO_USER_INBOX`` ("a notification has been sent to creator's inbox").
#: ``SEND_TO_USER`` is kept because older responses/integrations used it.
#: Missing ``SEND_TO_USER_INBOX`` made the inbox flow poll until its budget ran
#: out and then report a failure even though TikTok had delivered the draft.
TIKTOK_STATUS_POSTED = ("PUBLISH_COMPLETE", "PUBLISHED")
TIKTOK_STATUS_INBOX_READY = ("SEND_TO_USER", "SEND_TO_USER_INBOX")
TIKTOK_STATUS_FAILED = ("FAILED", "SEND_TO_USER_FAILED")
#: Any of these ends the status poll.
TIKTOK_STATUS_DONE = TIKTOK_STATUS_POSTED + TIKTOK_STATUS_INBOX_READY + TIKTOK_STATUS_FAILED
#: Still working: keep polling instead of reporting an outcome.
TIKTOK_STATUS_PENDING = ("PROCESSING_UPLOAD", "PROCESSING_DOWNLOAD")
#: Inbox drafts can take longer than the Direct Post budget to be announced.
TIKTOK_INBOX_POLL_ROUNDS = 30


class TikTokPublisher(BasePublisher):
    platform = "tiktok"
    INIT = "https://open.tiktokapis.com/v2/post/publish/video/init/"
    INBOX_INIT = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
    CREATOR = "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"
    STATUS = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"

    def __init__(self, app: Any, auth: AuthManager) -> None:
        super().__init__(app, auth)
        self._last_mode = ""

    def _mode(self, ctx: PublishContext) -> str:
        """TikTok path for this job: official inbox draft or direct post.

        Per-job ``extra.tiktok_mode`` wins; otherwise ``aut_tiktok_mode`` from
        settings is used (default "inbox" = safest draft-first flow).
        """
        extra = ctx.payload.get("extra") or {}
        mode = str(extra.get("tiktok_mode") or "").strip().lower()
        if mode not in ("inbox", "direct"):
            try:
                mode = str(self.app.config.get("aut_tiktok_mode") or "inbox").strip().lower()
            except Exception:
                mode = "inbox"
        return mode if mode in ("inbox", "direct") else "inbox"

    @property
    def verify_unknown_is_action(self) -> bool:
        """Inbox drafts must never be auto-retried when the result is unclear."""
        return self._last_mode == "inbox"

    def _headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}

    def _creator_info(self, token: str) -> dict[str, Any]:
        resp = requests.post(self.CREATOR, headers=self._headers(token), json={}, timeout=45)
        if resp.status_code >= 400:
            raise _graph_error(resp, "creator info failed")
        try:
            body = resp.json() or {}
        except Exception:
            body = {}
        err = body.get("error") or {}
        if str(err.get("code") or "ok") not in ("ok", "0"):
            raise PermissionDeniedError(f"creator_info error={err.get('code')} {err.get('message')}", user_message="TikTok menolak: pastikan app disetujui scope video.publish.")
        return body.get("data") or {}

    def publish(self, ctx: PublishContext) -> PublishResult:
        mode = self._mode(ctx)
        self._last_mode = mode
        if mode == "inbox":
            return self._publish_draft(ctx)
        return self._publish_direct(ctx)

    def _publish_direct(self, ctx: PublishContext) -> PublishResult:
        size = _media_size(ctx.media_path)
        token = self.auth.ensure_access_token(str(ctx.account["id"]))
        creator = self._creator_info(token)
        options = list(creator.get("privacy_level_options") or [])
        privacy = str(ctx.payload.get("privacy") or "")
        alias = {"public": "PUBLIC_TO_EVERYONE", "private": "SELF_ONLY", "friends": "MUTUAL_FOLLOW_FRIENDS", "unlisted": "SELF_ONLY"}
        privacy = alias.get(privacy, privacy)
        if not privacy or (options and privacy not in options):
            privacy = options[0] if options else "PUBLIC_TO_EVERYONE"
        # No placeholder title: TikTok accepts an empty title, but a fabricated
        # one would hide missing metadata from the user (spec §8.4).
        title = str(ctx.payload.get("title") or ctx.payload.get("caption") or "")[:2200]
        extra = ctx.payload.get("extra") or {}
        post_info = {"title": title, "privacy_level": privacy, "disable_comment": bool(extra.get("disable_comment", False)), "disable_duet": bool(extra.get("disable_duet", False)), "disable_stitch": bool(extra.get("disable_stitch", False)), "video_cover_timestamp_ms": int(extra.get("cover_timestamp_ms", 1000))}
        _check_cancel(ctx)
        ctx.progress(4, "Memulai Direct Post (TikTok)…")
        chunk_size, total_chunk_count = tiktok_chunk_plan(size)
        init = requests.post(self.INIT, headers=self._headers(token), json={"post_info": post_info, "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk_size, "total_chunk_count": total_chunk_count}}, timeout=60)
        if init.status_code >= 400:
            raise _graph_error(init, "tiktok init failed")
        try:
            body = init.json() or {}
        except Exception:
            body = {}
        err = body.get("error") or {}
        code = str(err.get("code") or "ok")
        if code not in ("ok", "0"):
            msg = str(err.get("message") or code)
            lowered = msg.lower()
            if "scope" in lowered or "audit" in lowered or "permission" in lowered:
                raise NeedsActionError(f"tiktok init error={code} {msg}", user_message="TikTok: client belum lolos audit video.publish. Konten client belum diaudit hanya tampil private — selesaikan audit TikTok dulu.")
            raise AutomationError(f"tiktok init error={code} {msg}", category="PROCESSING_FAILED", user_message=f"TikTok menolak: {msg[:180]}")
        data = body.get("data") or {}
        publish_id = str(data.get("publish_id") or "")
        upload_url = str(data.get("upload_url") or "")
        if not publish_id or not upload_url:
            raise AutomationError("tiktok init tanpa publish_id/upload_url", category="PROCESSING_FAILED", user_message="TikTok tidak memberi sesi upload.")
        ctx.progress(8, "Mengupload video (TikTok)…")
        self._tiktok_upload(upload_url, size, ctx, chunk_size, total_chunk_count)
        ctx.progress(80, "Menunggu pemrosesan TikTok…")
        status = self._poll_status(token, publish_id, ctx)
        if status in TIKTOK_STATUS_POSTED + TIKTOK_STATUS_INBOX_READY:
            ctx.progress(97, "Terverifikasi…")
            self._log(f"tiktok published publish_id={publish_id} status={status}")
            return PublishResult(ok=True, external_id=publish_id, verification_status=status)
        if status in TIKTOK_STATUS_FAILED:
            raise AutomationError(f"tiktok status={status}", category="PROCESSING_FAILED", user_message="TikTok melaporkan publish gagal. Coba lagi.")
        raise VerificationFailedError(f"tiktok status={status}", user_message="TikTok masih memproses. Status akan diverifikasi ulang.")

    def _publish_draft(self, ctx: PublishContext) -> PublishResult:
        """Official Upload (inbox) flow: send a draft the user finishes in the app.

        Uses the ``video.upload`` scope. Chopster does NOT publish the post, so
        the job is reported as ACTION_REQUIRED (DRAFT_READY) - never PUBLISHED.
        """
        size = _media_size(ctx.media_path)
        token = self.auth.ensure_access_token(str(ctx.account["id"]))
        _check_cancel(ctx)
        ctx.progress(4, "Menyiapkan draft TikTok (inbox)...")
        chunk_size, total_chunk_count = tiktok_chunk_plan(size)
        init = requests.post(
            self.INBOX_INIT,
            headers=self._headers(token),
            json={"source_info": {"source": "FILE_UPLOAD", "video_size": size,
                                  "chunk_size": chunk_size,
                                  "total_chunk_count": total_chunk_count}},
            timeout=60,
        )
        if init.status_code >= 400:
            raise _graph_error(init, "tiktok inbox init failed")
        try:
            body = init.json() or {}
        except Exception:
            body = {}
        err = body.get("error") or {}
        code = str(err.get("code") or "ok")
        if code not in ("ok", "0"):
            msg = str(err.get("message") or code)
            lowered = msg.lower()
            if "scope" in lowered or "audit" in lowered or "permission" in lowered:
                raise NeedsActionError(
                    f"tiktok inbox init error={code} {msg}",
                    user_message=("TikTok belum mengizinkan scope video.upload untuk app ini. "
                                  "Minta approval scope video.upload, atau ubah Settings > Automation > "
                                  "TikTok publish path ke 'Direct Post'."),
                )
            raise AutomationError(
                f"tiktok inbox init error={code} {msg}",
                category="PROCESSING_FAILED",
                user_message=f"TikTok menolak: {msg[:180]}",
            )
        data = body.get("data") or {}
        publish_id = str(data.get("publish_id") or "")
        upload_url = str(data.get("upload_url") or "")
        if not publish_id or not upload_url:
            raise AutomationError(
                "tiktok inbox init tanpa publish_id/upload_url",
                category="PROCESSING_FAILED",
                user_message="TikTok tidak memberi sesi upload draft.",
            )
        ctx.progress(8, "Mengupload video ke inbox TikTok...")
        self._tiktok_upload(upload_url, size, ctx, chunk_size, total_chunk_count)
        ctx.progress(88, "Menunggu TikTok menyiapkan draft...")
        status = self._poll_status(token, publish_id, ctx, attempts=TIKTOK_INBOX_POLL_ROUNDS)
        if status in TIKTOK_STATUS_INBOX_READY + TIKTOK_STATUS_POSTED:
            self._log(f"tiktok draft ready publish_id={publish_id} status={status}")
            return PublishResult(
                ok=True,
                status="ACTION_REQUIRED",
                external_id=publish_id,
                verification_status="DRAFT_READY",
                message="Draft siap - publish manual di aplikasi TikTok.",
            )
        if status in TIKTOK_STATUS_FAILED:
            raise AutomationError(
                f"tiktok inbox status={status}",
                category="PROCESSING_FAILED",
                user_message="TikTok gagal menyiapkan draft. Coba lagi.",
            )
        # Every chunk was accepted by TikTok, but the status endpoint has not
        # reported a terminal state yet - so the draft may well have arrived.
        # Reporting FAILED here is what made a delivered draft look like an
        # error, and the old VerificationFailedError asked for a retry that
        # could create a second draft. Hand it to the user instead.
        self._log(f"tiktok draft unresolved publish_id={publish_id} status={status or 'none'}")
        raise NeedsActionError(
            f"tiktok inbox status={status}",
            reason="verify_unknown",
            user_message=("Upload ke TikTok selesai, tapi TikTok belum melaporkan "
                          f"status draft ({status or 'tanpa status'}). Cek Inbox TikTok dulu - "
                          "kalau draft belum muncul, jalankan ulang job ini."),
        )

    def _tiktok_upload(self, upload_url: str, size: int, ctx: PublishContext,
                       chunk_size: int | None = None,
                       total_chunk_count: int | None = None) -> None:
        """PUT the media in the exact chunks declared during init.

        TikTok validates that the uploaded ``Content-Range`` boundaries match the
        ``chunk_size``/``total_chunk_count`` sent to the init endpoint, so the
        boundaries are computed once by :func:`tiktok_chunk_plan` and reused here
        (the final chunk absorbs the remainder and may exceed ``chunk_size``).
        """
        if not chunk_size or not total_chunk_count:
            chunk_size, total_chunk_count = tiktok_chunk_plan(size)
        attempts = 0
        offset = 0
        total = max(1, int(total_chunk_count))
        last_index = total - 1
        index = 0
        # While-loop instead of ``for index in range(total)``: a transient
        # failure must retry the SAME chunk. With a for-loop the ``continue``
        # below advanced the index while ``offset`` stayed put, so the retry
        # re-sent the range under the next declared chunk and the loop could
        # run out of indices with bytes still missing.
        with open(ctx.media_path, "rb") as fh:
            while offset < size and index < total:
                _check_cancel(ctx)
                want = int(chunk_size)
                if index == last_index:
                    want = size - offset            # trailing chunk keeps the remainder
                fh.seek(offset)
                data = fh.read(max(1, min(want, size - offset)))
                if not data:
                    break
                end = offset + len(data) - 1
                try:
                    resp = requests.put(upload_url, headers={"Content-Type": "video/mp4", "Content-Length": str(len(data)), "Content-Range": f"bytes {offset}-{end}/{size}"}, data=data, timeout=600)
                except (requests.ConnectionError, requests.Timeout) as exc:
                    attempts += 1
                    if attempts > 5:
                        raise RetryableError(str(exc), user_message="Koneksi upload TikTok terputus.")
                    time.sleep(2 * attempts)
                    continue
                if resp.status_code in (200, 201, 206):
                    offset = end + 1
                    index += 1
                    attempts = 0
                    ctx.progress(min(78, int(offset * 72 / max(1, size)) + 8), "Mengupload video (TikTok)…")
                    continue
                if resp.status_code >= 500:
                    attempts += 1
                    if attempts <= 5:
                        time.sleep(2 * attempts)
                        continue
                raise AutomationError(f"tiktok upload HTTP {resp.status_code}: {redact(str(resp.text)[:200])}", category="UPLOAD_FAILED", user_message="Upload ke server TikTok gagal.")
        if offset < size:
            raise RetryableError("upload incomplete", user_message="Upload TikTok belum lengkap.")

    def _poll_status(self, token: str, publish_id: str, ctx: PublishContext,
                     attempts: int = 12) -> str:
        """Poll the status endpoint until it reports a terminal value.

        ``attempts`` keeps the historical 12 rounds for Direct Post; the inbox
        flow passes a larger budget (:data:`TIKTOK_INBOX_POLL_ROUNDS`) because
        the draft notification can take longer than a minute to be announced.
        """
        last = ""
        for _ in range(max(1, int(attempts))):
            _check_cancel(ctx)
            resp = requests.post(self.STATUS, headers=self._headers(token), json={"publish_id": publish_id}, timeout=45)
            if resp.status_code >= 400:
                raise _graph_error(resp, "status fetch failed")
            try:
                data = (resp.json() or {}).get("data") or {}
            except Exception:
                data = {}
            last = str(data.get("status") or data.get("publish_status") or "")
            if last in TIKTOK_STATUS_DONE:
                return last
            time.sleep(5)
        self._log(f"tiktok status timeout publish_id={publish_id} last={last or 'none'}")
        return last or "PROCESSING"

    def verify(self, ctx: PublishContext, external_id: str) -> VerifyResult:
        token = self.auth.ensure_access_token(str(ctx.account["id"]))
        resp = requests.post(self.STATUS, headers=self._headers(token), json={"publish_id": external_id}, timeout=45)
        if resp.status_code >= 400:
            raise VerificationFailedError(f"HTTP {resp.status_code}", user_message="Verifikasi TikTok gagal dihubungi.")
        try:
            data = (resp.json() or {}).get("data") or {}
        except Exception:
            data = {}
        status = str(data.get("status") or data.get("publish_status") or "")
        if status in TIKTOK_STATUS_POSTED + TIKTOK_STATUS_INBOX_READY:
            if self._mode(ctx) == "inbox":
                # Official inbox upload: the draft is ready; the user publishes it
                # in the app, so the job must NOT be recorded as PUBLISHED.
                return VerifyResult(ok=True, status="ACTION_REQUIRED", external_id=external_id,
                                    verification_status="DRAFT_READY",
                                    message="Draft siap - publish manual di aplikasi TikTok.")
            return VerifyResult(ok=True, external_id=external_id, verification_status=status, external_url=str(data.get("post_url") or ""))
        if status in TIKTOK_STATUS_FAILED:
            return VerifyResult(ok=False, external_id=external_id, verification_status=status, message="TikTok publish gagal.")
        return VerifyResult(ok=False, external_id=external_id, verification_status=status or "PROCESSING", message="Masih diproses TikTok.")


# --------------------------------------------------------------------------- #
# Instagram Official API — resumable container + rupload + media_publish
# --------------------------------------------------------------------------- #

class InstagramApiPublisher(BasePublisher):
    platform = "instagram"
    RUPLOAD = "https://rupload.facebook.com/ig-api-upload/{container}"

    def publish(self, ctx: PublishContext) -> PublishResult:
        size = _media_size(ctx.media_path)
        token = self.auth.ensure_access_token(str(ctx.account["id"]))
        ig_id = str(ctx.account.get("external_id") or "")
        if not ig_id:
            raise PermissionDeniedError("IG user id kosong.", user_message="Akun Instagram tidak valid.")
        caption = str(ctx.payload.get("caption") or ctx.payload.get("description") or "")[:2200]
        _check_cancel(ctx)
        ctx.progress(4, "Membuat container Reels (Instagram)…")
        create = requests.post(f"{GRAPH}/{ig_id}/media", data={"media_type": "REELS", "upload_type": "resumable", "caption": caption, "access_token": token}, timeout=60)
        if create.status_code >= 400:
            raise _graph_error(create, "ig container create failed")
        try:
            container = str((create.json() or {}).get("id") or "")
        except Exception:
            container = ""
        if not container:
            raise AutomationError("IG container tanpa id", category="PROCESSING_FAILED", user_message="Instagram tidak membuat container.")
        ctx.progress(10, "Mengupload video (Instagram)…")
        self._rupload(container, token, size, ctx)
        ctx.progress(80, "Menunggu container siap…")
        code = self._wait_container(container, token, ctx)
        if code not in ("FINISHED", "PUBLISHED"):
            raise NeedsActionError(f"container status={code}", user_message=f"Instagram container status={code}. Selesaikan Page Publishing Authorization & 2FA pada Page, lalu coba lagi.")
        _check_cancel(ctx)
        pub = requests.post(f"{GRAPH}/{ig_id}/media_publish", data={"creation_id": container, "access_token": token}, timeout=60)
        if pub.status_code >= 400:
            raise _graph_error(pub, "ig media_publish failed")
        try:
            media_id = str((pub.json() or {}).get("id") or "") or container
        except Exception:
            media_id = container
        ctx.progress(97, "Terverifikasi…")
        self._log(f"instagram published media_id={media_id}")
        return PublishResult(ok=True, external_id=media_id, verification_status="published")

    def _rupload(self, container: str, token: str, size: int, ctx: PublishContext) -> None:
        url = self.RUPLOAD.format(container=container)
        attempts = 0
        offset = 0
        with open(ctx.media_path, "rb") as fh:
            while offset < size:
                _check_cancel(ctx)
                fh.seek(offset)
                data = fh.read(min(32 * 1024 * 1024, size - offset))
                if not data:
                    break
                end = offset + len(data) - 1
                try:
                    resp = requests.post(url, headers={"Authorization": f"OAuth {token}", "Content-Type": "video/mp4", "Content-Length": str(len(data)), "Content-Range": f"bytes {offset}-{end}/{size}", "offset": str(offset), "X-Entity-Name": Path(ctx.media_path).name, "X-Entity-Length": str(size), "X-Entity-Type": "video/mp4"}, data=data, timeout=600)
                except (requests.ConnectionError, requests.Timeout) as exc:
                    attempts += 1
                    if attempts > 5:
                        raise RetryableError(str(exc), user_message="Koneksi upload Instagram terputus.")
                    time.sleep(2 * attempts)
                    continue
                if resp.status_code in (200, 201, 202, 206):
                    offset = end + 1
                    attempts = 0
                    ctx.progress(min(78, int(offset * 70 / max(1, size)) + 10), "Mengupload video (Instagram)…")
                    continue
                raise AutomationError(f"ig rupload HTTP {resp.status_code}: {redact(str(resp.text)[:250])}", category="UPLOAD_FAILED", retryable=resp.status_code >= 500, user_message="Upload Instagram ditolak. App kemungkinan butuh Facebook Login for Business/audit — lihat Detail.")

    def _wait_container(self, container: str, token: str, ctx: PublishContext, tries: int = 12) -> str:
        code = ""
        for _ in range(tries):
            _check_cancel(ctx)
            resp = requests.get(f"{GRAPH}/{container}", params={"fields": "status_code", "access_token": token}, timeout=45)
            if resp.status_code >= 400:
                raise _graph_error(resp, "container status failed")
            try:
                code = str((resp.json() or {}).get("status_code") or "")
            except Exception:
                code = ""
            if code in ("FINISHED", "PUBLISHED", "ERROR", "EXPIRED"):
                return code
            time.sleep(10)
        return code or "IN_PROGRESS"

    def verify(self, ctx: PublishContext, external_id: str) -> VerifyResult:
        token = self.auth.ensure_access_token(str(ctx.account["id"]))
        try:
            resp = requests.get(f"{GRAPH}/{external_id}", params={"fields": "id,permalink", "access_token": token}, timeout=45)
        except requests.RequestException as exc:
            raise VerificationFailedError(str(exc), user_message="Verifikasi Instagram gagal.")
        if resp.status_code < 400:
            try:
                data = resp.json() or {}
            except Exception:
                data = {}
            return VerifyResult(ok=True, external_id=str(data.get("id") or external_id), external_url=str(data.get("permalink") or ""), verification_status="verified")
        return VerifyResult(ok=True, external_id=external_id, verification_status="accepted_media_publish_ok", message="media_publish sukses; objek tidak terbaca API.")


# --------------------------------------------------------------------------- #
# Instagram Browser Session — AI Browser Agent (spec §6.4 Mode B / §16+)
# --------------------------------------------------------------------------- #

class InstagramBrowserPublisher(BasePublisher):
    platform = "instagram"

    def publish(self, ctx: PublishContext) -> PublishResult:
        from chopster.automation.browser.agent import publish_via_agent
        from chopster.automation.browser.session import ensure_playwright, session_valid
        ensure_playwright()
        aid = str(ctx.account.get("id") or "")
        if not session_valid("instagram", aid):
            raise AuthRequiredError("Instagram browser session belum login.", reason="login_required", user_message="Buka Automation → Accounts → Instagram → Open Login, login manual di jendela browser Chopster, lalu lanjutkan.")
        self._log("delegating publish to AI browser agent")
        outcome = publish_via_agent(app=self.app, platform="instagram", media_path=ctx.media_path, caption=str(ctx.payload.get("caption") or ""), privacy=str(ctx.payload.get("privacy") or "public"), progress=ctx.progress, cancel_check=ctx.cancel_check, account_id=aid)
        if outcome.get("ok"):
            return PublishResult(ok=True, external_id=str(outcome.get("external_id") or ""), external_url=str(outcome.get("external_url") or ""), verification_status=str(outcome.get("verification") or "browser_confirmed"))
        category = str(outcome.get("category") or "UNKNOWN_RESULT")
        if category == "USER_ACTION_REQUIRED":
            raise NeedsActionError(str(outcome.get("error") or ""), reason=str(outcome.get("reason") or ""), user_message=str(outcome.get("message") or "Bantuan pengguna diperlukan."))
        raise AutomationError(str(outcome.get("error") or "browser publish failed"), category=category, retryable=bool(outcome.get("retryable")), user_message=str(outcome.get("message") or "Browser publish gagal."))


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

class BrowserPublisher(BasePublisher):
    """Generic Browser Session publisher for platforms without an API path.

    Drives the platform UI through the existing AI browser agent (which already
    knows each platform's upload URL and never bypasses a security challenge).
    Used only for accounts whose ``auth_mode`` is ``browser``.
    """

    platform = ""

    def __init__(self, app: Any, auth: AuthManager, platform: str) -> None:
        super().__init__(app, auth)
        self.platform = str(platform or "")

    def publish(self, ctx: PublishContext) -> PublishResult:
        from chopster.automation.browser.agent import publish_via_agent
        from chopster.automation.browser.session import ensure_playwright, session_valid
        ensure_playwright()
        aid = str(ctx.account.get("id") or "")
        if not session_valid(self.platform, aid):
            raise AuthRequiredError(
                f"{self.platform} browser session belum login.",
                reason="login_required",
                user_message=("Buka Automation > Accounts > + Sambungkan Akun > Browser Session, "
                              "login manual di jendela browser Chopster, lalu lanjutkan."))
        self._log(f"delegating publish to AI browser agent ({self.platform})")
        outcome = publish_via_agent(
            app=self.app, platform=self.platform, media_path=ctx.media_path,
            caption=str(ctx.payload.get("caption") or ""),
            privacy=str(ctx.payload.get("privacy") or "public"),
            progress=ctx.progress, cancel_check=ctx.cancel_check, account_id=aid,
        )
        if outcome.get("ok"):
            return PublishResult(
                ok=True, external_id=str(outcome.get("external_id") or ""),
                external_url=str(outcome.get("external_url") or ""),
                verification_status=str(outcome.get("verification") or "browser_confirmed"))
        category = str(outcome.get("category") or "UNKNOWN_RESULT")
        if category == "USER_ACTION_REQUIRED":
            raise NeedsActionError(str(outcome.get("error") or ""),
                                   reason=str(outcome.get("reason") or ""),
                                   user_message=str(outcome.get("message") or "Bantuan pengguna diperlukan."))
        raise AutomationError(str(outcome.get("error") or "browser publish failed"),
                              category=category, retryable=bool(outcome.get("retryable")),
                              user_message=str(outcome.get("message") or "Browser publish gagal."))


def get_publisher(app: Any, auth: AuthManager, platform: str, auth_mode: str = "oauth") -> BasePublisher:
    platform = str(platform or "").lower()
    if auth_mode == "browser" and platform != "instagram":
        # Any platform connected through Browser Session uses the generic agent.
        return BrowserPublisher(app, auth, platform)
    if platform == "youtube":
        return YouTubePublisher(app, auth)
    if platform == "facebook":
        return FacebookPublisher(app, auth)
    if platform == "tiktok":
        return TikTokPublisher(app, auth)
    if platform == "instagram":
        if auth_mode == "browser":
            return InstagramBrowserPublisher(app, auth)
        return InstagramApiPublisher(app, auth)
    raise UnsupportedOperationError(f"Platform {platform} tidak didukung.", user_message="Platform tidak didukung.")
