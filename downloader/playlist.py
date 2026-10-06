"""Playlist range parsing — e.g. '1-10,12,15-20' -> list[int]."""
from __future__ import annotations


def parse_playlist_range(spec: str) -> list[int]:
    """Parse yt-dlp playlist_items string into sorted unique ints.

    Accepts forms like "1-10,12", "1,3,5", "1 - 3 , 7". Returns [] for empty.
    Raises ValueError on invalid tokens.
    """
    s = (spec or "").strip()
    if not s:
        return []
    out: set[int] = set()
    for token in s.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            parts = token.split("-", 1)
            try:
                a = int(parts[0].strip())
                b = int(parts[1].strip())
            except ValueError as exc:
                raise ValueError(f"Rentang tidak valid: {token!r}") from exc
            if a < 1 or b < 1:
                raise ValueError(f"Indeks playlist harus >= 1: {token!r}")
            if a > b:
                a, b = b, a
            out.update(range(a, b + 1))
        else:
            try:
                n = int(token)
            except ValueError as exc:
                raise ValueError(f"Item tidak valid: {token!r}") from exc
            if n < 1:
                raise ValueError(f"Indeks playlist harus >= 1: {token!r}")
            out.add(n)
    return sorted(out)


def format_playlist_range(indices: list[int]) -> str:
    """Compress sorted indices back to range string, e.g. [1,2,3,5,7,8,9] -> '1-3,5,7-9'."""
    if not indices:
        return ""
    sorted_idx = sorted(set(indices))
    ranges: list[str] = []
    start = prev = sorted_idx[0]
    for n in sorted_idx[1:]:
        if n == prev + 1:
            prev = n
        else:
            ranges.append(str(start) if start == prev else f"{start}-{prev}")
            start = prev = n
    ranges.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(ranges)
