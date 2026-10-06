"""Downloader engine — ported faithfully from Tkinter reference (commit-parity)."""
from __future__ import annotations

import os
import re
import shutil
import sys
import subprocess
import time
import json
import tempfile
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from pathlib import Path
from typing import Any, Callable

from chopster.app.configuration import FILENAME_TEMPLATES, QUALITIES, SPEED_PRESETS
from chopster.app.paths import app_dir, user_data_dir
from chopster.downloader.ffmpeg import find_ffmpeg, find_ffprobe
from chopster.downloader.validators import detect_platform

ProgressCb = Callable[[int, str], None]
RowCb = Callable[[int, str, str, str], None]
LogCb = Callable[[str], None]

# Same ANSI strip as Tkinter reference
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _clean_error(msg: str) -> str:
    return ANSI_RE.sub("", msg).strip()


def archive_path() -> Path:
    """Store yt-dlp's archive in writable per-user data, migrating portable installs."""
    target = user_data_dir() / "download_archive.txt"
    legacy = app_dir() / "download_archive.txt"
    if not target.exists() and legacy.is_file():
        try:
            shutil.copy2(legacy, target)
        except OSError:
            # Keep the old archive usable if migration is temporarily blocked.
            return legacy
    return target


def _is_tiktok_webpage_challenge(url: str, exc: BaseException) -> bool:
    return (
        detect_platform(url) == "TikTok"
        and "unexpected response from webpage request" in str(exc).lower()
    )


def _requires_ffprobe(output_format: str) -> bool:
    return not str(output_format or "").startswith(("MP3", "M4A"))


def _download_output_dir(url: str, config: dict[str, Any]) -> Path:
    root = Path(config.get("out_dir") or Path.home() / "Downloads")
    if bool(config.get("subfolders", True)):
        return root / detect_platform(url)
    return root


@lru_cache(maxsize=16)
def _runtime_version(name: str, path: str) -> tuple[int, ...] | None:
    try:
        proc=subprocess.run([path,"--version"],capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=4)
        text=(proc.stdout or proc.stderr or "").strip()
        m=re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?",text)
        if not m:return None
        return tuple(int(x or 0) for x in m.groups())
    except Exception:
        return None

def _runtime_supported(name: str, path: str) -> bool:
    if name=="deno":
        v=_runtime_version(name,path); return bool(v and v >= (2,3,0))
    if name=="node":
        v=_runtime_version(name,path); return bool(v and v >= (22,0,0))
    return True

def _find_js_runtime() -> tuple[str, str] | None:
    """Find a supported yt-dlp JS runtime and reject obsolete Node/Deno versions."""
    candidates: list[tuple[str, str]] = []
    base_dirs: list[Path] = []
    try: base_dirs.append(app_dir())
    except Exception: pass
    if getattr(sys, "frozen", False): base_dirs.append(Path(sys.executable).parent)
    try:
        user_deno=Path.home()/".deno"/"bin"/"deno.exe"
        if user_deno.is_file(): candidates.append(("deno",str(user_deno)))
    except Exception: pass
    names={"deno":["deno.exe","deno"],"node":["node.exe","node"],"quickjs":["qjs.exe","qjs"]}
    for runtime,files in names.items():
        found=shutil.which(runtime)
        if found:candidates.append((runtime,found))
        for d in base_dirs:
            for name in files:
                fp=d/name
                if fp.is_file():candidates.append((runtime,str(fp)))
    order={"deno":0,"node":1,"quickjs":2}
    candidates.sort(key=lambda x:order.get(x[0],99))
    for name,path in candidates:
        if _runtime_supported(name,path): return name,path
    return None


def _youtube_runtime_opts() -> dict[str, Any]:
    """Return yt-dlp options required for modern YouTube extraction."""
    opts: dict[str, Any] = {}
    runtime = _find_js_runtime()
    if runtime:
        name, path = runtime
        opts["js_runtimes"] = {name: {"path": path}}
        # EJS is checked before a YouTube batch starts. Keep this options builder
        # free of remote fallback/network calls.
    return opts


def _is_youtube_url(url: str) -> bool:
    return detect_platform(url) == "YouTube"


def _platform_uses_login_fallback(url: str) -> bool:
    """Platforms commonly requiring an authenticated browser session."""
    return detect_platform(url) in {"TikTok", "Instagram", "Facebook", "Threads", "YouTube"}


def _looks_like_login_required(exc: BaseException) -> bool:
    msg = str(exc).lower()
    markers = (
        "log in for access",
        "login for access",
        "use --cookies-from-browser",
        "use --cookies or --cookies-from-browser",
        "not comfortable for some audiences",
        "this content is only available",
        "age-restricted",
        "sign in to confirm",
        "authentication required",
        "login required",
        "requires login",
        "private video",
    )
    return any(m in msg for m in markers)


def _looks_like_browser_cookie_setup_failure(exc: BaseException) -> bool:
    msg = str(exc).lower()
    markers = (
        "could not copy",
        "could not find",
        "could not find firefox cookies database",
        "no firefox cookies database",
        "failed to read browser cookies",
        "error decrypting cookies",
        "failed to decrypt",
        "cannot decrypt",
        "dpapi",
        "could not access browser cookie database",
        "cookies from browser",
        "cookie database",
        "profiles'",
        "profile directory",
    )
    return any(m in msg for m in markers)


def _looks_like_youtube_client_failure(exc: BaseException) -> bool:
    msg = str(exc).lower()
    markers = (
        "http error 403", "http error 429", "forbidden", "sabr",
        "page needs to be reloaded", "no supported javascript runtime",
        "challenge", "unable to extract", "requested format is not available",
        "no video formats found",
    )
    return any(m in msg for m in markers)


class _CallbackLogger:
    def __init__(self, callback=None):
        self.callback = callback

    def _emit(self, level, msg):
        text = _clean_error(str(msg or "")).strip()
        if text and self.callback:
            try:
                self.callback(f"yt-dlp [{level}] {text}")
            except Exception:
                pass

    def debug(self, msg):
        # yt-dlp emits very noisy debug lines; keep only meaningful progress.
        text = str(msg or "")
        if text.startswith("[debug]"):
            return
        self._emit("debug", text)

    def info(self, msg): pass
    def warning(self, msg):
        text = _clean_error(str(msg or ""))
        # Ignore normal yt-dlp chatter. Only surface warnings that look actionable.
        low=text.lower()
        if any(k in low for k in ("error", "unable", "forbidden", "429", "403", "challenge", "not found")):
            self._emit("warning", text)
    def error(self, msg):
        text = _clean_error(str(msg or ""))
        if _looks_like_browser_cookie_setup_failure(RuntimeError(text)):
            low = text.lower()
            if "dpapi" in low or "decrypt" in low:
                summary = (
                    "Cookie browser tidak dapat didekripsi oleh DPAPI. "
                    "Gunakan sesi Chrome/Edge yang sama atau file cookies Netscape dari Settings."
                )
            else:
                summary = "Database/profil cookie browser tidak ditemukan; fallback browser ini dilewati."
            self._emit("warning", summary)
            return
        if "unable to download video subtitles" in text.lower() or "unable to download subtitles" in text.lower():
            # Subtitle endpoints are optional and may rate-limit independently (e.g. HTTP 429).
            # Treat as warning so successful media downloads are not presented as hard failures.
            self._emit("warning", f"Subtitle dilewati: {text}")
            return
        self._emit("error", text)


