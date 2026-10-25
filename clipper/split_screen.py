"""Split-screen compositions for Chopster clips.

Two independent editorial modes live here and they must never leak into the
legacy framing modes ("Auto Framing", "Smart", "Two Person", "4:3", "1:1",
"Original", ...):

* ``SPLIT_TWO_PERSON`` - a permanent vertical (top/bottom) two-person layout.
  Each panel follows its own tracked person and reuses the close-up that is
  already present in the source: the crop window is only as small as the
  detected subject envelope requires, never smaller.
* ``SPLIT_MULTI_FOCUS`` - short, temporary side-by-side (horizontal) windows.
  The base framing stays exactly what the normal pipeline produced; the
  side-by-side composition only appears while a reaction moment is active and
  then disappears again.

When the source is *already* a two-feed side-by-side composition (OmeTV/Omegle
calls) the tracker may find fewer than two people even though the composition
is obvious. ``layout_panel_plan`` builds the two panels from that composition
instead (one panel per feed, subject inside the feed only aiming the crop), so
the same permanent top/bottom layout is still produced.

``forced_panel_plan`` is the hard guarantee behind the permanent split: because
the mode is an explicit user choice, two panels are *always* published. When
neither the tracker (two people) nor the layout probe (two feeds) produced a
pair, the frame itself becomes the pair - left half on top, right half below -
so a selected split mode can never silently fall back to the legacy single-panel
render.

Everything in this module is pure Python: it reads the camera ``timeline``
produced by :mod:`chopster.clipper.face_tracking` and returns dictionaries or
FFmpeg filter strings. No video decoding, no AI calls, no UI access, so the
behaviour is unit-testable without running a real render.
"""
from __future__ import annotations

import math
from typing import Any, Iterable

# Reframe mode identifiers shared by the UI, the tracker and the renderer.
SPLIT_TWO_PERSON = "Two-Person Split Screen"
SPLIT_MULTI_FOCUS = "Multi-Person Smart Focus (Reaction Split)"

# Legacy reframe modes that run the subject tracker / Camera Director. Kept here
# so the UI checks exist in exactly one place.
TRACKED_REFRAIM_MODES = ("Smart", "AI Camera Director", "Speaker Focus", "Two Person")

DESIRED_SUBJECT_HEIGHT = 0.62
MIN_CROP_HEIGHT_RATIO = 0.30
MIN_TRACK_COVERAGE = 0.16

# Panels that were not measured but derived from the frame geometry, used by
# ``forced_panel_plan`` so the selected split mode always stays a split. The
# distinct ids let the status text say where the panels came from.
FALLBACK_PANEL_IDS = ("split-auto-left", "split-auto-right")
# The fallback split line: exactly half of the frame per panel.
FALLBACK_SPLIT_X = 0.5


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return low
    if math.isnan(v):
        return low
    return max(low, min(high, v))


def _even(value: float, minimum: int = 2) -> int:
    try:
        v = int(round(float(value)))
    except (TypeError, ValueError):
        v = int(minimum)
    v = max(int(minimum), v)
    return v - (v % 2)


