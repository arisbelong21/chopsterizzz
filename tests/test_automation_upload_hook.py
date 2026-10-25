"""The Automation uploader must receive the hook, title and chapters.

The hook/title pipeline only pays off where a video is actually published:
Automation builds every clip's metadata with the same
``chopster.clipper.caption_generator.generate_captions`` call the Studio uses, and
maps that pack to a per-platform payload with ``chopster.automation.content``.

This module pins that whole seam end to end:

* the curator's winning title (brainstorm -> critic) is the one YouTube gets,
  and the other curated angles stay available as options;
* the chapter block built from the clip's real transcript timing is inside the
  YouTube description, ahead of the closing CTA;
* TikTok/Instagram receive the short hook caption, never a chapter block;
* the payload still passes ``validate_publish_payload`` - a chapter block must
  never park a job before upload;
* the room reserved for the block holds even when the AI description sits at the
  platform's character ceiling.
"""
from __future__ import annotations

import json
import re
import unittest
from unittest import mock

from chopster.ai.provider_interface import AIResponse
from chopster.automation.content import (
    YT_DESCRIPTION_LIMIT,
    map_for_platform,
    normalize_content_pack,
    validate_publish_payload,
)
from chopster.clipper.caption_generator import generate_captions
from chopster.clipper.transcript_manager import Segment, Transcript

#: House style: one of these words closes every caption and every description.
_CTA_WORDS = ("komentar", "follow", "bagikan", "simpan video", "tonton sampai")

#: A 64 second clip, so YouTube's own chapter rules can be satisfied: three
#: chapters, ten seconds apart, the first one at 0:00.
_CLIP_SEGMENTS = [
    Segment(start=14.0, end=19.0, text="gol penalti di detik akhir membuat stadion pecah"),
    Segment(start=30.0, end=36.0, text="kiper gagal mengantisipasi arah bola"),
    Segment(start=50.0, end=58.0, text="pelatih mengubah strategi di babak kedua"),
    Segment(start=70.0, end=78.0, text="wasit meninjau ulang lewat VAR"),
]

#: Shorter than the chapter minimum span, so no block may be produced at all.
_SHORT_SEGMENTS = [
    Segment(start=0.0, end=8.0, text="pembuka singkat soal gol penalti"),
    Segment(start=12.0, end=20.0, text="inti pembahasan penalti"),
]

_PACK_REPLY = json.dumps({
    "titles": ["PENALTI DETIK AKHIR", "STADION PECAH KARENA PENALTI"],
    "captions": ["Stadion pecah oleh gol penalti di detik akhir."],
    "description_short": "Gol penalti di detik akhir mengubah segalanya.",
    "description_long": "Rekap gol penalti dramatis di detik akhir.",
    "hashtags": ["#sepakbola", "#golpenalti"],
    "keywords": ["gol penalti"],
    "pinned_comment": "Setuju dengan keputusan wasit?",
    "thumbnail_text": ["GOL PENALTI"],
})

_BRAINSTORM_REPLY = json.dumps({"candidates": [
    {"title": "KIPER SALAH ANTISIPASI ARAH BOLA", "angle": "hasil spesifik"},
    {"title": "PENALTI DETIK AKHIR PECAHKAN STADION", "angle": "cerita/pengalaman"},
    {"title": "3 KESALAHAN KIPER DI BABAK KEDUA", "angle": "angka/data"},
]})

#: Totals 35 / 34 / 32, so the critic's ranking - not the brainstorm order -
#: decides which title Automation will upload.
_CRITIC_REPLY = json.dumps({"scores": [
    {"title": "KIPER SALAH ANTISIPASI ARAH BOLA", "angle": "hasil spesifik",
     "specificity": 9, "curiosity": 7, "clarity": 9, "honesty": 9},
    {"title": "PENALTI DETIK AKHIR PECAHKAN STADION", "angle": "cerita/pengalaman",
     "specificity": 8, "curiosity": 9, "clarity": 9, "honesty": 9},
    {"title": "3 KESALAHAN KIPER DI BABAK KEDUA", "angle": "angka/data",
     "specificity": 8, "curiosity": 8, "clarity": 8, "honesty": 8},
], "top": ["PENALTI DETIK AKHIR PECAHKAN STADION"]})

_WINNER = "PENALTI DETIK AKHIR PECAHKAN STADION"

#: "0:00 Intro", "1:20 Babak kedua", ... - the shape YouTube needs.
_CHAPTER_LINE_RE = re.compile(r"^\d{1,2}:\d{2} \S", re.MULTILINE)


class _RecordingOrchestrator:
    """Settings AI stand-in: records every prompt, replays canned replies."""

    def __init__(self, replies: list[str]):
        self.prompts: list[str] = []
        self._replies = list(replies)

    def __call__(self, _config=None, **_kwargs):
        return self

    def generate(self, req, *, local_fallback=None):
        self.prompts.append(str(req.prompt))
        text = self._replies.pop(0) if self._replies else "{}"
        return AIResponse(text=text, raw={"provider": "fake-ai"})


def _automation_config() -> dict:
    """The keys ``ui/pages/clipper_page.py`` puts into the Automation config.

    ``_ai_config()`` copies the whole settings dict, so a key the user never
    saved - like ``ai_title_two_stage`` - is simply absent and the shipped
    default decides whether the extra title stages run while publishing.
    """
    return {
        "ai_provider": "openai",
        "ai_model": "gpt-4o-mini",
        "ai_api_key": "test-key",
        "ai_timeout": 15,
        "ai_temperature": 0.2,
    }


