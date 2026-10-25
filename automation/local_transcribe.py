"""Local Whisper evidence for media that carries no transcript of its own.

Studio can be handed a clip whose export never attached a transcript (a file
picked from disk, or a hand-off that failed to carry one). The AI Content Pack
still has to describe *that* video, so the missing evidence is produced locally
instead of borrowing the transcript of whichever video happens to be open
elsewhere (spec §4.4/§7.4 — one clip, one context).

Transcription quality is a hard requirement here, not a preference: the pack
prompt is only ever as good as the words it receives, and lossy words are what
turned descriptions into generic templates. The model floor is therefore
``small``; a request below it is *upgraded* (never silently downgraded), while
the user's choice of medium / large-v2 / large-v3 is honoured as-is.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable

from chopster.automation.context import ClipContext, build_clip_context

#: Whisper models good enough to feed the AI Content Pack. The first entry is
#: the floor applied when the configured model is weaker than this.
PACK_MODELS: tuple[str, ...] = ("small", "medium", "large-v2", "large-v3")
MODEL_FLOOR: str = PACK_MODELS[0]

#: Names that mean something stronger/different than they read, mapped to the
#: canonical id we actually load.
_ALIASES: dict[str, str] = {
    "large": "large-v3",
    "large-v3-turbo": "large-v3",
    "medium.en": "medium",
    "small.en": "small",
}


def normalize_pack_model(value: Any) -> str:
    """Return a pack-grade Whisper model name — never below :data:`MODEL_FLOOR`.

    ``tiny``, ``base``, their ``.en`` variants and unknown names all fall back to
    the floor: a pack built on lossy transcription is exactly the "deskripsi
    template / kayak bot" complaint this module exists to prevent.
    """
    name = str(value or "").strip().lower()
    if not name:
        return MODEL_FLOOR
    name = _ALIASES.get(name, name)
    return name if name in PACK_MODELS else MODEL_FLOOR


def pack_whisper_model(config: Any = None) -> str:
    """Whisper model the AI Content Pack may use, already floored."""
    getter = getattr(config, "get", None)
    if not callable(getter):
        return MODEL_FLOOR
    for key in ("aut_pack_whisper_model", "transcribe_model"):
        try:
            chosen = str(getter(key) or "").strip()
        except Exception:
            chosen = ""
        if chosen:
            return normalize_pack_model(chosen)
    return MODEL_FLOOR


def _fingerprint(path: Path) -> str:
    """Stable id for one media file: the same file keeps its identity, and two
    different files can never share a context id."""
    try:
        stat = path.stat()
        payload = f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
    except Exception:
        payload = str(path)
    return hashlib.md5(payload.encode("utf-8", "ignore")).hexdigest()[:16]


def _setting(config: Any, key: str, default: str = "") -> str:
    getter = getattr(config, "get", None)
    if not callable(getter):
        return default
    try:
        return str(getter(key) or default)
    except Exception:
        return default


def media_context_from_file(path: str | Path, *, title: str = "", config: Any = None,
                            language: str = "auto", word_timestamps: bool = True,
                            progress_cb: Callable[[int, str], None] | None = None,
                            cancel_check: Callable[[], bool] | None = None) -> ClipContext:
    """Transcribe ``path`` locally and return evidence bound to *that* file.

    The returned :class:`ClipContext` proves which transcript the AI may use, so
    a locally imported video gets its own description instead of one written
    from another video's words.
    """
    target = Path(str(path or ""))
    if not target.exists():
        raise RuntimeError(f"File tidak ditemukan: {target.name or path}")
    model = pack_whisper_model(config)
    try:
        from chopster.clipper.transcript_manager import (
            load_cached_transcript,
            save_cached_transcript,
            transcribe,
        )
    except Exception as exc:          # pragma: no cover - import guard
        raise RuntimeError(
            "Transkripsi lokal belum tersedia (faster-whisper belum terpasang). "
            "Install lebih dulu supaya AI bisa membaca video ini.") from exc

    if cancel_check and cancel_check():
        raise RuntimeError("Dibatalkan")
    transcript = None
    try:
        transcript = load_cached_transcript(target, model, language, bool(word_timestamps))
    except Exception:
        transcript = None
    if transcript is None or not getattr(transcript, "segments", None):
        if progress_cb:
            try:
                progress_cb(0, f"Transkripsi lokal «{target.name}» (Whisper {model})…")
            except Exception:
                pass
        transcript = transcribe(
            target,
            model=model,
            model_dir=_setting(config, "transcribe_model_dir", ""),
            language=language,
            device=_setting(config, "transcribe_device", "auto") or "auto",
            word_timestamps=bool(word_timestamps),
            progress_cb=progress_cb,
        )
        try:
            save_cached_transcript(target, model, language, bool(word_timestamps), transcript)
        except Exception:
            pass
    if cancel_check and cancel_check():
        raise RuntimeError("Dibatalkan")

    segments: list[dict[str, Any]] = []
    for seg in getattr(transcript, "segments", None) or []:
        text = str(getattr(seg, "text", "") or "").strip()
        if not text:
            continue
        segments.append({"start": float(getattr(seg, "start", 0.0) or 0.0),
                         "end": float(getattr(seg, "end", 0.0) or 0.0),
                         "text": text})
    if not segments:
        raise RuntimeError(
            f"Transkripsi «{target.name}» kosong — tidak ada ucapan yang bisa dibaca AI.")

    return build_clip_context(
        source="manual",
        source_asset_id=f"local:{_fingerprint(target)}",
        media_path=str(target),
        title=str(title or target.stem),
        language=str(getattr(transcript, "language", "") or "") or "id",
        clip_start=0.0,
        clip_end=0.0,
        segments=segments,
        evidence=f"transkripsi Whisper lokal ({model}) untuk file ini",
    )