import asyncio
import json
import logging
import math
import os
import re
import uuid
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from google import genai
from google.genai import types
from pathlib import Path

from chopster.auto_clip_studio.engine.config import (
    TEMP_DIR,
    UPLOADS_DIR,
    compute_audio_energy_heatmap,
    get_video_file_metadata,
    logger,
    transcribe_local_video_file,
    download_youtube_audio_for_transcription,
)
from chopster.auto_clip_studio.engine.schemas.analyze import (
    AnalyzeRequest,
    AnalyzeResponse,
    HeatmapPoint,
    TranscriptLine,
    VideoAnalysis,
    ViralClip,
)
from chopster.auto_clip_studio.engine.services.ai_service import (
    KNOWN_FLASH_MODELS,
    get_flash_models_for_key,
    list_available_gemini_models,
)
from chopster.auto_clip_studio.engine.global_ai import resolve_ai_source
from chopster.auto_clip_studio.engine.services.gdrive_service import (
    download_google_drive_video,
    gdrive_video_id,
    is_google_drive_url,
)
from chopster.auto_clip_studio.engine.services.youtube_service import (
    fetch_transcript,
    fetch_video_metadata,
    get_supadata_keys,
    get_supadata_usage_data,
)
from chopster.auto_clip_studio.engine.utils.heatmap import get_average_heatmap_value
from chopster.auto_clip_studio.engine.utils.proxy import get_proxy_url
from chopster.auto_clip_studio.engine.utils.sse import _sse
from chopster.auto_clip_studio.engine.utils.text import (
    detect_transcript_language,
    extract_video_id,
    lowercase_hashtags_in_string,
    parse_manual_subtitles,
    resolve_language_override,
    sanitize_first_person_title,
)

router = APIRouter(tags=["Analyze"])

# ── AI fallback bounds ───────────────────────────────────────────────────────
# A single retired/busy Gemini model used to be retried for minutes (a hung
# request took ~140s), so the analysis never finished. These caps keep the
# whole AI step predictable: per-call timeout, total deadline, model cap, and
# an in-process cache of models that answered 404 (retired for this key).
AI_CALL_TIMEOUT_SECONDS = int(os.environ.get("ACS_AI_CALL_TIMEOUT_S", "60") or 60)
AI_ANALYSIS_DEADLINE_SECONDS = int(os.environ.get("ACS_AI_DEADLINE_S", "180") or 180)
MAX_MODELS_PER_ANALYSIS = int(os.environ.get("ACS_AI_MAX_MODELS", "5") or 5)


def _retired_cache_path() -> Path:
    try:
        from chopster.app.paths import user_data_dir
        base = Path(user_data_dir())
    except Exception:
        base = Path(os.environ.get("TEMP") or ".")
    return base / "acs_retired_models.json"


def _load_retired_models() -> set[str]:
    try:
        path = _retired_cache_path()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return {str(m) for m in data if str(m).strip()}
    except Exception:
        pass
    return set()


def _save_retired_models(models: set[str]) -> None:
    try:
        path = _retired_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(models), ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# Retired-for-this-key models, remembered across app restarts so the doomed
# "gemini-2.5-flash → 404" attempt does not repeat on every launch.
_UNAVAILABLE_MODELS: set[str] = _load_retired_models()


# Only files inside the app-managed media directories may be analyzed. This
# mirrors routers/media.py::_is_safe_path. A bare `os.path.exists(request.url)`
# check previously let a caller point ffprobe/Whisper at ANY readable file on
# disk and stream its transcript back.
_ANALYZE_ALLOWED_ROOTS = (UPLOADS_DIR, TEMP_DIR)


def _is_allowed_analyze_path(candidate: str) -> bool:
    """True only when `candidate` resolves inside an allowed media directory."""
    try:
        resolved = Path(candidate).resolve()
    except (OSError, ValueError):
        return False
    for root in _ANALYZE_ALLOWED_ROOTS:
        try:
            root_resolved = root.resolve()
        except (OSError, ValueError):
            continue
        if resolved == root_resolved or resolved.is_relative_to(root_resolved):
            return True
    return False


def _coerce_model_text(value) -> str:
    """Normalize text fields from AI providers before string-only processing."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return " ".join(
            text for item in value
            if (text := _coerce_model_text(item))
        )
    if isinstance(value, dict):
        for key in ("text", "content", "value"):
            if key in value:
                text = _coerce_model_text(value[key])
                if text:
                    return text
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def _coerce_model_text_list(value) -> list[str]:
    """Normalize quote fields to the list-of-strings expected by ViralClip."""
    values = value if isinstance(value, (list, tuple)) else [value]
    return [
        text for item in values
        if (text := _coerce_model_text(item))
    ]


def _coerce_model_int(value, default: int = 0) -> int:
    """Round numeric model output to the integer required by the response schema."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        match = re.search(r"[-+]?\d+(?:\.\d+)?", str(value or ""))
        if not match:
            return default
        try:
            number = float(match.group(0))
        except (TypeError, ValueError, OverflowError):
            return default
    try:
        return int(round(number))
    except (ValueError, OverflowError):
        return default


def _normalize_analysis_payload(payload) -> Optional[dict]:
    """Normalize common Gateway JSON shapes to {summary, clips}."""
    if isinstance(payload, list):
        if (
            len(payload) == 1
            and isinstance(payload[0], dict)
            and any(key in payload[0] for key in ("clips", "clip_candidates", "results"))
        ):
            payload = payload[0]
        else:
            # Some OpenAI-compatible models return the clip objects directly
            # instead of wrapping them in {"summary": ..., "clips": [...]}.
            payload = {"summary": "", "clips": payload}
    if not isinstance(payload, dict):
        return None

    nested = payload.get("data")
    if isinstance(nested, dict) and any(key in nested for key in ("clips", "clip_candidates", "results")):
        payload = nested

    clips = payload.get("clips")
    if clips is None:
        for key in ("clip_candidates", "recommendations", "results", "segments"):
            if key in payload:
                clips = payload[key]
                break
    if isinstance(clips, dict):
        nested_clips = next(
            (clips[key] for key in ("clips", "items", "results") if isinstance(clips.get(key), list)),
            None,
        )
        clips = nested_clips if nested_clips is not None else [clips]
    if clips is None:
        clips = []
    if not isinstance(clips, list):
        return None

    normalized_clips = []
    for item in clips:
        if isinstance(item, dict):
            normalized_clips.append(item)
        elif isinstance(item, list):
            normalized_clips.extend(clip for clip in item if isinstance(clip, dict))

    return {
        "summary": _coerce_model_text(
            _first_model_value(payload, "summary", "video_summary", "overview")
        ),
        "clips": normalized_clips,
    }


def _first_model_value(values: dict, *keys):
    for key in keys:
        if key in values and values[key] is not None:
            return values[key]
    return None


