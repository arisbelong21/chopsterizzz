"""Reliable FFmpeg renderer for clips, subtitles, aspect ratios and watermarks."""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from chopster.downloader.ffmpeg import find_ffmpeg
from chopster.clipper.media_probe import probe
from chopster.clipper.camera_director import make_clip_relative
from chopster.clipper.adaptive_framing import decide_framing

ProgressCb = Callable[[int, int, str], None]
ClipCb = Callable[[int, str, str], None]


@dataclass
class ExportResult:
    index: int
    success: bool
    output: str
    error: str = ""


QUALITY_PRESETS = {
    "fast": {"preset": "veryfast", "crf": "23"},
    "balanced": {"preset": "medium", "crf": "20"},
    "high": {"preset": "slow", "crf": "18"},
}

WATERMARK_POSITIONS = {
    "Top left": ("20", "20"),
    "Top right": ("W-w-20", "20"),
    "Bottom left": ("20", "H-h-20"),
    "Bottom right": ("W-w-20", "H-h-20"),
    "Center": ("(W-w)/2", "(H-h)/2"),
}

_ASS_TIME_RE = re.compile(r"^(\d+):(\d{2}):(\d{2})\.(\d{2})$")
_SRT_TIME_RE = re.compile(r"^(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})")
_FFMPEG_FILTER_CACHE: dict[tuple[str, str], bool] = {}


def _ffmpeg() -> str:
    ff = find_ffmpeg()
    if not ff:
        raise RuntimeError("FFmpeg tidak ditemukan. Install FFmpeg atau letakkan ffmpeg.exe di folder aplikasi.")
    return ff


def _ffmpeg_has_filter(ffmpeg: str, name: str) -> bool:
    key = (str(ffmpeg), str(name))
    cached = _FFMPEG_FILTER_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-filters"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8,
        )
        available = proc.returncode == 0 and re.search(
            rf"(?m)^\s*[TSC\.]{{3}}\s+{re.escape(name)}\s+", proc.stdout or ""
        ) is not None
    except Exception:
        available = False
    _FFMPEG_FILTER_CACHE[key] = available
    return available


def _text_watermark_image(text: str, font_size: int) -> Path:
    """Render a lossless RGBA watermark when FFmpeg lacks drawtext."""
    from PIL import Image, ImageDraw, ImageFont

    font_path = _default_font_file()
    try:
        font = ImageFont.truetype(str(font_path), max(10, int(font_size))) if font_path else ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()
    probe = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    draw = ImageDraw.Draw(probe)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    padding = 7
    width = max(2, right - left + padding * 2)
    height = max(2, bottom - top + padding * 2)
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=5, fill=(0, 0, 0, 72))
    draw.text((padding - left, padding - top), text, font=font, fill=(255, 255, 255, 255))
    fd, tmp_name = tempfile.mkstemp(prefix="chopster_wm_", suffix=".png")
    os.close(fd)
    path = Path(tmp_name)
    image.save(path, "PNG", optimize=True)
    return path


def _verify_output(path: Path, expect_audio: bool = True) -> tuple[bool, str]:
    if not path.exists() or path.stat().st_size == 0:
        return False, "File output tidak ada atau kosong"
    try:
        m = probe(path)
        if not m.has_video:
            return False, "Output tidak memiliki stream video"
        if expect_audio and not m.has_audio:
            return False, "Output kehilangan audio"
        if m.duration < 0.5:
            return False, "Durasi output terlalu pendek / container rusak"
        return True, ""
    except Exception as exc:
        return False, f"Verifikasi output gagal: {str(exc)[:300]}"


def _ass_to_seconds(value: str) -> float:
    m = _ASS_TIME_RE.match(value.strip())
    if not m:
        return 0.0
    h, mi, s, cs = map(int, m.groups())
    return h * 3600 + mi * 60 + s + cs / 100.0


