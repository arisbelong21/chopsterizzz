"""Media probe via FFprobe — extracts duration, resolution, codecs, fps."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from chopster.downloader.ffmpeg import find_ffmpeg


def _find_ffprobe() -> str | None:
    import shutil
    from chopster.app.paths import app_dir
    for cand in [app_dir() / "ffprobe.exe", app_dir() / "ffprobe"]:
        if cand.is_file():
            return str(cand)
    # Try alongside ffmpeg
    ff = find_ffmpeg()
    if ff:
        # Gyan build: ffprobe next to ffmpeg
        cand = Path(ff).parent / ("ffprobe.exe" if ff.endswith(".exe") else "ffprobe")
        if cand.exists():
            return str(cand)
    return shutil.which("ffprobe")


@dataclass
class MediaInfo:
    path: str
    duration: float  # seconds
    width: int
    height: int
    fps: float
    vcodec: str
    acodec: str
    has_audio: bool
    has_video: bool
    rotation: int
    bitrate: int


def probe(path: str | Path) -> MediaInfo:
    """Probe once per stable file revision; a changed file naturally gets a new key."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File tidak ditemukan: {p}")
    stat = p.stat()
    canonical = str(p.resolve())
    return _cached_probe(canonical, stat.st_size, stat.st_mtime_ns)


@lru_cache(maxsize=64)
def _cached_probe(path: str, size: int, mtime_ns: int) -> MediaInfo:
    # The size/mtime parameters intentionally participate in the cache key.
    del size, mtime_ns
    return _probe_uncached(Path(path))


def clear_probe_cache() -> None:
    """Clear cached FFprobe metadata (useful after external file replacement)."""
    _cached_probe.cache_clear()


def _probe_uncached(p: Path) -> MediaInfo:
    """Probe file with ffprobe JSON. Raises on failure."""
    if not p.exists():
        raise FileNotFoundError(f"File tidak ditemukan: {p}")
    ffprobe = _find_ffprobe()
    if not ffprobe:
        raise RuntimeError("FFprobe tidak ditemukan. Install FFmpeg.")
    cmd = [
        ffprobe, "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", str(p),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"FFprobe gagal: {result.stderr[:500]}")
    raw_json = result.stdout or ""
    if not raw_json.strip():
        raise RuntimeError(f"FFprobe tidak mengembalikan metadata JSON. stderr: {(result.stderr or "").strip()[:500]}")
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Output FFprobe bukan JSON valid: {raw_json[:500]}") from exc
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    duration = float(fmt.get("duration") or 0)
    # fallback: stream duration
    if not duration:
        for s in streams:
            if s.get("duration"):
                try:
                    duration = float(s["duration"])
                    break
                except Exception:
                    pass
    vstream = next((s for s in streams if s.get("codec_type") == "video"), None)
    astream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    width = int(vstream.get("width") or 0) if vstream else 0
    height = int(vstream.get("height") or 0) if vstream else 0
    # rotation
    rotation = 0
    if vstream:
        tags = vstream.get("tags") or {}
        side_data = vstream.get("side_data_list") or []
        rot = tags.get("rotate")
        if rot is None and side_data:
            rot = side_data[0].get("rotation")
        try:
            rotation = int(rot) if rot is not None else 0
        except (TypeError, ValueError):
            rotation = 0
    fps = 0.0
    if vstream:
        fps_s = vstream.get("avg_frame_rate") or vstream.get("r_frame_rate") or "0/1"
        try:
            num, den = fps_s.split("/")
            fps = float(num) / float(den) if float(den) != 0 else 0
        except Exception:
            fps = 0
    vcodec = (vstream.get("codec_name") or "") if vstream else ""
    acodec = (astream.get("codec_name") or "") if astream else ""
    bitrate = int(fmt.get("bit_rate") or 0)
    return MediaInfo(
        path=str(p),
        duration=duration,
        width=width,
        height=height,
        fps=fps,
        vcodec=vcodec,
        acodec=acodec,
        has_audio=astream is not None,
        has_video=vstream is not None,
        rotation=rotation,
        bitrate=bitrate,
    )


def format_duration(sec: float, end: float | None = None) -> str:
    """Format seconds as MM:SS / H:MM:SS.

    The optional second argument is kept for compatibility with older UI builds
    that passed (start, end); in that case a compact range is returned.
    """
    if end is not None:
        return f"{format_duration(sec)} → {format_duration(end)}"
    sec = max(0, int(sec))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def check_ffmpeg_available() -> tuple[bool, str]:
    ff = find_ffmpeg()
    fp = _find_ffprobe()
    if ff and fp:
        return True, f"FFmpeg: {ff} | FFprobe: {fp}"
    if ff:
        return False, f"FFmpeg ditemukan tetapi FFprobe tidak: {ff}"
    return False, "FFmpeg/FFprobe tidak ditemukan di PATH atau folder aplikasi."
