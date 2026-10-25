"""Two different videos must produce semantically different packs (no leakage).

The AI Content Pack is built from per-clip transcript evidence only, so a bite
from video A can never leak into the pack of video B (spec §4.4/§7.6).
"""
from __future__ import annotations

from chopster.automation.context import (
    ClipContext,
    build_clip_context,
    caption_context,
    context_status,
    focused_transcript,
    same_asset,
)


def _ctx_kuliner() -> ClipContext:
    return build_clip_context(
        source="acs",
        source_asset_id="asset-kuliner-1",
        media_path=r"C:\clips\sate_padang.mp4",
        title="Sate Padang Klasik",
        clip_start=10.0,
        clip_end=25.0,
        segments=[
            {"start": 8.0, "end": 12.0, "text": "bumbu rempah khas Padang yang gurih"},
            {"start": 15.0, "end": 20.0, "text": "sate dipanggang di atas arang"},
            {"start": 22.0, "end": 28.0, "text": "disajikan dengan kuah kuning pekat"},
        ],
    )


def _ctx_ai() -> ClipContext:
    return build_clip_context(
        source="acs",
        source_asset_id="asset-ai-2",
        media_path=r"C:\clips\pytorch_tips.mp4",
        title="Tips PyTorch",
        clip_start=5.0,
        clip_end=20.0,
        segments=[
            {"start": 4.0, "end": 9.0, "text": "GPU memory allocation yang efisien"},
            {"start": 10.0, "end": 15.0, "text": "tensor dan autograd secara singkat"},
            {"start": 16.0, "end": 22.0, "text": "custom dataset loader untuk pelatihan"},
        ],
    )


def _pack_text(ctx: ClipContext) -> str:
    """Collapse everything the pack could be generated from."""
    focus = focused_transcript(ctx)
    parts = [
        ctx.excerpt,
        str(ctx.title or ""),
        " ".join(s.text for s in focus.segments) if focus else "",
        str(caption_context(ctx).get("highlight_context") or ""),
    ]
    return " ".join(parts).lower()


def test_context_status_refuses_without_evidence():
    ok, reason = context_status(None)
    assert not ok and reason == "no_context"
    empty = build_clip_context(source="acs", source_asset_id="x", segments=[])
    ok, reason = context_status(empty)
    assert not ok and reason == "no_transcript"
    short = build_clip_context(source="acs", source_asset_id="x",
                               segments=[{"start": 0, "end": 3, "text": "halo"}])
    ok, reason = context_status(short)
    assert not ok and reason == "excerpt_too_short"


def test_two_videos_never_leak():
    a, b = _ctx_kuliner(), _ctx_ai()
    assert context_status(a)[0] and context_status(b)[0]

    text_a = _pack_text(a)
    text_b = _pack_text(b)

    assert "sate" in text_a or "padang" in text_a
    assert "pytorch" in text_b or "tensor" in text_b
    # semantic separation: video A's keywords must not appear in B's evidence
    for leaked in ("sate", "padang", "kuah kuning"):
        assert leaked not in text_b
    for leaked in ("pytorch", "tensor", "gpu"):
        assert leaked not in text_a


def test_window_excludes_other_topics():
    ctx = _ctx_kuliner()
    window = ctx.window()
    assert all(s["start"] < 37.0 and s["end"] > -2.0 for s in window)
    assert window == ctx.segments  # build_clip_context already windowed it


def test_same_asset_identity_proof():
    a = _ctx_kuliner()
    b = _ctx_ai()
    assert same_asset(a, media_path=str(a.media_path), source_path=str(a.media_path))
    assert not same_asset(a, media_path=str(a.media_path), source_path=str(b.media_path))
    assert not same_asset(a)