def _seconds_to_ass(sec: float) -> str:
    sec = max(0.0, sec)
    total_cs = int(round(sec * 100))
    h, rem = divmod(total_cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _srt_to_seconds(value: str) -> float:
    value = value.replace(",", ".")
    h, m, rest = value.split(":")
    s, ms = rest.split(".")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def _seconds_to_srt(sec: float) -> str:
    total_ms = max(0, int(round(sec * 1000)))
    h, rem = divmod(total_ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _prepare_subtitle_for_clip(path: str | Path, start: float, end: float) -> Path:
    """Create a clip-relative ASS/SRT in the OS temp directory."""
    src = Path(path)
    if not src.exists():
        raise FileNotFoundError(src)
    suffix = src.suffix.lower()
    text = src.read_text(encoding="utf-8-sig")
    fd, tmp_name = tempfile.mkstemp(prefix="chopster_sub_", suffix=suffix or ".ass")
    os.close(fd)
    dst = Path(tmp_name)

    if suffix == ".ass":
        out: list[str] = []
        for line in text.splitlines():
            if not line.startswith("Dialogue:"):
                out.append(line)
                continue
            parts = line.split(",", 9)
            if len(parts) < 10:
                continue
            cue_s, cue_e = _ass_to_seconds(parts[1]), _ass_to_seconds(parts[2])
            if cue_e <= start or cue_s >= end:
                continue
            new_s = max(cue_s, start) - start
            new_e = min(cue_e, end) - start
            if new_e <= new_s:
                continue
            parts[1] = _seconds_to_ass(new_s)
            parts[2] = _seconds_to_ass(new_e)
            out.append(",".join(parts))
        dst.write_text("\n".join(out) + "\n", encoding="utf-8")
        return dst

    if suffix == ".srt":
        lines = text.splitlines()
        out: list[str] = []
        i = 0
        number = 1
        while i + 1 < len(lines):
            timing = _SRT_TIME_RE.match(lines[i + 1].strip())
            if not timing:
                i += 1
                continue
            cue_s, cue_e = _srt_to_seconds(timing.group(1)), _srt_to_seconds(timing.group(2))
            j = i + 2
            while j < len(lines) and lines[j].strip():
                j += 1
            if cue_e > start and cue_s < end:
                new_s = max(cue_s, start) - start
                new_e = min(cue_e, end) - start
                out.extend([str(number), f"{_seconds_to_srt(new_s)} --> {_seconds_to_srt(new_e)}"])
                out.extend(lines[i + 2:j])
                out.append("")
                number += 1
            i = j + 1
        dst.write_text("\n".join(out), encoding="utf-8")
        return dst

    try:
        dst.unlink(missing_ok=True)
    except OSError:
        pass
    raise ValueError(f"Format subtitle tidak didukung untuk burn-in: {src.suffix}")


def _escape_filter_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def _escape_drawtext(value: str) -> str:
    # FFmpeg drawtext uses ':', ',', '\\' and apostrophe as filter syntax.
    return (
        str(value)
        .replace("\\", r"\\")
        .replace("'", r"\'")
        .replace(":", r"\:")
        .replace(",", r"\,")
        .replace("%", r"\%")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def _watermark_position(position: str) -> tuple[str, str]:
    return WATERMARK_POSITIONS.get(position, WATERMARK_POSITIONS["Bottom right"])


def _default_font_file() -> Path | None:
    candidates = []
    if os.name == "nt":
        windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
        candidates.extend([windir / "Fonts" / "segoeui.ttf", windir / "Fonts" / "arial.ttf"])
    else:
        candidates.extend([Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"), Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf")])
    return next((p for p in candidates if p.exists()), None)


def _cleanup_path(path: Path | None, attempts: int = 10, delay: float = 0.15) -> None:
    if not path:
        return
    for _ in range(attempts):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            time.sleep(delay)
        except OSError:
            return


def _run_ffmpeg(cmd: list[str], cancel_check: Callable[[], bool] | None = None, timeout: int = 900) -> tuple[int, str]:
    def stderr_tail(stream, limit: int = 5000) -> str:
        stream.flush()
        stream.seek(0, os.SEEK_END)
        end = stream.tell()
        stream.seek(max(0, end - limit))
        return stream.read().decode("utf-8", errors="replace")

    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        creationflags = 0
    # A PIPE that is not drained while polling can fill and deadlock FFmpeg.
    # A temporary file keeps memory bounded and allows us to return a useful tail.
    with tempfile.TemporaryFile(mode="w+b") as error_log:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=error_log,
            creationflags=creationflags,
        )
        started = time.monotonic()
        while True:
            if cancel_check and cancel_check():
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                        proc.wait(timeout=3)
                    except Exception:
                        pass
                return -999, "Dibatalkan\n" + stderr_tail(error_log, 3000)
            if proc.poll() is not None:
                return proc.returncode, stderr_tail(error_log)
            if time.monotonic() - started > timeout:
                try:
                    proc.kill()
                    proc.wait(timeout=3)
                except Exception:
                    pass
                return -998, "Timeout FFmpeg\n" + stderr_tail(error_log, 3000)
            time.sleep(0.2)


def export_clip(
    source: str | Path,
    start: float,
    end: float,
    output: str | Path,
    quality: str = "balanced",
    aspect: str = "original",
    burn_subtitle: str | None = None,
    copy_if_possible: bool = True,
    cancel_flag: Callable[[], bool] | None = None,
    watermark_text: str | None = None,
    watermark_image: str | None = None,
    watermark_position: str = "Bottom right",
    watermark_opacity: float = 0.75,
    watermark_scale: float = 0.22,
    watermark_font_size: int = 28,
    focus_points: list[dict] | None = None,
    audio_filter: str | None = None,
) -> ExportResult:
    source = Path(source)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        return ExportResult(0, False, str(output), f"Source tidak ditemukan: {source}")
    try:
        start, end = max(0.0, float(start)), max(float(start), float(end))
    except (TypeError, ValueError):
        return ExportResult(0, False, str(output), "Range clip tidak valid")
    if end - start < 0.5:
        return ExportResult(0, False, str(output), "Durasi clip minimal 0.5 detik")
    if cancel_flag and cancel_flag():
        return ExportResult(0, False, str(output), "Dibatalkan")

    try:
        ff = _ffmpeg()
        meta = probe(source)
    except Exception as exc:
        return ExportResult(0, False, str(output), str(exc))

    if not meta.has_video:
        return ExportResult(0, False, str(output), "Source tidak memiliki video")
    if meta.duration > 0:
        start = min(start, max(0.0, meta.duration - 0.05))
        end = min(end, meta.duration)
    duration = end - start
    if duration < 0.5:
        return ExportResult(0, False, str(output), "Range clip berada di luar durasi source")

    has_audio = meta.has_audio
    temp_sub: Path | None = None
    temp_watermark: Path | None = None
    temp_output = output.with_name(f".{output.stem}.rendering{output.suffix}")
    try:
        # Never overwrite a completed file in-place. Render atomically to a temp file.
        _cleanup_path(temp_output, attempts=4, delay=0.05)
        use_copy = (
            copy_if_possible
            and not burn_subtitle
            and not watermark_text
            and not watermark_image
            and aspect == "original"
            and start <= 0.001
        )
        if use_copy:
            cmd = [ff, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source),
                   "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-movflags", "+faststart", "-avoid_negative_ts", "make_zero", str(temp_output)]
        else:
            qp = QUALITY_PRESETS.get(quality, QUALITY_PRESETS["balanced"])
            aspect_res = {"9:16": (1080, 1920), "3:4": (1080, 1440), "4:5": (1080, 1350), "1:1": (1080, 1080), "4:3": (1440, 1080), "16:9": (1920, 1080)}
            if aspect != "original" and aspect not in aspect_res:
                return ExportResult(0, False, str(output), f"Aspect tidak dikenal: {aspect}")

            filters: list[str] = []
            if aspect != "original":
                w, h = aspect_res[aspect]

                if focus_points:
                    pts = make_clip_relative(focus_points, start, end)
                    target_ratio = float(w) / float(h)
                    source_ratio = float(meta.width or 16) / max(1.0, float(meta.height or 9))
                    crop_fraction_x = min(1.0, target_ratio / max(0.01, source_ratio))
                    crop_fraction_y = min(1.0, source_ratio / max(0.01, target_ratio))
                    half_x = crop_fraction_x * 0.5
                    half_y = crop_fraction_y * 0.5

                    def safe_center_value(p, axis: str) -> float:
                        center = float(p.get(axis, 0.5))
                        shot=str(p.get("shot", "solo"))
                        # A "fit" label may have been produced for 9:16. When the
                        # user later chooses 4:5/3:4/1:1/etc, recompute whether the
                        # group actually needs fitting for THIS aspect before using
                        # group bounds. This keeps the active speaker centered when
                        # the wider canvas can safely contain the group.
                        group_w = None if p.get("group_left") is None or p.get("group_right") is None else float(p.get("group_right")) - float(p.get("group_left"))
                        group_h = None if p.get("group_top") is None or p.get("group_bottom") is None else float(p.get("group_bottom")) - float(p.get("group_top"))
                        group_needed = shot in ("group", "two") or (shot == "fit" and ((group_w is not None and group_w > crop_fraction_x * .92) or (group_h is not None and group_h > crop_fraction_y * .92)))
                        if axis == "x":
                            lo_v, hi_v = p.get("subject_left"), p.get("subject_right")
                            if group_needed:
                                lo_v, hi_v = p.get("group_left", lo_v), p.get("group_right", hi_v)
                            half = half_x
                        else:
                            lo_v, hi_v = p.get("subject_top"), p.get("subject_bottom")
                            if group_needed:
                                lo_v, hi_v = p.get("group_top", lo_v), p.get("group_bottom", hi_v)
                            half = half_y
                        if lo_v is not None and hi_v is not None:
                            lo_v = max(0.0, min(1.0, float(lo_v)))
                            hi_v = max(0.0, min(1.0, float(hi_v)))
                            lo = max(half, hi_v - half)
                            hi = min(1.0 - half, lo_v + half)
                            if lo <= hi:
                                center = max(lo, min(hi, center))
                            else:
                                center = (lo_v + hi_v) * 0.5
                        return max(0.0, min(1.0, center))

                    def expr(axis: str):
                        parts = None
                        for p0, p1 in reversed(list(zip(pts[:-1], pts[1:]))):
                            t0 = float(p0.get("t", 0)); t1 = float(p1.get("t", t0 + 0.01))
                            n0 = safe_center_value(p0, axis); n1 = safe_center_value(p1, axis)
                            if axis == "x":
                                v0 = f"((iw-{w})*{n0:.6f})"
                                v1 = f"((iw-{w})*{n1:.6f})"
                            else:
                                v0 = f"((ih-{h})*{n0:.6f})"
                                v1 = f"((ih-{h})*{n1:.6f})"
                            dur_seg = max(.01, t1 - t0)
                            u = f"clip((t-{t0:.3f})/{dur_seg:.3f},0,1)"
                            eased = f"({u}*{u}*(3-2*{u}))"
                            seg = f"({v0}+({v1}-{v0})*{eased})"
                            parts = seg if parts is None else f"if(lt(t,{t1:.3f}),{seg},{parts})"
                        first = pts[0]
                        fv_n = safe_center_value(first, axis)
                        fv = f"((iw-{w})*{fv_n:.6f})" if axis == "x" else f"((ih-{h})*{fv_n:.6f})"
                        if parts is None:
                            return fv
                        return f"if(lt(t,{float(first.get('t', 0)):.3f}),{fv},{parts})"

                    xexpr = expr("x").replace(",", r"\,")
                    yexpr = expr("y").replace(",", r"\,")
                    crop_chain = f"scale=w='if(gt(a,{target_ratio:.6f}),-2,{w})':h='if(gt(a,{target_ratio:.6f}),{h},-2)',crop={w}:{h}:x='{xexpr}':y='{yexpr}'"

                    # Close-up safety: if a detected person would occupy too much
                    # of the crop window, do NOT simply zoom harder. Build a wider
                    # contextual window, fit it into the requested canvas, and use
                    # a blurred version of the source behind it. This is recalculated
                    # for every requested aspect ratio.
                    source_ratio = float(meta.width or 16) / max(1.0, float(meta.height or 9))
                    decisions = [decide_framing(p, source_aspect=source_ratio, target_aspect=target_ratio) for p in pts]
                    adaptive = any(d.mode == "adaptive_fit" for d in decisions)
                    if adaptive:
                        # Use one stable context size for the clip. It can be wider
                        # than the output crop, but it never exceeds the source.
                        ctx_w_norm = max([float(d.context_right - d.context_left) for d in decisions if d.mode == "adaptive_fit" and d.context_left is not None and d.context_right is not None] or [crop_fraction_x])
                        ctx_h_norm = max([float(d.context_bottom - d.context_top) for d in decisions if d.mode == "adaptive_fit" and d.context_top is not None and d.context_bottom is not None] or [crop_fraction_y])
                        ctx_w = max(2, min(int(meta.width or 2), int(round(ctx_w_norm * float(meta.width or 2)))))
                        ctx_h = max(2, min(int(meta.height or 2), int(round(ctx_h_norm * float(meta.height or 2)))))

                        def ctx_expr(axis: str) -> str:
                            parts = None
                            seq = list(zip(pts[:-1], pts[1:]))
                            for p0, p1 in reversed(seq):
                                t0 = float(p0.get("t", 0)); t1 = float(p1.get("t", t0 + 0.01))
                                n0 = float(p0.get(axis, 0.5)); n1 = float(p1.get(axis, 0.5))
                                if axis == "x":
                                    v0 = f"clip((iw-{ctx_w})*{n0:.6f},0,iw-{ctx_w})"
                                    v1 = f"clip((iw-{ctx_w})*{n1:.6f},0,iw-{ctx_w})"
                                else:
                                    v0 = f"clip((ih-{ctx_h})*{n0:.6f},0,ih-{ctx_h})"
                                    v1 = f"clip((ih-{ctx_h})*{n1:.6f},0,ih-{ctx_h})"
                                dur_seg = max(.01, t1 - t0)
                                u = f"clip((t-{t0:.3f})/{dur_seg:.3f},0,1)"
                                eased = f"({u}*{u}*(3-2*{u}))"
                                seg = f"({v0}+({v1}-{v0})*{eased})"
                                parts = seg if parts is None else f"if(lt(t,{t1:.3f}),{seg},{parts})"
                            first = pts[0]
                            n = float(first.get(axis, 0.5))
                            base = f"clip((iw-{ctx_w})*{n:.6f},0,iw-{ctx_w})" if axis == "x" else f"clip((ih-{ctx_h})*{n:.6f},0,ih-{ctx_h})"
                            return base if parts is None else f"if(lt(t,{float(first.get('t', 0)):.3f}),{base},{parts})"

                        cxexpr = ctx_expr("x").replace(",", r"\,")
                        cyexpr = ctx_expr("y").replace(",", r"\,")
                        bg = f"scale=w={w}:h={h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=luma_radius=22:luma_power=1"
                        fg = f"crop={ctx_w}:{ctx_h}:x='{cxexpr}':y='{cyexpr}',scale=w={w}:h={h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black@0"
                        filter_complex = f"[0:v]{bg}[bg];[0:v]{fg}[fg];[bg][fg]overlay=0:0[vfit]"
                        filters.append((filter_complex, ""))
                    else:
                        filters.append(crop_chain)
                else:
                    filters.append(f"scale=w={w}:h={h}:force_original_aspect_ratio=increase,crop={w}:{h}")
            if burn_subtitle:
                temp_sub = _prepare_subtitle_for_clip(burn_subtitle, start, end)
                subtitle_filter=f"subtitles='{_escape_filter_path(temp_sub)}'"
                fonts_dir=Path(__file__).resolve().parents[1]/"auto_clip_studio"/"engine"/"fonts"
                if fonts_dir.is_dir() and any(fonts_dir.glob("*.ttf")):
                    subtitle_filter+=f":fontsdir='{_escape_filter_path(fonts_dir)}'"
                filters.append(subtitle_filter)

            wm_text = (watermark_text or "").strip()
            wm_img = Path(watermark_image).expanduser() if watermark_image else None
            generated_text_watermark = False
            if wm_img and not wm_img.exists():
                return ExportResult(0, False, str(output), f"Watermark image tidak ditemukan: {wm_img}")
            opacity = max(0.05, min(1.0, float(watermark_opacity)))
            pos_x, pos_y = _watermark_position(watermark_position)
            if wm_text and not wm_img and not _ffmpeg_has_filter(ff, "drawtext"):
                temp_watermark = _text_watermark_image(wm_text, watermark_font_size)
                wm_img = temp_watermark
                generated_text_watermark = True

            if wm_img:
                if generated_text_watermark:
                    wm_scale = "scale=iw:ih"
                else:
                    pct=max(0.01,min(1.0,float(watermark_scale))); wm_scale = f"scale=w=max(2\\,trunc(iw*{pct:.4f}/2)*2):h=-1"
                wm_chain = f"[1:v]format=rgba,{wm_scale},colorchannelmixer=aa={opacity:.3f}[wm]"
                tuple_filter = next((x for x in filters if isinstance(x, tuple)), None)
                if tuple_filter:
                    graph,_ = tuple_filter
                    extras=[x for x in filters if not isinstance(x, tuple)]
                    graph += (";[vfit]" + ",".join(extras) + "[base]") if extras else ";[vfit]null[base]"
                    graph += ";" + wm_chain + f";[base][wm]overlay=x={pos_x}:y={pos_y}:eof_action=repeat:shortest=1[vout]"
                    filter_complex=graph
                else:
                    base_chain=",".join(filters) if filters else "null"
                    main_chain=f"[0:v]{base_chain}[base]"
                    overlay_chain=f"[base][wm]overlay=x={pos_x}:y={pos_y}:eof_action=repeat:shortest=1[vout]"
                    filter_complex=";".join([main_chain,wm_chain,overlay_chain])
                cmd=[ff,"-hide_banner","-loglevel","error","-y","-ss",f"{start:.3f}","-t",f"{duration:.3f}","-i",str(source),
                     "-loop","1","-i",str(wm_img),"-filter_complex",filter_complex,"-map","[vout]","-map","0:a:0?"]
            else:
                if wm_text:
                    safe = _escape_drawtext(wm_text)
                    font = _default_font_file()
                    font_opt = f"fontfile='{_escape_filter_path(font)}':" if font else ""
                    draw = (
                        f"drawtext={font_opt}text='{safe}':fontsize={max(10, int(watermark_font_size))}:"
                        f"fontcolor=white@{opacity:.3f}:box=1:boxcolor=black@0.28:boxborderw=7:"
                        f"x={pos_x}:y={pos_y}"
                    )
                    filters.append(draw)
                tuple_filter = next((x for x in filters if isinstance(x, tuple)), None)
                if tuple_filter:
                    graph, crop_chain = tuple_filter
                    # Keep the smart-fit graph simple and deterministic. If additional
                    # filters exist, apply them to the final portrait output.
                    extras=[x for x in filters if not isinstance(x, tuple)]
                    if extras:
                        graph += ";[vfit]" + ",".join(extras) + "[vout]"
                        map_label="[vout]"
                    else:
                        graph += ";[vfit]null[vout]"
                        map_label="[vout]"
                    cmd = [ff, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source),
                           "-filter_complex", graph, "-map", map_label, "-map", "0:a:0?"]
                else:
                    vf = ",".join(filters) if filters else "null"
                    cmd = [ff, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source),
                           "-map", "0:v:0", "-map", "0:a:0?"]
                    cmd += ["-vf", vf]

            cmd += ["-c:v", "libx264", "-preset", qp["preset"], "-crf", qp["crf"], "-pix_fmt", "yuv420p"]
            if has_audio:
                if audio_filter:
                    cmd += ["-af", str(audio_filter)]
                cmd += ["-c:a", "aac", "-b:a", "128k"]
            else:
                cmd += ["-an"]
            cmd += ["-movflags", "+faststart", "-avoid_negative_ts", "make_zero", str(temp_output)]

        rc, detail = _run_ffmpeg(cmd, cancel_check=cancel_flag, timeout=900)
        if rc == -999:
            _cleanup_path(temp_output)
            return ExportResult(0, False, str(output), "Dibatalkan")
        if rc != 0:
            _cleanup_path(temp_output)
            return ExportResult(0, False, str(output), detail.strip() or "FFmpeg gagal")
        if cancel_flag and cancel_flag():
            _cleanup_path(temp_output)
            return ExportResult(0, False, str(output), "Dibatalkan")

        ok, warn = _verify_output(temp_output, expect_audio=has_audio)
        if not ok:
            _cleanup_path(temp_output)
            return ExportResult(0, False, str(output), warn)
        _cleanup_path(output, attempts=8, delay=0.12)
        try:
            temp_output.replace(output)
        except OSError as exc:
            _cleanup_path(temp_output)
            return ExportResult(0, False, str(output), f"Tidak dapat memindahkan hasil render: {exc}")
        return ExportResult(0, True, str(output), warn)
    except Exception as exc:
        _cleanup_path(temp_output)
        return ExportResult(0, False, str(output), str(exc)[:2000])
    finally:
        # Cleanup is best-effort only. It must never convert a successful render to failure.
        _cleanup_path(temp_sub, attempts=12, delay=0.12)
        _cleanup_path(temp_watermark, attempts=12, delay=0.12)
        _cleanup_path(temp_output, attempts=3, delay=0.05)


