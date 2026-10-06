"""Scene detection via FFmpeg with cancellation/progress support."""
from __future__ import annotations

import re
import os
import subprocess
from pathlib import Path
from typing import Callable

from chopster.downloader.ffmpeg import find_ffmpeg

SCENE_RE = re.compile(r"pts_time:([0-9.]+)")


def detect_scenes(
    path: str | Path,
    threshold: float = 0.3,
    min_duration: float = 8,
    max_duration: float = 90,
    progress_cb: Callable[[int, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> list[tuple[float, float]]:
    """Detect scene boundaries using ffmpeg. Safe for a background worker."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(str(p))
    ff = find_ffmpeg()
    if not ff:
        raise RuntimeError("FFmpeg tidak ditemukan")

    from chopster.clipper.media_probe import probe
    meta = probe(p)
    duration = max(0.0, float(meta.duration))
    if duration <= 0:
        return []

    th = max(0.05, min(0.9, float(threshold)))
    if progress_cb:
        progress_cb(2, f"Scene Detect: menganalisis {duration:.0f} detik video…")

    cmd = [
        ff, "-hide_banner", "-loglevel", "info", "-i", str(p),
        "-filter:v", f"select='gt(scene,{th})',showinfo",
        "-an", "-f", "null", "-",
    ]
    kw = dict(stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1)
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(cmd, **kw)
    times: list[float] = []
    try:
        assert proc.stderr is not None
        for line in proc.stderr:
            if cancel_check and cancel_check():
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise RuntimeError("Dibatalkan")
            m = SCENE_RE.search(line)
            if m:
                try:
                    t = float(m.group(1))
                    times.append(t)
                    if progress_cb:
                        pct = min(92, max(5, int(t / duration * 90)))
                        progress_cb(pct, f"Scene Detect: menemukan titik potong di {t:0.1f}s")
                except ValueError:
                    pass
        rc = proc.wait(timeout=120)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
        raise RuntimeError("Scene Detect timeout setelah 120 detik")
    finally:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except Exception:
                pass
    if rc != 0 and not times:
        raise RuntimeError("FFmpeg gagal menjalankan Scene Detect")

    cuts = sorted(set(t for t in times if 0 < t < duration))
    filtered: list[float] = []
    last = 0.0
    for t in cuts:
        if t - last >= max(0.5, float(min_duration) - 0.5):
            filtered.append(t)
            last = t

    segments: list[tuple[float, float]] = []
    prev = 0.0
    for c in filtered:
        if c > prev + 1.0:
            segments.append((prev, c))
        prev = c
    if duration - prev >= 1.0:
        segments.append((prev, duration))

    out: list[tuple[float, float]] = []
    for s, e in segments:
        length = e - s
        if length <= max_duration + 1e-6:
            out.append((s, e))
        else:
            cur = s
            while cur < e - 0.5:
                nxt = min(cur + max_duration, e)
                out.append((cur, nxt))
                cur = nxt

    merged: list[tuple[float, float]] = []
    minimum_merge = max(1.0, float(min_duration) * 0.5)
    for s, e in out:
        if merged and (e - s) < minimum_merge:
            ps, _ = merged[-1]
            merged[-1] = (ps, e)
        else:
            merged.append((s, e))
    if progress_cb:
        progress_cb(100, f"Scene Detect selesai — {len(merged)} scene ditemukan")
    return merged
