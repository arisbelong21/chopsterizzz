"""Aspect-aware adaptive framing for close subjects.

The compositor should not simply crop a face that is already very close to the
source camera.  When the subject would occupy an uncomfortable amount of the
chosen canvas, we switch to a context-fit composition: a wider contextual
window is placed over a softly blurred full-frame background.  The decision is
made per target aspect ratio, so changing 9:16 -> 4:5 -> 1:1 recalculates it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class FramingDecision:
    mode: str = "crop"  # crop | adaptive_fit
    reason: str = ""
    center_x: float = 0.5
    center_y: float = 0.5
    context_left: float | None = None
    context_right: float | None = None
    context_top: float | None = None
    context_bottom: float | None = None
    subject_occupancy_x: float = 0.0
    subject_occupancy_y: float = 0.0


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def _box_from_point(p: dict[str, Any], group: bool = False) -> tuple[float, float, float, float] | None:
    if group:
        keys = ("group_left", "group_right", "group_top", "group_bottom")
    else:
        keys = ("subject_left", "subject_right", "subject_top", "subject_bottom")
    vals = [p.get(k) for k in keys]
    if any(v is None for v in vals):
        return None
    l, r, t, b = (_clamp(float(v)) for v in vals)
    return l, r, t, b


def _crop_fractions(source_aspect: float, target_aspect: float) -> tuple[float, float]:
    if target_aspect <= 0 or source_aspect <= 0:
        return 1.0, 1.0
    fx = min(1.0, target_aspect / source_aspect)
    fy = min(1.0, source_aspect / target_aspect)
    return fx, fy


def _context_box(center_x: float, center_y: float, width_norm: float, height_norm: float) -> tuple[float, float, float, float]:
    width_norm = _clamp(width_norm)
    height_norm = _clamp(height_norm)
    left = _clamp(center_x - width_norm * 0.5)
    top = _clamp(center_y - height_norm * 0.5)
    # Keep the full requested box on-canvas where possible.
    left = min(left, 1.0 - width_norm)
    top = min(top, 1.0 - height_norm)
    return left, left + width_norm, top, top + height_norm


def decide_framing(
    point: dict[str, Any] | object,
    *,
    source_aspect: float,
    target_aspect: float,
    desired_subject_width: float = 0.50,
    desired_subject_height: float = 0.62,
    max_crop_occupancy: float = 0.72,
    max_group_occupancy: float = 0.88,
) -> FramingDecision:
    """Return a deterministic composition decision for one camera point.

    Close-up safety is evaluated against the crop window of *the active aspect
    ratio*, not against a hard-coded portrait value.  When a subject is too
    large to leave comfortable visual breathing room, adaptive_fit is selected.
    """
    if hasattr(point, "__dict__"):
        p = dict(point.__dict__)
        cx = float(getattr(point, "x", 0.5)); cy = float(getattr(point, "y", 0.5))
    else:
        p = dict(point or {})
        cx = float(p.get("x", 0.5)); cy = float(p.get("y", 0.5))
    cx, cy = _clamp(cx), _clamp(cy)

    crop_w, crop_h = _crop_fractions(source_aspect, target_aspect)
    subject = _box_from_point(p, group=False)
    group = _box_from_point(p, group=True)
    active_box = group if str(p.get("shot", "")) in {"group", "two"} and group else subject
    if active_box is None:
        return FramingDecision(center_x=cx, center_y=cy)

    l, r, t, b = active_box
    bw, bh = max(0.0, r - l), max(0.0, b - t)
    occ_x = bw / max(0.001, crop_w)
    occ_y = bh / max(0.001, crop_h)

    # For a single close face/body, use a softer threshold. For a group, avoid
    # cutting people even before the crop becomes mathematically impossible.
    too_close = occ_x > max_crop_occupancy or occ_y > max_crop_occupancy
    too_wide = group is not None and (bw > crop_w * max_group_occupancy or bh > crop_h * max_group_occupancy)
    force_fit = str(p.get("shot", "")) == "fit"

    if not (too_close or too_wide or force_fit):
        return FramingDecision(
            mode="crop", reason="subject-safe crop", center_x=cx, center_y=cy,
            subject_occupancy_x=occ_x, subject_occupancy_y=occ_y,
        )

    # A square-ish / landscape context is much more comfortable for an extreme
    # close-up inside a vertical canvas.  For landscape outputs, stay closer to
    # the output/source aspect to avoid unnecessary letterboxing.
    if target_aspect < 1.0:
        context_aspect = max(target_aspect, min(1.0, source_aspect))
    else:
        context_aspect = min(source_aspect, max(target_aspect, 1.0))
    context_aspect = max(0.25, min(2.5, context_aspect))

    # Compute the smallest useful context window that reduces the subject to a
    # comfortable on-canvas occupancy. If the source cannot provide that much
    # context, use the entire source and let the blurred background preserve the
    # portrait/landscape canvas.
    width_needed = bw / max(0.10, desired_subject_width)
    height_needed = bh / max(0.10, desired_subject_height)
    width_from_height = height_needed * context_aspect / max(0.001, source_aspect)
    context_w = min(1.0, max(width_needed, width_from_height, crop_w))
    context_h = min(1.0, context_w * source_aspect / context_aspect)
    if context_h < height_needed:
        context_h = min(1.0, max(context_h, height_needed))
        context_w = min(1.0, max(context_w, context_h * context_aspect / max(0.001, source_aspect)))

    center_x = (l + r) * 0.5
    center_y = (t + b) * 0.5
    cl, cr, ct, cb = _context_box(center_x, center_y, context_w, context_h)
    return FramingDecision(
        mode="adaptive_fit",
        reason="close-subject breathing room" if not group else "group/context safe fit",
        center_x=center_x,
        center_y=center_y,
        context_left=cl, context_right=cr, context_top=ct, context_bottom=cb,
        subject_occupancy_x=occ_x, subject_occupancy_y=occ_y,
    )


def decision_as_dict(decision: FramingDecision) -> dict[str, Any]:
    return {
        "mode": decision.mode,
        "reason": decision.reason,
        "center_x": round(decision.center_x, 6),
        "center_y": round(decision.center_y, 6),
        "context_left": None if decision.context_left is None else round(decision.context_left, 6),
        "context_right": None if decision.context_right is None else round(decision.context_right, 6),
        "context_top": None if decision.context_top is None else round(decision.context_top, 6),
        "context_bottom": None if decision.context_bottom is None else round(decision.context_bottom, 6),
        "subject_occupancy_x": round(decision.subject_occupancy_x, 4),
        "subject_occupancy_y": round(decision.subject_occupancy_y, 4),
    }


def any_adaptive(points: Iterable[dict[str, Any]], *, source_aspect: float, target_aspect: float) -> bool:
    return any(decide_framing(p, source_aspect=source_aspect, target_aspect=target_aspect).mode == "adaptive_fit" for p in points)
