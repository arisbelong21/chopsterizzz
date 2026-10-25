"""Two-feed (side-by-side) source support for the split-screen features.

The pure tests build synthetic frames, so they validate the detection rules and
the generated FFmpeg graphs without decoding anything. The integration tests
generate a small OmeTV/Omegle style video (two feeds separated by a black seam)
and prove that both engines now produce the permanent top/bottom layout from the
composition itself - even when no face can be tracked at all.
"""
import subprocess

import pytest

from chopster.clipper.panel_layout import (
    LAYOUT_STRONG_CONFIDENCE,
    LayoutProbe,
    layout_panels,
)
from chopster.clipper.split_screen import (
    FALLBACK_PANEL_IDS,
    SPLIT_MULTI_FOCUS,
    SPLIT_TWO_PERSON,
    build_vstack_composition,
    forced_panel_plan,
    layout_panel_plan,
    panels_at,
    reaction_split_windows,
    region_crop_window,
    select_two_person_panels,
)

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")


def _textured(width, height, seed, base):
    """Deterministic imagery: one blob plus noise, like a webcam feed."""
    rng = np.random.default_rng(seed)
    img = np.full((height, width, 3), base, dtype=np.float32)
    img += rng.normal(0, 14, (height, width, 3))
    yy, xx = np.mgrid[0:height, 0:width]
    img += (60 * np.exp(-(((xx - width * 0.5) / (width * 0.22)) ** 2
                          + ((yy - height * 0.32) / (height * 0.28)) ** 2)))[:, :, None]
    return np.clip(img, 0, 255).astype(np.uint8)


def _two_feed_frame(seed=1, seam=10, width=1920, height=1080):
    half = (width - seam) // 2
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :half] = _textured(half, height, seed, 70)
    frame[:, half + seam:] = _textured(half, height, seed + 7, 130)
    return frame


def _probe(frames):
    probe = LayoutProbe()
    for idx, frame in enumerate(frames):
        probe.add(frame, idx * 0.75)
    return probe


def test_two_feed_composition_is_recognised_from_one_frame():
    info = _probe([_two_feed_frame()]).analyze()
    assert info is not None
    assert info["kind"] == "side_by_side"
    assert 0.45 <= info["split_x"] <= 0.55
    assert info["confidence"] >= LAYOUT_STRONG_CONFIDENCE


def test_two_feed_composition_needs_temporal_agreement():
    frames = [_two_feed_frame(seed=i) for i in range(1, 6)]
    info = _probe(frames).analyze()
    assert info is not None
    assert info["frames_agreed"] >= 3
    # One frame of the same clip alone is still enough (the UI probe).
    assert _probe(frames[:1]).analyze() is not None


def test_single_scene_frames_are_not_mistaken_for_two_feeds():
    assert _probe([_textured(1920, 1080, 3, 90)]).analyze() is None
    assert _probe([_textured(1920, 1080, i, 90) for i in range(4)]).analyze() is None


def test_layout_panels_keep_one_feed_per_panel():
    panels = layout_panels(0.5)
    assert [p["id"] for p in panels] == ["layout-left", "layout-right"]
    left, right = panels
    assert left["region"] == {"left": 0.0, "right": 0.5, "top": 0.0, "bottom": 1.0}
    assert right["region"] == {"left": 0.5, "right": 1.0, "top": 0.0, "bottom": 1.0}
    assert left["box"]["right"] <= right["box"]["left"]  # feeds never overlap
    assert left["cx"] < 0.5 < right["cx"]


def test_subject_inside_a_feed_only_aims_the_panel():
    subject = [{"left": 0.2, "right": 0.3, "top": 0.1, "bottom": 0.25}]
    left = layout_panels(0.5, subjects=subject)[0]
    assert left["region"] == {"left": 0.0, "right": 0.5, "top": 0.0, "bottom": 1.0}
    assert left["box"]["left"] < 0.25 < left["box"]["right"]
    assert left["box"]["top"] < 0.1 < left["box"]["bottom"]


def test_region_panel_crop_stays_inside_its_own_feed():
    panel = layout_panels(0.5)[0]
    window = region_crop_window(panel, 1920, 1080, 1080 / 960)
    assert window is not None
    assert window["w"] <= 960
    assert window["x"] + window["w"] <= 960  # the other feed is never pulled in
    assert region_crop_window(layout_panels(0.5, subjects=None)[1], 1920, 1080, 1080 / 960)["x"] >= 960
    assert region_crop_window({"box": {}}, 1920, 1080, 1.0) is None


