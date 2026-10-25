"""Two videos in one Generate AI Content Pack run must never share metadata.

Regression for the Studio complaint: generating packs for two different clips
produced the *same* description (template-like, "kelihatan bot"), echoed the
renderer's technical title ("menit 1 sampai 2"), or — for locally imported
files — described them from *another* video's transcript. One video, one
context, one metadata set (spec §4.4/§6.6/§7.4):

* every checked video is generated from its OWN evidence (handed-off clip
  transcript, or a local Whisper transcription of that exact file);
* the batch passes what earlier videos already received so nothing repeats;
* a renderer/import title is context to replace, never a title to reuse.
"""
from __future__ import annotations

import json

from chopster.ai.prompts import caption_prompt
from chopster.ai.provider_interface import AIResponse
from chopster.automation.context import (
    build_clip_context,
    caption_context,
    focused_transcript,
)
from chopster.automation.content import pack_signature
from chopster.automation.local_transcribe import (
    MODEL_FLOOR,
    normalize_pack_model,
    pack_whisper_model,
)
from chopster.clipper.caption_generator import generate_captions, is_technical_title


#: House style: every caption/description must invite an action in its last line.
_CTA_WORDS = ("komentar", "follow", "bagikan", "simpan video", "tonton sampai")
#: Filler words must never turn into hashtags (they make a pack look bot-made).
_STOPWORD_TAGS = ("#yang", "#dengan", "#untuk", "#dari", "#video", "#bagian")


def _assert_house_style(pack: dict) -> None:
    """CAPSLOCK titles, CTA in every caption, natural hashtags."""
    assert pack["titles"], "a pack must carry a usable title"
    for title in pack["titles"]:
        assert title == title.upper(), title
        assert not is_technical_title(title), title
    for field in ("description_short", "description_long", "captions"):
        values = pack.get(field)
        values = [values] if isinstance(values, str) else list(values or [])
        for value in values:
            assert any(word in value.lower() for word in _CTA_WORDS), value
    tags = list(pack["hashtags"])
    assert len(tags) >= 5, tags
    assert len(set(tags)) == len(tags), tags
    assert all(t.startswith("#") and t[1:].isalnum() for t in tags), tags
    assert not set(tags) & set(_STOPWORD_TAGS), tags


def _ctx_bola():
    """A football clip handed over with its own transcript (e.g. from ACS)."""
    return build_clip_context(
        source="acs",
        source_asset_id="asset-bola-1",
        media_path=r"C:\clips\final_bola.mp4",
        title="00:15 - 00:45",          # renderer/bot label, not a final title
        clip_start=15.0,
        clip_end=45.0,
        segments=[
            {"start": 14.0, "end": 19.0, "text": "gol penalti di detik akhir membuat stadion pecah"},
            {"start": 22.0, "end": 28.0, "text": "kiper berhasil menggagalkan tendangan penalti kedua"},
            {"start": 33.0, "end": 40.0, "text": "umpan silang dari sisi kiri berbuah gol sundulan penalti"},
        ],
    )


def _ctx_cerita():
    """A story/narrative clip — completely different topic (e.g. manual import)."""
    return build_clip_context(
        source="manual",
        source_asset_id="asset-cerita-2",
        media_path=r"C:\clips\alur_cerita.mp4",
        title="menit 1 sampai 2",       # renderer/bot label, not a final title
        clip_start=0.0,
        clip_end=0.0,
        segments=[
            {"start": 2.0, "end": 8.0, "text": "alur cerita dimulai dari seorang murid yang berani melawan aturan sekolah"},
            {"start": 12.0, "end": 18.0, "text": "konflik memuncak ketika kepala sekolah menemukan rahasia keluarga"},
            {"start": 24.0, "end": 30.0, "text": "akhir cerita menyisakan pertanyaan tentang keberanian dan pengorbanan"},
        ],
    )


def _pack_for(ctx, *, used_titles, used_descriptions, source_title, force_distinct=False):
    """Mirror ``AutomationPage._submit_pack_generation`` for ONE video."""
    cfg = dict(caption_context(ctx))
    cfg["source_title"] = source_title
    cfg["working_title"] = source_title
    cfg["avoid_titles"] = list(used_titles)
    cfg["avoid_descriptions"] = list(used_descriptions)
    if force_distinct:
        cfg["force_distinct"] = True
    # This regression is about batch isolation, not about the title pipeline: the
    # brainstorm→critic pass would spend the canned replies below on itself. It is
    # covered on its own in tests/test_title_mobile_chapters.py.
    cfg["ai_title_two_stage"] = False
    focus = focused_transcript(ctx)
    assert focus is not None and focus.segments, "clip context must carry transcript evidence"
    return generate_captions(focus, cfg, platform="youtube", language="id",
                             count=5, style="hooks")


