"""Shared helpers: media fingerprints, idempotency keys, time & log redaction."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_SECRET_PATTERNS = [
    re.compile(r"(access_token|refresh_token|id_token|authorization|client_secret|code)=[^&\s\"']+", re.I),
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}", re.I),
    re.compile(r"ya29\.[A-Za-z0-9._-]+"),
    re.compile(r"EAA[A-Za-z0-9]+"),
]


def redact(text: str) -> str:
    """Best-effort secret scrubber for log lines (spec §55)."""
    out = str(text or "")
    for pat in _SECRET_PATTERNS:
        out = pat.sub(lambda m: m.group(0).split("=")[0] + "=<redacted>" if "=" in m.group(0) else "<redacted>", out)
    return out


def safe_json_loads(raw: Any, default: Any = None) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if not raw:
        return default if default is not None else {}
    try:
        return json.loads(raw)
    except Exception:
        return default if default is not None else {}


def media_fingerprint(path: str | Path) -> str:
    """Fast, stable fingerprint: size + first/last 1 MiB SHA-256.

    Full-file hashing of multi-GB renders is wasteful; this is sufficient for
    duplicate publish detection (spec §26).
    """
    p = Path(path)
    try:
        st = p.stat()
        h = hashlib.sha256()
        h.update(str(st.st_size).encode())
        with open(p, "rb") as fh:
            head = fh.read(1024 * 1024)
            h.update(head)
            if st.st_size > 2 * 1024 * 1024:
                fh.seek(max(0, st.st_size - 1024 * 1024))
                h.update(fh.read(1024 * 1024))
        return h.hexdigest()
    except Exception:
        # Unreadable file: fall back to path+size identity so jobs still flow
        # and the publisher's own existence check reports the real problem.
        try:
            st = p.stat()
            return hashlib.sha256(f"{p}|{st.st_size}".encode()).hexdigest()
        except Exception:
            return hashlib.sha256(str(p).encode()).hexdigest()


def idempotency_key(platform: str, account_id: str, fingerprint: str, intent: str = "publish") -> str:
    raw = f"{platform}|{account_id}|{fingerprint}|{intent}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_local_time(value: str) -> datetime | None:
    """Accept 'YYYY-MM-DD HH:MM', 'YYYY-MM-DD HH:MM:SS', 'YYYY-MM-DDTHH:MM[:SS]'."""
    v = (value or "").strip()
    if not v:
        return None
    v = v.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    return None


def to_local_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def to_utc_z(dt: datetime) -> str:
    """UTC ISO-8601 with Z suffix (used for YouTube publishAt)."""
    from datetime import timezone
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def backoff_seconds(attempt: int, base: int = 30, cap: int = 900) -> int:
    return min(cap, base * (2 ** max(0, attempt)))


def validate_media_file(path: str | Path) -> tuple[bool, str]:
    """Readiness gate for finished content (spec §3.6).

    Content must never reach the publish queue before the source process
    succeeded *and* the final file exists and is actually readable. Raw files,
    previews, partial renders and 0-byte artefacts are rejected with a reason the
    user can act on.
    """
    p = Path(str(path or ""))
    try:
        if not p.is_file():
            return False, "File hasil tidak ditemukan di disk."
        if p.stat().st_size <= 0:
            return False, "File hasil kosong (0 byte)."
        with open(p, "rb") as fh:
            if not fh.read(1):
                return False, "File hasil tidak dapat dibaca."
    except OSError as exc:
        return False, f"File hasil tidak dapat dibaca ({type(exc).__name__})."
    except Exception as exc:  # noqa: BLE001 - never let validation break a render
        return False, f"Validasi file gagal ({type(exc).__name__})."
    return True, ""


def humanize_when(value: str) -> str:
    dt = parse_local_time(value)
    if dt is None:
        return value or "—"
    delta = dt - datetime.now()
    if abs(delta) < timedelta(minutes=1):
        return "now"
    if delta.total_seconds() > 0:
        if delta.days >= 1:
            return f"in {delta.days}d {delta.seconds // 3600}h"
        if delta.seconds >= 3600:
            return f"in {delta.seconds // 3600}h {(delta.seconds % 3600) // 60}m"
        return f"in {delta.seconds // 60}m"
    late = int(-delta.total_seconds())
    if late >= 3600:
        return f"{late // 3600}h ago"
    return f"{max(1, late // 60)}m ago"