def test_vstack_graph_uses_a_static_crop_for_feed_panels():
    panels = layout_panel_plan(0.5, start=0.0, end=6.0)
    assert [p["id"] for p in panels] == ["layout-left", "layout-right"]
    assert panels[0]["samples"][0]["t"] == 0.0 and panels[0]["samples"][-1]["t"] == 6.0
    graph, label = build_vstack_composition(panels, source_w=1920, source_h=1080, out_w=1080, out_h=1920)
    assert graph.count("[0:v]crop=") == 2
    assert "vstack=inputs=2[split_stack]" in graph
    assert "x='" not in graph  # feed panels must not drift with the person
    assert label == "[split_stack]"


def test_probe_timeline_feeds_the_reaction_rule():
    probe = _probe([_two_feed_frame(seed=i) for i in range(1, 6)])
    info = probe.analyze()
    rows = probe.timeline(info["split_x"], duration=3.75)
    assert len(rows) == 5
    assert all(len(row["faces"]) == 2 for row in rows)
    assert [f["id"] for f in rows[0]["faces"]] == ["layout-left", "layout-right"]
    assert any(f["motion"] > 0.0 for row in rows for f in row["faces"])
    windows = reaction_split_windows(rows, start=0.0, end=3.75)
    # The pair order follows the editorial ranking (screen side is not implied).
    assert windows and set(windows[0]["pair"]) == {"layout-left", "layout-right"}
    assert select_two_person_panels(rows, start=0.0, end=3.75)["found"] is True
    preview = panels_at(rows, 1.0)
    assert len(preview) == 2 and preview[0]["box"]["right"] <= preview[1]["box"]["left"]