def _run_batch(contexts, monkeypatch, orchestrator=None):
    """Generate one pack per video the way the Studio batch pass does."""
    if orchestrator is None:
        def orchestrator(_config=None):
            raise RuntimeError("AI tidak tersedia di test — jalur lokal deterministik")
    monkeypatch.setattr("chopster.ai.orchestrator.orchestrator_from_config", orchestrator)
    used_titles: list[str] = []
    used_descriptions: list[str] = []
    packs = []
    for ctx in contexts:
        pack = _pack_for(ctx, used_titles=used_titles,
                         used_descriptions=used_descriptions,
                         source_title=str(ctx.title or ""))
        sig_desc, sig_titles = pack_signature(pack)
        if sig_desc:
            used_descriptions.append(sig_desc)
        used_titles.extend(sig_titles)
        packs.append(pack)
    return packs


class _RecordingOrchestrator:
    """Fake Settings AI that records each prompt and replays canned JSON packs."""

    def __init__(self, replies: list[str]):
        self.prompts: list[str] = []
        self._replies = list(replies)

    def __call__(self, _config=None):
        return self

    def generate(self, req, *, local_fallback=None):
        self.prompts.append(str(req.prompt))
        text = self._replies.pop(0) if self._replies else "{}"
        return AIResponse(text=text, raw={"provider": "fake-ai"})


def test_two_videos_no_leakage(monkeypatch):
    """Two clips → two different packs, each built from its OWN transcript."""
    a, b = _run_batch([_ctx_bola(), _ctx_cerita()], monkeypatch)

    # Each description is born from that video's own words...
    desc_a = str(a.get("description_long") or a.get("description") or "").lower()
    desc_b = str(b.get("description_long") or b.get("description") or "").lower()
    assert any(w in desc_a for w in ("penalti", "kiper", "stadion", "sundulan")), desc_a
    assert any(w in desc_b for w in ("cerita", "sekolah", "murid", "pengorbanan")), desc_b
    # ...and never borrows the other video's topic.
    for leaked in ("penalti", "kiper", "stadion", "sundulan"):
        assert leaked not in desc_b, f"video A leaked into video B: {leaked!r}"
    for leaked in ("sekolah", "murid", "pengorbanan", "kepala"):
        assert leaked not in desc_a, f"video B leaked into video A: {leaked!r}"

    # Metadata sets are pairwise different (the "dua-duanya sama" complaint).
    assert a["titles"] != b["titles"]
    assert desc_a != desc_b
    assert a["description_short"] != b["description_short"]
    assert a["captions"] != b["captions"]
    assert a["hashtags"] != b["hashtags"]
    assert pack_signature(a)[0] != pack_signature(b)[0]
    assert not (set(a["titles"]) & set(b["titles"]))

    # The renderer's technical labels are never echoed back as titles.
    for t in list(a["titles"]) + list(b["titles"]):
        assert not is_technical_title(t), t
        assert t not in ("00:15 - 00:45", "menit 1 sampai 2")

    # ...and the local (no-AI) path already speaks the content-pack house style.
    _assert_house_style(a)
    _assert_house_style(b)


def test_second_video_prompt_carries_first_videos_metadata(monkeypatch):
    """The batch must tell the AI which titles/descriptions are already used."""
    replies = [
        json.dumps({
            "titles": ["Gol Penalti Detik Akhir yang Pecahkan Stadion"],
            "captions": ["Stadion pecah oleh gol penalti di detik akhir."],
            "description_short": "Gol penalti di detik akhir mengubah segalanya.",
            "description_long": "Rekap gol penalti dramatis di detik akhir, penyelamatan kiper, dan gol sundulan dari umpan silang.",
            "hashtags": ["#sepakbola", "#golpenalti"],
            "keywords": ["gol penalti"],
            "pinned_comment": "Setuju dengan keputusan wasit?",
            "thumbnail_text": ["GOL PENALTI"],
        }),
        json.dumps({
            "titles": ["Murid yang Berani Melawan Aturan Sekolah"],
            "captions": ["Rahasia keluarga yang mengubah alur cerita."],
            "description_short": "Kisah murid yang berani melawan aturan sekolah.",
            "description_long": "Alur cerita tentang murid pemberani, konflik dengan kepala sekolah, dan rahasia keluarga yang terungkap.",
            "hashtags": ["#alurcerita", "#drama"],
            "keywords": ["alur cerita"],
            "pinned_comment": "Bagian mana yang paling menegangkan?",
            "thumbnail_text": ["RAHASIA KELUARGA"],
        }),
    ]
    orch = _RecordingOrchestrator(replies)
    a, b = _run_batch([_ctx_bola(), _ctx_cerita()], monkeypatch,
                      orchestrator=orch)
    assert len(orch.prompts) == 2, "each video must get its own AI request"

    prompt_a, prompt_b = (p.lower() for p in orch.prompts)

    # The renderer/import title is handed over as a label to REPLACE.
    for prompt in (prompt_a, prompt_b):
        assert "bukan judul final" in prompt
        assert "dilarang dipakai" in prompt
    assert "00:15 - 00:45" in prompt_a
    assert "menit 1 sampai 2" in prompt_b

    # The second request must know what the first video already received.
    assert "judul yang sudah dipakai" in prompt_b
    assert "gol penalti detik akhir yang pecahkan stadion" in prompt_b
    assert "deskripsi video lain di batch ini" in prompt_b
    assert "rekap gol penalti dramatis" in prompt_b
    # But the first request starts clean — nothing to avoid yet.
    assert "judul yang sudah dipakai" not in prompt_a

    # The AI's own words are kept — the technical label is never the title — but
    # they are rendered in the house style (CAPSLOCK) before publishing.
    assert a["title"] == "GOL PENALTI DETIK AKHIR YANG PECAHKAN STADION"
    assert b["title"] == "MURID YANG BERANI MELAWAN ATURAN SEKOLAH"
    assert not is_technical_title(a["title"]) and not is_technical_title(b["title"])
    _assert_house_style(a)
    _assert_house_style(b)
    # The renderer/import label never survives as one of the options either.
    for pack in (a, b):
        assert not any(is_technical_title(t) for t in pack["titles"])