def export_batch(
    source: str | Path,
    clips: list[dict],
    out_dir: str | Path,
    quality: str = "balanced",
    aspect: str = "original",
    cancel_flag: Callable[[], bool] | None = None,
    on_progress: ProgressCb | None = None,
    on_clip: ClipCb | None = None,
    parallel: int = 1,
    burn_subtitle: str | None = None,
    watermark_text: str | None = None,
    watermark_image: str | None = None,
    watermark_position: str = "Bottom right",
    watermark_opacity: float = 0.75,
    watermark_scale: float = 0.22,
    watermark_font_size: int = 28,
    focus_points: list[dict] | None = None,
    audio_filter: str | None = None,
) -> tuple[int, int]:
    """Export immutable clip snapshots. UI code must not be accessed from here."""
    source = Path(source)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    total = len(clips)
    if total == 0:
        return 0, 0

    def render(c: dict):
        idx = int(c.get("index", 0))
        title = str(c.get("title") or f"Clip_{idx:03d}")
        safe = "".join(ch if ch.isalnum() or ch in " -_()" else "_" for ch in title)[:60] or f"Clip_{idx:03d}"
        out_path = out_dir / f"Clip_{idx:03d}_{safe}.mp4"
        if cancel_flag and cancel_flag():
            return idx, title, ExportResult(idx, False, str(out_path), "Dibatalkan")
        if on_clip:
            on_clip(idx, title, "Merender")
        res = export_clip(
            source, float(c["start"]), float(c["end"]), out_path,
            quality=quality, aspect=aspect, burn_subtitle=burn_subtitle,
            cancel_flag=cancel_flag,
            watermark_text=watermark_text, watermark_image=watermark_image,
            watermark_position=watermark_position, watermark_opacity=watermark_opacity, watermark_scale=watermark_scale,
            watermark_font_size=watermark_font_size, focus_points=focus_points, audio_filter=audio_filter,
            copy_if_possible=not any((burn_subtitle, watermark_text, watermark_image, focus_points, audio_filter)) and aspect == "original",
        )
        return idx, title, res

    ok = fail = 0
    if parallel <= 1:
        iterator = ((c, render(c)) for c in clips)
        for i, (c, result) in enumerate(iterator, 1):
            idx, title, res = result
            ok += int(res.success)
            fail += int(not res.success)
            if on_clip:
                on_clip(idx, title, "Berhasil" if res.success else f"Gagal: {res.error[:100]}")
            if on_progress:
                on_progress(i, total, f"Selesai {i}/{total}")
            if cancel_flag and cancel_flag():
                break
    else:
        with ThreadPoolExecutor(max_workers=max(1, min(3, int(parallel)))) as ex:
            futs = [ex.submit(render, c) for c in clips]
            for done, fut in enumerate(as_completed(futs), 1):
                try:
                    idx, title, res = fut.result()
                    ok += int(res.success)
                    fail += int(not res.success)
                    if on_clip:
                        on_clip(idx, title, "Berhasil" if res.success else f"Gagal: {res.error[:100]}")
                except Exception as exc:
                    fail += 1
                    if on_clip:
                        on_clip(0, "", f"Gagal: {exc}")
                if on_progress:
                    on_progress(done, total, f"Selesai {done}/{total}")
                if cancel_flag and cancel_flag():
                    break
    return ok, fail