def _face_rows(timeline: Iterable[dict[str, Any]], window: tuple[float, float] | None = None) -> list[dict[str, Any]]:
    """Normalise timeline rows into flat per-face samples within a time window."""
    lo, hi = (window or (None, None))
    rows: list[dict[str, Any]] = []
    for row in timeline or []:
        if not isinstance(row, dict):
            continue
        try:
            t = float(row.get("t", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if lo is not None and t < float(lo) - 0.001:
            continue
        if hi is not None and t > float(hi) + 0.001:
            continue
        for face in row.get("faces") or []:
            if not isinstance(face, dict):
                continue
            try:
                left = _clamp(face.get("left", 0.5))
                right = _clamp(face.get("right", 0.5))
                top = _clamp(face.get("top", 0.5))
                bottom = _clamp(face.get("bottom", 0.5))
            except (TypeError, ValueError):
                continue
            id_raw = face.get("id")
            left, right = min(left, right), max(left, right)
            top, bottom = min(top, bottom), max(top, bottom)
            if right - left <= 0.0 or bottom - top <= 0.0:
                continue
            rows.append({
                "t": t,
                "id": str(id_raw) if id_raw is not None else "",
                "cx": _clamp(face.get("x", (left + right) * 0.5)),
                "cy": _clamp(face.get("y", (top + bottom) * 0.5)),
                "left": left, "right": right, "top": top, "bottom": bottom,
                "motion": _clamp(face.get("motion", 0.0), 0.0, 12.0),
            })
    return rows


def rank_tracks(timeline: Iterable[dict[str, Any]], window: tuple[float, float] | None = None) -> list[dict[str, Any]]:
    """Rank tracked people by editorial relevance (coverage x area x activity)."""
    rows = _face_rows(timeline, window)
    if not rows:
        return []
    buckets: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if not r["id"]:
            continue
        buckets.setdefault(r["id"], []).append(r)
    if not buckets:
        return []
    total_times = max(1, len({round(r["t"], 2) for r in rows}))
    tracks: list[dict[str, Any]] = []
    for tid, samples in buckets.items():
        times = {round(s["t"], 2) for s in samples}
        area = sum((s["right"] - s["left"]) * (s["bottom"] - s["top"]) for s in samples) / len(samples)
        motion = sum(s["motion"] for s in samples) / len(samples)
        cx = sum(s["cx"] for s in samples) / len(samples)
        tracks.append({
            "id": tid,
            "coverage": len(times) / total_times,
            "samples": len(samples),
            "area": area,
            "motion": motion,
            "cx": cx,
        })
    tracks.sort(
        key=lambda t: (t["coverage"] * (0.4 + min(1.6, t["area"] * 6.0)) * (1.0 + min(0.5, t["motion"] / 8.0)), t["samples"]),
        reverse=True,
    )
    return tracks


def _fill_gaps(samples: list[dict[str, Any]], max_gap: float = 2.5) -> list[dict[str, Any]]:
    """Hold the last known position across short detection gaps.

    A tracker dropout of a few samples must not flip a panel to another person;
    the previous position is kept instead.
    """
    if len(samples) < 2:
        return samples
    out = [dict(samples[0])]
    for s in samples[1:]:
        prev = out[-1]
        if (float(s["t"]) - float(prev["t"])) > max_gap:
            held = dict(prev)
            held["t"] = round(float(s["t"]) - 0.001, 3)
            held["held"] = True
            out.append(held)
        out.append(dict(s))
    return out


def _median(values: list[float], default: float = 0.5) -> float:
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return default
    mid = len(clean) // 2
    if len(clean) % 2:
        return float(clean[mid])
    return float((clean[mid - 1] + clean[mid]) / 2.0)


def _panel_samples(rows: list[dict[str, Any]], track_id: str) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        if r["id"] != track_id:
            continue
        out.append({"t": round(r["t"], 3), "cx": round(r["cx"], 6), "cy": round(r["cy"], 6),
                    "left": round(r["left"], 6), "right": round(r["right"], 6),
                    "top": round(r["top"], 6), "bottom": round(r["bottom"], 6)})
    out.sort(key=lambda s: s["t"])
    return _fill_gaps(out)


def _panel_payload(track: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    samples = _panel_samples(rows, str(track["id"]))
    if not samples:
        return None
    return {
        "id": str(track["id"]),
        "coverage": round(float(track["coverage"]), 4),
        "cx": round(_median([s["cx"] for s in samples]), 6),
        "cy": round(_median([s["cy"] for s in samples]), 6),
        "box": {
            "left": round(_median([s["left"] for s in samples]), 6),
            "right": round(_median([s["right"] for s in samples]), 6),
            "top": round(_median([s["top"] for s in samples]), 6),
            "bottom": round(_median([s["bottom"] for s in samples]), 6),
        },
        "samples": samples,
    }


def select_two_person_panels(
    timeline: Iterable[dict[str, Any]],
    *,
    start: float = 0.0,
    end: float | None = None,
) -> dict[str, Any]:
    """Choose the two people for the permanent top/bottom split layout.

    Returns ``{"found": bool, "degraded": bool, "panels": [...]}``. ``found`` is
    False when nothing usable was tracked, so the caller can keep the legacy
    single-panel render instead of producing an empty half-frame. With exactly
    one tracked person the layout is still honoured (``degraded`` True) and the
    second panel shows a fitted wide context view.
    """
    window = None if end is None else (float(start), float(end))
    rows = _face_rows(timeline, window)
    tracks = [t for t in rank_tracks(timeline, window) if t["coverage"] >= MIN_TRACK_COVERAGE]
    if not tracks or not rows:
        return {"found": False, "degraded": False, "panels": [], "reason": "no tracked people"}

    panels: list[dict[str, Any] | None] = []
    for track in tracks[:2]:
        payload = _panel_payload(track, rows)
        if payload is not None:
            panels.append(payload)
    if not panels:
        return {"found": False, "degraded": False, "panels": [], "reason": "no tracked people"}

    degraded = len(panels) < 2
    # Screen order follows the source composition: the left person stays on top.
    panels.sort(key=lambda p: float((p or {}).get("cx", 0.5)))
    while len(panels) < 2:
        panels.append(None)
    return {
        "found": True,
        "degraded": degraded,
        "panels": panels,
        "reason": "single tracked person (wide context panel)" if degraded else "two-person vertical split",
    }


def panel_crop_px(panel: dict[str, Any], source_w: int, source_h: int, target_aspect: float,
                  *, desired_subject_height: float = DESIRED_SUBJECT_HEIGHT) -> tuple[int, int]:
    """Compute a crop window that keeps the source close-up instead of re-zooming.

    Two rules are combined:

    * an existing close-up is never enlarged - when the person already fills the
      frame the window stays at full source height;
    * a person who is small in the source is framed at ``desired_subject_height``
      of the panel, but the window always contains the detected envelope (+5%),
      so a face is never cut, and it never zooms tighter than
      ``MIN_CROP_HEIGHT_RATIO`` of the source height.
    """
    sw = max(2, int(source_w or 2))
    sh = max(2, int(source_h or 2))
    ar = float(target_aspect) if target_aspect and target_aspect > 0 else 9.0 / 16.0

    widest_h = min(float(sh), float(sw) / ar)
    box = panel.get("box") or {}
    box_h = max(0.04, _clamp(box.get("bottom", 0.5)) - _clamp(box.get("top", 0.5)))
    box_w = max(0.04, _clamp(box.get("right", 0.5)) - _clamp(box.get("left", 0.5)))
    needed_h = (box_h * sh) / max(0.20, float(desired_subject_height))
    # Never cut the person: the window must contain the whole envelope.
    contain_h = max(box_h * sh * 1.05, (box_w * sw * 1.05) / ar)
    crop_h = max(min(widest_h, needed_h), contain_h)
    crop_h = min(crop_h, float(sh))
    crop_h = max(crop_h, sh * MIN_CROP_HEIGHT_RATIO)
    crop_w = crop_h * ar
    if crop_w > sw:
        crop_w = float(sw)
        crop_h = crop_w / ar
    return _even(min(crop_w, float(sw))), _even(min(crop_h, float(sh)))


def region_crop_window(panel: dict[str, Any], source_w: int, source_h: int,
                       target_aspect: float) -> dict[str, int] | None:
    """Static crop window (px) fitting one feed region into one panel.

    Used by the two-feed fallback (see :mod:`chopster.clipper.panel_layout`).
    The region is the whole webcam feed, so the crop keeps the feed width and
    only trims the height the panel aspect cannot use. The person tracked
    inside that feed only moves the window - horizontal centring plus a small
    headroom above the head - it never changes its size, so a small webcam face
    cannot zoom the panel into the face. Returns ``None`` for normal panels.
    """
    if not isinstance(panel, dict):
        return None
    region = panel.get("region") if isinstance(panel.get("region"), dict) else None
    if not region:
        return None
    sw = max(2, int(source_w or 2))
    sh = max(2, int(source_h or 2))
    ar = float(target_aspect) if target_aspect and target_aspect > 0 else 9.0 / 16.0
    rl = _clamp(region.get("left", 0.0))
    rr = _clamp(region.get("right", 1.0))
    rt = _clamp(region.get("top", 0.0))
    rb = _clamp(region.get("bottom", 1.0))
    if rr - rl < 0.02 or rb - rt < 0.02:
        return None
    reg_w = (rr - rl) * sw
    reg_h = (rb - rt) * sh
    if reg_w / max(1.0, reg_h) > ar:
        crop_h, crop_w = reg_h, reg_h * ar
    else:
        crop_w, crop_h = reg_w, reg_w / ar
    crop_w = _even(min(crop_w, float(sw)))
    crop_h = _even(min(crop_h, float(sh)))
    min_x = rl * sw
    max_x = max(min_x, rr * sw - crop_w)
    min_y = rt * sh
    max_y = max(min_y, rb * sh - crop_h)
    box = panel.get("box") if isinstance(panel.get("box"), dict) else {}
    bl = _clamp(box.get("left", rl))
    br = _clamp(box.get("right", rr))
    bt = _clamp(box.get("top", rt))
    centre_x = (bl + br) * 0.5 * sw
    x = min(max(centre_x - crop_w * 0.5, min_x), max_x)
    y = min(max(bt * sh - crop_h * 0.08, min_y), max_y)
    return {"w": int(crop_w), "h": int(crop_h), "x": int(round(x)), "y": int(round(y))}


def _axis_value(axis: str, norm: float, size_px: int) -> str:
    if axis == "x":
        return f"((iw-{int(size_px)})*{_clamp(norm):.6f})"
    return f"((ih-{int(size_px)})*{_clamp(norm):.6f})"


def _eased_axis_expr(samples: list[dict[str, Any]], axis: str, size_px: int) -> str:
    """Smoothly interpolated crop offset, mirroring the existing camera path style."""
    seq = [(float(s.get("t", 0.0)), float(s.get(axis, 0.5))) for s in samples if s]
    if not seq:
        return _axis_value(axis, 0.5, size_px)
    if len(seq) == 1:
        return _axis_value(axis, seq[0][1], size_px)
    parts = None
    for (t0, n0), (t1, n1) in reversed(list(zip(seq[:-1], seq[1:]))):
        v0 = _axis_value(axis, n0, size_px)
        v1 = _axis_value(axis, n1, size_px)
        dur = max(0.01, t1 - t0)
        u = f"clip((t-{t0:.3f})/{dur:.3f},0,1)"
        eased = f"({u}*{u}*(3-2*{u}))"
        seg = f"({v0}+({v1}-{v0})*{eased})"
        parts = seg if parts is None else f"if(lt(t,{t1:.3f}),{seg},{parts})"
    first = _axis_value(axis, seq[0][1], size_px)
    return f"if(lt(t,{seq[0][0]:.3f}),{first},{parts})"


def _panel_filter(panel: dict[str, Any] | None, label: str, source_w: int, source_h: int,
                  out_w: int, out_h: int) -> str:
    if panel is None:
        # Degraded layout: show the full source (letterboxed) instead of an empty panel.
        return (f"[0:v]scale=w={out_w}:h={out_h}:force_original_aspect_ratio=decrease,"
                f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:color=black[{label}]")
    window = region_crop_window(panel, source_w, source_h, float(out_w) / max(1.0, float(out_h)))
    if window:
        # Two-feed source: the panel is its own feed, cropped statically so the
        # composition does not drift with the person's movement.
        return (f"[0:v]crop={window['w']}:{window['h']}:x={window['x']}:y={window['y']},"
                f"scale=w={out_w}:h={out_h}:force_original_aspect_ratio=increase,"
                f"crop={out_w}:{out_h}[{label}]")
    crop_w, crop_h = panel_crop_px(panel, source_w, source_h, float(out_w) / max(1.0, float(out_h)))
    samples = panel.get("samples") or []
    xexpr = _eased_axis_expr(samples, "x", crop_w).replace(",", r"\,")
    yexpr = _eased_axis_expr(samples, "y", crop_h).replace(",", r"\,")
    return (f"[0:v]crop={crop_w}:{crop_h}:x='{xexpr}':y='{yexpr}',"
            f"scale=w={out_w}:h={out_h}:force_original_aspect_ratio=increase,"
            f"crop={out_w}:{out_h}[{label}]")


def build_vstack_composition(
    panels: list[dict[str, Any] | None],
    *,
    source_w: int,
    source_h: int,
    out_w: int = 1080,
    out_h: int = 1920,
) -> tuple[str, str]:
    """Build the permanent top/bottom two-panel composition.

    Returns ``(filter_complex, out_label)``. The label is the *full canvas*
    output, ready for the existing subtitle/watermark/title chains.
    """
    if len([p for p in panels or []]) < 2:
        raise ValueError("split screen membutuhkan dua panel")
    total_w = _even(out_w, 2)
    panel_h = _even(max(2, out_h) / 2, 2)
    top = _panel_filter(panels[0], "split_top", source_w, source_h, total_w, panel_h)
    bottom = _panel_filter(panels[1], "split_bottom", source_w, source_h, total_w, panel_h)
    graph = f"{top};{bottom};[split_top][split_bottom]vstack=inputs=2[split_stack]"
    return graph, "[split_stack]"


def _window_samples(rows: list[dict[str, Any]], track_id: str, start: float, end: float) -> list[dict[str, Any]]:
    picked = [r for r in rows if r["id"] == track_id and start - 0.001 <= r["t"] <= end + 0.001]
    samples = [{"t": round(r["t"], 3), "cx": round(r["cx"], 6), "cy": round(r["cy"], 6),
                "left": round(r["left"], 6), "right": round(r["right"], 6),
                "top": round(r["top"], 6), "bottom": round(r["bottom"], 6)} for r in picked]
    samples.sort(key=lambda s: s["t"])
    return _fill_gaps(samples)


def reaction_split_windows(
    timeline: Iterable[dict[str, Any]],
    *,
    start: float = 0.0,
    end: float | None = None,
    min_duration: float = 1.2,
    max_duration: float = 6.0,
    min_gap: float = 1.0,
    max_windows: int = 6,
    activity_floor: float = 0.75,
) -> list[dict[str, Any]]:
    """Local fallback: moments where two people are simultaneously active.

    Used when no AI Camera Director plan is available, so the multi-person
    option still works offline. Intervals are merged, capped and separated by a
    minimum gap; the base framing is never replaced outside these windows.
    """
    window = None if end is None else (float(start), float(end))
    rows = _face_rows(timeline, window)
    tracks = rank_tracks(timeline, window)[:4]
    if len(tracks) < 2 or not rows:
        return []
    pair = (str(tracks[0]["id"]), str(tracks[1]["id"]))
    floor = max(0.05, float(activity_floor))
    active_times: list[float] = []
    for row_t in sorted({round(r["t"], 2) for r in rows}):
        levels = {tid: 0.0 for tid in pair}
        for r in rows:
            if round(r["t"], 2) != row_t or r["id"] not in levels:
                continue
            levels[r["id"]] = max(levels[r["id"]], float(r["motion"]))
        if levels[pair[0]] >= floor and levels[pair[1]] >= floor:
            active_times.append(float(row_t))
    if not active_times:
        return []

    deltas = [b - a for a, b in zip(active_times[:-1], active_times[1:]) if b > a]
    step = max(0.5, _median(deltas, 0.75)) if deltas else 0.75
    runs: list[list[float]] = []
    run_start = prev = active_times[0]
    for t in active_times[1:]:
        if (t - prev) <= step * 1.75:
            prev = t
            continue
        runs.append([run_start, prev])
        run_start = prev = t
    runs.append([run_start, prev])

    merged: list[list[float]] = []
    for ws, we in runs:
        we = max(we + step, ws + float(min_duration))
        we = min(we, ws + float(max_duration))
        if merged and (ws - merged[-1][1]) < float(min_gap):
            merged[-1][1] = max(merged[-1][1], we)
            continue
        merged.append([ws, we])

    out: list[dict[str, Any]] = []
    for ws, we in merged[:max(1, int(max_windows))]:
        left = _window_samples(rows, pair[0], ws, we)
        right = _window_samples(rows, pair[1], ws, we)
        if not left or not right:
            continue
        out.append({
            "start": round(float(ws), 3),
            "end": round(float(we), 3),
            "pair": [pair[0], pair[1]],
            "panel_left": {"id": pair[0], "samples": left},
            "panel_right": {"id": pair[1], "samples": right},
            "source": "local-reaction-activity",
            "reason": "two people active at the same time",
        })
    return out


def normalize_ai_windows(
    shots: Iterable[dict[str, Any]],
    timeline: Iterable[dict[str, Any]],
    *,
    start: float = 0.0,
    end: float | None = None,
    min_duration: float = 1.2,
    max_duration: float = 8.0,
    min_gap: float = 0.8,
    max_windows: int = 6,
) -> list[dict[str, Any]]:
    """Validate and clamp AI ``reaction_split`` shots into safe render windows.

    The Camera Director only proposes *when* a split helps. Coordinates always
    come from local tracking, and a proposal is discarded when either person
    cannot be located, so the video can never end up with a half-empty panel.
    """
    window = None if end is None else (float(start), float(end))
    rows = _face_rows(timeline, window)
    if not rows:
        return []
    known = {str(t["id"]) for t in rank_tracks(timeline, window)}
    by_time = sorted({round(r["t"], 2) for r in rows})

    def nearest_time(t: float) -> float:
        return min(by_time, key=lambda x: abs(x - t)) if by_time else t

    out: list[dict[str, Any]] = []
    for shot in shots or []:
        if not isinstance(shot, dict):
            continue
        raw_pair = shot.get("split_pair") or shot.get("pair") or []
        usable_pair = list(dict.fromkeys(str(p) for p in raw_pair if str(p) in known))
        declared = str(shot.get("shot") or "").strip()
        # The Camera Director may either name the moment explicitly
        # (shot="reaction_split") or simply attach the two-person pair to one of
        # its normal shot decisions. Every other shot keeps its legacy meaning.
        if declared != "reaction_split" and len(usable_pair) < 2:
            continue
        try:
            ws = float(shot.get("start", 0.0) or 0.0)
            we = float(shot.get("end", ws + min_duration) or (ws + min_duration))
        except (TypeError, ValueError):
            continue
        if end is not None:
            we = min(we, float(end))
        ws = max(ws, float(start))
        if we - ws < float(min_duration):
            we = ws + float(min_duration)
        we = min(we, ws + float(max_duration))
        pair = usable_pair[:2]
        if len(pair) < 2:
            pair = [t["id"] for t in rank_tracks(timeline, (ws, we))[:2]]
        if len(pair) < 2:
            continue
        left = _window_samples(rows, pair[0], ws, we)
        right = _window_samples(rows, pair[1], ws, we)
        if not left or not right:
            lo, hi = nearest_time(ws) - 0.6, nearest_time(we) + 0.6
            left = left or _window_samples(rows, pair[0], lo, hi)
            right = right or _window_samples(rows, pair[1], lo, hi)
        if not left or not right:
            continue
        if out and (ws - float(out[-1]["end"])) < float(min_gap):
            # Adjacent proposals merge so the screen never flickers, but the
            # merged window stays bounded by max_duration.
            merged_end = max(float(out[-1]["end"]), we)
            out[-1]["end"] = round(min(merged_end, float(out[-1]["start"]) + float(max_duration)), 3)
            continue
        out.append({
            "start": round(ws, 3),
            "end": round(we, 3),
            "pair": pair,
            "panel_left": {"id": pair[0], "samples": left},
            "panel_right": {"id": pair[1], "samples": right},
            "source": "ai-camera-director",
            "reason": str(shot.get("reason") or "AI reaction moment")[:160],
        })
        if len(out) >= max(1, int(max_windows)):
            break
    out.sort(key=lambda w: float(w["start"]))
    return out


def build_reaction_overlay(
    windows: list[dict[str, Any]],
    *,
    source_w: int,
    source_h: int,
    out_w: int = 1080,
    out_h: int = 1920,
    base_label: str = "[base]",
) -> tuple[str, str]:
    """Temporarily overlay a side-by-side pair on top of the normal framing.

    The base framing remains untouched; every window adds one horizontal
    composition that is only enabled while that moment is active, so the clip
    returns to its normal framing automatically.
    """
    clean = [w for w in windows or [] if isinstance(w, dict)]
    if not clean:
        return "", base_label
    total_w = _even(out_w, 2)
    total_h = _even(out_h, 2)
    panel_w = _even(total_w / 2, 2)
    chunks: list[str] = []
    label = base_label
    for idx, w in enumerate(clean):
        try:
            ws = max(0.0, float(w.get("start", 0.0)))
            we = max(ws + 0.2, float(w.get("end", ws + 1.0)))
        except (TypeError, ValueError):
            continue
        left_panel = w.get("panel_left") or {}
        right_panel = w.get("panel_right") or {}
        if not left_panel.get("samples") or not right_panel.get("samples"):
            continue
        left_label = f"rs{idx}L"
        right_label = f"rs{idx}R"
        stack_label = f"rs{idx}H"
        left = _panel_filter(left_panel, left_label, source_w, source_h, panel_w, total_h)
        right = _panel_filter(right_panel, right_label, source_w, source_h, panel_w, total_h)
        nxt = f"[rs{idx}out]"
        chunks.append(f"{left};{right};[{left_label}][{right_label}]hstack=inputs=2[{stack_label}]")
        chunks.append(f"{label}[{stack_label}]overlay=0:0:enable='between(t,{ws:.3f},{we:.3f})'{nxt}")
        label = nxt
    if not chunks:
        return "", base_label
    return ";".join(chunks), label


def _shift_samples(samples: Iterable[dict[str, Any]], shift: float) -> list[dict[str, Any]]:
    """Move sample times from source time to clip-relative time."""
    out: list[dict[str, Any]] = []
    for s in samples or []:
        if not isinstance(s, dict):
            continue
        q = dict(s)
        try:
            q["t"] = round(max(0.0, float(s.get("t", 0.0)) - float(shift)), 3)
        except (TypeError, ValueError):
            q["t"] = 0.0
        out.append(q)
    return out


def panel_plan_for_range(
    timeline: Iterable[dict[str, Any]],
    *,
    start: float,
    end: float,
) -> list[dict[str, Any] | None]:
    """Two-person panels limited to one clip and relative to its start.

    ``export_clip`` seeks with ``-ss`` before the input, so every filter
    expression is evaluated in clip-relative time. The tracker timeline is
    absolute, therefore both the selected people and the sample times are
    recomputed for the clip that is about to be rendered.
    """
    lo, hi = float(start), float(end)
    if hi - lo < 0.5:
        return []
    plan = select_two_person_panels(timeline, start=lo, end=hi)
    panels: list[dict[str, Any] | None] = []
    for panel in plan.get("panels") or []:
        if not panel:
            panels.append(None)
            continue
        q = dict(panel)
        q["samples"] = _shift_samples(panel.get("samples") or [], lo)
        panels.append(q)
    return panels


def layout_panel_plan(
    split_x: float,
    *,
    start: float = 0.0,
    end: float,
    subjects: Iterable[dict[str, Any]] | None = None,
    ids: tuple[str, str] = ("layout-left", "layout-right"),
    coverage: float = 1.0,
) -> list[dict[str, Any]]:
    """Two permanent panels for a source that is already split into two feeds.

    Fallback for OmeTV/Omegle style recordings: the tracker may have found
    fewer than two people while the composition itself is unmistakable. Each
    panel carries its own ``region`` (the feed) plus the person found inside it,
    which only aims the crop, so the permanent top/bottom layout still renders.
    """
    from .panel_layout import layout_panels

    lo = max(0.0, float(start))
    hi = max(lo + 0.02, float(end))
    panels: list[dict[str, Any]] = []
    for raw in layout_panels(split_x, subjects=subjects, ids=ids):
        q = dict(raw)
        q["coverage"] = round(float(coverage), 4)
        box = q.get("box") if isinstance(q.get("box"), dict) else {}
        sample = {
            "cx": q.get("cx", 0.5),
            "cy": q.get("cy", 0.5),
            "left": box.get("left", 0.0),
            "right": box.get("right", 1.0),
            "top": box.get("top", 0.0),
            "bottom": box.get("bottom", 1.0),
        }
        q["samples"] = [dict(sample, t=round(lo, 3)), dict(sample, t=round(hi, 3))]
        panels.append(q)
    return panels


def forced_panel_plan(
    *,
    start: float = 0.0,
    end: float,
    split_x: float | None = None,
    subjects: Iterable[dict[str, Any]] | None = None,
    ids: tuple[str, str] = FALLBACK_PANEL_IDS,
) -> list[dict[str, Any]]:
    """Two panels that always exist: the hard guarantee of the split mode.

    The permanent two-person mode is an explicit user choice, so it must never
    degrade into the legacy single-panel render. When neither the tracker (two
    people) nor the layout probe (two feeds) produced a pair, the frame itself is
    the pair: the left half becomes the top panel and the right half the bottom
    panel - at the measured ``split_x`` when a seam is known, otherwise exactly at
    the middle of the frame.

    ``subjects`` (normalised boxes) only *aims* the crop inside each half, so a
    tracked person in the left half can never leak into the right panel. The
    returned panels carry a ``region``, which is what makes the renderer use a
    static crop that cannot drift with the person's movement.
    """
    try:
        x = FALLBACK_SPLIT_X if split_x is None else float(split_x)
    except (TypeError, ValueError):
        x = FALLBACK_SPLIT_X
    if not (0.0 < x < 1.0) or math.isnan(x):
        x = FALLBACK_SPLIT_X
    try:
        return layout_panel_plan(x, start=float(start), end=float(end), subjects=subjects, ids=ids)
    except Exception:
        # Geometry only: the guaranteed path never raises.
        lo = max(0.0, float(start))
        hi = max(lo + 0.02, float(end))
        panels: list[dict[str, Any]] = []
        for panel_id, (left, right) in zip(ids, ((0.0, x), (x, 1.0))):
            box = {"left": round(left + 0.006, 4), "right": round(right - 0.006, 4),
                   "top": 0.006, "bottom": 0.994}
            sample = {"cx": round((left + right) / 2.0, 4), "cy": 0.5, **box}
            panels.append({
                "id": str(panel_id),
                "coverage": 1.0,
                "cx": sample["cx"],
                "cy": 0.5,
                "layout": "side_by_side",
                "region": {"left": round(left, 4), "right": round(right, 4), "top": 0.0, "bottom": 1.0},
                "box": box,
                "samples": [dict(sample, t=round(lo, 3)), dict(sample, t=round(hi, 3))],
            })
        return panels


def reaction_windows_for_range(
    timeline: Iterable[dict[str, Any]],
    *,
    start: float,
    end: float,
    shots: Iterable[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Reaction windows limited to one clip and relative to its start.

    The Camera Director proposes the moments; when no usable AI proposal is
    available the deterministic local activity analysis is used instead, so the
    multi-person mode keeps working offline.
    """
    lo, hi = float(start), float(end)
    if hi - lo < 0.5:
        return []
    windows = normalize_ai_windows(shots or [], timeline, start=lo, end=hi)
    if not windows:
        windows = reaction_split_windows(timeline, start=lo, end=hi)
    out: list[dict[str, Any]] = []
    for w in windows:
        if not isinstance(w, dict):
            continue
        try:
            ws = max(0.0, float(w.get("start", 0.0)) - lo)
            we = max(ws + 0.2, float(w.get("end", ws + 1.0)) - lo)
        except (TypeError, ValueError):
            continue
        we = min(we, hi - lo)
        if we - ws < 0.4:
            continue
        q = dict(w)
        q["start"] = round(ws, 3)
        q["end"] = round(we, 3)
        for key in ("panel_left", "panel_right"):
            panel = dict(w.get(key) or {})
            panel["samples"] = _shift_samples(panel.get("samples") or [], lo)
            q[key] = panel
        out.append(q)
    return out


def panels_at(
    timeline: Iterable[dict[str, Any]],
    t: float,
    *,
    window_seconds: float = 20.0,
    tolerance: float = 1.5,
) -> list[dict[str, Any] | None]:
    """Panels for one single moment, used by the WYSIWYG UI preview.

    The *selection* (which two people, and in which order) follows the same rule
    as the rendered composition, evaluated in a window around the previewed
    moment so it matches a clip that would contain it; only the movement is
    reduced to the frame the user is actually looking at.
    """
    try:
        moment = max(0.0, float(t))
    except (TypeError, ValueError):
        moment = 0.0
    half = max(float(tolerance), float(window_seconds) / 2.0)
    base = select_two_person_panels(timeline, start=moment - half, end=moment + half)
    selected = [p for p in (base.get("panels") or [])]
    if not base.get("found") or not selected:
        return []
    rows = _face_rows(timeline, (moment - float(tolerance), moment + float(tolerance)))
    panels: list[dict[str, Any] | None] = []
    for panel in selected:
        if not panel:
            panels.append(None)
            continue
        tid = str(panel.get("id"))
        mine = [r for r in rows if r["id"] == tid]
        if not mine:
            # Outside the detected range the person keeps their median position
            # instead of the panel flickering to the other person.
            box = dict(panel.get("box") or {})
            sample = {
                "t": round(moment, 3),
                "cx": float(panel.get("cx", 0.5)), "cy": float(panel.get("cy", 0.5)),
                "left": float(box.get("left", 0.35)), "right": float(box.get("right", 0.65)),
                "top": float(box.get("top", 0.15)), "bottom": float(box.get("bottom", 0.75)),
            }
        else:
            near = min(mine, key=lambda r: abs(float(r["t"]) - moment))
            sample = {"t": round(moment, 3), "cx": near["cx"], "cy": near["cy"],
                      "left": near["left"], "right": near["right"],
                      "top": near["top"], "bottom": near["bottom"]}
        q = dict(panel)
        q["cx"] = sample["cx"]
        q["cy"] = sample["cy"]
        q["box"] = {"left": sample["left"], "right": sample["right"],
                    "top": sample["top"], "bottom": sample["bottom"]}
        q["samples"] = _fill_gaps([sample])
        panels.append(q)
    return panels