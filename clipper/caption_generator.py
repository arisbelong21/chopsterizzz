"""AI + local social content pack generator."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from chopster.ai.prompts import (
    TITLE_CRITIC_DIMENSIONS,
    TITLE_FINAL_COUNT,
    TITLE_MAX_CHARS,
    TITLE_MOBILE_CHARS,
)
from chopster.clipper.transcript_manager import Transcript

#: The brainstorm→critic title pass costs two extra AI calls per clip, so it is
#: opt-out rather than opt-in: without it the pack keeps whatever single-shot
#: titles the model produced (``config["ai_title_two_stage"] = False``).
TITLE_TWO_STAGE_DEFAULT = True

#: Chapter rules taken from YouTube's own requirements: chapters must start at
#: 0:00, count at least three, and sit at least ten seconds apart. A clip shorter
#: than :data:`CHAPTER_MIN_SPAN` cannot satisfy that, so it gets no block at all.
CHAPTER_MIN_SPAN = 45.0
CHAPTER_MIN_STEP = 10.0
CHAPTER_MAX = 8

#: Room reserved for the description *body* before the chapter block is added.
#: Every platform truncates an over-long description from the tail, so the body
#: is capped here first: the chapter block lives at the tail (right before the
#: closing CTA) and must never be the part that gets cut off after the fact.
#: Comfortably below YouTube's 5000-character description limit, leaving space
#: for the block itself plus the hashtag line the publish mapper appends.
CHAPTER_BODY_BUDGET = 4400

#: A line that already looks like a chapter ("0:00 Intro") means somebody —
#: the model or an earlier pass — wrote a block, so it is never doubled.
_CHAPTER_LINE_RE = re.compile(r"^\s*\d{1,2}:\d{2}(?::\d{2})?\s+\S", re.MULTILINE)


#: Renderer labels that are artefacts of the cut, not hooks. Auto Clip Studio
#: and Content Clipper AI both name their exports this way ("00:15 - 00:45",
#: "menit 1 sampai 2"), and a content pack that echoes them back looks like a bot.
_TECHNICAL_TITLE_RE = re.compile(
    r"^\s*(?:\d{1,2}[:.]\d{2}\s*[-\u2013\u2014~]\s*\d{1,2}[:.]\d{2}"
    r"|menit\s*\d+\s*(?:sampai|s/d|[-\u2013\u2014~])\s*(?:menit\s*)?\d+"
    r"|clip\s*#?\s*\d+"
    r"|\d{1,2}\s*[-\u2013\u2014~]\s*\d{1,2}\s*menit)"
    r"\s*$",
    re.IGNORECASE,
)

#: Distinct shapes so two different videos never receive the same sentence.
#: Titles are authored in CAPSLOCK because that is the house style of the content
#: pack ("judul wajib huruf kapital semua"); ``_shout`` upper-cases the topic.
_TITLE_SHAPES = (
    "{topic}: YANG BIKIN BERHENTI SCROLL",
    "KENAPA {topic} JADI BAHAN PERHATIAN?",
    "{topic} - BAGIAN YANG JARANG DIBAHAS",
    "INI YANG TERJADI SAAT {topic} DIBAHAS",
    "{topic}: MOMEN PALING PENTING DI VIDEO INI",
    "JANGAN LEWATKAN BAGIAN {topic}",
    "{topic} DARI SUDUT YANG BERBEDA",
    "PELAJARAN PENTING SOAL {topic}",
)

#: Every caption closes with a CTA; a pack without one is the "kelihatan bot"
#: complaint. Rotated by the transcript hash so two videos never share a line.
_CTA_OPTIONS = (
    "Tulis pendapatmu di komentar.",
    "Follow untuk bagian berikutnya.",
    "Bagikan kalau kamu setuju.",
    "Tonton sampai habis, lalu bilang mana yang paling masuk akal.",
)

_CAPTION_SHAPES = (
    "{quote} Menurut kamu, bagian ini masuk akal atau justru sebaliknya? Tulis di komentar.",
    "Ada satu poin soal {topic} yang gampang kelewat: {quote} Bagian mana yang paling kamu setujui?",
    "{quote} Simpan video ini, lalu bilang bagian mana yang paling kamu setujui.",
    "Kalau kamu di posisi ini, keputusanmu akan sama? ({topic}) Tulis di komentar.",
    "Bagian {topic} ini bikin sudut pandang berubah. {quote} Tonton sampai habis.",
    "Ini kenapa {topic} layak didengar utuh: {quote} Follow untuk lanjutannya.",
)

#: Hashtags are part of the visible caption, so filler words ("#yang", "#dengan")
#: read as machine generated. Only real topic words become tags.
_HASHTAG_STOPWORDS = frozenset({
    "yang", "dengan", "untuk", "dari", "pada", "dalam", "akan", "tidak", "bukan",
    "juga", "karena", "tapi", "tetapi", "atau", "ini", "itu", "saja", "bisa",
    "harus", "sudah", "masih", "lebih", "sangat", "kita", "kami", "mereka", "kamu",
    "seorang", "adalah", "saat", "ketika", "kalau", "jika", "agar", "supaya",
    "oleh", "tentang", "hal", "sama", "banyak", "begitu", "secara", "tersebut",
    "punya", "buat", "jadi", "memang", "cuma", "hanya", "video", "bagian", "orang",
    "the", "and", "that", "this", "with", "from", "have", "been", "will", "your",
    "about", "they", "there", "what", "when", "were", "just", "like", "very",
})

#: Punctuation a bot leaves glued to the topic, including the dash variants of
#: renderer labels, without pasting non-ASCII literals into the source.
_EDGE_PUNCT_RE = re.compile(r"^[^\w]+|[^\w?!]+$")


def is_technical_title(title: str) -> bool:
    """True for renderer/bot labels such as ``00:15 - 00:45`` or ``menit 1 sampai 2``."""
    text = str(title or "").strip()
    if not text:
        return False
    if _TECHNICAL_TITLE_RE.match(text):
        return True
    # A "title" without a single word carries no meaning either.
    return not re.search(r"[A-Za-z\u00c0-\u00ff]{3,}", text)


def _shout(text: str, limit: int = 90) -> str:
    """CAPSLOCK display text without the punctuation a template would keep."""
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    clean = _EDGE_PUNCT_RE.sub("", clean).strip().upper()
    if len(clean) <= limit:
        return clean
    cut = clean[:limit]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return _EDGE_PUNCT_RE.sub("", cut).strip()


def _trim(text: str, limit: int) -> str:
    """Cut at a word boundary instead of chopping a sentence in half."""
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(clean) <= limit:
        return clean
    cut = clean[:limit]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" ,;:") + "..."


def _trim_body(text: str, limit: int) -> str:
    """Cap a long description while keeping its paragraph breaks.

    ``_trim`` flattens whitespace, so it is only safe for single-line fields;
    a description that lost its blank lines stops reading like a YouTube
    description at all. Whole trailing paragraphs are dropped here instead,
    which keeps the opening hook intact and leaves the room the chapter block
    needs.
    """
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    kept: list[str] = []
    used = 0
    for part in [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]:
        if used + len(part) + 2 > limit:
            break
        kept.append(part)
        used += len(part) + 2
    if kept:
        return "\n\n".join(kept)
    # One single paragraph longer than the cap: there is no blank line left to
    # protect, so the word-boundary trim can cut it.
    return _trim(text, limit)


def _natural_hashtags(words: list[str], topic: str, limit: int = 12) -> list[str]:
    """Hashtags built from THIS clip's own vocabulary, never a generic tag list."""
    tags: list[str] = []
    for raw in list(words) + re.findall(r"\b\w{4,}\b", str(topic or "").lower()):
        word = re.sub(r"[^\w]", "", str(raw or "").lower())
        if len(word) < 4 or word.isdigit() or word[:1].isdigit() or word in _HASHTAG_STOPWORDS:
            continue
        if word not in tags:
            tags.append(word)
    return [f"#{w}" for w in tags[:limit]]