def _audio_selector(audio_language: str = "Auto — Indonesia jika tersedia") -> str:
    """Select an audio track by language preference with safe fallback.

    This helper intentionally contains audio-only fallbacks because it is also
    used by MP3/M4A modes. It must NOT be interpolated directly after a video
    selector such as ``bestvideo+...`` because the ``/`` alternatives would
    otherwise become top-level alternatives and could select audio-only.
    """
    value=str(audio_language or "").strip().lower()
    if "indonesia" in value or value in {"id", "id-id"}:
        return "bestaudio[language^=id]/bestaudio[language^=id-ID]/bestaudio"
    if "english" in value or value in {"en", "en-us", "en-gb"}:
        return "bestaudio[language^=en]/bestaudio"
    if "original" in value or "default" in value:
        return "bestaudio"
    return "bestaudio[language^=id]/bestaudio[language^=id-ID]/bestaudio"


def _preferred_audio_selector(audio_language: str = "Auto — Indonesia jika tersedia") -> str:
    """Return ONE preferred audio selector (no ``/`` fallback chain).

    Keeping this expression atomic lets the video selector build its own
    video+audio fallbacks without ever dropping down to audio-only media.
    """
    value=str(audio_language or "").strip().lower()
    if "indonesia" in value or value in {"id", "id-id"}:
        return "bestaudio[language^=id]"
    if "english" in value or value in {"en", "en-us", "en-gb"}:
        return "bestaudio[language^=en]"
    return "bestaudio"


def _ydl_format(quality: str, output_format: str, h264: bool, audio_language: str = "Auto — Indonesia jika tersedia") -> str:
    if output_format.startswith("MP3"):
        return f"{_audio_selector(audio_language)}/bestaudio/best"
    if output_format.startswith("M4A"):
        audio=_audio_selector(audio_language)
        return f"{audio}[ext=m4a]/{audio}/bestaudio[ext=m4a]/bestaudio/best"

    cap = f"[height<={h}]" if (h := QUALITIES.get(quality)) else ""
    preferred_audio = _preferred_audio_selector(audio_language)

    # IMPORTANT: every slash-separated alternative below is a VIDEO-bearing
    # selection. The previous implementation embedded an audio selector that
    # itself contained ``/bestaudio`` inside ``bestvideo+...``. yt-dlp parses
    # that slash at the top level, which can legitimately fall back to an
    # audio-only file. That is the root cause of the "MP4 downloads only
    # audio" bug observed on YouTube.
    if h264:
        preferred_video = f"bestvideo{cap}[vcodec^=avc1]"
        any_video = f"bestvideo{cap}"
    else:
        preferred_video = f"bestvideo{cap}"
        any_video = preferred_video

    # Order: preferred codec+language -> same video + any audio -> any video
    # + preferred language -> any video + any audio -> progressive combined
    # format. There is deliberately NO standalone ``bestaudio`` alternative.
    # Direct/generic media URLs often do not expose a height field. Keep the
    # requested cap whenever metadata exists, then accept only unknown-height
    # progressive media instead of failing a valid direct video URL.
    unknown_height = f"/best[height<=?{h}][ext=mp4]/best[height<=?{h}]" if h else ""
    progressive = f"best{cap}[ext=mp4]/best{cap}{unknown_height}"
    return (
        f"{preferred_video}+{preferred_audio}/"
        f"{preferred_video}+bestaudio/"
        f"{any_video}+{preferred_audio}/"
        f"{any_video}+bestaudio/"
        f"{progressive}"
    )


def _find_ffprobe() -> str | None:
    """Find ffprobe next to the bundled/system ffmpeg or on PATH."""
    return find_ffprobe()


def _media_has_video(path: str) -> bool:
    """Return True only when the final media has at least one video stream."""
    if not path or not Path(path).is_file():
        return False
    ffprobe = _find_ffprobe()
    if not ffprobe:
        # No probe tool available: do not falsely reject an otherwise valid
        # download. The selector itself already guarantees a video-bearing path.
        return True
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_type", "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8,
        )
        return proc.returncode == 0 and "video" in (proc.stdout or "").lower()
    except Exception:
        return True


def _media_has_audio(path: str) -> bool:
    """Return True when final media has at least one audio stream."""
    if not path or not Path(path).is_file():
        return False
    ffprobe = _find_ffprobe()
    if not ffprobe:
        return True
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_type", "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8,
        )
        return proc.returncode == 0 and "audio" in (proc.stdout or "").lower()
    except Exception:
        return True


def _media_has_audio_video(path: str) -> bool:
    """Strict AV guard for video mode: final file must contain both streams."""
    return _media_has_video(path) and _media_has_audio(path)


def _probe_media_streams(path: str) -> list[dict[str, Any]]:
    """Return ffprobe stream metadata needed to check Windows MP4 compatibility."""
    ffprobe = _find_ffprobe()
    if not ffprobe:
        raise RuntimeError("ffprobe tidak ditemukan; kompatibilitas MP4 tidak bisa diperiksa.")
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries",
         "stream=codec_type,codec_name,pix_fmt,profile,channels",
         "-of", "json", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe gagal memeriksa file: {(proc.stderr or '').strip()[:300]}")
    try:
        return list(json.loads(proc.stdout or "{}").get("streams") or [])
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Metadata video tidak bisa dibaca oleh ffprobe.") from exc