def _coerce_model_seconds(value) -> Optional[float]:
    """Read seconds from numeric or common timestamp-shaped model output."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, dict):
        for key in ("seconds", "time", "value"):
            if key in value:
                return _coerce_model_seconds(value[key])
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        text = str(value).strip()
        if ":" in text:
            try:
                parts = [float(part) for part in text.split(":")]
                if len(parts) == 2:
                    seconds = parts[0] * 60 + parts[1]
                elif len(parts) == 3:
                    seconds = parts[0] * 3600 + parts[1] * 60 + parts[2]
                else:
                    return None
            except (TypeError, ValueError, OverflowError):
                return None
        else:
            match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
            if not match:
                return None
            try:
                seconds = float(match.group(0))
            except (TypeError, ValueError, OverflowError):
                return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def _match_quote_to_transcript(quote: str, transcript: list[dict]) -> Optional[tuple[float, float, int]]:
    """Find an exact, normalized quote in timed transcript lines."""
    word_pattern = r"[^\W_]+"
    quote_tokens = re.findall(word_pattern, quote.casefold(), flags=re.UNICODE)
    if len(quote_tokens) < 3:
        return None

    timed_tokens = []
    for line in transcript:
        try:
            line_start = float(line.get("start", 0.0))
            line_end = float(line.get("end", 0.0))
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(line_start) or not math.isfinite(line_end) or line_end <= line_start:
            continue
        text = _coerce_model_text(line.get("text", ""))
        for token in re.findall(word_pattern, text.casefold(), flags=re.UNICODE):
            timed_tokens.append((token, line_start, line_end))

    width = len(quote_tokens)
    for index in range(0, len(timed_tokens) - width + 1):
        if [entry[0] for entry in timed_tokens[index:index + width]] == quote_tokens:
            return (
                timed_tokens[index][1],
                timed_tokens[index + width - 1][2],
                width,
            )
    return None


def _recover_clip_times(raw_clip: dict, transcript: list[dict], duration: str,
                        range_start: float, range_end: float) -> Optional[tuple[float, float]]:
    """Recover omitted/zero AI timestamps by matching key quotes to timed subtitles."""
    quotes = _coerce_model_text_list(raw_clip.get("key_quotes"))
    matches = [
        match for quote in quotes
        if (match := _match_quote_to_transcript(quote, transcript))
    ]
    if not matches:
        return None

    # Prefer the longest exact match. Nearby quotes from the same clip can widen
    # its boundaries, but distant quote matches should not create a huge segment.
    anchor = max(matches, key=lambda item: item[2])
    quote_start, quote_end, _ = anchor
    nearby = [
        item for item in matches
        if abs(item[0] - quote_start) <= 45.0
    ]
    if nearby:
        quote_start = min(item[0] for item in nearby)
        quote_end = max(item[1] for item in nearby)

    desired = {"15s": 15.0, "30s": 30.0, "60s": 60.0}.get(
        str(duration or "").lower(),
        max(30.0, quote_end - quote_start),
    )
    desired = min(max(desired, quote_end - quote_start), max(0.1, range_end - range_start))
    clip_start = max(range_start, quote_start - max(0.0, desired - (quote_end - quote_start)) / 2)
    clip_end = min(range_end, clip_start + desired)
    clip_start = max(range_start, clip_end - desired)

    # Snap outward to available subtitle sentence boundaries while keeping the
    # matched quote inside the recovered range.
    starts = []
    ends = []
    for line in transcript:
        try:
            line_start = float(line.get("start", 0.0))
            line_end = float(line.get("end", 0.0))
        except (TypeError, ValueError, OverflowError):
            continue
        if range_start <= line_start <= min(clip_start, quote_start):
            starts.append(line_start)
        if max(clip_end, quote_end) >= line_end >= quote_end:
            ends.append(line_end)
    if starts:
        clip_start = max(starts)
    if ends:
        clip_end = max(ends)
    if clip_end <= clip_start:
        return None
    return round(clip_start, 2), round(clip_end, 2)


@router.get("/api/health")
def health_check(refresh: bool = False):
    is_vercel = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
    keys = get_supadata_keys()
    proxy = get_proxy_url()
    has_gemini = bool(os.environ.get("GEMINI_API_KEY"))
    supadata_info = get_supadata_usage_data(force=refresh) if keys else {
        "total_keys": 0,
        "total_limit": 0,
        "total_used": 0,
        "total_remaining": 0,
        "usage_percent": 0.0,
        "active_keys": 0,
        "exhausted_keys": 0,
        "keys_detail": [],
        "status": "not_configured"
    }
    return {
        "status": "ok",
        "message": "Chopster Auto Clip Studio API is active",
        "is_vercel": is_vercel,
        "proxy_configured": bool(proxy),
        "gemini_env_configured": has_gemini,
        "supadata_keys_count": len(keys),
        "supadata": supadata_info
    }


@router.get("/api/supadata-usage")
def supadata_usage_endpoint(refresh: bool = False):
    """Returns real-time usage and remaining credit quota across all configured Supadata API keys."""
    return get_supadata_usage_data(force=refresh)


@router.get("/api/models")
def list_available_models(api_key: str = ""):
    """List models using a one-off dev key or the native DPAPI-stored ACS key."""
    if not api_key:
        try:
            from chopster.auto_clip_studio.engine.global_ai import _load_acs_key
            api_key = _load_acs_key()
        except Exception:
            api_key = ""
    models = list_available_gemini_models(api_key)
    return {"models": models}


@router.post("/api/analyze")
async def analyze_video(request: AnalyzeRequest):
    """Stream real-time progress via Server-Sent Events, then deliver the final result."""

    async def stream():
        # Priority: ACS-specific key -> global Google key -> global Gateway
        # (vision-verified) -> none. The global AI is only used when its model
        # is verified to support images; otherwise ACS shows the key hint.
        _resolved = resolve_ai_source(request.api_key or "", os.environ.get("GEMINI_API_KEY") or "")
        is_mock = bool(_resolved.get("is_mock"))
        gemini_key = str(_resolved.get("gemini_key") or "")
        gateway_ctx = _resolved.get("gateway")
        use_gateway = _resolved.get("source") == "gateway"

        if _resolved.get("source") == "none":
            yield _sse({
                "error": "Gemini API Key is required to analyze videos. Enter the Auto Clip Studio Gemini API key below "
                         "(Get free key at https://aistudio.google.com/apikey). The global AI in Settings is only used "
                         "when its model is verified to support images.",
                "status": 400
            })
            return

        # ── Step 1: Detect Source Type & Extract Metadata ───────────────────
        req_clean = request.url.strip()
        # User-selected output language. 'auto'/None keeps the default behaviour (match the
        # video's own spoken language); any ISO code forces titles, hooks, captions, hashtags
        # and the Whisper spoken-language hint to that language.
        language_override = resolve_language_override(request.language)
        is_uploaded = False
        is_gdrive = is_google_drive_url(req_clean)
        uploaded_file_path: Optional[Path] = None

        if is_gdrive:
            yield _sse({
                "step": 1,
                "step_progress": 10,
                "overall_progress": 5,
                "stage": "Connecting to Google Drive",
                "detail": "Resolving Google Drive sharing link & preparing download...",
                "message": "Connecting to Google Drive..."
            })
            loop = asyncio.get_running_loop()
            gdrive_queue: asyncio.Queue = asyncio.Queue()

            def gdrive_progress(stage: str, detail: str, step_pct: int = 30):
                loop.call_soon_threadsafe(gdrive_queue.put_nowait, {
                    "step": 1,
                    "step_progress": step_pct,
                    "overall_progress": min(22, 5 + int(step_pct * 0.17)),
                    "stage": stage,
                    "detail": detail,
                    "message": detail
                })

            try:
                task = asyncio.create_task(
                    asyncio.to_thread(download_google_drive_video, req_clean, gdrive_progress)
                )
                while not task.done():
                    try:
                        evt = await asyncio.wait_for(gdrive_queue.get(), timeout=0.2)
                        yield _sse(evt)
                    except asyncio.TimeoutError:
                        pass
                while not gdrive_queue.empty():
                    yield _sse(gdrive_queue.get_nowait())

                uploaded_file_path = await task
                is_uploaded = True
            except Exception as e:
                yield _sse({"error": f"Failed to fetch video from Google Drive: {str(e)}", "status": 400})
                return
        elif req_clean.startswith("upload_") or req_clean.startswith("/api/video/") or req_clean.startswith("file://") or _is_allowed_analyze_path(req_clean):
            is_uploaded = True
        elif (UPLOADS_DIR / os.path.basename(req_clean.split("?")[0])).exists():
            is_uploaded = True
        else:
            matches = list(UPLOADS_DIR.glob(f"*{req_clean}*"))
            if matches:
                is_uploaded = True

        if is_uploaded:
            if uploaded_file_path is None:
                if _is_allowed_analyze_path(req_clean):
                    uploaded_file_path = Path(req_clean)
                elif (UPLOADS_DIR / os.path.basename(req_clean.split("?")[0])).exists():
                    uploaded_file_path = UPLOADS_DIR / os.path.basename(req_clean.split("?")[0])
                elif (TEMP_DIR / os.path.basename(req_clean.split("?")[0])).exists():
                    uploaded_file_path = TEMP_DIR / os.path.basename(req_clean.split("?")[0])
                else:
                    matches = list(UPLOADS_DIR.glob(f"*{req_clean}*"))
                    if matches:
                        uploaded_file_path = matches[0]
                    else:
                        yield _sse({"error": "Uploaded video file not found on disk. Please upload again.", "status": 404})
                        return

            video_id = uploaded_file_path.stem
            if is_gdrive:
                # The stored file carries a random download tag; the UI derives the
                # Drive link and the history cache key from video_id, so keep the
                # identity stable (tag-free) while video_url keeps the real filename.
                video_id = gdrive_video_id(req_clean, uploaded_file_path.stem) or video_id
            canonical_url = req_clean if is_gdrive else f"/api/video/{uploaded_file_path.name}"
            video_url = f"/api/video/{uploaded_file_path.name}"
            source_type = "gdrive" if is_gdrive else "upload"

            yield _sse({
                "step": 1,
                "step_progress": 30,
                "overall_progress": 8,
                "stage": "Inspecting Video File",
                "detail": f"Reading streams & container metadata from {uploaded_file_path.name}...",
                "message": f"Inspecting video file: {uploaded_file_path.name}..."
            })

            try:
                metadata = await asyncio.to_thread(get_video_file_metadata, uploaded_file_path)
                title = metadata.get("title") or (
                    f"GDrive: {video_id[len('gdrive_'):]}" if is_gdrive else uploaded_file_path.stem
                )
                channel = "Google Drive" if is_gdrive else "Local Upload"
                duration = metadata.get("duration", 0.0)
                is_live = False
                live_status = "not_live"
                yield _sse({
                    "step": 1,
                    "step_progress": 100,
                    "overall_progress": 25,
                    "stage": "Video Verified",
                    "detail": f"Loaded video \"{title[:45]}\" ({int(duration)}s, {metadata.get('width')}x{metadata.get('height')})",
                    "message": f"Loaded video — \"{title[:45]}\" ({int(duration)}s)"
                })
            except Exception as e:
                yield _sse({"error": f"Failed to probe uploaded video metadata: {str(e)}", "status": 500})
                return

            logger.info(f"Local video loaded: title='{title}', duration={duration}s, path={uploaded_file_path}")

            # ── Step 2: Heatmap via acoustic energy ──────────────────────────
            yield _sse({
                "step": 2,
                "step_progress": 40,
                "overall_progress": 35,
                "stage": "Computing Acoustic Heatmap",
                "detail": "Analyzing audio energy envelope & speech intensity peaks...",
                "message": "Computing audio engagement curve..."
            })
            try:
                heatmap = await asyncio.to_thread(compute_audio_energy_heatmap, uploaded_file_path, duration)
                yield _sse({
                    "step": 2,
                    "step_progress": 100,
                    "overall_progress": 50,
                    "stage": "Acoustic Peaks Decoded",
                    "detail": f"Acoustic engagement heatmap parsed — {len(heatmap)} audience interest data points generated.",
                    "message": f"Acoustic energy heatmap loaded ({len(heatmap)} points)."
                })
            except Exception as e:
                logger.warning(f"Heatmap calculation error: {e}")
                heatmap = []
                yield _sse({
                    "step": 2,
                    "step_progress": 100,
                    "overall_progress": 50,
                    "stage": "Dialogue Fallback",
                    "detail": "Proceeding with full dialogue transcript analysis.",
                    "message": "Proceeding with speech transcription..."
                })

            # ── Step 3: Speech Recognition via Whisper AI ───────────────────
            if request.subtitles:
                yield _sse({
                    "step": 3,
                    "step_progress": 30,
                    "overall_progress": 55,
                    "stage": "Parsing Subtitles",
                    "detail": "Parsing custom SRT/TXT subtitle timestamps...",
                    "message": "Parsing manual subtitles..."
                })
                try:
                    transcript_lines = parse_manual_subtitles(request.subtitles, duration)
                    if not transcript_lines:
                        raise Exception("Custom subtitles parsed into empty array.")
                    yield _sse({
                        "step": 3,
                        "step_progress": 100,
                        "overall_progress": 70,
                        "stage": "Subtitles Ready",
                        "detail": f"Custom subtitles parsed — {len(transcript_lines)} timestamped lines loaded.",
                        "message": f"Custom subtitles parsed — {len(transcript_lines)} lines loaded successfully."
                    })
                except Exception as e:
                    yield _sse({"error": f"Failed to parse manual subtitles: {str(e)}", "status": 400})
                    return
            else:
                loop = asyncio.get_running_loop()
                progress_queue = asyncio.Queue()

                def whisper_progress(stage: str, detail: str, step_pct: int = 30):
                    loop.call_soon_threadsafe(progress_queue.put_nowait, {
                        "step": 3,
                        "step_progress": step_pct,
                        "overall_progress": min(68, 50 + int(step_pct * 0.2)),
                        "stage": stage,
                        "detail": detail,
                        "message": detail
                    })

                yield _sse({
                    "step": 3,
                    "step_progress": 25,
                    "overall_progress": 55,
                    "stage": "Transcribing with Whisper",
                    "detail": "Running OpenAI Whisper neural speech model on video audio track...",
                    "message": "Transcribing video dialogue with Whisper AI..."
                })

                try:
                    task = asyncio.create_task(
                        asyncio.to_thread(
                            transcribe_local_video_file,
                            uploaded_file_path,
                            whisper_progress,
                            language_override['code'] if language_override else None
                        )
                    )
                    while not task.done():
                        try:
                            evt = await asyncio.wait_for(progress_queue.get(), timeout=0.2)
                            yield _sse(evt)
                        except asyncio.TimeoutError:
                            pass

                    while not progress_queue.empty():
                        yield _sse(progress_queue.get_nowait())

                    transcript_lines = await task
                    if not transcript_lines:
                        if is_mock:
                            transcript_lines = [
                                {"text": "Hello and welcome to this video.",            "start":  0.0, "duration": 3.0},
                                {"text": "Today we are looking at how this app works.",  "start":  3.0, "duration": 4.0},
                                {"text": "It finds viral hotspots and highlights them.",  "start":  7.0, "duration": 4.0},
                                {"text": "This works locally on any uploaded file.",     "start": 11.0, "duration": 3.0},
                            ]
                        else:
                            yield _sse({
                                "error": "No spoken words were detected in this video file. Ensure the video contains clear audible speech.",
                                "status": 400
                            })
                            return

                    yield _sse({
                        "step": 3,
                        "step_progress": 100,
                        "overall_progress": 70,
                        "stage": "Dialogue Transcribed",
                        "detail": f"Whisper speech-to-text complete — {len(transcript_lines)} timestamped dialogue sentences ready.",
                        "message": f"Whisper transcribed {len(transcript_lines)} dialogue segments successfully."
                    })
                except Exception as e:
                    if is_mock:
                        transcript_lines = [
                            {"text": "Sample speech from uploaded mock video.", "start": 0.0, "duration": 4.0},
                            {"text": "Highlighting viral moments automatically.", "start": 4.0, "duration": 5.0}
                        ]
                    else:
                        yield _sse({"error": f"Whisper transcription failed: {str(e)}", "status": 500})
                        return

        else:
            # ── YouTube extraction path ──────────────────────────────────────
            source_type = "youtube"
            video_url = None
            video_id = extract_video_id(request.url)
            if not video_id:
                if not is_mock:
                    yield _sse({"error": "Invalid YouTube URL or file reference. Please check and try again.", "status": 400})
                    return
                video_id = "dQw4w9WgXcQ"

            canonical_url = f"https://www.youtube.com/watch?v={video_id}"

            yield _sse({
                "step": 1,
                "step_progress": 30,
                "overall_progress": 8,
                "stage": "Connecting to YouTube",
                "detail": "Connecting to YouTube & fetching video metadata...",
                "message": "Connecting to YouTube — fetching video title and duration..."
            })

            channel = ""
            spoken_lang_hint = ""
            # Resolved ONCE at the YouTube-scope level (NOT inside the transcript try block) so
            # the Whisper fallback below can always read it. It used to be assigned only inside a
            # `try:` that may fail, which made the fallback reference an unbound name (NameError)
            # and crash the whole analysis whenever subtitles were unavailable.
            preferred_transcript_lang = (
                language_override['code'] if language_override else (spoken_lang_hint or None)
            )
            try:
                metadata = await asyncio.to_thread(fetch_video_metadata, canonical_url, request.proxy)
                title    = metadata["title"]
                channel  = metadata.get("channel", "")
                duration = metadata["duration"]
                heatmap  = metadata.get("heatmap") or []
                is_live  = metadata.get("is_live", False)
                live_status = metadata.get("live_status", "not_live")
                # YouTube's own declared audio language (e.g. 'id'). Used as a spoken-language
                # hint for transcript selection and local Whisper, so an Indonesian video is not
                # silently captioned and hooked in English.
                spoken_lang_hint = str(metadata.get("audio_language") or "").strip().lower().split("-")[0]
                preferred_transcript_lang = (
                    language_override['code'] if language_override else (spoken_lang_hint or None)
                )
                yield _sse({
                    "step": 1,
                    "step_progress": 100,
                    "overall_progress": 25,
                    "stage": "Video Verified",
                    "detail": f"Loaded metadata for \"{title[:45]}\" ({int(duration)}s)",
                    "message": f"Connected — \"{title[:45]}\" ({int(duration)}s)"
                })
            except Exception as e:
                if is_mock:
                    title = "Mock YouTube Video"
                    channel = "Chopster"
                    duration = 212.0
                    heatmap = []
                    is_live = False
                    live_status = "not_live"
                    yield _sse({
                        "step": 1,
                        "step_progress": 100,
                        "overall_progress": 25,
                        "stage": "Video Verified",
                        "detail": "Loaded mock video metadata (212s)",
                        "message": "Mock video metadata loaded"
                    })
                else:
                    msg = e.detail if isinstance(e, HTTPException) else str(e)
                    yield _sse({"error": f"Failed to fetch video details: {msg}", "status": 500})
                    return

            logger.info(f"Metadata fetched: title='{title}', duration={duration}s, heatmap_pts={len(heatmap)}")

            # ── Step 2: Heatmap ──────────────────────────────────────────────────
            yield _sse({
                "step": 2,
                "step_progress": 40,
                "overall_progress": 35,
                "stage": "Scraping Retention",
                "detail": "Extracting viewer replay telemetry and retention curve...",
                "message": "Scraping player viewer retention curve..."
            })
            if heatmap:
                yield _sse({
                    "step": 2,
                    "step_progress": 100,
                    "overall_progress": 50,
                    "stage": "Retention Decoded",
                    "detail": f"Viewer retention heatmap loaded — {len(heatmap)} audience interest data points parsed.",
                    "message": f"Viewer retention heatmap loaded — {len(heatmap)} data points scraped."
                })
            else:
                yield _sse({
                    "step": 2,
                    "step_progress": 100,
                    "overall_progress": 50,
                    "stage": "Dialogue Fallback",
                    "detail": "No heatmap curve available — relying on full transcript dialogue analysis.",
                    "message": "No heatmap available for this video — will rely on transcript content analysis."
                })

            # ── Step 3: Transcript ───────────────────────────────────────────────
            if request.subtitles:
                yield _sse({
                    "step": 3,
                    "step_progress": 30,
                    "overall_progress": 55,
                    "stage": "Parsing Subtitles",
                    "detail": "Parsing custom SRT/TXT subtitle timestamps...",
                    "message": "Parsing manual subtitles..."
                })
                try:
                    transcript_lines = parse_manual_subtitles(request.subtitles, duration)
                    if not transcript_lines:
                        raise Exception("Custom subtitles parsed into empty array.")
                    yield _sse({
                        "step": 3,
                        "step_progress": 100,
                        "overall_progress": 70,
                        "stage": "Subtitles Ready",
                        "detail": f"Custom subtitles parsed — {len(transcript_lines)} timestamped lines loaded.",
                        "message": f"Custom subtitles parsed — {len(transcript_lines)} lines loaded successfully."
                    })
                except Exception as e:
                    yield _sse({"error": f"Failed to parse manual subtitles: {str(e)}", "status": 400})
                    return
            else:
                loop = asyncio.get_running_loop()
                progress_queue = asyncio.Queue()

                def progress_callback(stage: str, detail: str, step_pct: int = 30):
                    loop.call_soon_threadsafe(progress_queue.put_nowait, {
                        "step": 3,
                        "step_progress": step_pct,
                        "overall_progress": min(68, 50 + int(step_pct * 0.2)),
                        "stage": stage,
                        "detail": detail,
                        "message": detail
                    })

                # Initial stage event
                yield _sse({
                    "step": 3,
                    "step_progress": 25,
                    "overall_progress": 55,
                    "stage": "Fetching Subtitles",
                    "detail": "Initializing multi-tier subtitle extraction pipeline...",
                    "message": "Initializing multi-tier subtitle extraction pipeline..."
                })

                transcript_error = None
                try:
                    # Language preference for transcript tiers. `preferred_transcript_lang` was
                    # already resolved once at YouTube-scope level above (user's explicit choice
                    # wins, otherwise YouTube's declared audio language is used as a hint), so it
                    # is intentionally NOT reassigned here — the Whisper fallback reads the same
                    # scope-level value.
                    task = asyncio.create_task(
                        asyncio.to_thread(
                            fetch_transcript,
                            video_id,
                            request.proxy,
                            progress_callback,
                            preferred_transcript_lang
                        )
                    )

                    while not task.done():
                        try:
                            evt = await asyncio.wait_for(progress_queue.get(), timeout=0.2)
                            yield _sse(evt)
                        except asyncio.TimeoutError:
                            pass

                    while not progress_queue.empty():
                        yield _sse(progress_queue.get_nowait())

                    transcript_lines = await task
                    if not transcript_lines:
                        transcript_error = RuntimeError("YouTube did not provide an accessible subtitle track.")
                except Exception as e:
                    transcript_error = e

                if transcript_error is not None:
                    if is_mock:
                        transcript_lines = [
                            {"text": "Hello and welcome to this video.", "start": 0.0, "duration": 3.0},
                            {"text": "Today we are looking at how this app works.", "start": 3.0, "duration": 4.0},
                            {"text": "It finds viral hotspots and highlights them.", "start": 7.0, "duration": 4.0},
                            {"text": "Most people think it's magic.", "start": 11.0, "duration": 3.0},
                            {"text": "But it uses YouTube player heatmaps.", "start": 14.0, "duration": 4.0},
                            {"text": "And processes them with Gemini AI models.", "start": 18.0, "duration": 4.0},
                            {"text": "This is changing how editors crop videos.", "start": 22.0, "duration": 5.0},
                            {"text": "If you want to grow on TikTok, try it.", "start": 27.0, "duration": 5.0},
                            {"text": "We will explore the code next.", "start": 32.0, "duration": 3.0},
                        ]
                        yield _sse({
                            "step": 3, "step_progress": 100, "overall_progress": 70,
                            "stage": "Subtitles Ready", "detail": "Mock mode — 9 sample dialogue lines loaded.",
                            "message": "Mock mode — using sample transcript."
                        })
                    elif is_live or live_status in ('is_live', 'is_upcoming', 'post_live'):
                        yield _sse({
                            "error": (
                                "YouTube subtitles are unavailable while this video is live, upcoming, or "
                                "still processing after a live stream. Try again after processing completes."
                            ),
                            "status": 400
                        })
                        return
                    else:
                        temp_audio = TEMP_DIR / f"youtube_transcribe_{video_id}_{uuid.uuid4().hex[:10]}.mp3"

                        def download_progress(progress: dict):
                            percent = float(progress.get("percent") or 0)
                            loop.call_soon_threadsafe(progress_queue.put_nowait, {
                                "step": 3,
                                "step_progress": min(55, int(percent * 0.55)),
                                "overall_progress": 55 + min(10, int(percent * 0.10)),
                                "stage": "Downloading Audio for Local Transcription",
                                "detail": f"Downloading audio only ({percent:.0f}%) for local Whisper transcription…",
                                "message": f"Downloading audio only ({percent:.0f}%) for local transcription…"
                            })

                        try:
                            yield _sse({
                                "step": 3, "step_progress": 10, "overall_progress": 56,
                                "stage": "Downloading Audio for Local Transcription",
                                "detail": "No usable YouTube subtitles were found. Downloading only the audio for local transcription.",
                                "message": "No YouTube subtitles found — downloading audio only for local Whisper transcription."
                            })
                            download_task = asyncio.create_task(asyncio.to_thread(
                                download_youtube_audio_for_transcription, canonical_url, str(temp_audio), download_progress
                            ))
                            while not download_task.done():
                                try:
                                    yield _sse(await asyncio.wait_for(progress_queue.get(), timeout=0.2))
                                except asyncio.TimeoutError:
                                    pass
                            await download_task

                            yield _sse({
                                "step": 3, "step_progress": 60, "overall_progress": 65,
                                "stage": "Transcribing with Local Whisper",
                                "detail": "Audio downloaded. Extracting timestamped speech locally with Whisper…",
                                "message": "Transcribing the downloaded audio locally with Whisper…"
                            })

                            def whisper_progress(stage: str, detail: str, step_pct: int = 70):
                                loop.call_soon_threadsafe(progress_queue.put_nowait, {
                                    "step": 3,
                                    "step_progress": max(60, min(95, int(step_pct))),
                                    "overall_progress": min(69, 65 + int(max(0, step_pct - 30) * 0.1)),
                                    "stage": "Transcribing with Local Whisper",
                                    "detail": detail,
                                    "message": detail
                                })

                            whisper_task = asyncio.create_task(asyncio.to_thread(
                                transcribe_local_video_file,
                                temp_audio,
                                whisper_progress,
                                preferred_transcript_lang
                            ))
                            while not whisper_task.done():
                                try:
                                    yield _sse(await asyncio.wait_for(progress_queue.get(), timeout=0.2))
                                except asyncio.TimeoutError:
                                    pass
                            transcript_lines = await whisper_task
                            if not transcript_lines:
                                raise RuntimeError("Local Whisper did not detect any speech in this video.")
                            yield _sse({
                                "step": 3, "step_progress": 100, "overall_progress": 70,
                                "stage": "Subtitles Ready",
                                "detail": f"Local Whisper generated {len(transcript_lines)} timestamped subtitle segments.",
                                "message": f"Local Whisper transcription complete — {len(transcript_lines)} subtitle segments ready."
                            })
                        except Exception as fallback_error:
                            msg = fallback_error.detail if isinstance(fallback_error, HTTPException) else str(fallback_error)
                            original = transcript_error.detail if isinstance(transcript_error, HTTPException) else str(transcript_error)
                            yield _sse({
                                "error": f"YouTube subtitles were unavailable ({original}). Local download/transcription also failed: {msg}",
                                "status": 400
                            })
                            return
                        finally:
                            try:
                                temp_audio.unlink(missing_ok=True)
                            except Exception as cleanup_error:
                                logger.warning(f"Could not remove temporary YouTube transcription file {temp_audio}: {cleanup_error}")
                else:
                    yield _sse({
                        "step": 3,
                        "step_progress": 100,
                        "overall_progress": 70,
                        "stage": "Subtitles Ready",
                        "detail": f"Subtitles loaded — {len(transcript_lines)} dialogue sentences with timestamps ready.",
                        "message": f"Subtitles loaded — {len(transcript_lines)} lines parsed successfully."
                    })

        # Estimate duration from transcript if missing
        if duration == 0.0 and transcript_lines:
            last = transcript_lines[-1]
            duration = last.get("start", 0.0) + last.get("duration", 0.0)

        # Slice transcript based on custom search range if provided
        start_bound = 0.0
        end_bound = duration
        if request.range_start is not None or request.range_end is not None:
            start_bound = request.range_start if request.range_start is not None else 0.0
            end_bound = request.range_end if request.range_end is not None else duration

            if start_bound < 0.0:
                start_bound = 0.0
            if end_bound > duration:
                end_bound = duration

            if start_bound >= end_bound:
                yield _sse({"error": "Invalid search range: start time must be less than end time.", "status": 400})
                return

            filtered_lines = []
            for line in transcript_lines:
                ls = line.get("start", 0.0)
                le = ls + line.get("duration", 0.0)
                if max(ls, start_bound) < min(le, end_bound):
                    filtered_lines.append(line)
            
            transcript_lines = filtered_lines
            if not transcript_lines:
                yield _sse({"error": f"No subtitles found in the specified range {start_bound}s to {end_bound}s.", "status": 400})
                return
            
            # Keep `duration` as the FULL video length. Clips, mock synthesis and
            # the fallback generator all clamp absolute transcript timestamps
            # against `duration` as an absolute end bound; overwriting it with the
            # range span made `end < start` clips that rendered as 1-second blips.
            logger.info(
                "Filtered transcript to custom range: %ss to %ss (span: %ss)",
                start_bound, end_bound, end_bound - start_bound,
            )

        # ── Fallback heatmap ────────────────────────────────────────────────
        # YouTube does not expose a most-replayed curve for every video
        # (log: heatmap_pts=0), which left the analysis graph empty. Build a
        # dialogue-activity curve from the transcript so the timeline always
        # shows data and the AI prompt receives engagement hints.
        if not heatmap and transcript_lines:
            try:
                total = float(duration or 0.0)
                if total <= 0:
                    total = max(
                        (float(l.get("start", 0.0) or 0.0)
                         + float(l.get("duration", 0.0) or 0.0) for l in transcript_lines),
                        default=0.0)
                if total > 0:
                    buckets = 120
                    step = max(1.0, total / buckets)
                    weights = [0.0] * buckets
                    for line in transcript_lines:
                        ls = float(line.get("start", 0.0) or 0.0)
                        ldur = float(line.get("duration", 0.0) or 0.0)
                        words = len(str(line.get("text", "")).split())
                        if words <= 0 or ldur <= 0:
                            continue
                        density = words / max(0.5, ldur)
                        first = int(ls // step)
                        last = int(max(ls, ls + ldur - 1e-6) // step)
                        for bi in range(max(0, first), min(buckets - 1, last) + 1):
                            weights[bi] += density
                    smoothed = []
                    for i in range(buckets):
                        lo, hi = max(0, i - 2), min(buckets, i + 3)
                        smoothed.append(sum(weights[lo:hi]) / (hi - lo))
                    peak = max(smoothed) or 1.0
                    heatmap = [
                        {"start_time": round(i * step, 2),
                         "end_time": round(min(total, (i + 1) * step), 2),
                         "value": round(min(1.0, 0.25 + 0.75 * (v / peak)), 4)}
                        for i, v in enumerate(smoothed)
                    ]
                    logger.info("Synthetic dialogue heatmap built: %d points", len(heatmap))
                    yield _sse({
                        "step": 2,
                        "step_progress": 100,
                        "overall_progress": 50,
                        "stage": "Dialogue Activity Curve",
                        "detail": ("YouTube tidak menyediakan data most-replayed — "
                                   f"grafik dibuat dari aktivitas dialog ({len(heatmap)} titik)."),
                        "message": "Grafik analisis dibuat dari aktivitas dialog.",
                        "warning": True,
                    })
            except Exception as exc:
                logger.warning("Synthetic dialogue heatmap failed: %s", exc)

        # Enrich transcript with heatmap engagement scores
        enriched_transcript = []
        for line in transcript_lines:
            ls   = line.get("start", 0.0)
            ld   = line.get("duration", 0.0)
            le   = ls + ld
            score = get_average_heatmap_value(ls, le, heatmap)
            enriched_transcript.append({
                "start":      round(ls, 2),
                "end":        round(le, 2),
                "text":       line.get("text", ""),
                "engagement": round(score, 3)
            })

        # ── Mock short-circuit ───────────────────────────────────────────────
        if is_mock:
            mock_stages = [
                ("Context Assembly", "Aligning 9 transcript dialogue lines with retention telemetry...", 30, 78),
                ("Viral Hook & Curiosity Detection", "Scanning transcript dialogue for viral hooks & curiosity gaps...", 65, 88),
                ("Virality Scoring & Selection", "Calculating virality coefficients and formatting clip candidates...", 92, 95),
            ]
            for s_name, s_detail, s_prog, o_prog in mock_stages:
                yield _sse({
                    "step": 4,
                    "step_progress": s_prog,
                    "overall_progress": o_prog,
                    "stage": s_name,
                    "detail": s_detail,
                    "model": "gemini-2.5-flash (Mock)",
                    "message": f"Mock AI ({s_name}): {s_detail}"
                })
                await asyncio.sleep(0.7)

            mock_clips = [
                ViralClip(title="Finding hotspots using heatmaps",  start_time=11.0, end_time=22.0, hook_time=14.0, virality_score=95,
                          key_quotes=["Uses YouTube player heatmaps.", "Processes using Gemini AI."],
                          transcript="Most people think it's magic. But it uses YouTube player heatmaps.",
                          title_suggestion="Unlock Video Virality Secrets",
                          caption_suggestion="Stop guessing what works! Here's how to use heatmaps to find viral hotspots in seconds. 🔥",
                          hashtag_suggestion="#viralclips #videoediting #heatmaps #aitools"),
                ViralClip(title="Grow on TikTok or Reels",          start_time=22.0, end_time=32.0, hook_time=27.0, virality_score=88,
                          key_quotes=["Changing how editors crop videos.", "If you want to grow on TikTok, try it."],
                          transcript="This is changing how editors crop videos. If you want to grow on TikTok, try it.",
                          title_suggestion="The Ultimate TikTok Growth Hack",
                          caption_suggestion="Want to scale your TikTok views? This tool will revolutionize your workflow. 🚀",
                          hashtag_suggestion="#tiktokgrowth #reels #shorts #editingtips"),
                ViralClip(title="Introductory overview of the tool", start_time=0.0,  end_time=11.0, hook_time=3.0, virality_score=72,
                          key_quotes=["Hello and welcome.", "Finds viral hotspots."],
                          transcript="Hello and welcome. It finds viral hotspots and highlights them.",
        title_suggestion="Meet Chopster AI",
                          caption_suggestion="Say hello to your new AI co-editor. Find the absolute best parts of any video instantly.",
        hashtag_suggestion="#chopsterbyaris #aiediting #growthmindset"),
            ]
            # Keep mock data valid for short/local files and honor the requested
            # count; this mode is used by desktop diagnostics and must still
            # produce renderable ranges.
            try:
                requested_mock_count = int(request.target_clip_count)
            except (TypeError, ValueError):
                requested_mock_count = len(mock_clips)
            requested_mock_count = max(1, min(len(mock_clips), requested_mock_count))
            safe_duration = max(1.0, float(duration or 200.0))
            mock_clips = mock_clips[:requested_mock_count]
            compact_mock_ranges = safe_duration < max(float(c.end_time) for c in mock_clips)
            for idx, clip in enumerate(mock_clips):
                if compact_mock_ranges:
                    start = safe_duration * idx / requested_mock_count
                    end = safe_duration * (idx + 1) / requested_mock_count
                else:
                    start = min(float(clip.start_time), max(0.0, safe_duration - 0.5))
                    end = min(float(clip.end_time), safe_duration)
                if end <= start:
                    start = min(safe_duration * idx / requested_mock_count, max(0.0, safe_duration - 0.5))
                    end = min(safe_duration, start + max(0.5, safe_duration / requested_mock_count))
                clip.start_time = round(start, 2)
                clip.end_time = round(end, 2)
                clip.hook_time = round(min(max(float(clip.hook_time), start), end), 2)
            mock_heatmap = [
                HeatmapPoint(start_time=i*10.0, end_time=(i+1)*10.0,
                             value=0.2 + (0.6 if i in [2,5,8,12,16] else 0.1))
                for i in range(20)
            ] if not heatmap else [
                HeatmapPoint(start_time=float(pt.get('start_time',0.0)),
                             end_time=float(pt.get('end_time',0.0)),
                             value=float(pt.get('value',0.0)))
                for pt in heatmap
            ]
            result = AnalyzeResponse(
                video_id=video_id, title=title, channel=channel, duration=duration or 200.0,
                heatmap=mock_heatmap,
        summary="Mock analysis: this video explains how Chopster helps creators edit video. #aitools #videoediting #productivity",
                clips=mock_clips,
                transcript=[
                    TranscriptLine(
                        start=float(line["start"]), end=float(line["end"]),
                        text=line["text"], engagement=line.get("engagement")
                    ) for line in enriched_transcript
                ],
                model="Mock Gemini",
                video_url=video_url,
                source_type=source_type,
            )
            yield _sse({
                "step": 4,
                "step_progress": 100,
                "overall_progress": 100,
                "stage": "Analysis Complete",
                "detail": "Generated 3 viral clip candidates successfully.",
                "done": True,
                "result": result.model_dump()
            })
            return

        is_long_video = duration > 3600
        is_auto_clip_count = False
        target_count_num = None
        if request.target_clip_count is not None:
            if isinstance(request.target_clip_count, str) and request.target_clip_count.lower() == 'auto':
                is_auto_clip_count = True
            else:
                try:
                    target_count_num = int(request.target_clip_count)
                    if target_count_num <= 0:
                        is_auto_clip_count = True
                except (ValueError, TypeError):
                    is_auto_clip_count = True
        else:
            is_auto_clip_count = True

        if not is_auto_clip_count and target_count_num:
            N = target_count_num
            if N <= 5:
                min_clips = max(1, N - 1)
                max_clips = N + 2
            elif N <= 10:
                min_clips = max(1, N - 2)
                max_clips = N + 3
            else:
                min_clips = max(1, N - 5)
                max_clips = N + 5
            clip_range = f"{min_clips}-{max_clips}"
            clip_count_instruction = (
                f"TARGET CLIP COUNT: Approximately {clip_range} clips. "
                f"Identify the highest-quality, most viral segments within this range."
            )
        else:
            clip_range = "up to around 200 (AI-determined based on interesting topics)"
            clip_count_instruction = (
                "DYNAMIC AUTO CLIP COUNT & TOPIC CURATION RULES:\n"
                "- You (the AI editor) decide the total number of clips to extract based on how many genuinely interesting, high-value, and viral topics exist in this video.\n"
                "- Upper Constraint Ceiling: Extract up to around 200 clips maximum (no need to reach exactly 200; extract as many as the video's content genuinely justifies, up to approximately 200 clips).\n"
                "- High-Interest Standalone Topics Required: Every single clip MUST focus on an interesting, distinct, and compelling topic, idea, debate, story, funny moment, or revelation. Do NOT produce repetitive, weak, or trivial filler clips just to inflate the count. Only create clips for moments that would actually captivate an audience."
            )

        # ── Step 4: Build prompt & Detect Language ─────────────────────────────
        detected_lang = detect_transcript_language(enriched_transcript, title)
        # A user-selected language always wins. Otherwise the detector is only trusted when it is
        # confident: a weak verdict must never be forced into English, the model then decides the
        # language from the dialogue itself (this is what used to make Indonesian videos English).
        if language_override:
            lang_code = language_override['code']
            lang_name = language_override['name']
            language_forced = True
            language_source = "user-selected"
        else:
            detected_code = detected_lang.get('code') or 'auto'
            if bool(detected_lang.get('uncertain')) or detected_code == 'auto':
                lang_code = 'auto'
                lang_name = "the video's own spoken language"
                language_forced = False
                language_source = "self-detect (detector inconclusive)"
            else:
                lang_code = detected_code
                lang_name = detected_lang.get('name') or detected_code.upper()
                language_forced = True
                language_source = "auto-detected"
        # Concrete code used when sanitising titles. sanitize_first_person_title only needs it to
        # pick a default subject noun. Prefer the forced/effective lang_code; when the detector was
        # inconclusive (lang_code == 'auto'), fall back to the detector's best guess so the subject
        # noun matches the dialogue's likely language instead of hardcoding English ("The Speaker").
        title_lang_code = (
            lang_code if lang_code not in (None, '', 'auto')
            else (detected_lang.get('code') if detected_lang.get('code') not in (None, '', 'auto') else 'en')
        )
        lang_label = lang_name.upper() if language_forced else "THE VIDEO'S OWN SPOKEN LANGUAGE"
        language_header = (
            f"{lang_name} (Code: '{lang_code}')" if language_forced
            else "AUTO-DETECT (the automatic detector was inconclusive - the AI must decide from the transcript)"
        )
        logger.info(
            f"Output language: {language_source} -> {lang_name} ({lang_code}) | detector={detected_lang.get('code')} "
            f"confidence={detected_lang.get('confidence')} uncertain={detected_lang.get('uncertain')}"
        )

        transcript_dump = []
        for line in enriched_transcript:
            eng = f"|{line['engagement']:.2f}" if heatmap and line['engagement'] > 0 else ""
            transcript_dump.append(f"{line['start']:.1f}|{line['end']:.1f}{eng} {line['text']}")

        MAX_LINES = 8000 if is_long_video else 4000
        if len(transcript_dump) > MAX_LINES:
            logger.warning(f"Transcript {len(transcript_dump)} lines — truncating to {MAX_LINES}.")
            transcript_dump = transcript_dump[:MAX_LINES]

        transcript_text = "\n".join(transcript_dump)

        is_auto_duration = str(request.duration).lower() == "auto"
        if is_auto_duration:
            dur_range = "Auto dynamic length (~15s to ~90s max)"
            duration_instruction = (
                "DYNAMIC AUTO CLIP DURATION GUIDELINES:\n"
                "- Intelligently adjust the duration of each individual clip based on its natural narrative flow and context so that NO CONTEXT, setup, premise, punchline, or explanation is cut off mid-thought.\n"
                "- Generate variant clip durations naturally: ranging flexibly from approximately 15 seconds up to around 90 seconds (1 minute 30 seconds maximum). Do NOT force all clips to be the same length (some can be ~20-30s, some ~40-60s, and deeper discussions or complex stories can be up to around 90s).\n"
                "- Strict maximum constraint: No clip should ever exceed 90 seconds (1 minute 30 seconds). Always ensure clips begin and end cleanly at complete sentence boundaries without mid-word or mid-sentence cuts."
            )
        else:
            dur_target = {"15s": "10-20s", "30s": "20-40s", "60s": "45-75s"}.get(request.duration, "20-40s")
            dur_range = dur_target
            duration_instruction = f"Target clip length: {dur_target}. Keep clips tightly focused around this duration."

        heatmap_note = (
            "Columns: start|end|audience_interest(0-1). Prioritise high-interest peaks."
            if heatmap else
            "No audience interest data. Use content hooks, energy, and story arcs."
        )
        focus_instruction = ""
        if request.custom_prompt and request.custom_prompt.strip():
            focus_instruction = (
                f"CRITICAL USER SEARCH FOCUS:\n"
                f"The user specifically wants you to find clips matching the following query/theme: \"{request.custom_prompt.strip()}\".\n"
                f"Prioritize and tailor your selection of viral clips to fit this request, while still ensuring they make good standalone clips.\n"
                f"IMPORTANT: Regardless of the language this query was typed in, your generated titles, summaries, quotes, and captions MUST REMAIN IN {lang_label}. DO NOT translate the video's content to match the language of the search query.\n\n"
            )

        if language_forced:
            language_block = (
                f"================================================================================\n"
                f"CRITICAL MANDATORY LANGUAGE RULE (ZERO TRANSLATION):\n"
                f"================================================================================\n"
                f"THE REQUIRED OUTPUT LANGUAGE IS: {lang_label} (Language Code: '{lang_code}').\n"
                f"YOU MUST GENERATE 100% OF ALL CONTENT STRICTLY AND EXCLUSIVELY IN {lang_label}.\n\n"
                f"1. NEVER TRANSLATE TO ANY OTHER LANGUAGE!\n"
                f"   - Every title, title suggestion, summary, caption, hashtag and key quote MUST BE 100% IN {lang_label}.\n"
                f"   - Even if the user's prompt or search query was written in another language, your output MUST REMAIN 100% IN {lang_label}.\n"
                f"2. Strict field requirements in {lang_label}:\n"
                f"   - `summary`: In {lang_label}.\n"
                f"   - `title`: Catchy title in {lang_label}, max 8 words.\n"
                f"   - `title_suggestion`: Alternative title in {lang_label}.\n"
                f"   - `caption_suggestion`: Engaging social caption in {lang_label}.\n"
                f"   - `hashtag_suggestion`: Relevant hashtags in {lang_label}.\n"
                f"   - `key_quotes`: MUST be verbatim spoken quotes directly from the transcript in {lang_label}.\n"
                f"================================================================================\n\n"
            )
        else:
            language_block = (
                f"================================================================================\n"
                f"CRITICAL MANDATORY LANGUAGE RULE (SELF-DETECT, ZERO TRANSLATION):\n"
                f"================================================================================\n"
                f"The automatic language detector was NOT confident about this video, so YOU must determine the main spoken language yourself from the transcript dialogue below and write 100% of your output in that SAME language.\n\n"
                f"1. NEVER TRANSLATE the video's spoken language into another language.\n"
                f"2. NEVER DEFAULT TO ENGLISH. Only use English when the dialogue itself is genuinely English.\n"
                f"3. If the dialogue mixes languages, follow the language used by the majority of complete sentences (an Indonesian host dropping English slang words still means Indonesian output).\n"
                f"4. `summary`, `title`, `title_suggestion`, `caption_suggestion`, `hashtag_suggestion` and `key_quotes` MUST all use that detected spoken language, and `key_quotes` must stay verbatim from the transcript.\n"
                f"================================================================================\n\n"
            )

        channel_context = f"- Channel / Host / Creator: {channel}\n" if channel else ""

        prompt = (
            f"You are an expert viral video clip editor finding top clip candidates for TikTok, Instagram Reels, and YouTube Shorts.\n"
            f"Analyze this YouTube video transcript and find {clip_range} high-performing, standalone clip candidates.\n\n"
            f"VIDEO CONTEXT:\n"
            f"- Video Title: {title}\n"
            f"{channel_context}"
            f"- Video Duration: {int(start_bound)}s to {int(end_bound)}s (Total: {int(max(0.0, end_bound - start_bound))}s) | Target clip length: {dur_range}\n"
            f"- Heatmap: {heatmap_note}\n"
            f"- DETECTED VIDEO SPOKEN LANGUAGE: {language_header}\n"
            f"- OUTPUT LANGUAGE MODE: {language_source}\n\n"
            f"{language_block}"
            f"{duration_instruction}\n\n"
            f"{clip_count_instruction}\n\n"
            f"{focus_instruction}"
            f"CRITICAL TITLE & ATTRIBUTION RULES (NO FIRST-PERSON 'I' OR 'ME'):\n"
            f"1. NEVER write clip titles or title suggestions using first-person pronouns ('I', 'me', 'my', 'mine', 'myself', or equivalents in other languages such as 'saya', 'aku', 'gue')!\n"
            f"2. The user posting or curating these clips is a third-party editor, NOT the person speaking in the video. Titles must NEVER make it look like the clip is expressing the user's personal opinion, story, or reaction (e.g. NEVER write 'Why I think this is bad', 'How I made $100K', 'My biggest mistake', or 'I was shocked' / in Indonesian: NEVER 'Kenapa saya...', 'Cara aku...', 'Opini gue...').\n"
            f"3. ALWAYS attribute statements to the context of the video and the actual person speaking:\n"
            f"   - Use the actual name of the speaker, host, or guest from the video title, channel name ({channel or 'Host'}), or transcript dialogue (e.g. '{channel or 'Speaker'} Explains...', 'Why {channel or 'Host'} Shocked Fans', '[Name] Reveals The Truth' / in Indonesian: '{channel or 'Host'} Menjelaskan...', 'Alasan {channel or 'Host'} Mengungkapkan...').\n"
            f"   - If the speaker's name is not explicitly stated, use their role or descriptive title (e.g. 'Host Reacts...', 'CEO Reveals...', 'Expert Explains...', 'Guest Breaks Down...' / in Indonesian: 'Host Menjelaskan...', 'Pakar Membongkar...').\n"
            f"   - Or use objective, curiosity-driven framing (e.g. 'The Real Truth About...', 'How To Master...', 'Why Most People Fail At...', 'The Harsh Reality of...' / in Indonesian: 'Fakta Sebenarnya Tentang...', 'Cara Menguasai...').\n"
            f"4. Keep titles snappy, viral, engaging, and under 8 words.\n\n"
            f"Transcript (start|end[|interest] text):\n---\n{transcript_text}\n---\n\n"
            f"Rules: use exact seconds from transcript; clips must start/end at sentence boundaries; do not overlap.\n"
            f"Return clips sorted by virality_score desc."
        )

        requested_model = (request.model or 'gemini-2.5-flash').strip()
        if any(dep in requested_model.lower() for dep in ['gemini-1.0', 'gemini-pro-vision']):
            logger.info(f"Requested model '{requested_model}' is outdated. Upgrading to gemini-2.5-flash.")
            requested_model = 'gemini-2.5-flash'

        yield _sse({
            "step": 4,
            "step_progress": 10,
            "overall_progress": 72,
            "stage": "Language & Context Assembly",
            "detail": f"Source language: {lang_name} ({lang_code}). Aligning {len(transcript_dump)} dialogue segments for {requested_model}...",
            "model": requested_model,
            "message": f"Verified language: {lang_name}. Zero-translation rule enforced for {requested_model}."
        })

        # ── Step 4: AI analysis call with retry/fallback ─────────────────────────────
        if use_gateway:
            # Global Gateway route: single verified model, no Flash chain.
            requested_model = str(gateway_ctx.get("model") or requested_model or "")
            models_to_try = [requested_model] if requested_model else [""]
            client = None
        else:
            # Gemini/Google route: use the already-selected model first and
            # keep a small local fallback list. If the selected model is
            # retired/unavailable, refresh Google's live Flash list on demand
            # rather than adding a paginated network request to every success.
            client = genai.Client(api_key=gemini_key)
            models_to_try = [requested_model] if requested_model else []
            for km in KNOWN_FLASH_MODELS:
                if km not in models_to_try:
                    models_to_try.append(km)

        # Skip models already known-retired for this key and cap the chain so a
        # long fallback list can never multiply into a multi-minute wait.
        models_to_try = [m for m in models_to_try
                         if m and m not in _UNAVAILABLE_MODELS][:MAX_MODELS_PER_ANALYSIS]
        logger.info(f"Flash fallback chain prepared: {models_to_try}")

        response = None
        last_error = None
        encountered_quota_error = None
        analysis_data = None
        successful_model = None
        gemini_models_refreshed = False
        analysis_started = asyncio.get_event_loop().time()

        for idx, model_name in enumerate(models_to_try):
            if (asyncio.get_event_loop().time() - analysis_started
                    >= AI_ANALYSIS_DEADLINE_SECONDS):
                logger.warning(
                    "AI analysis deadline (%ss) hit before %s; stopping fallback chain",
                    AI_ANALYSIS_DEADLINE_SECONDS, model_name)
                last_error = last_error or Exception(
                    f"AI deadline {AI_ANALYSIS_DEADLINE_SECONDS}s exceeded")
                break
            next_model_hint = models_to_try[idx + 1] if idx + 1 < len(models_to_try) else None
            MAX_RETRIES = 2
            
            for attempt in range(MAX_RETRIES):
                if attempt > 0:
                    wait = 2
                    previous_error = str(last_error or "").lower()
                    is_empty_gateway_retry = use_gateway and any(
                        marker in previous_error
                        for marker in ("respons kosong", "empty response", "unparseable response")
                    )
                    retry_detail = (
                        f"{model_name} returned no usable text/JSON; retrying in {wait}s "
                        f"({attempt + 1}/{MAX_RETRIES})..."
                        if is_empty_gateway_retry else
                        f"{model_name} busy — waiting {wait}s before retry ({attempt + 1}/{MAX_RETRIES})..."
                    )
                    yield _sse({
                        "step": 4,
                        "step_progress": 25,
                        "overall_progress": 75,
                        "stage": "Gateway Response Retry" if is_empty_gateway_retry else "Transient Retry",
                        "detail": retry_detail,
                        "model": model_name,
                        "message": retry_detail
                    })
                    await asyncio.sleep(wait)
                
                yield _sse({
                    "step": 4,
                    "step_progress": 18,
                    "overall_progress": 74,
                    "stage": "Neural Model Dispatch",
                    "detail": f"Dispatched {len(transcript_dump)} lines to {model_name} (attempt {attempt + 1})...",
                    "model": model_name,
                    "message": f"Calling {model_name} (attempt {attempt + 1}/{MAX_RETRIES})..."
                })
                
                # Execute AI call with heartbeat to keep mobile connection alive and show live stages
                if use_gateway:
                    from chopster.auto_clip_studio.engine.gateway_ai import run_gateway_analysis
                    task = asyncio.create_task(asyncio.to_thread(
                        run_gateway_analysis,
                        gateway_ctx, prompt, model_name,
                        32768,
                    ))
                else:
                    task = asyncio.create_task(asyncio.to_thread(
                        client.models.generate_content,
                        model=model_name,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            response_schema=VideoAnalysis,
                            temperature=0.2,
                            max_output_tokens=65536 if any(v in model_name for v in ['2.0', '2.5', '3.']) else 8192,
                        )
                    ))
                
                call_start = asyncio.get_event_loop().time()
                call_timed_out = False
                while not task.done():
                    done, _ = await asyncio.wait([task], timeout=2.0)
                    if not done:
                        elapsed = int(asyncio.get_event_loop().time() - call_start)

                        # Hard per-call cap: a hung request must not stall the
                        # whole analysis (previously ~140s on one model).
                        if elapsed >= AI_CALL_TIMEOUT_SECONDS:
                            call_timed_out = True
                            logger.warning(
                                "%s exceeded the %ss per-call timeout; moving on",
                                model_name, AI_CALL_TIMEOUT_SECONDS)
                            break
                        
                        if elapsed < 5:
                            stage = "Neural Context Loading"
                            detail = f"Transmitting {len(transcript_dump)} timestamped dialogue segments to {model_name}..."
                            step_prog = min(35, 12 + elapsed * 4)
                        elif elapsed < 12:
                            stage = "Retention Spike Cross-Analysis"
                            detail = f"Correlating viewer retention peaks against speaker dialogue to isolate viral moments..."
                            step_prog = min(55, 35 + int((elapsed - 5) * 3))
                        elif elapsed < 20:
                            stage = "Viral Hook & Curiosity Detection"
                            detail = f"Scanning transcript dialogue for opening hooks, punchlines, controversial takes & emotional peaks..."
                            step_prog = min(72, 55 + int((elapsed - 12) * 2.2))
                        elif elapsed < 30:
                            stage = "Coherence & Sentence Boundary Snapping"
                            detail = f"Ensuring clip candidates start and end naturally on sentence boundaries without mid-word cuts..."
                            step_prog = min(85, 72 + int((elapsed - 20) * 1.3))
                        elif elapsed < 42:
                            stage = "Virality Scoring & Selection"
                            display_clip_count = "all high-value" if is_auto_clip_count else f"the top {clip_range}"
                            detail = f"Calculating virality coefficients (1-100) and selecting {display_clip_count} highest potential clips..."
                            step_prog = min(92, 85 + int((elapsed - 30) * 0.7))
                        else:
                            stage = "Social Media Metadata Synthesis"
                            detail = f"Drafting attention-grabbing titles, social captions, and targeted hashtags ({elapsed}s)..."
                            step_prog = min(95, 92 + min(3, int((elapsed - 42) * 0.3)))

                        overall_prog = 70 + int(step_prog * 0.28)
                        yield _sse({
                            "step": 4,
                            "keepalive": True,
                            "step_progress": step_prog,
                            "overall_progress": overall_prog,
                            "stage": stage,
                            "detail": detail,
                            "model": model_name,
                            "elapsed": elapsed,
                            "message": f"[{model_name} | {elapsed}s] {stage}: {detail}"
                        })
                
                try:
                    if call_timed_out:
                        raise TimeoutError(
                            f"{model_name} timed out after {AI_CALL_TIMEOUT_SECONDS}s")
                    resp_candidate = await task
                    last_error = None
                    
                    # Parse structured response
                    parsed_data = None
                    if hasattr(resp_candidate, 'parsed') and resp_candidate.parsed is not None:
                        parsed = resp_candidate.parsed
                        parsed_data = {
                            "summary": getattr(parsed, 'summary', ''),
                            "clips": [
                                {
                                    "title": sanitize_first_person_title(getattr(c, 'title', ''), channel, lang=title_lang_code),
                                    "start_time": getattr(c, 'start_time', 0.0),
                                    "end_time": getattr(c, 'end_time', 0.0),
                                    "hook_time": getattr(c, 'hook_time', None),
                                    "virality_score": getattr(c, 'virality_score', 0),
                                    "key_quotes": getattr(c, 'key_quotes', []),
                                    "title_suggestion": sanitize_first_person_title(getattr(c, 'title_suggestion', ''), channel, lang=title_lang_code),
                                    "caption_suggestion": getattr(c, 'caption_suggestion', ''),
                                    "hashtag_suggestion": getattr(c, 'hashtag_suggestion', ''),
                                }
                                for c in (getattr(parsed, 'clips', []) or [])
                            ]
                        }
                    elif resp_candidate.text:
                        raw_text = resp_candidate.text.strip()
                        if raw_text.startswith("```"):
                            raw_text = re.sub(r"^```[a-zA-Z]*\n?", "", raw_text)
                            raw_text = re.sub(r"\n?```$", "", raw_text)
                        try:
                            parsed_data = json.loads(raw_text)
                        except Exception as json_err:
                            logger.warning(f"JSON parsing error from {model_name}: {json_err}")
                            parsed_data = None

                    if parsed_data is not None and use_gateway:
                        original_payload_type = type(parsed_data).__name__
                        parsed_data = _normalize_analysis_payload(parsed_data)
                        if parsed_data is None:
                            logger.warning(
                                "%s returned unsupported JSON payload shape (%s)",
                                model_name,
                                original_payload_type,
                            )
                        elif original_payload_type == "list":
                            logger.info(
                                "%s top-level clip list normalized (%d clips)",
                                model_name,
                                len(parsed_data.get("clips", [])),
                            )

                    if parsed_data is not None:
                        clips_found = len(parsed_data.get('clips', []))
                        if clips_found == 0 and next_model_hint is not None:
                            logger.warning(f"{model_name} returned 0 clips. Will try next flash model {next_model_hint}...")
                            yield _sse({
                                "step": 4,
                                "step_progress": 40,
                                "overall_progress": 78,
                                "stage": "Flash Model Fallback",
                                "detail": f"{model_name} returned 0 clips — switching to {next_model_hint} for deeper extraction...",
                                "model": next_model_hint,
                                "message": f"{model_name} returned 0 clips — switching to {next_model_hint}..."
                            })
                            last_error = Exception(f"{model_name} returned 0 clips")
                            break
                        
                        response = resp_candidate
                        analysis_data = parsed_data
                        successful_model = model_name
                        break
                    else:
                        last_error = Exception(f"{model_name} returned empty or unparseable response")
                        if use_gateway and attempt + 1 < MAX_RETRIES:
                            logger.warning(
                                "%s returned empty or invalid JSON; retrying once",
                                model_name,
                            )
                            continue
                        break
                        
                except Exception as e:
                    last_error = e
                    err_str = str(e).lower()
                    logger.warning(f"Error from {model_name} (attempt {attempt + 1}): {e}")

                    # Gateway requests can time out transiently while the remote
                    # model is generating. Retry this only on the ACS Gateway
                    # route, using the existing one-retry limit; do not change
                    # Gemini or shared AI-provider retry behavior.
                    is_gateway_timeout = use_gateway and any(
                        marker in err_str
                        for marker in ("read operation timed out", "timed out", "timeout")
                    )
                    if is_gateway_timeout:
                        if attempt + 1 < MAX_RETRIES:
                            logger.warning(
                                "%s timed out through the ACS Gateway; retrying once",
                                model_name,
                            )
                            continue
                        break

                    is_empty_gateway_response = use_gateway and any(
                        marker in err_str
                        for marker in ("respons kosong", "empty response", "unparseable response")
                    )
                    if is_empty_gateway_response:
                        # A deterministic output-token cutoff cannot be fixed
                        # by replaying the identical request and wastes another
                        # full model-generation round trip.
                        is_gateway_output_limit = "finish_reason=length" in err_str
                        if attempt + 1 < MAX_RETRIES and not is_gateway_output_limit:
                            logger.warning("%s returned no usable response; retrying once", model_name)
                            continue
                        break
                    
                    if any(x in err_str for x in ('429', 'quota', 'resource exhausted', 'rate limit')):
                        encountered_quota_error = e
                        break

                    if any(x in err_str for x in ('404', 'not found', 'not supported')):
                        # Retired for this key: never try it again this session.
                        _UNAVAILABLE_MODELS.add(model_name)
                        _save_retired_models(_UNAVAILABLE_MODELS)
                        if not use_gateway and not gemini_models_refreshed:
                            gemini_models_refreshed = True
                            try:
                                live_flash_models = await asyncio.to_thread(
                                    get_flash_models_for_key,
                                    client,
                                )
                                # If Google reports a model live again, stop
                                # treating it as retired.
                                still_retired = {m for m in _UNAVAILABLE_MODELS
                                                 if m not in set(live_flash_models or [])}
                                if still_retired != _UNAVAILABLE_MODELS:
                                    _UNAVAILABLE_MODELS.clear()
                                    _UNAVAILABLE_MODELS.update(still_retired)
                                    _save_retired_models(_UNAVAILABLE_MODELS)
                                attempted_models = set(models_to_try[:idx + 1])
                                remaining_models = models_to_try[idx + 1:]
                                refreshed_candidates = []
                                for candidate in live_flash_models + remaining_models:
                                    if (
                                        candidate
                                        and candidate not in _UNAVAILABLE_MODELS
                                        and candidate not in attempted_models
                                        and candidate not in refreshed_candidates
                                    ):
                                        refreshed_candidates.append(candidate)
                                models_to_try[idx + 1:] = refreshed_candidates[:MAX_MODELS_PER_ANALYSIS]
                                next_model_hint = (
                                    refreshed_candidates[0] if refreshed_candidates else None
                                )
                                if refreshed_candidates:
                                    logger.warning(
                                        "Gemini model %s is unavailable; refreshed live Flash "
                                        "fallback candidates: %s",
                                        model_name,
                                        refreshed_candidates,
                                    )
                            except Exception as discovery_error:
                                logger.warning(
                                    "Could not refresh Gemini Flash models after %s: %s",
                                    model_name,
                                    discovery_error,
                                )
                        break
                    
                    is_server_busy = any(x in err_str for x in ('503', 'unavailable', 'overloaded', '500', 'internal'))
                    if not is_server_busy:
                        break
            
            if analysis_data is not None and response is not None:
                break
                
            if next_model_hint is not None:
                err_summary = "quota reached" if any(x in str(last_error).lower() for x in ('429', 'quota', 'rate limit')) else \
                              "not available or deprecated" if "404" in str(last_error) else \
                              "temporarily busy"
                yield _sse({
                    "step": 4,
                    "step_progress": 35,
                    "overall_progress": 76,
                    "stage": "Flash Fallback",
                    "detail": f"{model_name} {err_summary} — switching to fallback {next_model_hint}...",
                    "model": next_model_hint,
                    "message": f"{model_name} {err_summary} — switching to flash fallback model {next_model_hint}..."
                })

        if analysis_data is None and enriched_transcript:
            # AI entirely unavailable (quota 429 / 503 spikes / retired models).
            # Do not stall the studio: build clip candidates locally from the
            # transcript and continue, clearly flagged as a local fallback.
            ai_unavailable_note = str(encountered_quota_error or last_error or "AI tidak tersedia")
            logger.warning(
                "All AI models failed (%s); using local transcript fallback",
                ai_unavailable_note[:160])
            yield _sse({
                "step": 4,
                "step_progress": 96,
                "overall_progress": 96,
                "stage": "Local Fallback",
                "detail": (
                    "AI Gemini tidak tersedia saat ini — ACS membuat kandidat klip "
                    "dari transkrip secara lokal. Kamu tetap bisa render & publish."),
                "message": "AI tidak tersedia — memakai pemilihan klip lokal dari transkrip.",
                "warning": True,
            })
            analysis_data = {"summary": "", "clips": []}
            successful_model = "local-transcript-fallback"

        if analysis_data is None:
            # If any model in the fallback chain suffered quota exhaustion, prioritize showing the quota explanation
            error_to_report = encountered_quota_error or last_error
            if error_to_report is not None:
                err_str = str(error_to_report).lower()
                if use_gateway:
                    logger.error(f"Gateway AI analysis failed for {requested_model}: {error_to_report}")
                    if "finish_reason=length" in err_str:
                        user_error = (
                            f"Gateway AI ({requested_model}) mencapai batas token output sebelum menghasilkan "
                            f"JSON lengkap. ACS sekarang mengirim batas output yang lebih besar; bila masih terjadi, "
                            f"pilih lebih sedikit klip atau rentang video yang lebih pendek. Detail: {error_to_report}"
                        )
                    elif any(x in err_str for x in ("respons kosong", "empty response", "unparseable response")):
                        user_error = (
                            f"Gateway AI ({requested_model}) tidak mengembalikan teks/JSON yang bisa dibaca "
                            f"setelah dicoba ulang. Coba rentang video lebih pendek atau model Gateway lain. "
                            f"Detail: {error_to_report}"
                        )
                    else:
                        user_error = f"Gateway AI ({requested_model}) gagal: {error_to_report}"
                    yield _sse({"error": user_error, "status": 502})
                elif any(x in err_str for x in ('429', 'quota', 'resource exhausted', 'rate limit')):
                    yield _sse({
                        "error": "Quota limit reached across all available Gemini Flash models for this API key. Free keys have a request limit per minute. Please change your API key, generate a fresh free key at aistudio.google.com, or wait 30–60 seconds before trying again.",
                        "status": 429
                    })
                elif any(x in err_str for x in ('503', 'unavailable', 'overloaded')):
                    yield _sse({
                        "error": "Google Gemini servers are currently experiencing high demand across all Flash models. Please change to a different Gemini API key or wait a few moments and try again.",
                        "status": 503
                    })
                elif any(x in err_str for x in ('401', '403', 'api_key', 'invalid', 'permission')):
                    yield _sse({
                        "error": "Invalid or restricted Gemini API key. Please change your API key or generate a new free key at aistudio.google.com.",
                        "status": 401
                    })
                elif any(x in err_str for x in ('404', 'not found', 'not supported')):
                    models_preview = ', '.join(models_to_try[:3])
                    yield _sse({
                        "error": f"All tested Gemini Flash models ({models_preview}...) were unavailable or not supported for this API key. Please change your Gemini API key or generate a new one at aistudio.google.com.",
                        "status": 404
                    })
                else:
                    logger.error(f"Gemini error after all fallback models: {error_to_report}")
                    yield _sse({
                        "error": f"AI analysis failed across all available Flash models ({str(error_to_report)}). Please change your Gemini API key or try again in a few moments.",
                        "status": 500
                    })
            else:
                if use_gateway:
                    yield _sse({
                        "error": f"Gateway AI ({requested_model}) tidak mengembalikan respons. Coba lagi atau gunakan model Gateway lain.",
                        "status": 502
                    })
                else:
                    yield _sse({
                        "error": "No response received after trying all available Gemini Flash models. Please change your Gemini API key or try again in a few moments.",
                        "status": 500
                    })
            return

        # Fallback clip synthesis if 0 clips were returned after all models
        if len(analysis_data.get('clips', [])) == 0 and enriched_transcript:
            logger.info("Generating fallback clips from heatmap and transcript segments...")
            sorted_lines = sorted(enriched_transcript, key=lambda l: l.get('engagement', 0.0), reverse=True)
            candidate_starts = []
            for l in sorted_lines:
                s = l['start']
                if not any(abs(s - existing) < 25.0 for existing in candidate_starts):
                    candidate_starts.append(s)
                if len(candidate_starts) >= 5:
                    break
            
            fallback_clips_list = []
            for i, st in enumerate(candidate_starts):
                if request.duration == "15s":
                    target_len = 15.0
                elif request.duration == "60s":
                    target_len = 60.0
                elif request.duration == "auto":
                    auto_lens = [25.0, 45.0, 75.0, 35.0, 60.0, 85.0, 20.0, 50.0]
                    target_len = auto_lens[i % len(auto_lens)]
                else:
                    target_len = 30.0
                et = min(duration, st + min(90.0, target_len))
                seg_lines = [l['text'] for l in enriched_transcript if max(l['start'], st) < min(l['end'], et)]
                seg_text = " ".join(seg_lines).strip()
                preview = seg_text[:60] + "..." if len(seg_text) > 60 else seg_text or f"Viral Highlight #{i+1}"
                if lang_code == 'id':
                    fallback_clips_list.append({
                        "title": f"Momen Menarik #{i+1}",
                        "start_time": st,
                        "end_time": et,
                        "hook_time": st,
                        "virality_score": max(70, int(95 - i * 5)),
                        "key_quotes": [seg_text[:80]] if seg_text else [],
                        "title_suggestion": f"Cuplikan Pilihan #{i+1}",
                        "caption_suggestion": f"Momen terbaik dari video: {preview} #viral #trending",
                        "hashtag_suggestion": "#viral #shorts #trending"
                    })
                elif lang_code == 'es':
                    fallback_clips_list.append({
                        "title": f"Momento Destacado #{i+1}",
                        "start_time": st,
                        "end_time": et,
                        "hook_time": st,
                        "virality_score": max(70, int(95 - i * 5)),
                        "key_quotes": [seg_text[:80]] if seg_text else [],
                        "title_suggestion": f"Momento Imperdible #{i+1}",
                        "caption_suggestion": f"Momento clave del video: {preview} #viral #trending",
                        "hashtag_suggestion": "#viral #shorts #trending"
                    })
                else:
                    fallback_clips_list.append({
                        "title": f"Key Highlight #{i+1}",
                        "start_time": st,
                        "end_time": et,
                        "hook_time": st,
                        "virality_score": max(70, int(95 - i * 5)),
                        "key_quotes": [seg_text[:80]] if seg_text else [],
                        "title_suggestion": f"Must Watch Moment #{i+1}",
                        "caption_suggestion": f"Key highlight from video: {preview} #viral #trending",
                        "hashtag_suggestion": "#viral #shorts #trending"
                    })
            analysis_data['clips'] = fallback_clips_list
            if not analysis_data.get('summary'):
                if lang_code == 'id':
                    analysis_data['summary'] = f"Analisis video \"{title}\" menemukan {len(fallback_clips_list)} segmen cuplikan pilihan. #viral #highlights"
                elif lang_code == 'es':
                    analysis_data['summary'] = f"Análisis de \"{title}\" identificando {len(fallback_clips_list)} segmentos clave. #viral #highlights"
                else:
                    analysis_data['summary'] = f"Analysis of \"{title}\" identifying {len(fallback_clips_list)} key segments. #viral #highlights"

        clip_count = len(analysis_data.get('clips', []))
        yield _sse({
            "step": 4,
            "step_progress": 98,
            "overall_progress": 98,
            "stage": "Clip Verification & Alignment",
            "detail": f"Verified {clip_count} clip segments with precise video timestamps and key quotes in {lang_name}.",
            "model": successful_model or requested_model,
            "message": f"Found {clip_count} viral clip candidates ({lang_name}) with {successful_model or requested_model} — reconstructing transcripts..."
        })
        completion_provider = "Gateway AI" if use_gateway else "Gemini"
        logger.info(f"{completion_provider} analysis complete with {successful_model or requested_model}. Found {clip_count} clips in {lang_name}.")

        # Reconstruct clip transcripts from enriched_transcript
        final_clips = []
        for raw_clip in analysis_data.get('clips', []):
            if use_gateway:
                start = _coerce_model_seconds(_first_model_value(
                    raw_clip, "start_time", "startTime", "start_seconds", "start"
                ))
                end = _coerce_model_seconds(_first_model_value(
                    raw_clip, "end_time", "endTime", "end_seconds", "end"
                ))
                hook = _coerce_model_seconds(_first_model_value(
                    raw_clip, "hook_time", "hookTime", "hook_seconds", "hook"
                ))
                if start is None or end is None or end <= start:
                    recovered = _recover_clip_times(
                        raw_clip,
                        enriched_transcript,
                        request.duration,
                        float(start_bound or 0.0),
                        float(end_bound or duration),
                    )
                    if recovered:
                        start, end = recovered
                        logger.warning(
                            "Recovered missing clip timestamps from key_quotes for %r: %.2f-%.2fs",
                            _coerce_model_text(raw_clip.get("title", "")),
                            start,
                            end,
                        )
                    else:
                        logger.warning(
                            "Skipping clip without usable timestamps or transcript quote match: %r",
                            _coerce_model_text(raw_clip.get("title", "")),
                        )
                        continue
                if hook is None or not (start <= hook <= end):
                    hook = start
            else:
                # Preserve the original native Gemini response handling.
                start = raw_clip.get('start_time', 0.0)
                end = raw_clip.get('end_time', 0.0)
                hook = raw_clip.get('hook_time')
                if hook is None or not (start <= hook <= end):
                    hook = start
            
            # Enforce max 90 seconds (1m 30s) duration constraint for auto duration mode
            if is_auto_duration and (end - start) > 90.0:
                max_end = start + 90.0
                best_end = max_end
                for l in enriched_transcript:
                    l_end = l.get('end', 0.0)
                    if start + 15.0 <= l_end <= max_end:
                        best_end = l_end
                end = best_end
            
            clip_lines = [
                line.get("text", "")
                for line in enriched_transcript
                if max(line.get("start", 0.0), start) < min(line.get("end", 0.0), end)
            ]
            
            # Only Gateway responses need shape coercion; keep native Gemini's
            # schema-validated values on the original code path.
            if use_gateway:
                caption_sug = lowercase_hashtags_in_string(
                    _coerce_model_text(raw_clip.get('caption_suggestion', ''))
                )
                hashtag_sug = lowercase_hashtags_in_string(
                    _coerce_model_text(raw_clip.get('hashtag_suggestion', ''))
                )
                title_value = _coerce_model_text(raw_clip.get('title', ''))
                title_suggestion_value = _coerce_model_text(raw_clip.get('title_suggestion', ''))
                virality_score = _coerce_model_int(raw_clip.get('virality_score', 0))
                key_quotes = _coerce_model_text_list(raw_clip.get('key_quotes'))
            else:
                caption_sug = lowercase_hashtags_in_string(raw_clip.get('caption_suggestion', ''))
                hashtag_sug = lowercase_hashtags_in_string(raw_clip.get('hashtag_suggestion', ''))
                title_value = raw_clip.get('title', '')
                title_suggestion_value = raw_clip.get('title_suggestion', '')
                virality_score = raw_clip.get('virality_score', 0)
                key_quotes = raw_clip.get('key_quotes') or []
            
            final_clips.append(ViralClip(
                title=sanitize_first_person_title(title_value, channel, lang=title_lang_code),
                start_time=start,
                end_time=end,
                hook_time=hook,
                virality_score=virality_score,
                key_quotes=key_quotes,
                transcript=" ".join(clip_lines),
                title_suggestion=sanitize_first_person_title(title_suggestion_value, channel, lang=title_lang_code),
                caption_suggestion=caption_sug,
                hashtag_suggestion=hashtag_sug
            ))

        if use_gateway and not final_clips:
            yield _sse({
                "error": (
                    "Gateway AI returned clip suggestions but no usable timestamps, and ACS could not "
                    "match their quotes to the timed transcript. Try another model or rerun analysis."
                ),
                "status": 422
            })
            return

        # Enforce max constraint of ~200 clips for auto clip count
        if is_auto_clip_count and len(final_clips) > 200:
            final_clips = final_clips[:200]

        response_heatmap = [
            HeatmapPoint(
                start_time=float(pt.get('start_time', 0.0)),
                end_time=float(pt.get('end_time', 0.0)),
                value=float(pt.get('value', 0.0))
            )
            for pt in (heatmap or [])
        ]

        response_transcript = [
            TranscriptLine(
                start=float(line["start"]),
                end=float(line["end"]),
                text=line["text"],
                engagement=line.get("engagement")
            )
            for line in enriched_transcript
        ]

        if use_gateway:
            # Gateway models may omit or mistype the summary field.
            summary_text = _coerce_model_text(analysis_data.get("summary", ""))
            if not summary_text:
                if lang_code == "id":
                    summary_text = f"Analisis video \"{title}\" menemukan {len(final_clips)} kandidat klip. #viral #highlights"
                elif lang_code == "es":
                    summary_text = f"Análisis de \"{title}\" identificó {len(final_clips)} clips destacados. #viral #highlights"
                else:
                    summary_text = f"Analysis of \"{title}\" identified {len(final_clips)} highlight clips. #viral #highlights"
            clean_summary = lowercase_hashtags_in_string(summary_text)
        else:
            # Preserve the original native Gemini summary path.
            clean_summary = lowercase_hashtags_in_string(analysis_data.get("summary", ""))

        final_result = AnalyzeResponse(
            video_id=video_id,
            title=title,
            channel=channel,
            duration=duration,
            heatmap=response_heatmap,
            summary=clean_summary,
            clips=final_clips,
            transcript=response_transcript,
            model=successful_model or requested_model,
            video_url=video_url,
            source_type=source_type
        )

        yield _sse({"done": True, "result": final_result.model_dump()})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection":    "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )
