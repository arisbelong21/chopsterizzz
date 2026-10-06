"""Clip planner — generates clip boundaries for all modes (A-E)."""
from __future__ import annotations

from dataclasses import dataclass

from chopster.clipper.media_probe import format_duration


@dataclass
class ClipPlan:
    index: int  # 1-based
    start: float
    end: float
    title: str = ""

    @property
    def duration(self) -> float:
        return max(0, self.end - self.start)

    def label(self) -> str:
        return f"Clip {self.index:03d}  {format_duration(self.start)} → {format_duration(self.end)}  ({format_duration(self.duration)})"


def plan_fixed_duration(duration: float, clip_secs: float, start_offset: float = 0, end_offset: float | None = None) -> list[ClipPlan]:
    """Mode A — fixed duration sequential tiling."""
    if clip_secs <= 0:
        raise ValueError("Durasi clip harus > 0")
    if duration <= 0:
        return []
    end = end_offset if end_offset is not None else duration
    end = min(end, duration)
    start = max(0, start_offset)
    if start >= end:
        return []
    clips: list[ClipPlan] = []
    idx = 1
    cur = start
    while cur + 0.05 < end:
        nxt = min(cur + clip_secs, end)
        # drop trailing fragment shorter than 1s or less than half clip? configurable; keep if >= 1s
        if nxt - cur < 1.0 and clips:
            break
        clips.append(ClipPlan(index=idx, start=cur, end=nxt))
        idx += 1
        cur = nxt
        if cur >= end - 1e-6:
            break
    return clips


def plan_fixed_overlap(duration: float, clip_secs: float, overlap: float, start_offset: float = 0, end_offset: float | None = None) -> list[ClipPlan]:
    """Mode B — fixed duration with overlap (step = clip_secs - overlap)."""
    if clip_secs <= 0:
        raise ValueError("Durasi clip harus > 0")
    if overlap < 0:
        raise ValueError("Overlap tidak boleh negatif")
    if overlap >= clip_secs:
        raise ValueError("Overlap harus lebih kecil dari durasi clip")
    step = clip_secs - overlap
    if duration <= 0:
        return []
    end = end_offset if end_offset is not None else duration
    end = min(end, duration)
    start = max(0, start_offset)
    if start >= end:
        return []
    clips: list[ClipPlan] = []
    idx = 1
    cur = start
    while cur + 1.0 <= end:
        nxt = cur + clip_secs
        if nxt > end:
            # last partial: if remaining >= 50% of clip, include; otherwise stop
            remaining = end - cur
            if remaining >= clip_secs * 0.5 and remaining >= 3:
                nxt = end
            else:
                break
        clips.append(ClipPlan(index=idx, start=cur, end=min(nxt, end)))
        idx += 1
        cur += step
        if cur >= end - 1e-6:
            break
        if idx > 10000:
            break
    return clips


def plan_fixed_interval(duration: float, clip_secs: float, interval: float, start: float = 0, end: float | None = None) -> list[ClipPlan]:
    """Mode C — clip duration + interval between clip starts."""
    if clip_secs <= 0 or interval <= 0:
        raise ValueError("Durasi dan interval harus > 0")
    if duration <= 0:
        return []
    end_v = end if end is not None else duration
    end_v = min(end_v, duration)
    start = max(0, start)
    if start >= end_v:
        return []
    clips: list[ClipPlan] = []
    idx = 1
    cur = start
    while cur + 0.5 <= end_v:
        nxt = min(cur + clip_secs, end_v)
        if nxt - cur >= 1.0:
            clips.append(ClipPlan(index=idx, start=cur, end=nxt))
            idx += 1
        cur += interval
        if cur >= end_v - 1e-6:
            break
        if idx > 10000:
            break
    return clips


def plan_from_timestamps(duration: float, timestamps: list[tuple[float, float]]) -> list[ClipPlan]:
    """Build clips from explicit (start, end) pairs — used by scene/AI modes."""
    clips: list[ClipPlan] = []
    for i, (s, e) in enumerate(timestamps, 1):
        s = max(0, s)
        e = min(duration, max(s + 0.1, e))
        if e - s >= 0.5:
            clips.append(ClipPlan(index=i, start=s, end=e))
    # re-index
    for idx, c in enumerate(clips, 1):
        c.index = idx
    return clips


def reindex(clips: list[ClipPlan]) -> None:
    for i, c in enumerate(clips, 1):
        c.index = i
