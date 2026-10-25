"""Two-feed (side-by-side) source layout detection for split-screen planning.

OmeTV/Omegle style recordings are already a composition of two webcam feeds
placed next to each other. Face detection alone is unreliable on them: the
faces are small webcam faces and the frame the UI probes is often the very
first (connecting/placeholder) frame of the clip, so a face-only detector
reports "no two people" although the composition is an obvious, stable
two-feed split.

This module recognises the *composition* itself, using only frames the caller
has already decoded - no extra decoding, no AI, no UI access:

* both sides must look like real imagery (column contrast),
* a narrow seam (a flat and/or dark band) must separate them near the middle,
* the two sides must be independent feeds (brightness/contrast step at the seam),

and it measures per-side frame-difference motion on those same frames, so the
reaction rule keeps working even when no face is ever tracked. Faces stay
useful: the caller may pass them as ``subjects`` and they are only used to
tighten a panel *inside* its own feed, so a small webcam face can never become
an extreme zoom of the whole frame.

Known boundary: the rules are geometric, so a single-camera frame with a
straight, full-height, uniform dark band exactly at the frame centre is
indistinguishable from a feed gap and is reported as a two-feed source. The
callers therefore only use the verdict when the user explicitly picked a
split-screen mode *and* the tracker found fewer than two people, and they say so
in the status text.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

SEAM_SEARCH = (0.30, 0.70)
# Column contrast of one feed in the probe profile. Real footage lands around
# 5-12; this only rules out a source that is visually flat on both sides, where
# "two feeds" is unverifiable anyway.
MIN_SIDE_CONTRAST = 3.0
FRAME_SCORE_FLOOR = 0.45
STILL_SCORE_FLOOR = 0.55
LAYOUT_MIN_CONFIDENCE = 0.55
# Two seams belong to the same feed boundary when they sit this close.
SEAM_AGREEMENT = 0.035
# Frame-difference activity of a webcam feed is tiny, so it is scaled up to the
# same 0..12 range the face tracker uses for its ``motion`` field.
MOTION_SCALE = 2.0
# A composition this convincing is preferred over a face-only panel pair, so a
# small webcam face never decides the framing of a whole feed.
LAYOUT_STRONG_CONFIDENCE = 0.62
# The column profile keeps the native width (a thin feed seam must not be
# averaged away by a horizontal downscale) and only decimates rows: a vertical
# seam survives vertical decimation untouched. Very wide sources are capped so
# the profile stays small.
MAX_PROBE_WIDTH = 1600
PROBE_ROWS = 216
MAX_SEAM_CANDIDATES = 64
MAX_PROBE_FRAMES = 64


def _clamp(value: Any, low: float = 0.0, high: float = 1.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return low
    if math.isnan(v):
        return low
    return max(low, min(high, v))


class LayoutProbe:
    """Cheap accumulator that recognises a two-feed side-by-side source.

    The caller feeds it the frames it decodes anyway (``add``) and asks for the
    verdict once (``analyze``). A single frame is enough for the WYSIWYG frame
    probe; several frames make the verdict stricter (the seam must stay at the
    same place over time), which is what a full-clip analysis does.
    """

    def __init__(self, *, max_width: int = MAX_PROBE_WIDTH, rows: int = PROBE_ROWS,
                 max_frames: int = MAX_PROBE_FRAMES) -> None:
        self.max_width = max(64, int(max_width))
        self.rows = max(32, int(rows))
        self.width = 0  # fixed by the first accepted frame
        self.max_frames = max(1, int(max_frames))
        self._means: list[Any] = []
        self._stds: list[Any] = []
        self._diffs: list[Any] = []
        self._times: list[float | None] = []
        self._prev = None

    @property
    def frames(self) -> int:
        return len(self._means)

    @property
    def times(self) -> list[float | None]:
        return list(self._times)

    def add(self, frame: Any, t: float | None = None) -> bool:
        """Add one already decoded frame (BGR or gray). False when skipped."""
        if frame is None or self.frames >= self.max_frames:
            return False
        try:
            import cv2
            import numpy as np
        except Exception:
            return False
        try:
            gray = frame
            if getattr(frame, "ndim", 0) == 3:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            height, width = gray.shape[:2]
            if int(width) < 64 or int(height) < 32:
                return False
            target_w = min(int(width), self.max_width)
            if not self.width:
                self.width = target_w
            if target_w != self.width:
                return False  # mixed resolutions inside one clip
            target_h = min(int(height), self.rows)
            if target_w != int(width) or target_h != int(height):
                gray = cv2.resize(gray, (target_w, target_h), interpolation=cv2.INTER_AREA)
            gray = np.asarray(gray, dtype="float32")
        except Exception:
            return False
        if gray.shape[1] != self.width or gray.shape[0] < 8:
            return False
        try:
            self._means.append(gray.mean(axis=0))
            self._stds.append(gray.std(axis=0))
            if self._prev is not None and self._prev.shape == gray.shape:
                self._diffs.append(np.abs(gray - self._prev).mean(axis=0))
            else:
                self._diffs.append(None)
            self._times.append(float(t) if t is not None else None)
            self._prev = gray
        except Exception:
            return False
        return True

    def side_motion(self, split_x: float) -> tuple[float, float]:
        """Mean frame-difference activity of the left/right feed (0..12 scale)."""
        width = self.width
        if width <= 0:
            return 0.0, 0.0
        x = int(round(_clamp(split_x, 0.05, 0.95) * width))
        left_slice = slice(2, max(3, x - 2))
        right_slice = slice(min(width - 2, x + 3), width - 2)
        left = [float(d[left_slice].mean()) for d in self._diffs if d is not None and d.size == width]
        right = [float(d[right_slice].mean()) for d in self._diffs if d is not None and d.size == width]
        lm = min(12.0, (sum(left) / len(left)) * MOTION_SCALE) if left else 0.0
        rm = min(12.0, (sum(right) / len(right)) * MOTION_SCALE) if right else 0.0
        return round(lm, 3), round(rm, 3)

    def _refine_dark_seam(self, split_x: float) -> float:
        """Snap the seam onto the darkest feed gap near the candidate column.

        The best-scoring column often sits on the edge of the gap (a flat and
        stepped column is not necessarily the darkest one), so the reported
        boundary is verified against the actual dark band of every frame.
        """
        width = self.width
        if width <= 0:
            return split_x
        span = max(3, int(round(width * 0.035)))
        centre = int(round(_clamp(split_x, 0.05, 0.95) * width))
        picked: list[float] = []
        for mean, std in zip(self._means, self._stds):
            lo = max(0, centre - span)
            hi = min(width, centre + span + 1)
            block = mean[lo:hi]
            if not getattr(block, "size", 0):
                continue
            idx = int(lo + block.argmin())
            left_slice = slice(2, max(3, idx - 3))
            right_slice = slice(min(width - 2, idx + 4), width - 2)
            if left_slice.stop - left_slice.start < 4 or right_slice.stop - right_slice.start < 4:
                continue
            if min(float(std[left_slice].mean()), float(std[right_slice].mean())) < MIN_SIDE_CONTRAST:
                continue
            side_mean = min(float(mean[left_slice].mean()), float(mean[right_slice].mean()))
            if float(mean[idx]) > side_mean - 8.0:
                continue
            picked.append(idx / float(width))
        if len(picked) < max(1, int(math.ceil(0.5 * max(1, self.frames)))):
            return split_x
        return _median(picked)

    def analyze(self, *, min_confidence: float = LAYOUT_MIN_CONFIDENCE) -> dict[str, Any] | None:
        """Return the two-feed composition of the accumulated frames, or None."""
        if not self._means or self.width <= 0:
            return None
        candidates = []
        for mean, std in zip(self._means, self._stds):
            best = _frame_seam(mean, std, self.width)
            if best is not None:
                candidates.append(best)
        if not candidates:
            return None

        frames = self.frames
        if frames >= 3:
            strong = [c for c in candidates if c["score"] >= FRAME_SCORE_FLOOR]
            need = max(2, int(math.ceil(0.6 * frames)))
            if len(strong) < need:
                return None
            agree = _agreeing_seams(strong)
            if len(agree) < max(2, int(math.ceil(0.6 * len(strong)))):
                return None
            spatial = sum(c["score"] for c in agree) / len(agree)
            stability = len(agree) / float(len(strong))
            confidence = min(1.0, spatial * (0.75 + 0.25 * stability))
            split_x = _median([c["x"] for c in agree])
            evidence = _merge_evidence(agree)
        else:
            best = max(candidates, key=lambda c: c["score"])
            if best["score"] < STILL_SCORE_FLOOR:
                return None
            confidence = best["score"]
            split_x = float(best["x"])
            evidence = dict(best["evidence"])

        split_x = self._refine_dark_seam(split_x)
        left_motion, right_motion = self.side_motion(split_x)
        if frames >= 3 and spatial < 0.62 and min(left_motion, right_motion) < 0.2:
            # Weak, static evidence: a single dark object in a normal frame must
            # never be mistaken for a permanent two-feed call composition.
            return None
        if left_motion > 0.05 and right_motion > 0.05:
            confidence = min(1.0, confidence + 0.05)
        if confidence < float(min_confidence):
            return None
        return {
            "kind": "side_by_side",
            "split_x": round(float(split_x), 4),
            "confidence": round(float(confidence), 3),
            "frames": frames,
            "frames_agreed": len(agree) if frames >= 3 else 1,
            "left_motion": left_motion,
            "right_motion": right_motion,
            "evidence": evidence,
        }

    def timeline(self, split_x: float, *, ids: Sequence[str] = ("layout-left", "layout-right"),
                 duration: float | None = None) -> list[dict[str, Any]]:
        """Two-face timeline rows (one face per feed) with measured motion.

        The rows use the exact shape :mod:`chopster.clipper.split_screen`
        expects, so the reaction rule keeps working on a two-feed source even
        when no face was ever tracked.
        """
        x = _clamp(split_x, 0.05, 0.95)
        width = self.width
        seam = int(round(x * width))
        left_slice = slice(2, max(3, seam - 2))
        right_slice = slice(min(width - 2, seam + 3), width - 2)
        left_id = str(ids[0]) if len(ids) > 0 else "layout-left"
        right_id = str(ids[1]) if len(ids) > 1 else "layout-right"
        centres = ((x * 0.5, left_id), (x + (1.0 - x) * 0.5, right_id))
        span = float(duration) if duration else 0.0
        count = max(1, self.frames - 1)
        rows: list[dict[str, Any]] = []
        for idx in range(self.frames):
            diff = self._diffs[idx] if idx < len(self._diffs) else None
            motions = []
            for sl in (left_slice, right_slice):
                if diff is None or diff.size != width:
                    motions.append(0.0)
                else:
                    motions.append(min(12.0, float(diff[sl].mean()) * MOTION_SCALE))
            t = self._times[idx] if idx < len(self._times) else None
            if t is None:
                t = (span * idx / count) if span > 0 else 0.0
            faces = []
            for (cx, cid), motion in zip(centres, motions):
                c = _clamp(cx, 0.02, 0.98)
                faces.append({"id": cid, "x": round(c, 4), "y": 0.4,
                              "left": round(max(0.0, c - 0.02), 4),
                              "right": round(min(1.0, c + 0.02), 4),
                              "top": 0.0, "bottom": 1.0, "motion": round(motion, 3)})
            rows.append({"t": round(float(t), 3), "faces": faces, "source": "layout-probe"})
        return rows


def _median(values: Iterable[float]) -> float:
    clean = sorted(float(v) for v in values if v is not None)
    if not clean:
        return 0.5
    mid = len(clean) // 2
    if len(clean) % 2:
        return clean[mid]
    return (clean[mid - 1] + clean[mid]) / 2.0


def _agreeing_seams(strong: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    middle = _median([c["x"] for c in strong])
    return [c for c in strong if abs(float(c["x"]) - middle) <= SEAM_AGREEMENT]


def _merge_evidence(agreed: Sequence[dict[str, Any]]) -> dict[str, Any]:
    keys: set[str] = set()
    for item in agreed:
        ev = item.get("evidence") or {}
        if isinstance(ev, dict):
            keys.update(ev.keys())
    out: dict[str, Any] = {}
    for key in sorted(keys):
        vals = [float((c.get("evidence") or {}).get(key, 0.0) or 0.0) for c in agreed]
        out[key] = round(sum(vals) / max(1, len(vals)), 3)
    return out


def _frame_seam(mean: Any, std: Any, width: int) -> dict[str, Any] | None:
    """Best plausible feed seam of a single frame (normalised x + score)."""
    if mean is None or std is None:
        return None
    width = int(width)
    if width < 64 or len(mean) != width or len(std) != width:
        return None
    lo = max(8, int(round(width * SEAM_SEARCH[0])))
    hi = min(width - 9, int(round(width * SEAM_SEARCH[1])))
    if hi - lo < 8:
        return None
    best: dict[str, Any] | None = None
    for x in _seam_candidates(mean, std, width, lo, hi):
        candidate = _seam_at(x, mean, std, width)
        if candidate is not None and (best is None or candidate["score"] > float(best["score"])):
            best = candidate
    if best is None:
        return None
    centre = int(round(float(best["x"]) * width))
    for x in range(max(lo, centre - 3), min(hi, centre + 3) + 1):
        candidate = _seam_at(x, mean, std, width)
        if candidate is not None and candidate["score"] > float(best["score"]):
            best = candidate
    return best


def _seam_candidates(mean: Any, std: Any, width: int, lo: int, hi: int) -> list[int]:
    """Columns worth scoring: a feed seam is a dark and/or flat band.

    Scoring every column of the native-width profile is needlessly slow, so the
    darkest and flattest columns of the search band are picked first (a seam is
    one of the two by definition), which keeps the full evidence check but
    removes the per-column cost of the whole band.
    """
    try:
        import numpy as np
    except Exception:
        step = max(1, (hi - lo) // MAX_SEAM_CANDIDATES)
        return list(range(lo, hi + 1, step))
    window = 2 * max(2, width // 120) + 1
    kernel = np.ones(window, dtype="float32") / float(window)
    smooth_mean = np.convolve(np.asarray(mean, dtype="float32"), kernel, mode="same")
    smooth_std = np.convolve(np.asarray(std, dtype="float32"), kernel, mode="same")
    keep = max(8, MAX_SEAM_CANDIDATES // 2)
    band = slice(lo, hi + 1)
    picked: set[int] = set()
    for profile in (np.clip(smooth_mean - mean, 0.0, None), np.clip(smooth_std - std, 0.0, None)):
        section = profile[band]
        if getattr(section, "size", 0) == 0:
            continue
        top = np.argsort(section)[-keep:]
        picked.update(int(lo + int(i)) for i in top)
    if not picked:
        return list(range(lo, hi + 1))
    return sorted(picked)


def _seam_at(x: int, mean: Any, std: Any, width: int) -> dict[str, Any] | None:
    """Evidence for one candidate seam column (None when it is not plausible)."""
    left_slice = slice(2, max(3, x - 2))
    right_slice = slice(min(width - 2, x + 3), width - 2)
    if left_slice.stop - left_slice.start < 6 or right_slice.stop - right_slice.start < 6:
        return None
    left_std = float(std[left_slice].mean())
    right_std = float(std[right_slice].mean())
    side_contrast = min(left_std, right_std)
    if side_contrast < MIN_SIDE_CONTRAST:
        return None
    seam_slice = slice(max(0, x - 1), x + 2)
    seam_std = float(std[seam_slice].mean())
    seam_mean = float(mean[seam_slice].mean())
    left_mean = float(mean[left_slice].mean())
    right_mean = float(mean[right_slice].mean())
    flat_score = 1.0 - min(1.0, seam_std / max(1.0, side_contrast))
    border_left = 1.0 - min(1.0, float(std[max(0, x - 5):max(1, x - 1)].mean()) / max(1.0, left_std))
    border_right = 1.0 - min(1.0, float(std[min(width, x + 2):min(width, x + 6)].mean()) / max(1.0, right_std))
    border_flat = max(0.0, min(border_left, border_right))
    step = abs(float(mean[max(0, x - 8):max(1, x - 2)].mean())
               - float(mean[min(width - 1, x + 3):min(width, x + 9)].mean()))
    step_score = min(1.0, step / 36.0)
    bright_score = min(1.0, abs(left_mean - right_mean) / 32.0)
    dark_score = min(1.0, max(0.0, min(left_mean, right_mean) - seam_mean) / 24.0)
    centre_score = 1.0 - min(1.0, abs((x / float(width)) - 0.5) / 0.20)
    if flat_score < 0.35 and step_score < 0.25:
        return None
    if bright_score < 0.08 and step_score < 0.25 and dark_score < 0.25:
        return None
    if max(flat_score, dark_score, border_flat) < 0.45:
        # The seam itself has to look like a seam: a flat and/or dark band.
        # A scene edge inside one continuous image is not enough.
        return None
    score = (0.22 * flat_score + 0.20 * border_flat + 0.24 * step_score
             + 0.12 * bright_score + 0.10 * dark_score + 0.12 * centre_score)
    # A feed seam is a dark band, so snap to the darkest column of the
    # neighbourhood: the raw best column often sits on the band's edge.
    seam_x = x
    if dark_score >= 0.3:
        span = max(2, int(round(width * 0.03)))
        lo2, hi2 = max(0, x - span), min(width, x + span + 1)
        block = mean[lo2:hi2]
        if getattr(block, "size", 0):
            seam_x = lo2 + int(block.argmin())
    return {
        "x": seam_x / float(width),
        "score": score,
        "evidence": {
            "flat": round(flat_score, 3),
            "border": round(border_flat, 3),
            "step": round(step_score, 3),
            "brightness": round(bright_score, 3),
            "dark": round(dark_score, 3),
            "side_contrast": round(side_contrast, 2),
        },
    }


def layout_panels(split_x: float, *, subjects: Iterable[dict[str, Any]] | None = None,
                  ids: Sequence[str] = ("layout-left", "layout-right"),
                  inset: float = 0.006) -> list[dict[str, Any]]:
    """One panel per detected feed: the feed region plus the subject inside it.

    ``region`` is the feed itself (what the panel must show), ``box`` is the
    person found inside that feed and is only used to position the crop, so a
    small webcam face can never turn the panel into an extreme zoom.
    """
    x = _clamp(split_x, SEAM_SEARCH[0], SEAM_SEARCH[1])
    bounds = ((0.0, x), (x, 1.0))
    left_id = str(ids[0]) if len(ids) > 0 else "layout-left"
    right_id = str(ids[1]) if len(ids) > 1 else "layout-right"
    panels: list[dict[str, Any]] = []
    for idx, (bl, br) in enumerate(bounds):
        region = {"left": round(_clamp(bl), 4), "right": round(_clamp(br), 4),
                  "top": 0.0, "bottom": 1.0}
        box = _subject_box_in(bl, br, subjects, inset=inset)
        panels.append({
            "id": (left_id, right_id)[min(idx, 1)],
            "coverage": 1.0,
            "cx": round((region["left"] + region["right"]) / 2.0, 4),
            "cy": round((box["top"] + box["bottom"]) / 2.0, 4),
            "layout": "side_by_side",
            "region": region,
            "box": box,
        })
    return panels


def _subject_box_in(left: float, right: float, subjects: Iterable[dict[str, Any]] | None,
                    *, inset: float = 0.006) -> dict[str, float]:
    """Union of the subject boxes inside one feed, or the inset feed itself."""
    half = {"left": round(_clamp(left + inset), 4), "right": round(_clamp(right - inset), 4),
            "top": round(_clamp(inset), 4), "bottom": round(_clamp(1.0 - inset), 4)}
    union = None
    for sub in subjects or []:
        if not isinstance(sub, dict):
            continue
        try:
            sl = _clamp(sub.get("left", 0.5)); sr = _clamp(sub.get("right", 0.5))
            st = _clamp(sub.get("top", 0.5)); sb = _clamp(sub.get("bottom", 0.5))
        except (TypeError, ValueError):
            continue
        sl, sr = min(sl, sr), max(sl, sr)
        st, sb = min(st, sb), max(st, sb)
        if sr - sl <= 0.0 or sb - st <= 0.0:
            continue
        if not (left <= (sl + sr) / 2.0 <= right):
            continue
        if union is None:
            union = [sl, sr, st, sb]
        else:
            union = [min(union[0], sl), max(union[1], sr), min(union[2], st), max(union[3], sb)]
    if union is None:
        return half
    cx = (union[0] + union[1]) / 2.0
    cy = (union[2] + union[3]) / 2.0
    # A tracked face becomes a head-and-shoulders panel, never the whole frame.
    w = max(0.0, union[1] - union[0]) * 1.9 + 0.06
    h = max(0.0, union[3] - union[2]) * 2.1 + 0.10
    box = {
        "left": round(max(left, min(cx - w / 2.0, right)), 4),
        "right": round(min(right, max(cx + w / 2.0, left)), 4),
        "top": round(_clamp(cy - h / 2.0), 4),
        "bottom": round(_clamp(cy + h / 2.0), 4),
    }
    if box["right"] - box["left"] < 0.02 or box["bottom"] - box["top"] < 0.02:
        return half
    return box