#: A pack without a CTA is the "kelihatan bot" complaint, so every caption and
#: description must finish with one of these invitations.
_CTA_MARKERS = ("komentar", "follow", "bagikan", "simpan video", "tonton sampai", "subscribe")


def _as_text_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v or "").strip()]
    return []


def _has_cta(text: str) -> bool:
    low = str(text or "").lower()
    return any(marker in low for marker in _CTA_MARKERS)


def _enforce_house_style(pack: dict, working_title: str, text: str) -> dict:
    """Apply the content-pack house style to a pack, whoever wrote it.

    The prompt asks for CAPSLOCK titles, a hook + CTA description and natural
    hashtags, but instructions can be ignored by the model. This guard makes the
    Studio-visible pack safe either way: no Title-Case title, no caption without
    a CTA, no renderer label, and never the source video's own title handed back.
    """
    if not isinstance(pack, dict):
        return pack
    source = str(working_title or "").strip().lower()
    words = re.findall(r"\b\w{4,}\b", str(text or "").lower())

    titles: list[str] = []
    for raw in _as_text_list(pack.get("title")) + _as_text_list(pack.get("titles")):
        # Mobile-first length discipline: a title that outruns the phone cut
        # hides its own reason to click, so it is trimmed at a word boundary
        # here instead of being truncated later by the platform.
        title = _shout(raw, TITLE_MAX_CHARS)
        if not title or title in titles:
            continue
        # A renderer label or the source title itself is never a pack title.
        if is_technical_title(title) or title.lower() == source:
            continue
        titles.append(title)
    if not titles:
        topic = _shout(" ".join(words[:3]), 48) or _shout(source, 48) or "VIDEO INI"
        titles = [_shout(_TITLE_SHAPES[i % len(_TITLE_SHAPES)].format(topic=topic), TITLE_MAX_CHARS)
                  for i in range(3)]
    pack["titles"] = titles
    pack["title"] = titles[0]

    cta = _CTA_OPTIONS[len(words) % len(_CTA_OPTIONS)]
    for key in ("caption", "description", "description_short", "description_long"):
        value = str(pack.get(key) or "").strip()
        if value and not _has_cta(value):
            pack[key] = _trim(f"{value} {cta}", 5000)
    pack["captions"] = [c if _has_cta(c) else _trim(f"{c} {cta}", 2200)
                        for c in _as_text_list(pack.get("captions"))]
    if not pack["captions"] and str(pack.get("description_short") or "").strip():
        pack["captions"] = [str(pack["description_short"]).strip()]

    tags: list[str] = []
    for raw in _as_text_list(pack.get("hashtags")):
        word = re.sub(r"[^\w]", "", str(raw).lstrip("#").lower())
        if len(word) < 4 or word.isdigit() or word[:1].isdigit() or word in _HASHTAG_STOPWORDS:
            continue
        if f"#{word}" not in tags:
            tags.append(f"#{word}")
    if len(tags) < 5:
        for tag in _natural_hashtags(words[:24], ""):
            if tag not in tags:
                tags.append(tag)
    if tags:
        pack["hashtags"] = tags[:15]

    thumbs: list[str] = []
    for raw in _as_text_list(pack.get("thumbnail_text")):
        thumb = _shout(raw, 40)
        if thumb:
            thumbs.append(thumb)
    if thumbs:
        pack["thumbnail_text"] = thumbs
    return pack


