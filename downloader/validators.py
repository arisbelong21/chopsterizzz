"""URL validators & platform detection."""
from __future__ import annotations

import re
from urllib.parse import urlparse

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)


def detect_platform(url: str) -> str:
    h = (urlparse(url).hostname or "").lower()
    if h in ("youtu.be", "youtube.com") or h.endswith(".youtube.com"):
        return "YouTube"
    if h == "tiktok.com" or h.endswith(".tiktok.com"):
        return "TikTok"
    if h in ("facebook.com", "fb.watch") or h.endswith(".facebook.com"):
        return "Facebook"
    if h == "instagram.com" or h.endswith(".instagram.com"):
        return "Instagram"
    if h == "threads.net" or h.endswith(".threads.net"):
        return "Threads"
    return "Lainnya"


def valid_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.hostname)
    except Exception:
        return False


def clean_urls(raw: str) -> list[str]:
    """Extract unique valid URLs from free-form text."""
    found = URL_RE.findall(raw or "")
    cleaned = [u.strip().rstrip(".,;)]}") for u in found]
    return list(dict.fromkeys(u for u in cleaned if valid_url(u)))


def is_playlist_url(url: str) -> bool:
    return "list=" in url or "/playlist" in url
