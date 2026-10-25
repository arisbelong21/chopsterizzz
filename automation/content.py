"""ContentMapper — AI Content Pack (existing Chopster) → platform payloads.

Input: the dict produced by chopster.clipper.caption_generator / AI Content Pack:
    titles[], title, captions[], hashtags[], keywords[], description_short,
    description_long, description, pinned_comment, thumbnail_text[]
Output: normalized model, then per-platform PlatformPublishPayload mapping.
(spec §14 — do NOT blindly paste every generated field into every platform.)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from chopster.automation.models import PLATFORMS

# Hard platform limits used for the final payload (spec §8.3).
YT_TITLE_LIMIT = 100
YT_DESCRIPTION_LIMIT = 5000
YT_TAG_TOTAL_LIMIT = 480


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [part for part in re.split(r"[\s,]+", value) if part.strip()]
    return []


def _clean_hashtags(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        tag = str(raw).strip().replace(" ", "")
        if not tag:
            continue
        if not tag.startswith("#"):
            tag = "#" + tag
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
    return out[:30]


@dataclass
class NormalizedContent:
    title: str = ""
    titles: list[str] = field(default_factory=list)
    description: str = ""
    description_long: str = ""
    caption: str = ""
    captions: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    seo_keywords: list[str] = field(default_factory=list)
    pinned_comment: str = ""
    thumbnail_text: list[str] = field(default_factory=list)
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "titles": list(self.titles),
            "description": self.description,
            "description_long": self.description_long,
            "caption": self.caption,
            "captions": list(self.captions),
            "hashtags": list(self.hashtags),
            "seo_keywords": list(self.seo_keywords),
            "pinned_comment": self.pinned_comment,
            "thumbnail_text": list(self.thumbnail_text),
            "source": self.source,
        }


def normalize_content_pack(pack: dict[str, Any] | None) -> NormalizedContent:
    pack = pack if isinstance(pack, dict) else {}
    titles = _as_list(pack.get("titles"))
    title = str(pack.get("title") or (titles[0] if titles else "")).strip()
    if title and title not in titles:
        titles.insert(0, title)
    captions = _as_list(pack.get("captions"))
    caption = str(pack.get("caption") or (captions[0] if captions else "")).strip()
    if caption and caption not in captions:
        captions.insert(0, caption)
    desc_short = str(pack.get("description_short") or pack.get("description") or "").strip()
    desc_long = str(pack.get("description_long") or "").strip() or desc_short
    return NormalizedContent(
        title=title,
        titles=titles,
        description=desc_short or desc_long,
        description_long=desc_long,
        caption=caption,
        captions=captions,
        hashtags=_clean_hashtags(_as_list(pack.get("hashtags"))),
        seo_keywords=_as_list(pack.get("keywords") or pack.get("seo_keywords")),
        pinned_comment=str(pack.get("pinned_comment") or "").strip(),
        thumbnail_text=_as_list(pack.get("thumbnail_text")),
        source=str(pack.get("source") or ""),
    )


def _truncate(text: str, limit: int) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _join_hashtags(tags: list[str], limit: int) -> str:
    out: list[str] = []
    used = 0
    for tag in tags:
        if used + len(tag) + 1 > limit:
            break
        out.append(tag)
        used += len(tag) + 1
    return " ".join(out)


def _same_text(left: str, right: str) -> bool:
    """True when both texts are the same once whitespace/case are ignored."""
    norm = " ".join(str(left or "").split()).strip().lower()
    other = " ".join(str(right or "").split()).strip().lower()
    return bool(norm) and norm == other


def _transcript_blurb(clip_context: dict[str, Any] | None,
                      limit: int = 900) -> str:
    """A short description built from that clip's own transcript window.

    Used only when the content pack carries no description (or merely repeats
    the title): a preset/Studio publish then still gets a real, factual text
    instead of the title pasted a second time. Nothing is invented — the words
    come from the audio that was actually analysed for this clip.
    """
    if not isinstance(clip_context, dict):
        return ""
    segments = clip_context.get("segments")
    if not isinstance(segments, list) or not segments:
        return ""
    try:
        start = float(clip_context.get("clip_start") or 0.0)
        end = float(clip_context.get("clip_end") or 0.0)
    except (TypeError, ValueError):
        start, end = 0.0, 0.0
    picked: list[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        if end > start:
            try:
                s_start = float(seg.get("start") or 0.0)
                s_end = float(seg.get("end") or 0.0)
            except (TypeError, ValueError):
                s_start, s_end = 0.0, 0.0
            if s_end <= start or s_start >= end:
                continue
        picked.append(text)
    if not picked:
        picked = [str(s.get("text") or "").strip()
                  for s in segments if isinstance(s, dict)]
    text = re.sub(r"\s+", " ", " ".join(p for p in picked if p)).strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:-") + "…"


def map_for_platform(platform: str, content: NormalizedContent,
                     privacy: str = "", title_override: str = "",
                     description_override: str = "", caption_override: str = "",
                     made_for_kids: bool | None = None,
                     category_id: str = "",
                     clip_context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Map normalized content to a platform publish payload (spec §14).

    ``made_for_kids`` is the YouTube audience declaration. ``None`` means the
    job never recorded a declaration - the publisher then refuses to upload
    instead of silently marking the video as "not made for kids" (spec §9).
    ``True``/``False`` is transmitted verbatim; the adapter never invents a
    value on the user's behalf.

    ``clip_context`` is the per-clip transcript evidence stored with the job.
    It is only consulted when the pack has no description (or only repeats the
    title), so a description is never just a second copy of the title.
    """
    platform = str(platform or "").lower()
    if platform not in PLATFORMS:
        raise ValueError(f"Unknown platform: {platform}")

    title = title_override or content.title or ""
    if not title and content.captions:
        title = content.captions[0]
    caption = caption_override or content.caption or (content.captions[0] if content.captions else "")
    desc_long = description_override or content.description_long or content.description
    # Never let the description be a copy of the title. Runs without an AI pack
    # (preset/recipe and Studio publishes) used to send the clip title as the
    # description, so YouTube showed the same line twice. When the pack has
    # nothing to say — or the AI returned the title verbatim — fall back to the
    # clip's own transcript window. An explicit user override is never replaced.
    if not description_override and (not desc_long.strip() or _same_text(desc_long, title)):
        desc_long = _transcript_blurb(clip_context) or desc_long
    desc_short = content.description or content.description_long
    hashtags = content.hashtags
    keywords = content.seo_keywords

    payload: dict[str, Any] = {
        "platform": platform,
        "privacy": privacy or "default",
        "title": title,
        "description": desc_long,
        "caption": caption,
        "tags": [],
        "hashtags": hashtags,
        "pinned_comment": content.pinned_comment,
        # ``made_for_kids`` stays ``None`` until the user/snapshot actually
        # answers YouTube's "made for kids?" question; the adapter refuses to
        # invent that declaration (spec §9). See ``audience_declared``.
        "made_for_kids": made_for_kids if isinstance(made_for_kids, bool) else None,
        "audience": ("kids" if made_for_kids is True
                     else "not_kids" if made_for_kids is False else ""),
        "category_id": str(category_id or ""),
    }

    if platform == "youtube":
        # title ≤100, description ≤5000, tags ≤500 chars total.
        payload["title"] = _truncate(title, YT_TITLE_LIMIT)
        tag_words = [t.lstrip("#") for t in hashtags[:15]] + keywords[:20]
        seen: set[str] = set()
        tags: list[str] = []
        for word in tag_words:
            w = str(word).strip()[:30]
            if w and w.lower() not in seen:
                seen.add(w.lower())
                tags.append(w)
        tag_blob = ", ".join(tags)
        while len(tag_blob) > YT_TAG_TOTAL_LIMIT and tags:
            tags.pop()
            tag_blob = ", ".join(tags)
        payload["tags"] = tags
        hashtag_line = _join_hashtags(hashtags, 450)
        # The hashtag line is part of the *visible* description, so reserve room
        # for it first. Truncating afterwards (the old order) could slice the
        # whole hashtag block off the tail of a long AI description.
        body_budget = YT_DESCRIPTION_LIMIT - (len(hashtag_line) + 2 if hashtag_line else 0)
        # Last resort only: when neither the pack nor the clip's transcript gave
        # us anything, send the per-clip title so YouTube never receives an
        # empty description (spec §8.4). Everything better comes from the pack
        # or ``clip_context`` above.
        description = _truncate(desc_long or title, max(1, body_budget))
        if hashtag_line:
            description = f"{description}\n\n{hashtag_line}".strip()
        payload["description"] = _truncate(description, YT_DESCRIPTION_LIMIT)
        payload["caption"] = payload["description"]
    elif platform == "tiktok":
        # TikTok: caption + hashtags in a single title/comment field (≤ 2200).
        # The pack form asks for a per-platform caption, but a title-only pack
        # is a completely normal case (the manual import flow fills "Title"
        # first). Falling back to the user's own title — exactly what the
        # Facebook branch has always done — keeps a real, user-written text in
        # the payload instead of pre-flight-blocking the upload with
        # "Judul belum diisi". Nothing is ever invented: when no title and no
        # caption exist this is still empty and the pre-flight still blocks.
        body = _truncate(caption or title, 1800)
        tag_line = _join_hashtags(hashtags, 380)
        merged = f"{body} {tag_line}".strip() if tag_line else body
        payload["caption"] = _truncate(merged, 2200)
        payload["title"] = _truncate(payload["caption"], 90)
        payload["description"] = payload["caption"]
    elif platform == "instagram":
        body = _truncate(caption, 1800)
        tag_line = _join_hashtags(hashtags, 350)
        merged = f"{body}\n\n{tag_line}".strip() if tag_line else body
        payload["caption"] = _truncate(merged, 2200)
        payload["title"] = _truncate(title, 100)
        payload["description"] = payload["caption"]
    elif platform == "facebook":
        payload["title"] = _truncate(title, 255)
        body = _truncate(desc_short or caption, 4000)
        tag_line = _join_hashtags(hashtags, 450)
        payload["description"] = f"{body}\n\n{tag_line}".strip() if tag_line else body
        payload["caption"] = _truncate(caption or title, 1000)
    return payload