# ---------------------------------------------------------------- title stage

def _title_two_stage_enabled(config: dict, pack: dict) -> bool:
    """Whether this pack may spend two extra AI calls on the title pipeline.

    A pack that already came back from the local fallback means the AI was
    unreachable: paying two more calls there only delays the result, so the
    single-shot titles are kept as they are.
    """
    if not bool((config or {}).get("ai_title_two_stage", TITLE_TWO_STAGE_DEFAULT)):
        return False
    return str(pack.get("source") or "") not in {"local", "local-fallback"}


def _parse_json_object(text: str) -> Any:
    """Parse a JSON reply that may still be wrapped in a ```json fence."""
    raw = str(text or "").strip()
    if "```" in raw:
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.S | re.I)
        if match:
            raw = match.group(1).strip()
    return json.loads(raw)


def _score_rows(payload: Any) -> list[dict]:
    """Normalise whatever the critic returned into a list of score rows."""
    if isinstance(payload, dict):
        rows = payload.get("scores")
        if rows is None:
            rows = payload.get("candidates") or payload.get("titles") or []
    else:
        rows = payload
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for row in rows:
        if isinstance(row, dict):
            out.append(row)
        elif str(row or "").strip():
            out.append({"title": str(row).strip()})
    return out


def _title_key(text: str) -> str:
    """Case/space-insensitive identity, so the critic's echo matches a candidate."""
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def select_titles(candidates: list, scored: Any, avoid_titles: list[str] | None = None,
                  count: int = TITLE_FINAL_COUNT) -> list[str]:
    """Rank brainstormed titles and keep the best ones from *different* angles.

    Pure function (no AI, no IO) so the selection rule is testable on its own:
    every candidate is scored 1-10 on specificity/curiosity/clarity/honesty, the
    dishonest ones drop out, and the top slots are filled greedily by angle so a
    pack never offers four paraphrases of the same idea.
    """
    avoid = {_title_key(t) for t in (avoid_titles or []) if str(t).strip()}
    pool: list[dict] = []
    seen: set[str] = set()
    for candidate in candidates or []:
        if isinstance(candidate, dict):
            title = _shout(candidate.get("title"), TITLE_MAX_CHARS)
            angle = str(candidate.get("angle") or "").strip().lower()
        else:
            title = _shout(candidate, TITLE_MAX_CHARS)
            angle = ""
        key = _title_key(title)
        if not title or not key or key in seen or key in avoid or is_technical_title(title):
            continue
        seen.add(key)
        pool.append({"title": title, "angle": angle, "total": 0, "honesty": 0})
    if not pool:
        return []

    by_key = {_title_key(row.get("title")): row
              for row in _score_rows(scored) if _title_key(row.get("title"))}
    for item in pool:
        row = by_key.get(_title_key(item["title"]), {})
        total = 0
        for dim in TITLE_CRITIC_DIMENSIONS:
            try:
                value = max(0, min(10, int(float(row.get(dim) or 0))))
            except (TypeError, ValueError):
                value = 0
            total += value
            if dim == "honesty":
                item["honesty"] = value
        item["total"] = total
        item["angle"] = item["angle"] or str(row.get("angle") or "").strip().lower()

    if by_key:
        # Honesty is the clickbait guard: a title the critic marked as
        # unsupported never ships, however curious it reads.
        kept = [i for i in pool if i["honesty"] > 3]
    else:
        # The critic answered with nothing usable: keep the brainstorm pool
        # instead of throwing the whole stage away.
        kept = list(pool)
    if not kept:
        return []
    # Equal scores: prefer the title closest to the mobile ideal length, because
    # it is the one that still reads whole on a phone.
    kept.sort(key=lambda item: (item["total"], -abs(len(item["title"]) - TITLE_MOBILE_CHARS)),
              reverse=True)

    picked: list[dict] = []
    picked_ids: set[int] = set()
    used_angles: set[str] = set()
    for item in kept:
        if len(picked) >= count:
            break
        if item["angle"] and item["angle"] in used_angles:
            continue
        picked.append(item)
        picked_ids.add(id(item))
        used_angles.add(item["angle"])
    # Fewer distinct angles than requested: fill the rest with the next best,
    # so the caller never receives fewer options than the pool can supply.
    for item in kept:
        if len(picked) >= count:
            break
        if id(item) not in picked_ids:
            picked.append(item)
            picked_ids.add(id(item))
    return [item["title"] for item in picked]