def _ensure_windows_compatible_mp4(path: str, log_callback=None) -> str:
    """Make H.264 8-bit 4:2:0 + AAC MP4 for broad Windows player support.

    TikTok and other extractors may fall back to HEVC/AV1/VP9 even when the UI
    requests H.264. Re-encode only when the downloaded streams need it.
    """
    source = Path(path)
    streams = _probe_media_streams(str(source))
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None or audio is None:
        raise RuntimeError("File unduhan tidak memiliki stream video dan audio.")

    # Windows' built-in players have the broadest support for 8-bit 4:2:0 AVC
    # and AAC-LC stereo in MP4. Non-standard H.264 pixel formats need conversion.
    compatible = (
        video.get("codec_name") == "h264"
        and video.get("pix_fmt") == "yuv420p"
        and audio.get("codec_name") == "aac"
        and int(audio.get("channels") or 0) <= 2
    )
    target = source if source.suffix.lower() == ".mp4" else source.with_suffix(".mp4")
    if compatible and target == source:
        return str(source)
    if source != target and target.exists():
        stem = source.stem + "_H264"
        target = source.with_name(f"{stem}.mp4")
        suffix = 2
        while target.exists():
            target = source.with_name(f"{stem}_{suffix}.mp4")
            suffix += 1

    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{target.stem}.", suffix=".compat.mp4", dir=str(target.parent), delete=False
        ) as temp:
            temp_path = temp.name
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            raise RuntimeError("FFmpeg tidak ditemukan untuk membuat MP4 H.264/AAC yang kompatibel.")
        if log_callback:
            try:
                log_callback("Menyesuaikan codec ke H.264/AAC agar video kompatibel dengan pemutar Windows…")
            except Exception:
                pass
        proc = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "0",
                "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-ac", "2",
                "-movflags", "+faststart", "-f", "mp4", temp_path,
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0 or not Path(temp_path).is_file() or Path(temp_path).stat().st_size == 0:
            detail = (proc.stderr or "").strip()[-500:]
            raise RuntimeError(f"Konversi H.264/AAC gagal. {detail}".strip())
        converted_streams = _probe_media_streams(temp_path)
        converted_video = next((s for s in converted_streams if s.get("codec_type") == "video"), {})
        converted_audio = next((s for s in converted_streams if s.get("codec_type") == "audio"), {})
        if (converted_video.get("codec_name") != "h264"
                or converted_video.get("pix_fmt") != "yuv420p"
                or converted_audio.get("codec_name") != "aac"):
            raise RuntimeError("Hasil konversi belum memenuhi format H.264/AAC yang kompatibel.")
        os.replace(temp_path, target)
        temp_path = ""
        if source != target:
            source.unlink(missing_ok=True)
        if log_callback:
            try:
                log_callback("Codec siap: MP4 H.264/AAC (kompatibel dengan Windows).")
            except Exception:
                pass
        return str(target)
    finally:
        if temp_path:
            try:
                Path(temp_path).unlink(missing_ok=True)
            except OSError:
                pass


def _browser_process_running(browser: str) -> bool:
    """Best-effort Windows check to avoid guaranteed Chromium cookie DB lock errors."""
    names = {
        "chrome": ["chrome.exe"],
        "edge": ["msedge.exe"],
        "brave": ["brave.exe"],
        "chromium": ["chromium.exe", "chromium-browser.exe"],
        "firefox": ["firefox.exe"],
    }
    wanted = names.get(browser, [])
    if not wanted or os.name != "nt":
        return False
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=4).stdout.lower()
        return any(f'"{n.lower()}"' in out for n in wanted)
    except Exception:
        return False


def _browser_candidates(source: str) -> list[str]:
    value = str(source or "Auto").strip().lower()
    if value in {"", "auto"}:
        # Prefer Firefox first because yt-dlp's own FAQ notes it often works better
        # for cookie extraction on some setups; Chromium browsers follow.
        return ["firefox", "chrome", "edge", "brave", "chromium"]
    if value in {"chrome", "edge", "firefox", "brave", "chromium"}:
        return [value]
    return []


def _browser_has_cookie_database(browser: str) -> bool:
    """Avoid futile yt-dlp cookie attempts when the browser has no profile DB."""
    home = Path.home()
    roaming = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
    local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")

    if browser == "firefox":
        roots = [
            roaming / "Mozilla" / "Firefox" / "Profiles",
            home / ".mozilla" / "firefox",
            home / "Library" / "Application Support" / "Firefox" / "Profiles",
        ]
        roots.extend(local.glob("Packages/Mozilla.Firefox_*/LocalCache/Roaming/Mozilla/Firefox/Profiles"))
        return any(root.is_dir() and any(root.glob("*/cookies.sqlite")) for root in roots)

    roots_by_browser = {
        "chrome": [
            local / "Google" / "Chrome" / "User Data",
            home / ".config" / "google-chrome",
            home / "Library" / "Application Support" / "Google" / "Chrome",
        ],
        "edge": [
            local / "Microsoft" / "Edge" / "User Data",
            home / ".config" / "microsoft-edge",
            home / "Library" / "Application Support" / "Microsoft Edge",
        ],
        "brave": [
            local / "BraveSoftware" / "Brave-Browser" / "User Data",
            home / ".config" / "BraveSoftware" / "Brave-Browser",
            home / "Library" / "Application Support" / "BraveSoftware" / "Brave-Browser",
        ],
        "chromium": [
            local / "Chromium" / "User Data",
            home / ".config" / "chromium",
            home / "Library" / "Application Support" / "Chromium",
        ],
    }
    for root in roots_by_browser.get(browser, []):
        if not root.is_dir():
            continue
        try:
            profiles = [root, *(p for p in root.iterdir() if p.is_dir())]
        except OSError:
            continue
        for profile in profiles:
            if any((profile / relative).is_file() for relative in ("Network/Cookies", "Cookies")):
                return True
    return False


def _progressive_video_format(quality: str) -> str:
    """A single-file video+audio fallback; useful when YouTube GVS 403s separate streams."""
    cap = f"[height<={h}]" if (h := QUALITIES.get(quality)) else ""
    unknown_height = f"/best[height<=?{h}][ext=mp4]/best[height<=?{h}]" if h else ""
    return f"best[ext=mp4]{cap}/best[ext=webm]{cap}/best{cap}{unknown_height}"


def _youtube_client_options(base: dict[str, Any], client: str, *, progressive: bool = False, quality: str = "Terbaik yang tersedia") -> dict[str, Any]:
    fallback = dict(base)
    extractor_args = dict(fallback.get("extractor_args") or {})
    youtube_args = dict(extractor_args.get("youtube") or {})
    youtube_args["player_client"] = [client]
    extractor_args["youtube"] = youtube_args
    fallback["extractor_args"] = extractor_args
    fallback["overwrites"] = True
    fallback["continuedl"] = False
    if progressive:
        fallback["format"] = _progressive_video_format(quality)
    return fallback


