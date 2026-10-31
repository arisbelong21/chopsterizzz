"""Mobile-first titles (brainstorm -> critic) and deterministic YouTube chapters.

The content pack was adapted from the grounded long-form pipeline in three ways:

* every title obeys the phone title cut (``TITLE_MOBILE_CHARS`` /
  ``TITLE_MAX_CHARS``) with the subject keyword in the first three words;
* titles can be produced in two stages - stage 1 brainstorms across seven
  angles, stage 2 scores specificity/curiosity/clarity/honesty and keeps the
  best candidates whose angles *differ*;
* the long description gets a chapter block whose timestamps come from the real
  transcript, with the CTA still closing the description.

Everything here is additive: a failing brainstorm, a silent critic or a clip too
short for YouTube's chapter rules must leave the existing pack intact.
"""
from __future__ import annotations

import json

from chopster.ai.prompts import (
    TITLE_BRAINSTORM_COUNT,
    TITLE_CRITIC_DIMENSIONS,
    TITLE_MAX_CHARS,
    TITLE_MOBILE_CHARS,
    caption_prompt,
    title_brainstorm_prompt,
    title_critic_prompt,
)
from chopster.ai.provider_interface import AIResponse
from chopster.clipper.caption_generator import (
    chapter_lines,
    ensure_chapters,
    generate_captions,
    select_titles,
)
from chopster.clipper.transcript_manager import Segment, Transcript