def refine_titles_two_stage(pack: dict, text: str, config: dict, *, language: str = "id",
                            working_title: str = "",
                            avoid_titles: list[str] | None = None,
                            count: int = TITLE_FINAL_COUNT) -> dict:
    """Spend two AI calls (brainstorm → critic) to promote genuinely better titles.

    Adapted from the grounded long-form pipeline: stage 1 widens the pool across
    seven angles, stage 2 scores specificity/curiosity/clarity/honesty and keeps
    the highest scoring candidates whose angles differ. The pass is strictly
    additive — any failure leaves the pack's existing titles untouched.
    """
    existing = _as_text_list(pack.get("titles")) or _as_text_list(pack.get("title"))
    try:
        from chopster.ai.orchestrator import orchestrator_from_config
        from chopster.ai.prompts import (
            TITLE_BRAINSTORM_SYSTEM,
            TITLE_CRITIC_SYSTEM,
            title_brainstorm_prompt,
            title_critic_prompt,
        )
        from chopster.ai.provider_interface import AIRequest

        orchestrator = orchestrator_from_config(config)
        health = getattr(orchestrator, "health", None)
        if health is not None and getattr(health, "circuit_open", False):
            return pack
        timeout = int((config or {}).get("ai_timeout") or 90)
        temperature = float((config or {}).get("ai_temperature") or 0.2)

        def _ask(system: str, prompt: str, max_tokens: int) -> Any:
            req = AIRequest(prompt=prompt, system=system,
                            model=(config or {}).get("ai_model") or "",
                            temperature=temperature, max_tokens=max_tokens, timeout=timeout)
            # No local fallback: the caller already holds usable titles, so an
            # unreachable provider must not answer with a fake candidate list.
            return _parse_json_object(orchestrator.generate(req, local_fallback=None).text)

        brainstormed = _ask(
            TITLE_BRAINSTORM_SYSTEM,
            title_brainstorm_prompt(text, language=language, working_title=working_title,
                                    avoid_titles=avoid_titles),
            4000)
        candidates = brainstormed.get("candidates") if isinstance(brainstormed, dict) else brainstormed
        if not isinstance(candidates, list) or len(candidates) < 3:
            return pack
        scored = _ask(
            TITLE_CRITIC_SYSTEM,
            title_critic_prompt(candidates, text, language=language, count=count),
            4000)
        chosen = select_titles(candidates, scored, avoid_titles, count)
        if not chosen:
            return pack
        merged = list(chosen)
        for title in existing:
            if title not in merged:
                merged.append(title)
        # Best-first: the pack's single ``title`` is the winner, the rest of the
        # brainstorm stays visible as options in the Studio.
        pack["titles"] = merged[:10]
        pack["title"] = pack["titles"][0]
        angles_by_title = {
            _shout(c.get("title"), TITLE_MAX_CHARS): str(c.get("angle") or "").strip()
            for c in candidates if isinstance(c, dict)
        }
        pack["title_angles"] = [angles_by_title.get(title, "") for title in chosen]
        pack["title_source"] = "brainstorm-critic"
    except Exception:
        # Fail open: an unhelpful brainstorm must never cost the pack its titles.
        return pack
    return pack