def test_house_style_holds_even_when_the_ai_ignores_the_prompt(monkeypatch):
    """A Title-Case answer from the model still ships as a CAPSLOCK, CTA-ready pack.

    The prompt asks for capslock titles, a hook + CTA and natural hashtags, but
    the Studio must not *depend* on the model obeying: every pack is normalised
    before it can reach the publish queue.
    """
    replies = [
        json.dumps({
            "titles": ["00:15 - 00:45", "Penalti Detik Akhir yang Pecahkan Stadion"],
            "captions": ["Stadion pecah oleh gol penalti di detik akhir."],
            "description_short": "Gol penalti di detik akhir mengubah segalanya.",
            "description_long": "Rekap gol penalti dramatis dan penyelamatan kiper.",
            "hashtags": ["#sepakbola", "#yang", "#gol penalti", "#sepakbola"],
            "thumbnail_text": ["gol penalti"],
        }),
    ]
    orch = _RecordingOrchestrator(replies)
    pack = _run_batch([_ctx_bola()], monkeypatch, orchestrator=orch)[0]

    # The echoed renderer label is dropped, the AI's words stay (in CAPSLOCK).
    assert pack["title"].startswith("PENALTI DETIK AKHIR"), pack["title"]
    for title in pack["titles"]:
        assert title != "00:15 - 00:45", title
    assert pack["thumbnail_text"] == ["GOL PENALTI"]

    # Hashtags stay natural: prefixed, deduped, filler words removed.
    assert "#sepakbola" in pack["hashtags"]
    assert "#golpenalti" in pack["hashtags"]
    _assert_house_style(pack)


def test_force_distinct_orders_a_rewrite():
    """``force_distinct`` must demand a rewrite, not accept the same words."""
    prompt = caption_prompt(
        "transkrip video tentang bola",
        platform="youtube", language="id", count=5, style="hooks",
        working_title="menit 1 sampai 2",
        avoid_titles=["gol penalti detik akhir"],
        avoid_descriptions=["Gol penalti di detik akhir mengubah segalanya."],
        force_distinct=True,
    )
    low = prompt.lower()
    assert "peringatan keunikan" in low
    assert "judul yang sudah dipakai" in low
    assert "deskripsi video lain di batch ini" in low
    assert "dilarang memakai judul teknis" in low


def test_pack_signature_flags_duplicate_descriptions():
    """Same words, different whitespace → same signature (collision detected)."""
    sig_a, _titles_a = pack_signature(
        {"description_long": "Rahasia bumbu yang gurih dipanggang di atas arang."})
    sig_b, _ = pack_signature(
        {"description_long": "Rahasia  bumbu yang gurih dipanggang di atas arang."})
    assert sig_a == sig_b and sig_a
    sig_c, _ = pack_signature(
        {"description_long": "Pertandingan berakhir lewat gol penalti dramatis."})
    assert sig_c != sig_a


def test_pack_whisper_model_never_falls_to_tiny():
    """Lossy transcription is what made descriptions generic — never below small."""
    for weak in ("tiny", "base", "tiny.en", "base.en", "", None, "entah-apa"):
        assert normalize_pack_model(weak) == MODEL_FLOOR, weak
    assert normalize_pack_model("small") == "small"
    assert normalize_pack_model("medium") == "medium"
    assert normalize_pack_model("large-v3") == "large-v3"
    assert normalize_pack_model("large") == "large-v3"

    class _Cfg:
        def get(self, _key, default=None):
            return "tiny"

    assert pack_whisper_model(_Cfg()) == MODEL_FLOOR