def _clear_stale_partials(path: str) -> None:
    """Remove obvious partial siblings created by a failed download."""
    if not path:
        return
    try:
        p = Path(path)
        for candidate in (p, Path(str(p) + ".part"), Path(str(p) + ".ytdl")):
            if candidate.is_file():
                candidate.unlink(missing_ok=True)
    except Exception:
        pass


def _cleanup_partial_artifacts(paths: set[str]) -> None:
    """Delete yt-dlp incomplete transfer files, never completed media outputs."""
    for raw_path in paths:
        if not raw_path:
            continue
        path = Path(raw_path)
        candidates = {
            path if path.name.endswith((".part", ".ytdl")) else Path(str(path) + ".part"),
            path if path.name.endswith((".part", ".ytdl")) else Path(str(path) + ".ytdl"),
        }
        try:
            candidates.update(path.parent.glob(path.name + ".part-Frag*"))
        except OSError:
            pass
        for candidate in candidates:
            try:
                if candidate.is_file():
                    candidate.unlink(missing_ok=True)
            except OSError:
                pass


def _safe_video_recovery_format(quality: str, h264: bool) -> str:
    """Emergency format selector used only if a platform returns audio-only.

    Every slash-separated branch contains video; the final branch is yt-dlp's
    best combined format. This deliberately ignores audio-language preference
    because the priority of this recovery path is a valid video file.
    """
    cap = f"[height<={h}]" if (h := QUALITIES.get(quality)) else ""
    if h264:
        video = f"bestvideo{cap}[vcodec^=avc1]"
    else:
        video = f"bestvideo{cap}"
    any_video = f"bestvideo{cap}"
    unknown_height = f"/best[height<=?{h}][ext=mp4]/best[height<=?{h}]" if h else ""
    return f"{video}+bestaudio/{any_video}+bestaudio/best{cap}[ext=mp4]/best{cap}{unknown_height}"