#: A clip long enough for YouTube chapters: 14.0 -> 78.0 spans 64 seconds.
_LONG_SEGMENTS = [
    Segment(start=14.0, end=19.0, text="gol penalti di detik akhir membuat stadion pecah"),
    Segment(start=30.0, end=36.0, text="kiper gagal mengantisipasi arah bola"),
    Segment(start=50.0, end=58.0, text="pelatih mengubah strategi di babak kedua"),
    Segment(start=70.0, end=78.0, text="wasit meninjau ulang lewat VAR"),
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

#: Totals: 35 / 34 / 32 - the critic's ranking decides the winner, not the
#: order the brainstorm happened to use.
_CRITIC_REPLY = json.dumps({"scores": [
    {"title": "KIPER SALAH ANTISIPASI ARAH BOLA", "angle": "hasil spesifik",
     "specificity": 9, "curiosity": 7, "clarity": 9, "honesty": 9},
    {"title": "PENALTI DETIK AKHIR PECAHKAN STADION", "angle": "cerita/pengalaman",
     "specificity": 8, "curiosity": 9, "clarity": 9, "honesty": 9},
    {"title": "3 KESALAHAN KIPER DI BABAK KEDUA", "angle": "angka/data",
     "specificity": 8, "curiosity": 8, "clarity": 8, "honesty": 8},
], "top": ["PENALTI DETIK AKHIR PECAHKAN STADION"]})


class _FakeOrchestrator:
    """Settings AI stand-in that records every prompt and replays canned replies."""

    def __init__(self, replies: list[str]):
        self.prompts: list[str] = []
        self._replies = list(replies)

    def __call__(self, _config=None, **_kwargs):
        return self

    def generate(self, req, *, local_fallback=None):
        self.prompts.append(str(req.prompt))
        text = self._replies.pop(0) if self._replies else "{}"
        return AIResponse(text=text, raw={"provider": "fake-ai"})


def _transcript() -> Transcript:
    return Transcript(language="id", segments=list(_LONG_SEGMENTS))


def _run(monkeypatch, config: dict, replies: list[str]) -> tuple[dict, _FakeOrchestrator]:
    orch = _FakeOrchestrator(replies)
    monkeypatch.setattr("chopster.ai.orchestrator.orchestrator_from_config", orch)
    cfg = {"language": "id"}
    cfg.update(config)
    pack = generate_captions(_transcript(), cfg, platform="youtube", language="id",
                             count=5, style="hooks")
    return pack, orch


# ------------------------------------------------------------------ prompt side

def test_caption_prompt_carries_the_mobile_title_rules():
    """The AI must be told what the phone cut does to its title."""
    prompt = caption_prompt("transkrip tentang gol penalti", platform="youtube",
                            language="id", count=5, style="hooks")
    low = prompt.lower()
    assert f"under {TITLE_MOBILE_CHARS} characters" in low, prompt
    assert f"max {TITLE_MAX_CHARS}" in low, prompt
    assert "first 3 words" in low, prompt
    assert "understandable at a glance on mobile" in low, prompt
    # The payoff rule is spelled out the way the grounded pipeline states it.
    assert f"phones cut after ~{TITLE_MOBILE_CHARS} characters" in low, prompt
    # ...and the model is never allowed to invent chapter timestamps itself.
    assert "jangan menulis timestamp" in low, prompt


def test_brainstorm_prompt_widens_across_angles_and_keeps_dedupe():
    prompt = title_brainstorm_prompt(
        "transkrip tentang gol penalti", language="id",
        working_title="00:15 - 00:45", avoid_titles=["GOL PENALTI DETIK AKHIR"])
    low = prompt.lower()
    assert f"Tulis {TITLE_BRAINSTORM_COUNT} kandidat" in prompt, prompt
    for angle in ("curiosity gap", "how-to", "kontrarian", "angka/data"):
        assert angle in low, angle
    assert f"maksimal {TITLE_MAX_CHARS} karakter" in low, prompt
    assert "3 kata pertama" in low, prompt
    # The renderer label is context to replace, and the batch dedupe still applies.
    assert "bukan judul final" in low, prompt
    assert "gol penalti detik akhir" in low, prompt
    assert '"candidates"' in prompt, prompt


def test_critic_prompt_lists_candidates_and_four_dimensions():
    candidates = [
        {"title": "GOL PENALTI DETIK AKHIR", "angle": "hasil spesifik"},
        {"title": "KENAPA KIPER GAGAL", "angle": "pertanyaan"},
    ]
    prompt = title_critic_prompt(candidates, "transkrip gol penalti", language="id")
    for dim in TITLE_CRITIC_DIMENSIONS:
        assert dim in prompt.lower(), dim
    assert "1. [hasil spesifik] GOL PENALTI DETIK AKHIR" in prompt, prompt
    assert "2. [pertanyaan] KENAPA KIPER GAGAL" in prompt, prompt
    assert '"scores"' in prompt, prompt
    # The judge must also be asked for *different* angles, not four paraphrases.
    assert "sudutnya BERBEDA" in prompt, prompt


# --------------------------------------------------------------- selection side

def test_select_titles_keeps_different_angles_and_drops_clickbait():
    candidates = [
        {"title": "GOL PENALTI DETIK AKHIR", "angle": "hasil spesifik"},
        {"title": "GOL PENALTI DETIK TERAKHIR", "angle": "hasil spesifik"},
        {"title": "KENAPA KIPER TIDAK SIAP", "angle": "pertanyaan"},
        {"title": "3 KESALAHAN KIPER", "angle": "angka/data"},
        {"title": "RAHASIA YANG SEMUA ORANG SEMBUNYIKAN", "angle": "curiosity gap"},
    ]
    scored = {"scores": [
        {"title": "GOL PENALTI DETIK AKHIR", "angle": "hasil spesifik",
         "specificity": 9, "curiosity": 8, "clarity": 9, "honesty": 10},
        {"title": "GOL PENALTI DETIK TERAKHIR", "angle": "hasil spesifik",
         "specificity": 8, "curiosity": 8, "clarity": 9, "honesty": 10},
        {"title": "KENAPA KIPER TIDAK SIAP", "angle": "pertanyaan",
         "specificity": 8, "curiosity": 9, "clarity": 8, "honesty": 9},
        {"title": "3 KESALAHAN KIPER", "angle": "angka/data",
         "specificity": 7, "curiosity": 7, "clarity": 9, "honesty": 9},
        {"title": "RAHASIA YANG SEMUA ORANG SEMBUNYIKAN", "angle": "curiosity gap",
         "specificity": 2, "curiosity": 10, "clarity": 6, "honesty": 2},
    ]}
    picked = select_titles(candidates, scored, count=3)
    assert not any(t.lower() == "rahasia yang semua orang sembunyikan"
                   for t in picked), "clickbait must not ship"
    assert picked == ["Gol Penalti Detik Akhir", "Kenapa Kiper Tidak Siap",
                      "3 Kesalahan Kiper"], picked
    # The runner-up of an already-used angle loses to a lower scoring new angle.
    assert "Gol Penalti Detik Terakhir" not in picked, picked

    # A title another video already used never comes back.
    avoided = select_titles(candidates, scored, avoid_titles=["GOL PENALTI DETIK AKHIR"], count=3)
    assert "Gol Penalti Detik Akhir" not in avoided, avoided

    # A silent critic keeps the brainstorm pool instead of an empty list.
    fallback = select_titles(candidates, {}, count=2)
    assert len(fallback) == 2, fallback


def test_house_style_trims_a_title_that_outruns_the_phone_cut(monkeypatch):
    long_title = ("PENALTI DETIK AKHIR YANG MEMBUAT SELURUH STADION BERGEMURUH "
                  "DAN SEMUA PENONTON BERDIRI")
    assert len(long_title) > TITLE_MAX_CHARS
    reply = json.dumps({
        "titles": [long_title],
        "captions": ["Stadion pecah oleh gol penalti di detik akhir."],
        "description_short": "Gol penalti di detik akhir mengubah segalanya.",
        "description_long": "Rekap gol penalti dramatis di detik akhir.",
        "hashtags": ["#sepakbola"],
        "thumbnail_text": ["GOL PENALTI"],
    })
    pack, _orch = _run(monkeypatch, {"ai_title_two_stage": False}, [reply])
    assert len(pack["title"]) <= TITLE_MAX_CHARS, pack["title"]
    # The phone cut keeps human casing instead of shouting the remainder.
    assert pack["title"].startswith("Penalti Detik Akhir"), pack["title"]
    assert not pack["title"].isupper(), pack["title"]


# ------------------------------------------------------------------ two stages

def test_two_stage_pass_promotes_the_critics_winner(monkeypatch):
    """Pack -> brainstorm -> critic: the best *different* angle becomes the title."""
    pack, orch = _run(monkeypatch, {"ai_title_two_stage": True, "source_title": "00:15 - 00:45"},
                      [_PACK_REPLY, _BRAINSTORM_REPLY, _CRITIC_REPLY])
    assert len(orch.prompts) == 3, "one pack request plus the two title stages"
    # Stage 1 gets the renderer label as context to replace, never to reuse.
    assert "bukan judul final" in orch.prompts[1].lower(), orch.prompts[1]
    # Stage 2 sees the brainstormed candidates.
    assert "KIPER SALAH ANTISIPASI ARAH BOLA" in orch.prompts[2], orch.prompts[2]

    assert pack["title"] == "Penalti Detik Akhir Pecahkan Stadion", pack["title"]
    assert pack["titles"][0] == pack["title"], pack["titles"]
    assert pack["title_source"] == "brainstorm-critic", pack
    assert pack["title_angles"][0] == "cerita/pengalaman", pack["title_angles"]
    # The single-shot titles stay visible as the remaining options.
    assert "Penalti Detik Akhir" in pack["titles"], pack["titles"]
    assert len(set(pack["titles"])) == len(pack["titles"]), pack["titles"]


def test_two_stage_failure_keeps_the_single_shot_titles(monkeypatch):
    """A broken brainstorm must never cost the pack the titles it already has."""
    pack, orch = _run(monkeypatch, {"ai_title_two_stage": True},
                      [_PACK_REPLY, "bukan json sama sekali", "{}"])
    assert len(orch.prompts) == 2, "the pass stops after the unusable brainstorm"
    assert pack["title"] == "Penalti Detik Akhir", pack["title"]
    assert "title_source" not in pack, pack


def test_two_stage_is_skipped_when_the_ai_already_fell_back(monkeypatch):
    """Once the AI is unreachable, two more calls only delay the pack."""
    def boom(_config=None, **_kwargs):
        raise RuntimeError("AI tidak tersedia")

    monkeypatch.setattr("chopster.ai.orchestrator.orchestrator_from_config", boom)
    pack = generate_captions(_transcript(), {"ai_title_two_stage": True},
                             platform="youtube", language="id", count=5, style="hooks")
    assert pack["source"] == "local-fallback", pack.get("source")
    assert pack["titles"], pack
    # The local pack still leaves with the chapter block applied.
    assert "0:00 " in pack["description_long"], pack["description_long"]


# --------------------------------------------------------------------- chapters

def test_chapters_come_from_real_timestamps_and_start_at_zero():
    lines = chapter_lines(_LONG_SEGMENTS)
    assert len(lines) >= 3, lines
    assert lines[0].startswith("0:00 "), lines
    stamps = [line.split(" ", 1)[0] for line in lines]
    seconds = [int(s.split(":")[0]) * 60 + int(s.split(":")[1]) for s in stamps]
    assert seconds == sorted(seconds), seconds
    assert all(b - a >= 10 for a, b in zip(seconds, seconds[1:])), seconds
    # Every chapter name is real transcript wording, not a model invention.
    assert "penalti" in lines[0].lower(), lines


def test_short_clip_gets_no_chapter_block():
    """YouTube needs three chapters ten seconds apart - a short clip gets none."""
    short = [
        Segment(start=0.0, end=8.0, text="pembuka singkat"),
        Segment(start=12.0, end=20.0, text="inti pembahasan singkat"),
    ]
    assert chapter_lines(short) == []
    assert chapter_lines([]) == []


def test_chapters_keep_the_cta_last_and_are_never_doubled():
    pack = {"description_long": "Pembuka hook.\n\nIsi pembahasan.\n\nTulis pendapatmu di komentar."}
    ensure_chapters(pack, _LONG_SEGMENTS)
    text = pack["description_long"]
    lines = text.splitlines()
    assert lines[0] == "Pembuka hook.", text
    assert lines[-1] == "Tulis pendapatmu di komentar.", text
    assert "0:00 " in text, text
    ensure_chapters(pack, _LONG_SEGMENTS)
    assert pack["description_long"] == text, "the block must not be appended twice"


def test_chapters_are_appended_when_the_description_has_no_cta():
    pack = {"description_long": "Ringkasan isi video ini."}
    ensure_chapters(pack, _LONG_SEGMENTS)
    assert pack["description_long"].startswith("Ringkasan isi video ini.\n\n0:00 "), \
        pack["description_long"]


def test_a_model_written_chapter_block_is_left_alone():
    """The description is never rewritten into two competing chapter lists."""
    pack = {"description_long": "Pembuka.\n\n0:00 Awal\n0:20 Tengah\n0:40 Akhir\n\nTulis di komentar."}
    ensure_chapters(pack, _LONG_SEGMENTS)
    assert pack["description_long"].count("0:00") == 1, pack["description_long"]