# ----------------------------------------------------------- description part

def _chapter_stamp(seconds: float) -> str:
    """``m:ss`` (or ``h:mm:ss``) exactly the way YouTube chapters are written."""
    total = max(0, int(round(float(seconds or 0))))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _chapter_label(text: str, limit: int = 42) -> str:
    """A short chapter name taken from what was actually said at that moment."""
    clean = re.sub(r"\s+", " ", re.sub(r"[^\w\s?!,.:'-]", " ", str(text or ""))).strip()
    if not clean:
        return ""
    label = ""
    for word in clean.split(" "):
        candidate = f"{label} {word}".strip()
        if len(candidate) > limit:
            break
        label = candidate
    label = (label or clean.split(" ")[0]).rstrip(" ,;:-.")
    return (label[:1].upper() + label[1:]) if label else ""


def chapter_lines(segments, *, min_span: float = CHAPTER_MIN_SPAN,
                  min_step: float = CHAPTER_MIN_STEP, limit: int = CHAPTER_MAX) -> list[str]:
    """``0:00 Pembuka`` lines built from the clip's REAL transcript timing.

    Timestamps are read from the transcript, never written by the model, so a
    chapter can never point at a moment that does not exist. A clip too short for
    YouTube's own rules — first chapter at 0:00, at least three chapters, ten
    seconds apart — returns an empty list instead of a broken block, and every
    offset is relative to the clip's first segment so the list always starts at
    0:00.
    """
    usable: list[tuple[float, float, str]] = []
    for seg in segments or []:
        try:
            start = float(getattr(seg, "start", 0) or 0)
            end = float(getattr(seg, "end", start) or start)
        except (TypeError, ValueError):
            continue
        label = _chapter_label(getattr(seg, "text", ""))
        if label:
            usable.append((start, end, label))
    if not usable:
        return []
    usable.sort(key=lambda item: item[0])
    if usable[-1][1] - usable[0][0] < min_span:
        return []
    lines = [f"0:00 {usable[0][2]}"]
    last_start = usable[0][0]
    for start, _end, label in usable[1:]:
        if len(lines) >= limit:
            break
        if start - last_start < min_step:
            continue
        lines.append(f"{_chapter_stamp(start - usable[0][0])} {label}")
        last_start = start
    # Two chapters are not chapters: YouTube needs three, so ship none instead.
    return lines if len(lines) >= 3 else []


