"""Split-screen composition rules for the two new framing features.

These tests are pure: they exercise the timeline -> composition planning and the
generated FFmpeg filter graphs without decoding any video.
"""
from chopster.clipper.split_screen import (
    SPLIT_MULTI_FOCUS,
    SPLIT_TWO_PERSON,
    TRACKED_REFRAIM_MODES,
    build_reaction_overlay,
    build_vstack_composition,
    normalize_ai_windows,
    panel_crop_px,
    panel_plan_for_range,
    panels_at,
    rank_tracks,
    reaction_windows_for_range,
    reaction_split_windows,
    select_two_person_panels,
)


def _face(track_id, left, right, top, bottom, motion=0.0):
    return {
        "id": track_id,
        "x": round((left + right) / 2.0, 4),
        "y": round((top + bottom) / 2.0, 4),
        "left": left, "right": right, "top": top, "bottom": bottom,
        "motion": motion,
    }


def _timeline(seconds, tracks, motions=None):
    """tracks: {id: (left, right, top, bottom)}; motions: {id: [per-second values]}"""
    rows = []
    for t in range(seconds):
        faces = []
        for tid, box in tracks.items():
            motion = 0.0
            if motions and tid in motions:
                series = motions[tid]
                motion = float(series[min(t, len(series) - 1)])
            faces.append(_face(tid, box[0], box[1], box[2], box[3], motion))
        rows.append({"t": float(t), "shot": "solo", "target_id": next(iter(tracks)), "faces": faces})
    return rows


def test_two_person_panels_follow_source_order_and_ignore_transient_face():
    timeline = _timeline(8, {1: (0.05, 0.45, 0.20, 0.75), 2: (0.55, 0.95, 0.20, 0.75)})
    # A third person appears for a single sample only; it must not take a panel.
    timeline[3]["faces"].append(_face(9, 0.46, 0.54, 0.40, 0.55, 0.0))
    plan = select_two_person_panels(timeline, start=0.0, end=8.0)
    assert plan["found"] is True
    assert plan["degraded"] is False
    assert [p["id"] for p in plan["panels"]] == ["1", "2"]


def test_two_person_panels_need_a_usable_timeline():
    plan = select_two_person_panels([], start=0.0, end=10.0)
    assert plan["found"] is False
    assert plan["panels"] == []


def test_single_person_degrades_to_wide_context_instead_of_empty_panel():
    timeline = _timeline(6, {1: (0.30, 0.70, 0.20, 0.80)})
    plan = select_two_person_panels(timeline, start=0.0, end=6.0)
    assert plan["found"] is True
    assert plan["degraded"] is True
    assert plan["panels"][1] is None
    graph, label = build_vstack_composition(plan["panels"], source_w=1920, source_h=1080)
    assert "force_original_aspect_ratio=decrease" in graph
    assert label == "[split_stack]"


def test_vstack_graph_stacks_two_full_width_panels_top_bottom():
    timeline = _timeline(6, {1: (0.05, 0.45, 0.25, 0.70), 2: (0.55, 0.95, 0.25, 0.70)})
    plan = select_two_person_panels(timeline, start=0.0, end=6.0)
    graph, label = build_vstack_composition(plan["panels"], source_w=1920, source_h=1080,
                                           out_w=1080, out_h=1920)
    assert graph.count("[0:v]crop=") == 2
    assert "vstack=inputs=2[split_stack]" in graph
    assert "scale=w=1080:h=960" in graph
    assert label == "[split_stack]"


def test_existing_close_up_is_preserved_instead_of_zoomed_in():
    large_box = {"box": {"left": 0.30, "right": 0.72, "top": 0.10, "bottom": 0.95}}
    crop_w, crop_h = panel_crop_px(large_box, 1920, 1080, 1080 / 960)
    assert crop_h == 1080               # never tighter than the source already was
    assert crop_w <= 1920

    small_box = {"box": {"left": 0.40, "right": 0.60, "top": 0.30, "bottom": 0.55}}
    zoom_w, zoom_h = panel_crop_px(small_box, 1920, 1080, 1080 / 960)
    assert zoom_w <= 1920
    assert zoom_h < 1080                # a small subject still needs a reasonable crop
    assert zoom_h >= int(1080 * 0.30)   # but never an excessive zoom


def test_vstack_requires_two_panels():
    import pytest

    with pytest.raises(ValueError):
        build_vstack_composition([{"id": "1", "box": {}, "samples": []}], source_w=1920, source_h=1080)