def _transcript(segments=None) -> Transcript:
    return Transcript(language="id", segments=list(segments or _CLIP_SEGMENTS))


def _automation_pack(segments=None, config: dict | None = None,
                     replies: list[str] | None = None):
    """Run the real pipeline the way Automation runs it, with canned AI replies."""
    orch = _RecordingOrchestrator(
        replies if replies is not None
        else [_PACK_REPLY, _BRAINSTORM_REPLY, _CRITIC_REPLY])
    cfg = _automation_config()
    if config:
        cfg.update(config)
    with mock.patch("chopster.ai.orchestrator.orchestrator_from_config", orch):
        pack = generate_captions(_transcript(segments), cfg, platform="youtube",
                                 language="id", count=5, style="hooks")
    return pack, orch


def _payload(platform: str, pack: dict) -> dict:
    """Exactly what the publisher hands to the platform API."""
    return map_for_platform(platform, normalize_content_pack(pack),
                            made_for_kids=False)


class AutomationUploadHookTests(unittest.TestCase):
    """The pack Automation generates -> the payload the uploader sends."""

    # ------------------------------------------------------------ title stage

    def test_automation_config_runs_the_two_stage_title_pass_by_default(self):
        """Publishing must get the curated title without extra setup."""
        pack, orch = _automation_pack()
        self.assertEqual(len(orch.prompts), 3,
                         "one pack request + brainstorm + critic")
        self.assertEqual(pack["title"], _WINNER)
        self.assertEqual(pack["title_source"], "brainstorm-critic")
        self.assertEqual(pack["titles"][0], _WINNER)
        # The single-shot titles stay visible as the other options.
        self.assertIn("PENALTI DETIK AKHIR", pack["titles"])

    def test_youtube_payload_uploads_the_curated_title(self):
        pack, _orch = _automation_pack()
        payload = _payload("youtube", pack)
        self.assertEqual(payload["title"], _WINNER)
        self.assertLessEqual(len(payload["title"]), 100)

    def test_turning_the_two_stage_pass_off_keeps_automation_uploadable(self):
        pack, orch = _automation_pack(config={"ai_title_two_stage": False},
                                      replies=[_PACK_REPLY])
        self.assertEqual(len(orch.prompts), 1)
        self.assertTrue(pack["title"].strip())
        payload = _payload("youtube", pack)
        self.assertTrue(payload["title"].strip())
        self.assertEqual(validate_publish_payload("youtube", payload), [])

    # -------------------------------------------------------------- chapters

    def test_youtube_description_keeps_the_chapters_before_the_cta(self):
        pack, _orch = _automation_pack()
        self.assertIn("0:00 ", pack["description_long"])
        description = _payload("youtube", pack)["description"]
        low = description.lower()
        self.assertIn("0:00 ", description, description)
        self.assertGreaterEqual(len(_CHAPTER_LINE_RE.findall(description)), 3,
                                description)
        self.assertTrue(any(word in low for word in _CTA_WORDS), description)
        closing = max(low.rindex(word) for word in _CTA_WORDS if word in low)
        self.assertLess(low.index("0:00 "), closing, description)

    def test_youtube_payload_still_passes_the_publish_validation(self):
        """A chapter block must never park the job before it is uploaded."""
        payload = _payload("youtube", _automation_pack()[0])
        self.assertEqual(validate_publish_payload("youtube", payload), [])
        self.assertLessEqual(len(payload["description"]), YT_DESCRIPTION_LIMIT)

    def test_short_caption_platforms_get_the_hook_without_a_chapter_block(self):
        pack, _orch = _automation_pack()
        for platform in ("tiktok", "instagram"):
            payload = _payload(platform, pack)
            self.assertNotIn("0:00 ", payload["caption"], platform)
            self.assertTrue(payload["caption"].startswith(
                str(pack["captions"][0])[:20]), payload["caption"])
            self.assertTrue(any(word in payload["caption"].lower()
                                for word in _CTA_WORDS), payload["caption"])
            self.assertEqual(validate_publish_payload(platform, payload), [])

    def test_a_clip_too_short_for_chapters_still_uploads_cleanly(self):
        pack, _orch = _automation_pack(_SHORT_SEGMENTS)
        self.assertNotIn("0:00 ", pack["description_long"])
        payload = _payload("youtube", pack)
        self.assertEqual(payload["title"], _WINNER)
        self.assertEqual(validate_publish_payload("youtube", payload), [])

    def test_a_description_at_the_character_ceiling_keeps_its_chapters(self):
        """The reserved room is what stops the block being truncated away."""
        long_body = "Pembahasan panjang soal gol penalti dan kiper. " * 200
        reply = json.dumps({
            "titles": ["PENALTI DETIK AKHIR"],
            "captions": ["Stadion pecah oleh gol penalti di detik akhir."],
            "description_short": "Gol penalti di detik akhir mengubah segalanya.",
            "description_long": long_body,
            "hashtags": ["#sepakbola", "#golpenalti"],
            "keywords": ["gol penalti"],
        })
        pack, _orch = _automation_pack(config={"ai_title_two_stage": False},
                                       replies=[reply])
        self.assertGreater(len(long_body), YT_DESCRIPTION_LIMIT)
        self.assertIn("0:00 ", pack["description_long"])
        payload = _payload("youtube", pack)
        self.assertIn("0:00 ", payload["description"],
                      payload["description"][:200])
        self.assertGreaterEqual(
            len(_CHAPTER_LINE_RE.findall(payload["description"])), 3)
        self.assertEqual(validate_publish_payload("youtube", payload), [])


if __name__ == "__main__":   # pragma: no cover
    unittest.main()