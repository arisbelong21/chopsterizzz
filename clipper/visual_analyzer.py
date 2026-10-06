"""Visual analyzer utilities for sampled frame understanding."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from chopster.downloader.ffmpeg import find_ffmpeg
from chopster.clipper.media_probe import probe


def sample_frames(path: str | Path, fps: float = 1.0, max_frames: int = 20) -> list[dict]:
    meta = probe(path)
    duration = meta.duration
    if duration <= 0:
        return []
    interval = max(0.5, 1.0 / max(fps, 0.01))
    # Keep visual sampling intentionally sparse for API cost and responsiveness.
    interval = max(interval, 5.0)
    out = []
    t = 0.0
    idx = 0
    while t < duration and idx < max_frames:
        out.append({"time": t, "note": "sampled frame"})
        t += interval
        idx += 1
    return out


def extract_frame_images(path: str | Path, timestamps: list[float], max_width: int = 640) -> tuple[Path, list[Path]]:
    """Extract a few JPEG frames into a temporary folder; caller owns cleanup."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("FFmpeg tidak ditemukan untuk mengambil frame")
    root = Path(tempfile.mkdtemp(prefix="chopster_vision_"))
    outputs: list[Path] = []
    for i, timestamp in enumerate(timestamps):
        out = root / f"frame_{i:02d}.jpg"
        cmd = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-ss", f"{max(0,float(timestamp)):.3f}", "-i", str(path), "-frames:v", "1", "-vf", f"scale='min({int(max_width)},iw)':-2", "-q:v", "7", "-y", str(out)]
        cp = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        if cp.returncode == 0 and out.exists() and out.stat().st_size > 0:
            outputs.append(out)
    return root, outputs


def select_visual_timestamps(path: str | Path, *, scenes: list | None = None, camera_timeline: list | None = None, max_frames: int = 8, duration: float | None = None) -> list[float]:
    """Select representative visual timestamps without decoding the whole video.

    Preference order: scene boundaries, camera/subject changes, then evenly spaced
    coverage. The result is deterministic, bounded, and safe for a one-hour source.
    """
    if duration is None:
        meta = probe(path)
        duration = float(meta.duration or 0.0)
    else:
        duration = float(duration or 0.0)
    if duration <= 0:
        return []
    max_frames = max(2, int(max_frames or 8))
    candidates: list[float] = [0.0, max(0.0, duration - 0.25)]
    for sc in scenes or []:
        try:
            if isinstance(sc, (list, tuple)) and sc:
                candidates.append(float(sc[0]))
                if len(sc) > 1:
                    candidates.append(float(sc[1]))
            elif isinstance(sc, dict):
                candidates.append(float(sc.get("start", 0.0)))
                candidates.append(float(sc.get("end", 0.0)))
        except Exception:
            continue
    prev_key = None
    for row in camera_timeline or []:
        try:
            key = (str(row.get("target_id")), str(row.get("shot")), str(row.get("active_speaker")))
            if key != prev_key:
                candidates.append(float(row.get("t", 0.0)))
                prev_key = key
        except Exception:
            continue
    step = duration / max(1, max_frames - 1)
    candidates.extend(i * step for i in range(max_frames))
    cleaned = sorted({round(max(0.0, min(duration - 0.05, float(t))), 2) for t in candidates})
    if len(cleaned) <= max_frames:
        return cleaned
    # Keep the earliest/latest plus evenly distributed interior samples.
    keep = [cleaned[0]]
    inner = cleaned[1:-1]
    slots = max_frames - 2
    for i in range(slots):
        idx = round(i * (len(inner) - 1) / max(1, slots - 1))
        if inner:
            keep.append(inner[idx])
    keep.append(cleaned[-1])
    return sorted(dict.fromkeys(keep))[:max_frames]