def _with_chapters(text: str, block: str) -> str:
    """Slip the chapter list in *before* the closing CTA so the CTA stays last.

    The description is capped first so the block always fits: platforms truncate
    an over-long description from the tail, and the chapters that were just built
    must never be what gets cut off after the fact (see
    :data:`CHAPTER_BODY_BUDGET`).
    """
    room = max(200, CHAPTER_BODY_BUDGET - len(block) - 2)
    parts = [part.strip() for part in re.split(r"\n\s*\n", str(text or "").strip()) if part.strip()]
    if parts and _has_cta(parts[-1]):
        body = _trim_body("\n\n".join(parts[:-1]), room)
        return f"{body}\n\n{block}\n\n{parts[-1]}" if body else f"{block}\n\n{parts[-1]}"
    body = _trim_body(str(text or "").strip(), room)
    return f"{body}\n\n{block}" if body else block


def ensure_chapters(pack: dict, segments) -> dict:
    """Add a YouTube chapter block to the long description of a long enough clip.

    Chapters already present in the description are kept as they are — the block
    is never doubled, and the prompt forbids the model from inventing timestamps.
    """
    if not isinstance(pack, dict):
        return pack
    field = "description_long" if str(pack.get("description_long") or "").strip() else "description"
    current = str(pack.get(field) or "")
    if _CHAPTER_LINE_RE.search(current):
        return pack
    lines = chapter_lines(segments)
    if not lines:
        return pack
    pack[field] = _with_chapters(current, "\n".join(lines))
    return pack


def _finalize_pack(pack: dict, *, text: str, working_title: str, config: dict,
                   language: str, avoid_titles: list[str] | None, segments) -> dict:
    """House-style guard → optional brainstorm→critic titles → chapters.

    Every return path of :func:`generate_captions` ends here, so an AI pack, a
    force-distinct retry and the local fallback all leave with the same rules
    applied.
    """
    pack = _enforce_house_style(pack, working_title, text)
    if _title_two_stage_enabled(config, pack):
        pack = refine_titles_two_stage(pack, text, config, language=language,
                                       working_title=working_title,
                                       avoid_titles=avoid_titles)
        pack = _enforce_house_style(pack, working_title, text)
    return ensure_chapters(pack, segments)


def _first_sentence(text: str, limit: int = 220) -> str:
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    if not clean:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", clean)
    return (parts[0] if parts else clean).strip()[:limit]


