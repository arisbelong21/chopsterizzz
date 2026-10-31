"""Per-video context pack — what the AI may know about ONE clip.

The AI Content Pack must describe the clip the user actually selected, so a
context is always bound to a single source asset (spec §7.4/§7.6). The evidence
is transcript/subtitle text Chopster already produced:

* Auto Clip Studio passes the analysed transcript it used for the clip render.
* Content Clipper AI passes the transcript it used for the clip export.
* Studio falls back to a persisted project transcript only when the clip source
  path can be proven identical.

This module never invents content and never borrows another video's context: a
missing/insufficient context is reported so the UI can refuse instead of
fabricating metadata (spec §7.3/§8.4).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chopster.clipper.transcript_manager import Segment, Transcript

# Context is read a little before/after the clip so a sentence that starts just
# outside the cut is still understood, without dragging in a different topic.
CLIP_PAD_SECONDS = 12.0
MIN_EXCERPT_CHARS = 40
MAX_EXCERPT_CHARS = 12000

SOURCES = ("acs", "cca", "manual")


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_segments(raw: Any) -> list[dict[str, Any]]:
    """Accept the transcript shapes Chopster already produces.

    Supported: ``[{start, end, text}]``, ``[{start, duration, text}]``,
    ``[{start, end, words:[{start,end,word}]}]`` and ``Transcript``.
    """
    if isinstance(raw, Transcript):
        return [{"start": float(s.start), "end": float(s.end), "text": str(s.text)}
                for s in raw.segments if str(s.text or "").strip()]
    if isinstance(raw, dict):
        for key in ("segments", "transcript", "items", "data"):
            if isinstance(raw.get(key), list):
                return normalize_segments(raw[key])
        return []
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for row in raw:
        if isinstance(row, (list, tuple)) and len(row) >= 3:
            row = {"start": row[0], "end": row[1], "text": row[2]}
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or row.get("line") or "").strip()
        if not text:
            continue
        start = _to_float(row.get("start"))
        if row.get("end") is not None:
            end = _to_float(row.get("end"), start)
        elif row.get("duration") is not None:
            end = start + _to_float(row.get("duration"))
        else:
            end = start + max(1.8, len(text.split()) * 0.38)
        if end <= start:
            end = start + 1.0
        out.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    out.sort(key=lambda r: (r["start"], r["end"]))
    return out


def window_segments(segments: list[dict[str, Any]], start: float, end: float,
                    pad: float = CLIP_PAD_SECONDS) -> list[dict[str, Any]]:
    """Segments overlapping ``[start, end]`` (+pad)."""
    if not segments:
        return []
    if end <= start:
        return list(segments)
    lo = max(0.0, float(start) - float(pad))
    hi = float(end) + float(pad)
    picked = [s for s in segments if float(s["end"]) > lo and float(s["start"]) < hi]
    return picked or [s for s in segments if float(s["end"]) > start and float(s["start"]) < end]


def _clip_excerpt(segments: list[dict[str, Any]], limit: int = MAX_EXCERPT_CHARS) -> str:
    text = " ".join(str(s.get("text") or "").strip() for s in segments).strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


@dataclass
class ClipContext:
    """Evidence-bound description of one finished clip (spec §7.4)."""

    source: str = "manual"
    source_asset_id: str = ""
    project_id: str = ""
    media_path: str = ""
    title: str = ""
    language: str = "id"
    clip_start: float = 0.0
    clip_end: float = 0.0
    evidence: str = ""
    segments: list[dict[str, Any]] = field(default_factory=list)
    #: The analysis AI's own grounded suggestions for this clip. Unlike the
    #: renderer/import ``title`` (a label to REPLACE), these are real copy the
    #: video AI already wrote from the transcript, so publish keeps them as
    #: candidate material instead of throwing them away (spec §8.2).
    ai_title_suggestion: str = ""
    ai_caption_suggestion: str = ""

    @property
    def has_transcript(self) -> bool:
        return bool(self.segments)

    @property
    def excerpt(self) -> str:
        return _clip_excerpt(self.segments)

    def window(self) -> list[dict[str, Any]]:
        return window_segments(self.segments, self.clip_start, self.clip_end)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "source": self.source,
            "source_asset_id": self.source_asset_id,
            "project_id": self.project_id,
            "media_path": self.media_path,
            "title": self.title,
            "language": self.language,
            "clip_start": round(float(self.clip_start), 3),
            "clip_end": round(float(self.clip_end), 3),
            "evidence": self.evidence,
            "segments": [dict(s) for s in self.segments],
            "ai_title_suggestion": self.ai_title_suggestion,
            "ai_caption_suggestion": self.ai_caption_suggestion,
        }

    @staticmethod
    def from_dict(data: Any) -> "ClipContext | None":
        if not isinstance(data, dict):
            return None
        if not any(data.get(k) for k in ("segments", "source_asset_id", "media_path", "evidence")):
            return None
        return ClipContext(
            source=str(data.get("source") or "manual"),
            source_asset_id=str(data.get("source_asset_id") or ""),
            project_id=str(data.get("project_id") or ""),
            media_path=str(data.get("media_path") or ""),
            title=str(data.get("title") or ""),
            language=str(data.get("language") or "id"),
            clip_start=_to_float(data.get("clip_start")),
            clip_end=_to_float(data.get("clip_end")),
            evidence=str(data.get("evidence") or ""),
            segments=normalize_segments(data.get("segments")),
            ai_title_suggestion=str(data.get("ai_title_suggestion") or ""),
            ai_caption_suggestion=str(data.get("ai_caption_suggestion") or ""),
        )


def build_clip_context(*, source: str = "manual", source_asset_id: str = "",
                       project_id: str = "", media_path: str = "", title: str = "",
                       language: str = "id", clip_start: Any = None,
                       clip_end: Any = None, segments: Any = None,
                       evidence: str = "", ai_title_suggestion: str = "",
                       ai_caption_suggestion: str = "") -> ClipContext:
    """Build a context from real evidence only (empty when nothing is provable)."""
    ctx = ClipContext(
        source=(source or "manual").lower() if (source or "").lower() in SOURCES else "manual",
        source_asset_id=str(source_asset_id or ""),
        project_id=str(project_id or ""),
        media_path=str(media_path or ""),
        title=str(title or ""),
        language=str(language or "id"),
        clip_start=_to_float(clip_start),
        clip_end=_to_float(clip_end),
        evidence=str(evidence or ""),
        segments=normalize_segments(segments),
        ai_title_suggestion=str(ai_title_suggestion or ""),
        ai_caption_suggestion=str(ai_caption_suggestion or ""),
    )
    if ctx.clip_end and ctx.clip_end > ctx.clip_start:
        ctx.segments = window_segments(ctx.segments, ctx.clip_start, ctx.clip_end)
    return ctx


def segments_from_words(words: Any, gap: float = 0.7) -> list[dict[str, Any]]:
    """Group word-level ``{start, end, word}`` timestamps into spoken lines.

    Words closer than ``gap`` seconds belong to the same line; a longer pause
    starts a new line. The result is a transcript-shaped list of
    ``{start, end, text}`` rows that ``build_clip_context`` accepts directly.
    Both renderers produce word-level timestamps, so this is what turns those
    into genuine clip-window evidence for the AI Content Pack.
    """
    if not isinstance(words, (list, tuple)):
        return []
    raw: list[dict[str, Any]] = []
    for w in words:
        if not isinstance(w, dict):
            continue
        word = str(w.get("word") or w.get("text") or "").strip()
        if not word:
            continue
        raw.append({"start": _to_float(w.get("start")),
                    "end": max(_to_float(w.get("end"), _to_float(w.get("start"))),
                               _to_float(w.get("start")) + 0.1),
                    "text": word})
    rows = normalize_segments(raw)
    if not rows:
        return []
    out: list[dict[str, Any]] = []
    line: list[dict[str, Any]] = [rows[0]]
    for row in rows[1:]:
        if float(row["start"]) - float(line[-1]["end"]) > float(gap or 0.0):
            out.append({
                "start": round(float(line[0]["start"]), 3),
                "end": round(float(line[-1]["end"]), 3),
                "text": " ".join(str(r["text"]) for r in line),
            })
            line = []
        line.append(row)
    if line:
        out.append({
            "start": round(float(line[0]["start"]), 3),
            "end": round(float(line[-1]["end"]), 3),
            "text": " ".join(str(r["text"]) for r in line),
        })
    return out


def detect_context_language(ctx: Any) -> str:
    """The clip's spoken language from its OWN transcript, or ``"auto"``.

    Shared by the template publish path and the Studio Create tab so "follow
    the video language" never silently falls back to Indonesian when a clip's
    stored ``language`` is still the literal ``"auto"``.
    """
    try:
        from chopster.auto_clip_studio.engine.utils import detect_transcript_language
    except Exception:
        return "auto"
    segments = list(getattr(ctx, "segments", []) or [])
    lines = [{"text": str(s.get("text") or "")}
             for s in segments if isinstance(s, dict)]
    if not lines:
        return "auto"
    title = str(getattr(ctx, "title", "") or "")
    try:
        detected = detect_transcript_language(lines, title)
    except Exception:
        return "auto"
    code = str((detected or {}).get("code") or "").strip().lower()
    return code or "auto"


def resolve_pack_language(ctx: Any, choice: str) -> str:
    """Map a UI language choice (``id``/``en``/``auto``) to a concrete code.

    ``auto`` follows the clip: its stored language first, then a fresh
    detection from the clip's own transcript, and only then Indonesian.
    """
    pref = str(choice or "").strip().lower() or "auto"
    if pref not in ("auto", "video", "follow"):
        return pref
    lang = str(getattr(ctx, "language", "") or "").strip().lower()
    if lang and lang != "auto":
        return lang
    detected = detect_context_language(ctx)
    return detected if detected and detected != "auto" else "id"


def context_status(ctx: ClipContext | None) -> tuple[bool, str]:
    """Is the context strong enough to generate honest metadata?"""
    if ctx is None:
        return False, "no_context"
    if not ctx.segments:
        return False, "no_transcript"
    if len(ctx.excerpt) < MIN_EXCERPT_CHARS:
        return False, "excerpt_too_short"
    return True, "ok"


def focused_transcript(ctx: ClipContext) -> Transcript | None:
    """Transcript limited to the clip window, ready for ``generate_captions``."""
    rows = ctx.window()
    if not rows:
        return None
    segments = [Segment(start=float(r["start"]), end=float(r["end"]), text=str(r["text"]))
                for r in rows]
    return Transcript(language=ctx.language or "id", segments=segments)


def caption_context(ctx: ClipContext) -> dict[str, Any]:
    """``highlight_context`` entries for ``clipper.caption_generator``.

    Feeding the real clip window into the generator is what stops a long
    podcast's opening minutes from describing a clip taken at minute 50.
    """
    if not ctx.segments:
        return {}
    start = ctx.clip_start or float(ctx.segments[0]["start"])
    end = ctx.clip_end or float(ctx.segments[-1]["end"])
    rows = ctx.window()
    excerpt = _clip_excerpt(rows, 1500)
    context: dict[str, Any] = {
        "highlight_context": [{
            "start": round(start, 3),
            "end": round(end, 3),
            "score": 100,
            "hook": str(ctx.title or "")[:120],
            "reason": ctx.evidence or f"clip window {start:.1f}-{end:.1f}s from {ctx.source}",
            "excerpt": excerpt,
        }],
        "clip_context": ctx.to_dict(),
    }
    # Hand the video AI's own grounded copy forward as adoptable suggestions, so
    # a real title/caption it wrote is never discarded at publish time.
    if str(ctx.ai_title_suggestion or "").strip():
        context["ai_title_suggestion"] = str(ctx.ai_title_suggestion).strip()
    if str(ctx.ai_caption_suggestion or "").strip():
        context["ai_caption_suggestion"] = str(ctx.ai_caption_suggestion).strip()
    return context


def same_asset(ctx: ClipContext | None, *, media_path: str = "", source_path: str = "") -> bool:
    """True only when two paths provably point at the same media file."""
    if not str(media_path or "").strip() or not str(source_path or "").strip():
        # Unknown paths must never "match" just because both resolve to the CWD.
        return False
    try:
        a = Path(str(media_path or "")).resolve()
        b = Path(str(source_path or "")).resolve()
    except Exception:
        return False
    if not str(a) or not str(b):
        return False
    return a == b


def dumps(ctx: ClipContext) -> str:
    import json
    return json.dumps(ctx.to_dict(), ensure_ascii=False)