def build_ydl_opts(
    *,
    out_dir: str,
    quality: str = "Terbaik yang tersedia",
    output_format: str = "MP4 (Video + Audio)",
    h264: bool = True,
    filename_template: str = "[Platform] Judul",
    subfolders: bool = True,
    speed_limit: str = "Tidak terbatas",
    cookies_file: str = "",
    browser_cookie_source: str = "Auto",
    proxy: str = "",
    subtitles: bool = False,
    subtitle_langs: str = "id,en",
    thumbnail: bool = False,
    audio_language: str = "Auto — Indonesia jika tersedia",
    download_playlist: bool = False,
    playlist_items: str = "",
    progress_hook=None,
    retry_mode: bool = False,
    log_callback=None,
) -> dict[str, Any]:
    # Parity with Tkinter ydl_base_opts() (excluding for_info branch)
    tmpl = FILENAME_TEMPLATES.get(filename_template, "%(title)s.%(ext)s")
    # Tkinter uses os.path.join(out_root, "%(extractor)s", template) when subfolders — not the in-template check
    if subfolders and not retry_mode:
        # Always prefix subfolder, even if template already has %(extractor)s — matches Tkinter logic exactly
        # Tkinter checks: if subfolders: outtmpl = out_root + extractor + template
        # For engine we respect that: join with extractor
        outtmpl = str(Path(out_dir) / "%(extractor)s" / tmpl)
    else:
        outtmpl = str(Path(out_dir) / tmpl)

    # Resolve audio_mode same as Tkinter
    audio_mode = None
    if output_format.startswith("MP3"):
        audio_mode = "mp3"
    elif output_format.startswith("M4A"):
        audio_mode = "m4a"

    opts: dict[str, Any] = {
        "format": _ydl_format(quality, output_format, h264, audio_language),
        "outtmpl": outtmpl,
        "noplaylist": not download_playlist,
        "quiet": True,
        "no_warnings": True,
        "logger": _CallbackLogger(log_callback),
        "socket_timeout": 20,
        "extractor_retries": 1,
        "file_access_retries": 1,
        "restrictfilenames": False,
        "windowsfilenames": True,
        "continuedl": True,
        "overwrites": retry_mode,  # Tkinter: False normal, True when retry
        "download_archive": str(archive_path()) if not retry_mode else None,
        "ignoreerrors": False,
        "retries": 3,
        "fragment_retries": 3,
    }
    # Modern YouTube requires a JS challenge runtime. These options are harmless
    # for non-YouTube extractors and make the desktop app behave like current yt-dlp.
    opts.update(_youtube_runtime_opts())

    # Clean None download_archive
    if opts["download_archive"] is None:
        opts.pop("download_archive", None)

    if download_playlist and playlist_items.strip():
        opts["playlist_items"] = playlist_items.strip()

    # Audio branch vs video branch — parity
    if audio_mode == "mp3":
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
        # audio: no merge_output_format, no postprocessor_args
    elif audio_mode == "m4a":
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a", "preferredquality": "192"}]
    else:
        opts["merge_output_format"] = "mp4"
        opts["postprocessor_args"] = {"merger": ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k"]}
        opts["format_sort"] = ["vcodec:h264", "res", "br"] if h264 else ["res", "br"]

    limit = SPEED_PRESETS.get(speed_limit, 0)
    if limit:
        opts["ratelimit"] = limit

    ff = find_ffmpeg()
    if ff:
        opts["ffmpeg_location"] = str(Path(ff).parent) if Path(ff).is_file() else ff

    if cookies_file and Path(cookies_file).exists():
        opts["cookiefile"] = cookies_file
    # Optional browser-session auth is injected by the retry logic only.
    # Do not read browser cookies for every download by default.
    if proxy:
        opts["proxy"] = proxy
    # IMPORTANT: subtitle fetching is deliberately decoupled from media download.
    # YouTube/other subtitle endpoints can 429 independently; media must not be
    # retried or marked failed because optional subtitles were rate-limited.
    if thumbnail and not audio_mode:
        opts["writethumbnail"] = True
        # Keep cover images out of the video list. Explorer may show an
        # unassociated WebP/JPEG as a generic blank-page icon beside the video.
        current_outtmpl = opts["outtmpl"]
        opts["outtmpl"] = {
            "default": current_outtmpl,
            "thumbnail": "%(title)s [%(id)s].%(ext)s",
        }
        paths = dict(opts.get("paths") or {})
        paths["thumbnail"] = str(Path(out_dir) / "Thumbnails")
        opts["paths"] = paths

    if progress_hook is not None:
        opts["progress_hooks"] = [progress_hook]
    return opts


def fetch_metadata(url: str) -> dict[str, Any]:
    import yt_dlp
    opts: dict[str, Any] = {"quiet": True, "no_warnings": True, "skip_download": True}
    opts.update(_youtube_runtime_opts())
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if not isinstance(info, dict):
            return {"url": url, "title": url}
        return info


def _looks_like_subtitle_failure(exc: Exception) -> bool:
    msg = str(exc).lower()
    return (
        "unable to download video subtitles" in msg
        or "unable to download subtitles" in msg
    )


def _subtitle_output_options(
    options: dict[str, Any],
    subtitles_dir: Path,
) -> dict[str, Any]:
    """Keep subtitle sidecars separate from videos and uniquely identify them."""
    result = dict(options)
    current = result.get("outtmpl")
    templates = dict(current) if isinstance(current, dict) else {"default": current}
    templates["subtitle"] = "%(title)s [%(id)s].%(ext)s"
    result["outtmpl"] = templates
    paths = dict(result.get("paths") or {})
    paths["subtitle"] = str(subtitles_dir)
    result["paths"] = paths
    return result


def download_one(
    idx: int,
    url: str,
    config: dict[str, Any],
    progress_hook=None,
    retry_mode: bool = False,
    log_callback=None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    import yt_dlp
    root_out_dir = config.get("out_dir") or str(Path.home() / "Downloads")
    Path(root_out_dir).mkdir(parents=True, exist_ok=True)
    out_dir = str(_download_output_dir(url, config))
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    observed_output_paths: set[str] = set()

    def hook(d):
        for key in ("filename", "tmpfilename"):
            value = d.get(key)
            if value:
                observed_output_paths.add(str(value))
        if cancel_check and cancel_check():
            # yt-dlp understands this exception and stops the current transfer
            # without turning a user-requested cancellation into a generic error.
            raise yt_dlp.utils.DownloadCancelled("Dibatalkan oleh pengguna")
        if progress_hook and d.get("status") in {"downloading", "finished"}:
            try:
                progress_hook(d)
            except Exception:
                pass

    output_format = str(config.get("output_format", "MP4 (Video + Audio)"))
    wants_audio_only = output_format.startswith(("MP3", "M4A"))

    opts = build_ydl_opts(
        out_dir=out_dir,
        quality=config.get("quality", "Terbaik yang tersedia"),
        output_format=output_format,
        h264=bool(config.get("h264", True)),
        filename_template=config.get("filename_template", "[Platform] Judul"),
        # The URL-derived platform folder is deterministic across extractors
        # (e.g. TikTok versus a redirector), so yt-dlp must not add another
        # extractor-named level here.
        subfolders=False,
        speed_limit=config.get("speed_limit", "Tidak terbatas"),
        cookies_file=config.get("cookies_file", ""),
        browser_cookie_source=config.get("browser_cookie_source", "Auto"),
        proxy=config.get("proxy", ""),
        subtitles=False,  # subtitles are a post-success optional task
        subtitle_langs=config.get("subtitle_langs", "id,en"),
        thumbnail=bool(config.get("thumbnail")),
        audio_language=config.get("audio_language", "Auto — Indonesia jika tersedia"),
        download_playlist=bool(config.get("download_playlist")),
        playlist_items=config.get("playlist_items", ""),
        progress_hook=hook,
        retry_mode=retry_mode,
        log_callback=log_callback,
    )

    def _extract_with(current_opts: dict[str, Any]):
        with yt_dlp.YoutubeDL(current_opts) as ydl:
            return ydl, ydl.extract_info(url, download=True)

    # Keep a small, intentional retry matrix. Avoid exploding every base option
    # into every browser/client combination (which created noisy duplicate errors).
    attempts: list[dict[str, Any]] = [opts]
    if _is_youtube_url(url):
        # Progressive single-file fallback is especially valuable under current
        # YouTube GVS/PO-token 403 behavior; it guarantees video+audio in one URL
        # when such a format is exposed by the client. See yt-dlp PO Token guide.
        prog = dict(opts)
        prog["format"] = _progressive_video_format(config.get("quality", "Terbaik yang tersedia"))
        attempts.append(prog)
        for client, progressive in (("web_safari", True), ("web_embedded", True), ("android_vr", True)):
            attempts.append(_youtube_client_options(opts, client, progressive=progressive, quality=config.get("quality", "Terbaik yang tersedia")))

    # Auth/browser fallbacks are appended only once and only after the non-cookie
    # attempts above. This avoids the old behavior of trying Chrome cookies for
    # every subtitle/client variation.
    if _platform_uses_login_fallback(url) and not opts.get("cookiefile"):
        for browser in _browser_candidates(config.get("browser_cookie_source", "Auto")):
            if not _browser_has_cookie_database(browser):
                if log_callback:
                    try:
                        log_callback(
                            f"Cookie {browser} dilewati: profil/database cookies tidak ditemukan."
                        )
                    except Exception:
                        pass
                continue
            if _browser_process_running(browser):
                if log_callback:
                    try:
                        log_callback(f"Browser {browser} sedang berjalan; sesi cookie dilewati untuk menghindari database terkunci. Tutup browser sepenuhnya atau gunakan FILE COOKIES.")
                    except Exception:
                        pass
                continue
            fallback = dict(opts)
            # Python API form: (browser, profile, keyring, container)
            profile = str(config.get("browser_cookie_profile", "") or "").strip() or None
            fallback["cookiesfrombrowser"] = (browser, profile, None, None)
            fallback["overwrites"] = True
            fallback["continuedl"] = False
            attempts.append(fallback)

    last_exc: Exception | None = None
    last_non_cookie_exc: Exception | None = None
    recovery_attempted = False
    attempt_no = 0
    successful_opts: dict[str, Any] | None = None
    successful_info: dict[str, Any] | None = None
    successful_ydl = None

    while attempt_no < len(attempts):
        if cancel_check and cancel_check():
            raise yt_dlp.utils.DownloadCancelled("Dibatalkan oleh pengguna")
        current_opts = attempts[attempt_no]
        try:
            ydl, info = _extract_with(current_opts)
            successful_opts = current_opts
            successful_info = info if isinstance(info, dict) else None
            successful_ydl = ydl
            title = ""
            fp = ""
            is_playlist = False
            if isinstance(info, dict):
                if info.get("_type") == "playlist" and info.get("entries"):
                    is_playlist = True
                    title = info.get("title") or url
                    fp = out_dir
                else:
                    title = info.get("title") or url
                    if info.get("filepath"):
                        fp = str(info.get("filepath"))
                    if not fp:
                        try:
                            fp = ydl.prepare_filename(info)
                        except Exception:
                            fp = ""
                    # Do not prefer requested_downloads component paths: those
                    # are often separate video/audio inputs before the merger.

            if wants_audio_only or is_playlist:
                if not is_playlist and fp and Path(fp).is_file():
                    _cleanup_partial_artifacts({fp})
                result = {"idx": idx, "url": url, "title": title or url, "filepath": fp, "platform": detect_platform(url)}
                # Optional subtitles after media success for video jobs.
                if not wants_audio_only and bool(config.get("subtitles")) and successful_info:
                    subtitle_config = dict(config)
                    subtitle_config["out_dir"] = out_dir
                    _download_subtitles_optional(
                        url, successful_opts or opts, subtitle_config, log_callback=log_callback
                    )
                return result

            # Strict final media guard: video mode must have BOTH video and audio.
            candidates: list[Path] = []
            if fp:
                candidates.append(Path(fp))
                p = Path(fp)
                if p.suffix.lower() != ".mp4":
                    candidates.append(p.with_suffix(".mp4"))
            existing = next((c for c in candidates if c.is_file()), None)
            if existing is not None and _media_has_audio_video(str(existing)):
                fp = str(existing)
                if bool(config.get("h264", True)):
                    fp = _ensure_windows_compatible_mp4(fp, log_callback=log_callback)
                if bool(config.get("subtitles")):
                    subtitle_config = dict(config)
                    subtitle_config["out_dir"] = out_dir
                    _download_subtitles_optional(
                        url, successful_opts or opts, subtitle_config, log_callback=log_callback
                    )
                _cleanup_partial_artifacts({fp})
                return {"idx": idx, "url": url, "title": title or url, "filepath": fp, "platform": detect_platform(url)}

            # If yt-dlp returned success but there is no final AV artifact, force
            # one safe recovery attempt using a progressive video+audio format.
            if not recovery_attempted:
                recovery_attempted = True
                if fp:
                    _clear_stale_partials(fp)
                recovery = dict(opts)
                recovery["format"] = _progressive_video_format(config.get("quality", "Terbaik yang tersedia"))
                recovery.pop("download_archive", None)
                recovery["overwrites"] = True
                recovery["continuedl"] = False
                attempts.append(recovery)
                if log_callback:
                    try: log_callback("Output akhir tidak berisi video+audio lengkap; mencoba format progressive aman.")
                    except Exception: pass
                attempt_no += 1
                continue
            raise RuntimeError("Download ditolak: file akhir harus memiliki video dan audio. Chopster tidak menandai partial/audio-only sebagai berhasil.")
        except Exception as exc:
            last_exc = exc
            cookie_fail = _looks_like_browser_cookie_setup_failure(exc)
            if not cookie_fail:
                last_non_cookie_exc = exc
            if _looks_like_login_required(exc) and attempt_no + 1 < len(attempts):
                _cleanup_partial_artifacts(observed_output_paths)
                if log_callback:
                    try: log_callback("Situs membutuhkan sesi login; mencoba jalur autentikasi yang tersedia...")
                    except Exception: pass
                attempt_no += 1
                continue
            if _is_tiktok_webpage_challenge(url, exc) and attempt_no + 1 < len(attempts):
                _cleanup_partial_artifacts(observed_output_paths)
                if log_callback:
                    try:
                        log_callback(
                            "TikTok mengembalikan challenge halaman; mencoba fallback sesi browser yang tersedia..."
                        )
                    except Exception:
                        pass
                attempt_no += 1
                continue
            if cookie_fail and attempt_no + 1 < len(attempts):
                # Do not let DPAPI/cookie-db errors replace the actual site error.
                _cleanup_partial_artifacts(observed_output_paths)
                attempt_no += 1
                continue
            if _looks_like_youtube_client_failure(exc) and attempt_no + 1 < len(attempts):
                _cleanup_partial_artifacts(observed_output_paths)
                attempt_no += 1
                continue
            _cleanup_partial_artifacts(observed_output_paths)
            raise
        attempt_no += 1

    if last_non_cookie_exc is not None:
        _cleanup_partial_artifacts(observed_output_paths)
        # Give a useful authentication hint if browser extraction was the last
        # thing that failed after the actual site access was already rejected.
        if last_exc is not None and _looks_like_browser_cookie_setup_failure(last_exc):
            raise RuntimeError(
                f"Sumber menolak akses/download: {last_non_cookie_exc}. "
                "Post ini mensyaratkan sesi login. Chopster tidak berhasil membaca cookie browser "
                "(profil tidak ditemukan atau Windows DPAPI menolak dekripsi). Pastikan akun sudah "
                "login di browser, tutup browser sepenuhnya lalu coba lagi, atau pilih file cookies "
                "format Netscape dari Settings. Tanpa cookie login yang valid, post terbatas tidak "
                "dapat diunduh."
            ) from last_exc
        raise last_non_cookie_exc
    assert last_exc is not None
    _cleanup_partial_artifacts(observed_output_paths)
    raise last_exc


def _download_subtitles_optional(
    url: str,
    successful_opts: dict[str, Any],
    config: dict[str, Any],
    *,
    log_callback=None,
) -> None:
    """Fetch optional subtitles after media succeeds; subtitle HTTP 429 never fails media."""
    import yt_dlp
    langs = [s.strip() for s in (config.get("subtitle_langs") or "id,en").split(",") if s.strip()] or ["id", "en", "en-US"]
    sub_opts = dict(successful_opts)
    # The media URL has already been recorded in yt-dlp's archive. Remove that
    # guard for the separate subtitle pass, or yt-dlp may skip the URL entirely.
    sub_opts.pop("download_archive", None)
    out_dir = config.get("out_dir") or str(Path.home() / "Downloads")
    subtitles_dir = Path(out_dir) / "Subtitles"
    before = {
        path for path in subtitles_dir.rglob("*")
        if path.is_file()
    } if subtitles_dir.is_dir() else set()
    sub_opts = _subtitle_output_options(sub_opts, subtitles_dir)
    sub_opts["skip_download"] = True
    sub_opts["writesubtitles"] = True
    sub_opts["writeautomaticsub"] = True
    sub_opts["subtitleslangs"] = langs
    sub_opts["ignoreerrors"] = True
    sub_opts["quiet"] = True
    sub_opts["no_warnings"] = False
    try:
        with yt_dlp.YoutubeDL(sub_opts) as ydl:
            ydl.extract_info(url, download=True)
        created = [
            path for path in subtitles_dir.rglob("*")
            if path.is_file() and path not in before
        ] if subtitles_dir.is_dir() else []
        for path in created:
            try:
                if path.stat().st_size == 0:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
        created = [
            path for path in created
            if path.is_file() and path.stat().st_size > 0
        ]
        if log_callback:
            try:
                if created:
                    log_callback(
                        f"Subtitle opsional tersimpan: {len(created)} file di folder Subtitles."
                    )
                else:
                    log_callback(
                        "Subtitle opsional: tidak ada file baru (track tidak tersedia atau sudah ada)."
                    )
            except Exception: pass
    except Exception as exc:
        text = _clean_error(str(exc))
        low = text.lower()
        if "429" in low or "unable to download" in low or "subtitle" in low:
            if log_callback:
                try: log_callback(f"Subtitle opsional dilewati: {text}")
                except Exception: pass
            return
        if log_callback:
            try: log_callback(f"Subtitle opsional gagal, video tetap aman: {text}")
            except Exception: pass


def download_batch(
    urls: list[str],
    config: dict[str, Any],
    *,
    parallel: int = 2,
    on_progress: ProgressCb | None = None,
    on_row: RowCb | None = None,
    on_log: LogCb | None = None,
    cancel_flag: Callable[[], bool] | None = None,
    db=None,
    retry_mode: bool = False,
    skip_urls: set[str] | None = None,
) -> tuple[int, int]:
    """Batch with Tkinter-parity: skip handling, audio check, disk check delegated to caller."""
    import yt_dlp

    fmt = str(config.get("output_format", "MP4 (Video + Audio)"))
    missing_ffmpeg = not find_ffmpeg()
    missing_ffprobe = _requires_ffprobe(fmt) and not find_ffprobe()
    if missing_ffmpeg or missing_ffprobe:
        msg = (
            "Komponen media belum lengkap. Semua format memerlukan ffmpeg; "
            "mode video juga memerlukan ffprobe untuk validasi audio/video. "
            "Pasang ffmpeg.exe dan, untuk video, ffprobe.exe di PATH atau di sebelah aplikasi."
        )
        if on_log:
            try:
                on_log(msg)
            except Exception:
                pass
        for i, u in enumerate(urls, 1):
            if on_row:
                try:
                    on_row(i, detect_platform(u), u, "Gagal: komponen media belum lengkap")
                except Exception:
                    pass
            if db is not None:
                try:
                    db.add_history(u, detect_platform(u), u, "Gagal", "", error=msg)
                except Exception:
                    pass
        return 0, len(urls)

    runtime = _find_js_runtime()
    youtube_urls = [u for u in urls if _is_youtube_url(u) and u not in (skip_urls or set())]
    blocked_urls: dict[str, str] = {}
    tiktok_urls = [
        u for u in urls
        if detect_platform(u) == "TikTok" and u not in (skip_urls or set())
    ]
    if tiktok_urls:
        try:
            import curl_cffi  # noqa: F401
        except ImportError:
            msg = (
                "Dukungan impersonation TikTok belum terpasang. "
                "Bangun/instal Chopster dengan yt-dlp[default,curl-cffi], lalu coba lagi."
            )
            if on_log:
                try:
                    on_log(msg)
                except Exception:
                    pass
            for i, u in enumerate(urls, 1):
                if u not in tiktok_urls:
                    continue
                blocked_urls[u] = msg
                if on_row:
                    try:
                        on_row(i, detect_platform(u), u, "Gagal: dukungan curl-cffi belum terpasang")
                    except Exception:
                        pass
                if db is not None:
                    try:
                        db.add_history(u, detect_platform(u), u, "Gagal", "", error=msg)
                    except Exception:
                        pass
    if youtube_urls:
        try:
            import yt_dlp_ejs  # noqa: F401
        except ImportError:
            msg="Paket yt-dlp-ejs belum terpasang/versinya tidak cocok. Jalankan install_dependencies.bat lalu ulangi."
            if on_log:
                try:on_log(msg)
                except Exception:pass
            for i,u in enumerate(urls,1):
                if u in (skip_urls or set()) or u not in youtube_urls: continue
                blocked_urls[u] = msg
                if on_row:
                    try:on_row(i,detect_platform(u),u,"Gagal: yt-dlp-ejs belum terpasang")
                    except Exception:pass

    if youtube_urls and runtime is None:
        log_msg = (
            "YouTube siap tetapi JavaScript runtime belum ditemukan. "
            "yt-dlp saat ini membutuhkan Deno >=2.3 (disarankan) atau Node >=22 "
            "untuk challenge JavaScript. Jalankan install_dependencies.bat lalu "
            "pasang Deno/Node, atau letakkan deno.exe/node.exe di folder Chopster."
        )
        if on_log:
            try: on_log(log_msg)
            except Exception: pass
        # Fail fast instead of appearing stuck while yt-dlp retries extraction.
        for i, u in enumerate(urls, 1):
            if u in (skip_urls or set()) or u not in youtube_urls:
                continue
            blocked_urls[u] = log_msg
            if on_row:
                try: on_row(i, detect_platform(u), u, "Gagal: JS runtime YouTube tidak ditemukan")
                except Exception: pass
            if db is not None:
                try: db.add_history(u, detect_platform(u), u, "Gagal", "", error=log_msg)
                except Exception: pass
    if youtube_urls and on_log and runtime is not None:
        try: on_log(f"YouTube: JS runtime aktif ({runtime[0]}: {runtime[1]})")
        except Exception: pass

    total = len(urls)
    ok = 0
    fail = len(blocked_urls)
    skipped = skip_urls or set()
    progress_lock = Lock()
    progress_by_idx: dict[int, float] = {
        i: (100.0 if urls[i - 1] in skipped or urls[i - 1] in blocked_urls else 0.0)
        for i in range(1, total + 1)
    }
    progress_emit_state: dict[int, tuple[float, float]] = {}

    def emit_overall(idx: int, item_pct: float, message: str):
        if not on_progress:
            return
        now = time.monotonic()
        previous_pct, previous_at = progress_emit_state.get(idx, (-1.0, 0.0))
        item_pct = max(0.0, min(100.0, float(item_pct)))
        # yt-dlp can emit dozens of hooks per second. Throttling UI updates keeps
        # the interface responsive without changing transfer speed or accuracy.
        if item_pct < 100.0 and abs(item_pct - previous_pct) < 0.5 and now - previous_at < 0.18:
            return
        progress_emit_state[idx] = (item_pct, now)
        with progress_lock:
            progress_by_idx[idx] = item_pct
            overall = sum(progress_by_idx.values()) / max(1, total)
        try:on_progress(int(max(0,min(100,round(overall)))), message)
        except Exception:pass

    # Pre-add waiting rows (skip tag handled via on_log)
    if on_row:
        for i, u in enumerate(urls, 1):
            if u in skipped:
                try:
                    on_row(i, detect_platform(u), u, "Dilewati (duplikat)")
                except Exception:
                    pass
            elif u not in blocked_urls:
                try:
                    on_row(i, detect_platform(u), u, "Menghubungkan...")
                except Exception:
                    pass

    parallel = max(1, min(3, int(parallel or 1)))

    def log(msg: str):
        if on_log:
            try:
                on_log(msg)
            except Exception:
                pass

    def task(idx_url):
        idx, url = idx_url
        if url in skipped:
            return (idx, url, True, "Dilewati", "")
        if cancel_flag and cancel_flag():
            if on_row:
                try:
                    on_row(idx, detect_platform(url), url, "Dibatalkan")
                except Exception:
                    pass
            return (idx, url, False, "Dibatalkan", "")

        try:
            # Make the UI visibly alive before yt-dlp emits its first byte counter.
            emit_overall(idx, 0.0, f"Menghubungkan {idx}/{total} …")
            def hook(d):
                status = d.get("status")
                if status == "downloading":
                    pct = None
                    raw_pct = str(d.get("_percent_str") or d.get("_progress_str") or "")
                    try:
                        m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", raw_pct)
                        if m:
                            pct = float(m.group(1).replace(",", "."))
                    except Exception:
                        pct = None
                    if pct is None:
                        downloaded = d.get("downloaded_bytes") or 0
                        total_b = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                        if total_b:
                            pct = float(downloaded) / float(total_b) * 100.0
                        else:
                            # Some extractors cannot know the final size. Keep the
                            # UI visibly moving; label this as preparation/indeterminate
                            # rather than pretending it is an exact byte percentage.
                            pct = 0.0
                    extra = ""
                    speed = ANSI_RE.sub("", d.get("_speed_str") or "").strip()
                    eta = ANSI_RE.sub("", d.get("_eta_str") or "").strip()
                    if speed and "N/A" not in speed: extra += f" • {speed}"
                    if eta and "N/A" not in eta: extra += f" • ETA {eta}"
                    emit_overall(idx,pct,f"Mengunduh {idx}/{total} — {pct:.0f}%{extra}")
                elif status == "finished":
                    emit_overall(idx,100.0,f"Memproses hasil {idx}/{total}…")

            # Surface extraction/post-processing immediately; the previous build
            # hid yt-dlp's logger, making a stalled YouTube challenge look like
            # an item stuck forever in "Menunggu".
            if on_row:
                try: on_row(idx, detect_platform(url), url, "Menghubungkan...")
                except Exception: pass
            # Pass retry_mode through so opts differ parity-wise.
            res = download_one(
                idx, url, config, progress_hook=hook, retry_mode=retry_mode,
                log_callback=log, cancel_check=cancel_flag,
            )
            title = res.get("title") or url
            fp = res.get("filepath") or ""
            plat = res.get("platform") or detect_platform(url)
            if url in skipped:
                if on_row:
                    try:
                        on_row(idx, plat, title, "Dilewati")
                    except Exception:
                        pass
                return (idx, url, True, "Dilewati", fp)
            if on_row:
                try:
                    on_row(idx, plat, title, "Berhasil")
                except Exception:
                    pass
            if db is not None:
                try:
                    db.add_history(url, plat, title, "Berhasil", fp, format_sel=fmt)
                except Exception:
                    pass
            return (idx, url, True, "Berhasil", fp)
        except Exception as exc:
            raw = str(exc).strip()
            # Parity: Tkinter treats "already been recorded in archive" as skipped not fail
            if "already been recorded in the archive" in raw.lower() or "already been downloaded" in raw.lower():
                plat = detect_platform(url)
                if on_row:
                    try:
                        on_row(idx, plat, url, "Dilewati (arsip)")
                    except Exception:
                        pass
                log(f"Dilewati (sudah di arsip): {url}")
                return (idx, url, True, "Dilewati", "")
            # Check cancellation
            if isinstance(exc, yt_dlp.utils.DownloadCancelled) or "DownloadCancelled" in type(exc).__name__:
                if on_row:
                    try:
                        on_row(idx, detect_platform(url), url, "Dibatalkan")
                    except Exception:
                        pass
                return (idx, url, False, "Dibatalkan", "")
            # Missing/blocked optional subtitles are not a video-download failure.
            # yt-dlp can report this after the media itself is available; keep the
            # row successful and explain that subtitles were simply skipped.
            if _looks_like_subtitle_failure(exc):
                msg = "Subtitle tidak tersedia/ditolak sumber — video tetap berhasil diunduh."
                if on_row:
                    try:
                        on_row(idx, detect_platform(url), url, "Berhasil • subtitle dilewati")
                    except Exception:
                        pass
                if db is not None:
                    try:
                        db.add_history(url, detect_platform(url), url, "Berhasil", "", format_sel=fmt, error=msg)
                    except Exception:
                        pass
                log(f"Info {url}: {msg}")
                return (idx, url, True, "Berhasil", "")
            msg = _clean_error(raw)[:500]
            if on_row:
                try:
                    on_row(idx, detect_platform(url), url, f"Gagal: {msg[:80]}")
                except Exception:
                    pass
            if db is not None:
                try:
                    db.add_history(url, detect_platform(url), url, "Gagal", "", error=msg)
                except Exception:
                    pass
            log(f"Gagal {url}: {msg}")
            return (idx, url, False, msg, "")

    # Remove skipped from actual parallel work. They remain visible in the row
    # statuses, but must not be reported as successful downloads.
    active = [
        (i, u) for i, u in enumerate(urls, 1)
        if u not in skipped and u not in blocked_urls
    ]
    if not active:
        return ok, fail

    with ThreadPoolExecutor(max_workers=parallel) as ex:
        futs = {ex.submit(task, pair): pair[0] for pair in active}
        done = 0
        for fut in as_completed(futs):
            done += 1
            try:
                idx, url, success, msg, fp = fut.result()
                if msg == "Dibatalkan":
                    pass
                elif success:
                    ok += 1
                else:
                    fail += 1
            except Exception as exc:
                fail += 1
                log(f"Error: {exc}")
            # Emit overall done count including skipped
            if on_progress and not (cancel_flag and cancel_flag()):
                try:
                    on_progress(int((len(skipped) + done) / total * 100), f"Selesai {len(skipped)+done}/{total}")
                except Exception:
                    pass
        # Let queued tasks observe the cancel flag and update their rows. Cancelling
        # futures here would leave "Menghubungkan..." rows and count CancelledError
        # as a download failure.

    return ok, fail

# Backward-compatible alias for callers that used old signature
archive_path_for_test = archive_path
