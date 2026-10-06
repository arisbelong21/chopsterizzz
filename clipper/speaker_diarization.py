"""Optional real speaker diarization for Chopster.

The core app stays dependency-safe: when pyannote is unavailable or a Hugging Face
credential is not configured, the module returns an empty result instead of breaking
transcription/reframing. The camera director can then use visual mouth/activity cues.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any


def diarize_audio(path: str | Path, start: float = 0.0, end: float | None = None,
                  max_speakers: int = 8, hf_token: str = "") -> list[dict[str, Any]]:
    """Return [{start,end,speaker,confidence}] using pyannote when available.

    This is deliberately optional because pyannote's model weights are large and
    require a Hugging Face access token on many installations.
    """
    token = (hf_token or os.environ.get("HUGGINGFACE_TOKEN") or os.environ.get("HF_TOKEN") or "").strip()
    if not token:
        return []
    try:
        from pyannote.audio import Pipeline  # type: ignore
    except Exception:
        return []

    src = Path(path)
    if not src.exists():
        return []
    try:
        import subprocess
        with tempfile.TemporaryDirectory(prefix="chopster_diar_") as td:
            wav = Path(td) / "audio.wav"
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
            if start > 0:
                cmd += ["-ss", f"{float(start):.3f}"]
            cmd += ["-i", str(src)]
            if end is not None:
                cmd += ["-t", f"{max(0.1, float(end) - float(start)):.3f}"]
            cmd += ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)]
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)

            pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", use_auth_token=token)
            result = pipeline(str(wav), min_speakers=1, max_speakers=max(1, int(max_speakers)))
            annotation = result[0] if isinstance(result, tuple) else result
            out: list[dict[str, Any]] = []
            for turn, _, speaker in annotation.itertracks(yield_label=True):
                s = float(turn.start) + float(start)
                e = float(turn.end) + float(start)
                if end is not None and s >= float(end):
                    continue
                e = min(e, float(end)) if end is not None else e
                if e <= s:
                    continue
                out.append({"start": s, "end": e, "speaker": str(speaker), "confidence": 0.95})
            return out
    except Exception:
        return []


def speaker_at(segments: list[dict[str, Any]], t: float) -> str | None:
    active = [s for s in segments if float(s.get("start", 0)) <= t <= float(s.get("end", 0))]
    if not active:
        return None
    return str(max(active, key=lambda x: float(x.get("confidence", 0.5))).get("speaker"))
