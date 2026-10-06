"""Advanced local helpers used by the Chopster v6 unified workspace.
All helpers are optional/fallback friendly: the app remains usable without extra AI services.
"""
from __future__ import annotations

import json, math, re, time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

TIME_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?$")
FILLERS = {
    "um", "uh", "hmm", "erm", "eee", "aaa", "anu", "eh", "like", "you know",
    "kayak", "seperti", "jadi", "sebenarnya", "maksudnya", "basically"
}


def parse_timecode(value: str | float | int) -> float:
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    s = str(value).strip()
    if not s:
        return 0.0
    if s.replace('.', '', 1).isdigit():
        return max(0.0, float(s))
    m = TIME_RE.match(s)
    if not m:
        raise ValueError(f"Timecode tidak valid: {value}")
    h = int(m.group(1) or 0); mi = int(m.group(2)); sec = int(m.group(3)); ms = int((m.group(4) or "0").ljust(3, "0"))
    return h * 3600 + mi * 60 + sec + ms / 1000


def format_timecode(seconds: float, long: bool = False) -> str:
    total_ms = max(0, int(round(float(seconds) * 1000)))
    h, rem = divmod(total_ms, 3600000); m, rem = divmod(rem, 60000); s, ms = divmod(rem, 1000)
    if long or h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def remove_filler_segments(transcript):
    """Return a copy with obvious filler-only segments removed."""
    from chopster.clipper.transcript_manager import Transcript, Segment
    out = []
    removed = []
    for seg in transcript.segments:
        text = seg.text.strip()
        low = re.sub(r"[^\w\s]", "", text.lower()).strip()
        words = low.split()
        if words and (len(words) <= 4 and any(w in FILLERS for w in words)):
            removed.append(seg)
            continue
        out.append(Segment(seg.start, seg.end, text, list(seg.words)))
    return Transcript(transcript.language, out, " ".join(s.text for s in out)), removed


def search_transcript(transcript, query: str):
    q = query.strip().lower()
    if not q:
        return []
    return [s for s in transcript.segments if q in s.text.lower()]


def transcript_excerpt(transcript, start: float, end: float) -> str:
    return " ".join(s.text for s in transcript.segments if s.end > start and s.start < end).strip()


def estimate_speakers(transcript, max_speakers: int = 2):
    """Lightweight offline speaker labels. Uses pauses/turn-taking; optional diarization can replace it later."""
    if not transcript or not transcript.segments:
        return []
    speaker = 0; prev_end = transcript.segments[0].start
    out = []
    for i, seg in enumerate(transcript.segments):
        gap = max(0.0, seg.start - prev_end)
        # Turn after a meaningful pause or question/answer cadence.
        if i and (gap >= 0.75 or (out and out[-1]["text"].rstrip().endswith("?") and len(seg.text) > 8)):
            speaker = (speaker + 1) % max(1, max_speakers)
        out.append({"start": seg.start, "end": seg.end, "speaker": speaker + 1, "text": seg.text})
        prev_end = seg.end
    return out


def broll_suggestions(text: str, limit: int = 8) -> list[dict]:
    words = re.findall(r"[\wÀ-ÿ]{5,}", text.lower())
    stop = {"adalah", "dengan", "untuk", "yang", "dari", "kalau", "karena", "sebagai", "mereka", "tentang", "this", "that", "with", "from", "have", "will", "there"}
    freq = {}
    for w in words:
        if w not in stop: freq[w] = freq.get(w, 0) + 1
    return [{"keyword": k, "suggestion": f"B-roll visual terkait: {k}", "search_query": k} for k, _ in sorted(freq.items(), key=lambda x: (-x[1], x[0]))[:limit]]


@dataclass
class FocusPoint:
    t: float
    x: float
    y: float = 0.5
    source: str = "center"
    confidence: float = 0.5
    # Normalized bounds of the currently selected face/person. These are used
    # by the compositor to keep the detected person inside the output crop for
    # ANY aspect ratio, not just 9:16.
    subject_left: float | None = None
    subject_right: float | None = None
    subject_top: float | None = None
    subject_bottom: float | None = None
    group_left: float | None = None
    group_right: float | None = None
    group_top: float | None = None
    group_bottom: float | None = None
    target_id: str | None = None
    shot: str = "solo"
    reason: str = ""


def center_focus(duration: float, step: float = 1.0) -> list[FocusPoint]:
    return [FocusPoint(t=t, x=0.5, y=0.5, source="center") for t in _frange(0, duration, step)]


def _frange(start, stop, step):
    t = start
    while t < stop + 1e-6:
        yield t
        t += step


def save_json(path: str | Path, data) -> Path:
    p = Path(path); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"); return p


def load_json(path: str | Path, default=None):
    p = Path(path)
    try: return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default
    except Exception: return default