def audience_declared(payload: dict[str, Any] | None) -> bool:
    """True only when YouTube's 'made for kids?' question was actually answered.

    ``None`` (or absent) means the run never recorded a declaration, which must
    block the upload; we never default to ``False`` on the user's behalf.
    """
    if not isinstance(payload, dict):
        return False
    return isinstance(payload.get("made_for_kids"), bool)


# Only fields the platform API truly requires may block a job. YouTube's
# `videos.insert` needs a title and, by policy, an explicit audience
# declaration; the other platforms accept an empty caption.
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "youtube": ("title", "description"),
}


def validate_publish_payload(platform: str, payload: dict[str, Any] | None) -> list[str]:
    """Return human-readable problems that must block the upload (spec §8.4).

    An empty list means the request may be sent. Missing *required* metadata is
    never replaced by a default here - a clip is parked for the user instead of
    being published with an empty title/description.
    """
    platform = str(platform or "").lower()
    problems: list[str] = []
    if not isinstance(payload, dict):
        return ["Payload publish tidak terbentuk."]
    for field_name in REQUIRED_FIELDS.get(platform, ("title",)):
        if not str(payload.get(field_name) or "").strip():
            problems.append({
                "title": "Judul belum diisi",
                "description": "Deskripsi belum diisi",
                "caption": "Caption belum diisi",
            }.get(field_name, f"{field_name} belum diisi"))
    if platform == "youtube":
        if not audience_declared(payload):
            problems.append(
                "Deklarasi audiens YouTube belum dipilih "
                "(apakah video ini dibuat untuk anak-anak?)")
        if len(str(payload.get("description") or "")) > YT_DESCRIPTION_LIMIT:
            problems.append("Deskripsi melebihi batas 5000 karakter YouTube")
        if len(str(payload.get("title") or "")) > YT_TITLE_LIMIT:
            problems.append("Judul melebihi batas 100 karakter YouTube")
    return problems


def default_content_pack() -> NormalizedContent:
    return NormalizedContent()


def pack_signature(pack: dict[str, Any] | None) -> tuple[str, list[str]]:
    """Normalized ``(description, titles)`` of one pack.

    Two videos in the same batch must never receive the same description, so
    Studio compares these signatures while it generates and retries once when a
    collision is detected (spec §6.6 — one video, one metadata set).
    """
    if not isinstance(pack, dict):
        return "", []
    description = " ".join(str(pack.get("description_long") or pack.get("description_short")
                               or pack.get("description") or "").split()).lower()
    titles = [" ".join(str(t).split()).lower() for t in (pack.get("titles") or [])
              if str(t).strip()]
    if not titles and str(pack.get("title") or "").strip():
        titles = [" ".join(str(pack["title"]).split()).lower()]
    return description[:400], titles