def test_reaction_windows_open_only_while_both_people_are_active():
    motions = {1: [0.0, 0.0, 2.0, 2.0, 0.0, 0.0, 0.0], 2: [0.0, 0.0, 1.8, 1.9, 0.0, 0.0, 0.0]}
    timeline = _timeline(7, {1: (0.05, 0.45, 0.25, 0.70), 2: (0.55, 0.95, 0.25, 0.70)}, motions)
    windows = reaction_split_windows(timeline, start=0.0, end=7.0)
    assert len(windows) == 1
    assert 2.0 <= windows[0]["start"] <= 2.2
    assert 4.0 <= windows[0]["end"] <= 6.5
    assert windows[0]["panel_left"]["id"] == "1"
    assert windows[0]["panel_right"]["id"] == "2"


def test_reaction_windows_stay_empty_when_only_one_person_is_active():
    motions = {1: [0.0, 0.0, 2.0, 2.0], 2: [0.0, 0.0, 0.0, 0.0]}
    timeline = _timeline(4, {1: (0.05, 0.45, 0.25, 0.70), 2: (0.55, 0.95, 0.25, 0.70)}, motions)
    assert reaction_split_windows(timeline, start=0.0, end=4.0) == []


def test_reaction_overlay_keeps_normal_framing_and_reverts_after_each_window():
    motions = {1: [0.0, 2.0, 2.0, 0.0], 2: [0.0, 1.8, 1.9, 0.0]}
    timeline = _timeline(4, {1: (0.05, 0.45, 0.25, 0.70), 2: (0.55, 0.95, 0.25, 0.70)}, motions)
    windows = reaction_split_windows(timeline, start=0.0, end=4.0)
    graph, label = build_reaction_overlay(windows, source_w=1920, source_h=1080, base_label="[base]")
    assert graph.startswith("[0:v]crop=")
    assert "hstack=inputs=2" in graph
    assert "overlay=0:0:enable='between(t," in graph
    assert graph.split(";")[-1].endswith(label)
    assert label != "[base]"


def test_reaction_overlay_is_a_noop_without_windows():
    graph, label = build_reaction_overlay([], source_w=1920, source_h=1080)
    assert graph == ""
    assert label == "[base]"


def test_ai_reaction_shots_are_clamped_and_use_local_coordinates():
    motions = {1: [2.0] * 24, 2: [2.0] * 24, 3: [0.2] * 24}
    boxes = {1: (0.05, 0.45, 0.25, 0.70), 2: (0.35, 0.62, 0.25, 0.70), 3: (0.70, 0.95, 0.25, 0.70)}
    timeline = _timeline(24, boxes, motions)
    shots = [
        {"shot": "reaction_split", "start": 1.0, "end": 30.0, "split_pair": ["1", "3"], "reason": "interaction"},
        {"shot": "solo", "start": 6.0, "end": 8.0, "target_id": "2"},
        {"shot": "reaction_split", "start": 14.0, "end": 15.0, "split_pair": ["unknown", "unknown"]},
        {"shot": "reaction_split", "start": 15.1, "end": 16.0, "split_pair": ["1", "2"]},
    ]
    windows = normalize_ai_windows(shots, timeline, start=0.0, end=24.0, max_duration=8.0)
    assert len(windows) == 2                      # the solo shot is ignored
    assert windows[0]["pair"] == ["1", "3"]
    assert windows[0]["end"] == 9.0               # clamped to max_duration
    assert windows[1]["pair"] == ["1", "2"]       # unknown pair falls back to the ranked people
    assert windows[1]["end"] >= 16.0              # touching windows merged instead of flickering
    assert all(w["end"] - w["start"] <= 8.0 for w in windows)
    assert all(w["panel_left"]["samples"] and w["panel_right"]["samples"] for w in windows)
    assert all(len(w["pair"]) == 2 for w in windows)
    assert all(p in {"1", "2", "3"} for w in windows for p in w["pair"])


def test_new_modes_do_not_replace_the_legacy_reframe_modes():
    assert set(TRACKED_REFRAIM_MODES) == {"Smart", "AI Camera Director", "Speaker Focus", "Two Person"}
    assert SPLIT_TWO_PERSON not in TRACKED_REFRAIM_MODES
    assert SPLIT_MULTI_FOCUS not in TRACKED_REFRAIM_MODES
    assert SPLIT_TWO_PERSON != "Two Person"


def test_rank_tracks_prefers_the_person_that_is_actually_present():
    timeline = _timeline(6, {1: (0.05, 0.45, 0.25, 0.70), 2: (0.55, 0.95, 0.25, 0.70)})
    for row in timeline[3:]:
        row["faces"] = [f for f in row["faces"] if f["id"] != 2]
        row["faces"].append(_face(7, 0.60, 0.66, 0.40, 0.48, 0.0))
    ranked = rank_tracks(timeline, (0.0, 6.0))
    assert ranked[0]["id"] == "1"
    assert [t["id"] for t in ranked[:2]] == ["1", "2"]