# --------------------------------------------------------------------------- #
# Integration: a real (generated) OmeTV/Omegle style recording.
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def two_feed_video(tmp_path_factory):
    """A 3s 960x540 encoded clip: two feeds, black seam, audio, no real faces."""
    from chopster.downloader.ffmpeg import ffmpeg_available, find_ffmpeg

    if not ffmpeg_available():
        pytest.skip("ffmpeg/ffprobe not available")
    target = tmp_path_factory.mktemp("two_feed") / "ome_tv.mp4"
    cmd = [
        find_ffmpeg(), "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc2=size=474x540:rate=25:duration=3",
        "-f", "lavfi", "-i", "testsrc2=size=474x540:rate=25:duration=3",
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-filter_complex",
        "[0:v]hue=h=60[a];[1:v]hue=h=200[b];"
        "[a]pad=480:540:0:0:black[ap];[b]pad=480:540:6:0:black[bp];[ap][bp]hstack=inputs=2[v]",
        "-map", "[v]", "-map", "2:a", "-shortest",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "3",
        str(target),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 or not target.exists():
        pytest.skip(f"ffmpeg could not build the fixture video: {proc.stderr[-200:]}")
    return target


def test_acs_detects_the_two_feed_source(two_feed_video):
    from chopster.auto_clip_studio.engine.video_engine import detect_reaction_windows, detect_two_person_boxes

    info = detect_two_person_boxes(str(two_feed_video), max_samples=8)
    assert info["found"] is True
    assert info["layout"]["kind"] == "side_by_side"
    assert 0.45 <= info["layout"]["split_x"] <= 0.55
    assert info["reason"] == "two-feed side-by-side layout"
    assert len(info["panels"]) == 2
    left, right = info["panels"]
    assert left["region"]["right"] <= right["region"]["left"] + 0.05
    assert all(p.get("region") for p in info["panels"])
    # The measured per-feed rows keep the reaction rule usable as well.
    assert detect_reaction_windows(str(two_feed_video), max_samples=8)


def test_acs_seam_probe_reports_no_people_for_a_single_scene(tmp_path):
    """A single-camera clip must not turn into a split layout by accident."""
    from chopster.auto_clip_studio.engine.video_engine import detect_two_person_boxes

    single = tmp_path / "single_scene.avi"
    writer = cv2.VideoWriter(str(single), cv2.VideoWriter_fourcc(*"MJPG"), 25, (960, 540))
    if not writer.isOpened():
        pytest.skip("cv2 could not open a video writer")
    try:
        rng = np.random.default_rng(7)
        yy, xx = np.mgrid[0:540, 0:960]
        for idx in range(40):
            frame = (110 + 40 * (yy / 540.0))[:, :, None] * np.ones((1, 1, 3), dtype=np.float32)
            frame += (25 * np.exp(-(((xx - 460 - idx * 3) / 150.0) ** 2
                                    + ((yy - 220) / 190.0) ** 2)))[:, :, None]
            frame += rng.normal(0, 6, frame.shape)
            writer.write(np.clip(frame, 0, 255).astype(np.uint8))
    finally:
        writer.release()
    info = detect_two_person_boxes(str(single), max_samples=6)
    assert info["found"] is False
    assert info["layout"] == {}


def test_cca_split_mode_and_legacy_mode_on_the_same_clip(two_feed_video):
    from chopster.clipper.face_tracking import detect_face_focus

    camera, split = {}, {}
    points = detect_face_focus(str(two_feed_video), 0.0, 3.0, 0.9, 600, SPLIT_TWO_PERSON, None,
                               {"target_aspect": 9.0 / 16.0, "ai_camera_director": False},
                               evidence_out=camera, split_out=split)
    assert points and points[0].source == "layout-only"
    panels = [p for p in (split["panel_plan"] or []) if p]
    assert split["source"] == "side-by-side layout"
    assert split["layout"]["kind"] == "side_by_side"
    assert split["degraded"] is False
    assert len(panels) == 2 and all(p.get("region") for p in panels)
    assert camera["timeline"]  # the two measured feeds are published
    # The reaction mode reuses the same measured feed rows when the tracker is blind.
    reaction = {}
    detect_face_focus(str(two_feed_video), 0.0, 3.0, 0.9, 600, SPLIT_MULTI_FOCUS, None,
                      {"target_aspect": 9.0 / 16.0, "ai_camera_director": False}, split_out=reaction)
    assert reaction["reaction_windows"] or "dua feed" in str(reaction["reason"])
    # Legacy modes keep their existing contract: no faces -> no camera plan.
    with pytest.raises(RuntimeError):
        detect_face_focus(str(two_feed_video), 0.0, 3.0, 0.9, 600, "Smart", None,
                          {"target_aspect": 9.0 / 16.0, "ai_camera_director": False})


def test_cca_export_stacks_left_feed_on_top_of_the_right_feed(two_feed_video, tmp_path):
    from chopster.clipper.clip_renderer import export_clip
    from chopster.clipper.face_tracking import detect_face_focus

    split = {}
    detect_face_focus(str(two_feed_video), 0.0, 3.0, 0.9, 600, SPLIT_TWO_PERSON, None,
                      {"target_aspect": 9.0 / 16.0, "ai_camera_director": False}, split_out=split)
    panels = [p for p in (split["panel_plan"] or []) if p]
    output = tmp_path / "split.mp4"
    result = export_clip(str(two_feed_video), 0.0, 3.0, str(output), quality="fast", aspect="9:16",
                         copy_if_possible=False, composition="split_v", panel_plan=panels)
    assert result.success is True, result.error
    src = cv2.VideoCapture(str(two_feed_video))
    ok, source_frame = src.read()
    src.release()
    out = cv2.VideoCapture(str(output))
    ok_out, rendered = out.read()
    out.release()
    assert ok and ok_out
    assert (rendered.shape[1], rendered.shape[0]) == (1080, 1920)
    seam = source_frame.shape[1] // 2
    height, width = rendered.shape[:2]
    top = rendered[: height // 2].reshape(-1, 3).mean(axis=0)
    bottom = rendered[height // 2:].reshape(-1, 3).mean(axis=0)
    left_feed = cv2.resize(source_frame[:, : seam - 6], (width, height // 2),
                           interpolation=cv2.INTER_AREA).reshape(-1, 3).mean(axis=0)
    right_feed = cv2.resize(source_frame[:, seam + 6:], (width, height // 2),
                            interpolation=cv2.INTER_AREA).reshape(-1, 3).mean(axis=0)
    assert abs(top - left_feed).mean() < abs(top - right_feed).mean()
    assert abs(bottom - right_feed).mean() < abs(bottom - left_feed).mean()


# --------------------------------------------------------------------------- #
# Hard guarantee: a selected split mode always produces its two panels.
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def single_scene_video(tmp_path_factory):
    """A 1.6s single-camera clip without faces and without a feed seam."""
    target = tmp_path_factory.mktemp("single_scene") / "single_scene.avi"
    writer = cv2.VideoWriter(str(target), cv2.VideoWriter_fourcc(*"MJPG"), 25, (960, 540))
    if not writer.isOpened():
        pytest.skip("cv2 could not open a video writer")
    try:
        rng = np.random.default_rng(7)
        yy, xx = np.mgrid[0:540, 0:960]
        for idx in range(40):
            frame = (110 + 40 * (yy / 540.0))[:, :, None] * np.ones((1, 1, 3), dtype=np.float32)
            frame += (25 * np.exp(-(((xx - 460 - idx * 3) / 150.0) ** 2
                                    + ((yy - 220) / 190.0) ** 2)))[:, :, None]
            frame += rng.normal(0, 6, frame.shape)
            writer.write(np.clip(frame, 0, 255).astype(np.uint8))
    finally:
        writer.release()
    return target


def test_forced_panel_plan_always_returns_two_panels():
    panels = forced_panel_plan(end=6.0)
    assert [p["id"] for p in panels] == list(FALLBACK_PANEL_IDS)
    assert [p["region"] for p in panels] == [
        {"left": 0.0, "right": 0.5, "top": 0.0, "bottom": 1.0},
        {"left": 0.5, "right": 1.0, "top": 0.0, "bottom": 1.0},
    ]
    assert panels[0]["samples"][0]["t"] == 0.0 and panels[0]["samples"][-1]["t"] == 6.0
    # A measured seam is honoured; a bogus value can never break the pair.
    assert forced_panel_plan(end=2.0, split_x=0.62)[0]["region"]["right"] == 0.62
    assert forced_panel_plan(end=2.0, split_x=float("nan"))[0]["region"]["right"] == 0.5
    assert len(forced_panel_plan(end=2.0, split_x=0.9)) == 2
    # A subject only aims the crop inside its own half.
    aimed = forced_panel_plan(end=2.0, subjects=[{"left": 0.1, "right": 0.2, "top": 0.1, "bottom": 0.3}])
    assert aimed[0]["box"]["left"] < 0.2 < aimed[0]["box"]["right"]
    assert aimed[1]["region"] == {"left": 0.5, "right": 1.0, "top": 0.0, "bottom": 1.0}
    graph, label = build_vstack_composition(panels, source_w=1920, source_h=1080, out_w=1080, out_h=1920)
    assert "vstack=inputs=2[split_stack]" in graph and label == "[split_stack]"


def test_cca_two_person_mode_always_plans_two_panels(single_scene_video):
    """No faces, no feeds: the chosen mode still publishes its guaranteed pair."""
    from chopster.clipper.face_tracking import detect_face_focus

    split = {}
    detect_face_focus(str(single_scene_video), 0.0, 1.6, 0.9, 600, SPLIT_TWO_PERSON, None,
                      {"target_aspect": 9.0 / 16.0, "ai_camera_director": False}, split_out=split)
    assert split["source"] == "automatic split"
    assert split["guaranteed"] is True
    assert split["tracked_people"] == 0
    assert "atas = separuh kiri" in split["reason"]
    panels = [p for p in (split["panel_plan"] or []) if p]
    assert len(panels) == 2 and all(p.get("region") for p in panels)
    graph, _ = build_vstack_composition(panels, source_w=960, source_h=540, out_w=1080, out_h=1920)
    assert "vstack=inputs=2[split_stack]" in graph
    # Legacy modes keep their contract: a face-less source still fails loudly.
    with pytest.raises(RuntimeError):
        detect_face_focus(str(single_scene_video), 0.0, 1.6, 0.9, 600, "Smart", None,
                          {"target_aspect": 9.0 / 16.0, "ai_camera_director": False})


def test_acs_two_person_mode_always_renders_two_panels(single_scene_video):
    from chopster.auto_clip_studio.engine.video_engine import detect_two_person_boxes

    # The WYSIWYG probe stays honest ...
    honest = detect_two_person_boxes(str(single_scene_video), max_samples=6)
    assert honest["found"] is False and honest["fallback"] is False
    # ... while the renderer of the chosen mode gets its guaranteed pair.
    info = detect_two_person_boxes(str(single_scene_video), max_samples=6, guarantee=True)
    assert info["found"] is True and info["fallback"] is True
    assert len(info["panels"]) == 2
    left, right = info["panels"]
    assert left["region"] == {"left": 0.0, "right": 0.5, "top": 0.0, "bottom": 1.0}
    assert right["region"] == {"left": 0.5, "right": 1.0, "top": 0.0, "bottom": 1.0}
    graph, label = build_vstack_composition(info["panels"], source_w=960, source_h=540,
                                            out_w=1080, out_h=1920)
    assert "vstack=inputs=2[split_stack]" in graph and label == "[split_stack]"


def test_cca_export_splits_a_faceless_clip_into_two_forced_panels(single_scene_video, tmp_path):
    from chopster.clipper.clip_renderer import export_clip

    output = tmp_path / "forced_split.mp4"
    result = export_clip(str(single_scene_video), 0.0, 1.6, str(output), quality="fast", aspect="9:16",
                         copy_if_possible=False, composition="split_v",
                         panel_plan=forced_panel_plan(end=1.6))
    assert result.success is True, result.error
    out = cv2.VideoCapture(str(output))
    ok, rendered = out.read()
    out.release()
    assert ok
    assert (rendered.shape[1], rendered.shape[0]) == (1080, 1920)