def _local_captions(transcript: Transcript, platform: str, tone: str, count: int, style: str,
                    language: str, highlight_context: list[dict] | None = None,
                    working_title: str = "") -> dict:
    """Deterministic local pack built from the clip's OWN words.

    Two different transcripts can never end up with the same description: the
    opening line is a real quote from the clip, the shapes are picked from a
    hash of that text, and template filler is only used when there is no
    transcript at all.
    """
    text = " ".join(s.text for s in transcript.segments).strip()
    snippet = text[:2400]
    words = re.findall(r"\b\w{4,}\b", snippet.lower())
    freq: dict[str, int] = {}
    for w in words:
        freq[w] = freq.get(w, 0) + 1
    top = sorted(freq, key=lambda k: (-freq[k], k))[:8]
    context = highlight_context or []
    top_candidate = context[0] if context else None
    quote = _first_sentence(snippet, 220)
    short_quote = _first_sentence(snippet, 150)
    # A renderer label ("menit 1 sampai 2") is never a topic; the clip's own
    # words are.
    topic = ""
    if top_candidate:
        candidate = str(top_candidate.get("hook") or "").strip()
        if candidate and not is_technical_title(candidate):
            topic = candidate[:120]
    if not topic:
        topic = ", ".join(top[:3])
    if not topic:
        topic = _first_sentence(snippet, 90) or (working_title if not is_technical_title(working_title) else "")
    if not topic:
        topic = "video ini"
    seed = int(hashlib.md5((snippet or text or topic).encode("utf-8", "ignore")).hexdigest()[:8], 16)
    n_titles = max(1, int(count))
    n_captions = max(1, int(count))
    # A hook that starts mid-sentence reads like a scraped caption; capitalise it.
    hook = (short_quote[:1].upper() + short_quote[1:]) if short_quote else ""
    # CAPSLOCK titles are the content-pack house style. The topic is shouted and
    # clipped *before* the shapes are rendered, so two long topics can never
    # truncate into the same title.
    topic_shout = _shout(topic, 48) or "VIDEO INI"
    titles = [_shout(_TITLE_SHAPES[(seed + i) % len(_TITLE_SHAPES)].format(topic=topic_shout), 90)
              for i in range(n_titles)]
    captions = [_CAPTION_SHAPES[((seed // 7) + i) % len(_CAPTION_SHAPES)].format(
        topic=topic, quote=hook or f"Video ini membahas {topic}.")
        for i in range(n_captions)]
    # Natural hashtags: this clip's own vocabulary instead of a generic list.
    hashtags = _natural_hashtags(top, topic)
    details = ", ".join(top[:6]) or topic
    opening = hook or f"Video ini membahas {topic}."
    # Hook first, CTA last. The CTA rotates per transcript hash so no two clips
    # in the same batch close with the same sentence.
    cta = _CTA_OPTIONS[seed % len(_CTA_OPTIONS)]
    desc_short = _trim(f"{opening} Fokus pembahasannya {topic}. {cta}", 300)
    desc_long = (
        f"{opening}\n\n"
        f"Di bagian ini pembahasan bergerak di sekitar {topic}. "
        f"Detail yang muncul dalam percakapan: {details}. "
        "Perhatikan bagaimana poinnya dibuka, contoh yang dipakai, lalu tanggapan "
        "terhadap sudut pandang lain — konteks sebelum dan sesudah potongan ini "
        "membantu memahami pembahasannya secara utuh.\n\n"
        f"Kata kunci yang menandai bagian ini: {details}.\n\n"
        f"Ada bagian yang menurutmu perlu ditambahkan? {cta}"
    )
    return {
        "titles": titles,
        "title": titles[0],
        "captions": captions,
        "hashtags": hashtags,
        "keywords": top,
        "description_short": desc_short,
        "description_long": desc_long,
        "description": desc_short,
        "pinned_comment": "Kamu lebih setuju atau tidak setuju dengan poin di video ini? Jelaskan alasannya.",
        "thumbnail_text": [_shout(topic, 24) or "BAGIAN INI", "MENURUT KAMU?",
                           "BAGIAN INI PENTING", "DENGAR SAMPAI AKHIR"],
        "highlight_start": top_candidate.get("start") if top_candidate else None,
        "highlight_end": top_candidate.get("end") if top_candidate else None,
        "highlight_score": top_candidate.get("score") if top_candidate else None,
        "highlight_hook": top_candidate.get("hook") if top_candidate else None,
        "source": "local",
    }


def generate_captions(transcript: Transcript | str, config: dict, platform: str = "TikTok", tone: str = "casual", language: str = "id", count: int = 5, style: str = "hooks") -> dict:
    text = transcript.raw_text if isinstance(transcript, Transcript) else str(transcript)
    if isinstance(transcript, Transcript) and not text:
        text = " ".join(s.text for s in transcript.segments)
    highlight_context = config.get("highlight_context") or []
    # The clip's renderer title is context to REPLACE, never a title to reuse.
    working_title = str(config.get("source_title") or config.get("working_title") or "").strip()
    avoid_titles = [str(t) for t in (config.get("avoid_titles") or []) if str(t).strip()][:8]
    avoid_descriptions = [str(d) for d in (config.get("avoid_descriptions") or [])
                          if str(d).strip()][:6]
    # When the user has selected/highlighted moments, feed the AI their local
    # transcript context first. This prevents a long podcast's opening minutes
    # from dominating titles/descriptions for a later selected clip.
    if isinstance(transcript, Transcript) and highlight_context:
        focused_parts=[]
        for item in highlight_context[:6]:
            try:
                st=float(item.get("start",0)); en=float(item.get("end",st+30))
            except Exception:
                continue
            excerpt=" ".join(seg.text for seg in transcript.segments if seg.end > max(0,st-12) and seg.start < en+12).strip()
            if excerpt:
                focused_parts.append(f"[{st:.1f}-{en:.1f}] {excerpt}")
        if focused_parts:
            text = "\n\n".join(focused_parts) + "\n\nFULL TRANSCRIPT CONTEXT:\n" + text[:6000]
    # Always route through the central orchestrator. Text features use Settings AI
    # when configured, otherwise they fall back directly to the deterministic
    # local engine. Embedded Gemini remains dedicated to visual reasoning.
    try:
        from chopster.ai.prompts import CAPTION_SYSTEM, caption_prompt
        from chopster.ai.schemas import validate_caption
        from chopster.ai.provider_interface import AIRequest
        prompt = caption_prompt(text[:12000], platform=platform, tone=tone, language=language,
                                count=count, style=style, working_title=working_title,
                                avoid_titles=avoid_titles,
                                avoid_descriptions=avoid_descriptions,
                                force_distinct=bool(config.get("force_distinct")))
        if highlight_context:
            best = highlight_context[0]
            prompt += (
                f"\n\nVIRAL ANALYZER CONTEXT\nStart: {best.get('start',0)}s\nEnd: {best.get('end',0)}s\n"
                f"Score: {best.get('score',0)}/100\nHook: {best.get('hook','')}\n"
                f"Reason: {best.get('reason','')}\nExcerpt: {best.get('excerpt','')}"
            )
        master_context = config.get("master_context") or {}
        if master_context:
            prompt += "\n\nMASTER EDITOR BRAIN CONTEXT\n" + json.dumps(master_context, ensure_ascii=False)[:5000]
            prompt += "\nGunakan konteks ini untuk membuat judul/caption yang spesifik pada clip, bukan generik."
        from chopster.ai.orchestrator import orchestrator_from_config
        orchestrator = orchestrator_from_config(config)
        req = AIRequest(prompt=prompt, system=CAPTION_SYSTEM, model=config.get("ai_model") or "", temperature=float(config.get("ai_temperature") or .2), max_tokens=max(5000, int(config.get("ai_max_tokens") or 8000)), timeout=int(config.get("ai_timeout") or 90))
        def _local_json():
            return json.dumps(_local_captions(tr_local, platform, tone, count, style, language,
                                              highlight_context, working_title),
                              ensure_ascii=False)
        tr_local = transcript if isinstance(transcript, Transcript) else Transcript(language=language, segments=[])
        if isinstance(transcript, str):
            from chopster.clipper.transcript_manager import Segment
            tr_local = Transcript(language=language, segments=[Segment(start=0, end=10, text=text[:800])], raw_text=text)
        resp = orchestrator.generate(req, local_fallback=_local_json)
        t = resp.text.strip()
        if "```" in t:
            m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.S)
            if m:
                t = m.group(1)
        data = json.loads(t)
        out = validate_caption(data)
        if isinstance(out, dict):
            raw_meta = resp.raw if isinstance(resp.raw, dict) else {}
            out.setdefault("source", raw_meta.get("provider") or "settings-ai")
            if raw_meta.get("ai_error") and not out.get("ai_error"):
                out["ai_error"] = str(raw_meta.get("ai_error"))[:300]
        return _finalize_pack(out, text=text, working_title=working_title, config=config,
                              language=language, avoid_titles=avoid_titles,
                              segments=tr_local.segments)
    except Exception as exc:
        tr = transcript if isinstance(transcript, Transcript) else Transcript(language=language, segments=[])
        if isinstance(transcript, str):
            from chopster.clipper.transcript_manager import Segment
            tr = Transcript(language=language, segments=[Segment(start=0, end=10, text=text[:800])], raw_text=text)
        res = _local_captions(tr, platform, tone, count, style, language, highlight_context,
                              working_title)
        res["source"] = "local-fallback"
        res["ai_error"] = str(exc)[:300]
        return _finalize_pack(res, text=text, working_title=working_title, config=config,
                              language=language, avoid_titles=avoid_titles, segments=tr.segments)