def _two_room_timeline():
    """Person 2 leaves after 10s, person 3 takes the right half of the frame."""
    rows = _timeline(19, {1: (0.05, 0.45, 0.25, 0.70)})
    for row in rows:
        t = int(row["t"])
        right = 2 if t < 10 else 3
        row["faces"].append(_face(right, 0.55, 0.95, 0.25, 0.70, 1.5))
    return rows


def test_clip_range_plan_selects_the_people_inside_the_clip_and_shifts_the_time():
    panels = panel_plan_for_range(_two_room_timeline(), start=12.0, end=18.0)
    assert [p["id"] for p in panels] == ["1", "3"]
    times = [s["t"] for p in panels for s in (p["samples"] or [])]
    assert times and min(times) >= 0.0 and max(times) <= 6.0


def test_clip_range_plan_is_degraded_but_usable_with_one_person():
    timeline = _timeline(10, {1: (0.30, 0.70, 0.20, 0.80)})
    panels = panel_plan_for_range(timeline, start=2.0, end=6.0)
    assert len(panels) == 2 and panels[1] is None
    # A range shorter than the minimum clip duration is refused instead of rendering garbage.
    assert panel_plan_for_range(timeline, start=2.0, end=2.2) == []


def test_reaction_windows_for_range_keep_only_moments_inside_the_clip():
    motions = {1: [0.0] * 10, 2: [0.0] * 10}
    for t in (2, 3, 4):
        motions[1][t] = 2.0
        motions[2][t] = 1.9
    for t in (8,):
        motions[1][t] = 2.0
        motions[2][t] = 1.9
    timeline = _timeline(10, {1: (0.05, 0.45, 0.25, 0.70), 2: (0.55, 0.95, 0.25, 0.70)}, motions)
    windows = reaction_windows_for_range(timeline, start=2.0, end=7.0)
    assert len(windows) == 1
    assert 0.0 <= windows[0]["start"] <= 0.25
    assert windows[0]["end"] <= 5.0
    assert all(s["t"] <= 5.0 for key in ("panel_left", "panel_right") for s in windows[0][key]["samples"])
    # The moment at 8s belongs to another clip, not to this one.
    assert reaction_windows_for_range(timeline, start=8.5, end=9.5) == []


def test_ai_shot_may_carry_the_pair_without_renaming_the_shot():
    timeline = _timeline(12, {1: (0.05, 0.45, 0.25, 0.70), 2: (0.55, 0.95, 0.25, 0.70)})
    shots = [
        {"shot": "solo", "start": 1.0, "end": 4.0, "target_id": "1", "split_pair": ["1", "2"]},
        {"shot": "solo", "start": 6.0, "end": 9.0, "target_id": "1", "split_pair": ["1", "missing"]},
    ]
    windows = normalize_ai_windows(shots, timeline, start=0.0, end=12.0)
    assert len(windows) == 1
    assert windows[0]["pair"] == ["1", "2"]


def test_panels_at_matches_the_rendered_selection_for_one_moment():
    timeline = _two_room_timeline()
    panels = panels_at(timeline, 13.5)
    assert [p["id"] for p in panels] == ["1", "3"]
    assert all(len(p["samples"]) == 1 for p in panels)
    assert panels[0]["box"]["right"] <= panels[1]["box"]["left"]
    # Nothing tracked yet -> no split to preview.
    assert panels_at([], 3.0) == []


def test_export_batch_forwards_a_per_clip_split_plan(monkeypatch, tmp_path):
    """A clip may carry its own composition; clips without one keep the batch default."""
    from chopster.clipper import clip_renderer

    captured = []

    class _Result:
        success = True
        error = ""

        def __init__(self, output):
            self.output = str(output)

    def fake_export_clip(source, start, end, output, **kwargs):
        captured.append({"start": start, "end": end, **kwargs})
        return _Result(output)

    monkeypatch.setattr(clip_renderer, "export_clip", fake_export_clip)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"not-a-real-video")
    panels = panel_plan_for_range(_two_room_timeline(), start=0.0, end=6.0)
    clips = [
        {"index": 1, "start": 0.0, "end": 6.0, "title": "A", "composition": "split_v", "panel_plan": panels},
        {"index": 2, "start": 6.0, "end": 12.0, "title": "B"},
    ]
    ok, fail = clip_renderer.export_batch(source, clips, tmp_path / "out", quality="fast", aspect="9:16")
    assert (ok, fail) == (2, 0)
    assert captured[0]["composition"] == "split_v"
    assert captured[0]["panel_plan"] == panels
    assert captured[0]["copy_if_possible"] is False
    assert captured[1]["composition"] == "auto"
    assert captured[1]["panel_plan"] is None
    assert captured[1]["reaction_windows"] is None