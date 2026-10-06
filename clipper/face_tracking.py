"""Stable multi-person subject analysis for Chopster.

This module provides editorial evidence for the Camera Director. The camera is
locked to a selected subject; the detector is allowed to move internally, but the
rendered crop only changes at accepted shot transitions.
"""
from __future__ import annotations

from pathlib import Path
import math
import os
from typing import Any

from chopster.clipper.camera_director import stabilize_camera_path, ai_camera_director, ai_camera_director_visual, apply_ai_shots, _local_motion_shots
from chopster.clipper.adaptive_framing import decide_framing, decision_as_dict
from chopster.clipper.speaker_diarization import diarize_audio, speaker_at
from chopster.downloader.ffmpeg import find_ffmpeg


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def _norm_box(box, width: int, height: int):
    x, y, w, h = [float(v) for v in box]
    return (_clamp(x / max(1.0, width)), _clamp((x + w) / max(1.0, width)),
            _clamp(y / max(1.0, height)), _clamp((y + h) / max(1.0, height)))


def _box_area(box) -> float:
    return max(0.0, float(box[2])) * max(0.0, float(box[3]))


def _iou(a, b) -> float:
    ax, ay, aw, ah = map(float, a)
    bx, by, bw, bh = map(float, b)
    x1, y1 = max(ax, bx), max(ay, by)
    x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = max(1e-6, aw * ah + bw * bh - inter)
    return inter / union


def _center_distance(a, b, width: int, height: int) -> float:
    ax, ay, aw, ah = map(float, a)
    bx, by, bw, bh = map(float, b)
    ac = ((ax + aw * 0.5) / max(1, width), (ay + ah * 0.5) / max(1, height))
    bc = ((bx + bw * 0.5) / max(1, width), (by + bh * 0.5) / max(1, height))
    return math.hypot(ac[0] - bc[0], ac[1] - bc[1])


def _match_tracks(tracks: dict[int, dict[str, Any]], faces: list[tuple], width: int, height: int, next_id: int):
    remaining = set(range(len(faces)))
    candidates = []
    for tid, tr in tracks.items():
        if tr.get("miss", 0) > 8:
            continue
        for idx in remaining:
            face = faces[idx]
            iou = _iou(tr["face"], face)
            dist = _center_distance(tr["face"], face, width, height)
            if iou >= 0.04 or dist <= 0.18:
                # Higher IoU and shorter center distance wins.
                cost = -(iou * 2.5 - dist)
                candidates.append((cost, tid, idx))
    assignments: dict[int, int] = {}
    for _, tid, idx in sorted(candidates):
        if tid in assignments or idx not in remaining:
            continue
        assignments[tid] = idx
        remaining.remove(idx)
    for idx in sorted(remaining, key=lambda i: _box_area(faces[i]), reverse=True):
        tracks[next_id] = {"face": faces[idx], "subject": faces[idx], "motion_ema": 0.0,
                            "age": 1, "miss": 0, "last_t": 0.0}
        assignments[next_id] = idx
        next_id += 1
    return assignments, next_id


def _expand_face(face, width: int, height: int):
    x, y, w, h = [float(v) for v in face]
    # A conservative upper-body envelope keeps face + shoulders/chest inside the crop
    # without zooming so far out that a vertical short becomes unusable.
    return (
        max(0.0, x - 0.75 * w),
        max(0.0, y - 0.40 * h),
        min(float(width), x + 1.75 * w),
        min(float(height), y + 2.60 * h),
    )


def _union_box(a, b):
    ax1, ay1, ax2, ay2 = a; bx1, by1, bx2, by2 = b
    return min(ax1, bx1), min(ay1, by1), max(ax2, bx2), max(ay2, by2)


def _detect_subjects(frame, face_cascades, upperbody_cascade):
    import cv2
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)
    h, w = frame.shape[:2]
    faces: list[tuple] = []
    for cascade in face_cascades:
        if cascade.empty():
            continue
        try:
            rows = cascade.detectMultiScale(gray, scaleFactor=1.08, minNeighbors=5, minSize=(30, 30))
        except Exception:
            rows = []
        for r in rows:
            if not any(_iou(r, old) > 0.55 for old in faces):
                faces.append(tuple(map(int, r)))
    faces = sorted(faces, key=_box_area, reverse=True)[:12]

    bodies = []
    if upperbody_cascade is not None and not upperbody_cascade.empty():
        try:
            bodies = list(upperbody_cascade.detectMultiScale(gray, scaleFactor=1.05, minNeighbors=4, minSize=(50, 70)))
        except Exception:
            bodies = []

    out = []
    used_bodies: set[int] = set()
    for face in faces:
        expanded = _expand_face(face, w, h)
        fx, fy, fw, fh = [float(v) for v in face]
        fcx, fcy = fx + fw * .5, fy + fh * .55
        best_body = None; best_score = 0.0; best_idx = None
        for bi, body in enumerate(bodies):
            bx, by, bw, bh = [float(v) for v in body]
            inside = (bx <= fcx <= bx + bw) and (by <= fcy <= by + bh)
            inter = _iou((bx, by, bw, bh), face)
            score = 0.7 * (1.0 if inside else 0.0) + 0.3 * inter
            if score > best_score:
                best_score = score; best_body = body; best_idx = bi
        if best_body is not None and best_score >= .55:
            used_bodies.add(int(best_idx))
            bx, by, bw, bh = map(float, best_body)
            body_box = (max(0.0, bx), max(0.0, by), min(w, bx + bw), min(h, by + bh))
            expanded = _union_box(expanded, body_box)
        sx1, sy1, sx2, sy2 = expanded
        sx1 = max(0.0, min(w - 1.0, sx1)); sx2 = max(sx1 + 1.0, min(w, sx2))
        sy1 = max(0.0, min(h - 1.0, sy1)); sy2 = max(sy1 + 1.0, min(h, sy2))
        out.append({
            "face": face,
            "subject": (sx1, sy1, sx2 - sx1, sy2 - sy1),
            "face_center": ((fx + fw * .5) / w, (fy + fh * .46) / h),
            "kind": "face",
        })

    # Body-only fallback: when a face is occluded, side-facing, small, or briefly
    # missed, keep a person envelope rather than jumping to the next visible face.
    # The synthetic head box is intentionally conservative and is never given the
    # same speaking/mouth-motion weight as a real face detection.
    for bi, body in enumerate(bodies):
        if bi in used_bodies:
            continue
        bx, by, bw, bh = map(float, body)
        if bw < max(50.0, w * .06) or bh < max(70.0, h * .12):
            continue
        # Ignore a body box that overlaps an existing subject heavily.
        if any(_iou(body, d["subject"]) > .45 for d in out):
            continue
        fx = bx + bw * .26; fy = by + bh * .05
        fw = bw * .48; fh = bh * .30
        face = (int(fx), int(fy), max(24, int(fw)), max(24, int(fh)))
        subject = (max(0.0, bx), max(0.0, by), min(w, bx + bw) - max(0.0, bx), min(h, by + bh) - max(0.0, by))
        out.append({
            "face": face,
            "subject": subject,
            "face_center": ((bx + bw * .5) / w, (by + bh * .18) / h),
            "kind": "body",
        })
    return out[:12], gray


def _mouth_motion(gray, face, prev_gray):
    import cv2
    if prev_gray is None:
        return 0.0
    x, y, w, h = [int(v) for v in face]
    x0 = max(0, x); y0 = max(0, int(y + h * .48))
    x1 = min(gray.shape[1], x + w); y1 = min(gray.shape[0], int(y + h * .92))
    if x1 - x0 < 10 or y1 - y0 < 8:
        return 0.0
    a = gray[y0:y1, x0:x1]; b = prev_gray[y0:y1, x0:x1]
    if a.shape != b.shape or a.size == 0:
        return 0.0
    return float(cv2.absdiff(a, b).mean())


def _safe_group(subjects, width: int, height: int):
    boxes = [_norm_box(s["subject"], width, height) for s in subjects]
    if not boxes:
        return .5, .5, None, None, None, None
    left = min(b[0] for b in boxes); right = max(b[1] for b in boxes)
    top = min(b[2] for b in boxes); bottom = max(b[3] for b in boxes)
    return (left + right) * .5, (top + bottom) * .5, left, right, top, bottom


def _adaptive_sampling(duration: float, requested: float) -> float:
    req = max(.5, float(requested or .75))
    if duration <= 8 * 60:
        return req
    if duration <= 20 * 60:
        return max(req, 1.2)
    if duration <= 40 * 60:
        return max(req, 1.5)
    if duration <= 60 * 60:
        return max(req, 1.8)
    return max(req, 2.2)


def _provider_from_config(config):
    """Return the central orchestrator, never a raw provider."""
    if not config:
        return None
    try:
        from chopster.ai.orchestrator import orchestrator_from_config
        return orchestrator_from_config(config)
    except Exception:
        return None


def detect_face_focus(path, start=0.0, end=None, sample_seconds=.75, max_samples=None,
                      mode="Smart", transcript=None, ai_config=None, evidence_out=None, visual_context=None, progress_cb=None):
    try:
        import cv2
    except Exception as exc:
        raise RuntimeError("OpenCV belum terpasang; Smart Reframe tidak dapat dijalankan.") from exc
    from chopster.clipper.advanced_features import FocusPoint
    from chopster.clipper.media_probe import probe

    ai_config = dict(ai_config or {})
    meta = probe(path)
    target_aspect = float(ai_config.get("target_aspect") or (9/16))
    if target_aspect <= 0:
        target_aspect = 9/16
    end = meta.duration if end is None else min(float(end), meta.duration)
    start = max(0.0, float(start))
    if end <= start:
        return []

    duration = end - start
    sample_step = _adaptive_sampling(duration, sample_seconds)
    target_samples = max(1, int(math.ceil(duration / sample_step)) + 1)
    if max_samples is not None:
        target_samples = min(target_samples, max(1, int(max_samples)))
    else:
        target_samples = min(target_samples, 1800)
    sample_step = duration / max(1, target_samples - 1) if target_samples > 1 else duration

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError("Video tidak dapat dibuka untuk person tracking")

    # Decode sequentially instead of performing one random seek per sample.
    # Random CAP_PROP_POS_MSEC seeks are especially expensive on long H.264/H.265
    # podcasts and were a major source of the "1 hour takes forever" behavior.
    fps = max(1.0, float(getattr(meta, "fps", 30.0) or 30.0))
    start_frame = max(0, int(round(start * fps)))
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
    next_frame = start_frame
    current_frame = start_frame

    cascade_dir = Path(cv2.data.haarcascades)
    face_cascades = [cv2.CascadeClassifier(str(cascade_dir / "haarcascade_frontalface_alt2.xml")),
                     cv2.CascadeClassifier(str(cascade_dir / "haarcascade_profileface.xml")),
                     cv2.CascadeClassifier(str(cascade_dir / "haarcascade_frontalface_default.xml"))]
    upper = cv2.CascadeClassifier(str(cascade_dir / "haarcascade_upperbody.xml"))
    if all(c.empty() for c in face_cascades):
        cap.release(); raise RuntimeError("OpenCV face detector tidak dapat dimuat")

    # Pyannote is expensive and must never silently run unless explicitly available/configured.
    diar = []
    use_diar = bool(ai_config.get("speaker_diarization_enabled", False))
    hf_token = str(ai_config.get("hf_token") or os.environ.get("HUGGINGFACE_TOKEN") or os.environ.get("HF_TOKEN") or "").strip()
    if use_diar and hf_token:
        try:
            diar = diarize_audio(path, start, end, max_speakers=8, hf_token=hf_token)
        except Exception:
            diar = []

    tracks: dict[int, dict[str, Any]] = {}
    next_id = 1
    active_id: int | None = None
    active_since = start
    last_valid = None
    prev_gray = None
    speaker_track: dict[str, dict[int, float]] = {}
    raw: list[FocusPoint] = []
    timeline: list[dict[str, Any]] = []

    try:
        for idx in range(target_samples):
            if max_samples is not None and idx >= max_samples:
                break
            if progress_cb and (idx == 0 or idx % max(1, target_samples // 24) == 0):
                pct = int((idx / max(1, target_samples)) * 100)
                progress_cb(pct, f"Person/Face tracking — sample {idx+1}/{target_samples}")
            t = min(end, start + idx * sample_step)
            desired_frame = start_frame + int(round((t - start) * fps))
            while current_frame < desired_frame:
                if not cap.grab():
                    break
                current_frame += 1
            ok, frame = cap.retrieve()
            if not ok:
                if last_valid is not None:
                    hold = dict(last_valid); hold["t"] = t - start; hold["confidence"] = max(.32, float(hold.get("confidence", .5)) - .01)
                    raw.append(FocusPoint(**{k: hold[k] for k in ("t","x","y","source","confidence","subject_left","subject_right","subject_top","subject_bottom","group_left","group_right","group_top","group_bottom","target_id","shot","reason") if k in hold}))
                continue

            current_frame = max(current_frame, desired_frame)
            oh, ow = frame.shape[:2]
            long_form_width = 768.0 if duration >= 8 * 60 else 960.0
            if duration >= 20 * 60:
                long_form_width = 700.0
            scale = min(1.0, long_form_width / max(1.0, float(ow)))
            if scale < .999:
                frame = cv2.resize(frame, (max(1, int(ow * scale)), max(1, int(oh * scale))))
            detections, gray = _detect_subjects(frame, face_cascades, upper)
            faces = [tuple(d["face"]) for d in detections]
            dw, dh = frame.shape[1], frame.shape[0]
            assignments, next_id = _match_tracks(tracks, faces, dw, dh, next_id)

            for tid, trk in tracks.items():
                if tid in assignments:
                    det = detections[assignments[tid]]
                    face = det["face"]
                    trk["face"] = face; trk["subject"] = det["subject"]; trk["miss"] = 0
                    trk["age"] = int(trk.get("age", 1)) + 1; trk["last_t"] = t
                else:
                    trk["miss"] = int(trk.get("miss", 0)) + 1

            candidates = []
            for tid, det_idx in assignments.items():
                if tracks[tid].get("miss", 0) > 0:
                    continue
                det = detections[det_idx]
                face = det["face"]; subject = det["subject"]
                kind = det.get("kind", "face")
                motion = _mouth_motion(gray, face, prev_gray) if kind == "face" else 0.0
                trk = tracks[tid]
                trk["motion_ema"] = .74 * float(trk.get("motion_ema", 0.0)) + .26 * motion
                cx, cy = det["face_center"]
                sl, sr, st, sb = _norm_box(subject, dw, dh)
                fl, fr, ft, fb = _norm_box(face, dw, dh)
                face_area = _box_area(face) / max(1.0, dw * dh)
                base_kind = 0.0 if kind == "face" else -0.12
                score = base_kind + trk["motion_ema"] + min(.32, face_area * 8.0) + min(.18, int(trk.get("age",1)) * .008)
                candidates.append({"id": tid, "face": face, "subject": subject, "x": cx, "y": cy,
                                   "motion": motion, "motion_ema": trk["motion_ema"], "score": score, "kind": kind,
                                   "sl": sl, "sr": sr, "st": st, "sb": sb, "fl": fl, "fr": fr, "ft": ft, "fb": fb})

            visible = sorted(candidates, key=lambda c: c["score"], reverse=True)
            active_speaker = speaker_at(diar, t)
            mapped_tid = None
            if active_speaker and visible:
                speaker_track.setdefault(active_speaker, {})
                for c in visible:
                    speaker_track[active_speaker][c["id"]] = speaker_track[active_speaker].get(c["id"], 0.0) + max(0.0, c["motion_ema"])
                pairs = sorted(speaker_track[active_speaker].items(), key=lambda z: z[1], reverse=True)
                if pairs and (len(pairs) == 1 or pairs[0][1] >= max(.01, pairs[1][1]) * 1.22):
                    mapped_tid = pairs[0][0]

            if mode in ("Smart", "Speaker Focus", "AI Camera Director") and visible:
                best = visible[0]
                if len(visible) > 1:
                    face_visible = next((c for c in visible if c.get("kind") == "face"), None)
                    if face_visible is not None and face_visible["score"] >= best["score"] - 0.08:
                        best = face_visible
                if mapped_tid is not None:
                    mapped = next((c for c in visible if c["id"] == mapped_tid), None)
                    if mapped is not None:
                        best = mapped

                current = next((c for c in visible if c["id"] == active_id), None)
                # Strong lock: keep target until there is sustained evidence for a real turn.
                if active_id is None:
                    active_id = best["id"]; active_since = t; current = best
                if current is None:
                    if last_valid is not None and (t - active_since) < 3.0:
                        current = None
                    else:
                        active_id = best["id"]; active_since = t; current = best
                if current is not None and best["id"] != active_id:
                    second_motion = visible[1]["motion_ema"] if len(visible) > 1 else 0.0
                    decisive = best["motion_ema"] >= max(1.15, second_motion * 1.18) and (t - active_since) >= 2.0
                    if decisive:
                        active_id = best["id"]; active_since = t; current = best

                if current is None and last_valid is not None:
                    q = dict(last_valid); q["t"] = t - start; q["source"] = "locked-hold"; q["confidence"] = max(.30, float(q.get("confidence", .55)) - .015)
                    raw.append(FocusPoint(**{k: q.get(k) for k in FocusPoint.__dataclass_fields__.keys()}))
                    prev_gray = gray
                    continue

                if current is None:
                    current = best

                if len(visible) >= 2:
                    group = visible[:min(4, len(visible))]
                    cxg, cyg, gl, gr, gt, gb = _safe_group(group, dw, dh)
                    target_ratio = target_aspect
                    source_ratio = float(meta.width or 16) / max(1.0, float(meta.height or 9))
                    crop_fraction_x = min(1.0, target_ratio / max(.01, source_ratio))
                    wide = (gr-gl) > crop_fraction_x * .90
                else:
                    cxg = cyg = gl = gr = gt = gb = None; wide = False

                shot = "fit" if wide else "solo"
                source = "speaker-diarized" if active_speaker and mapped_tid is not None else ("speaker-mouth" if current["motion_ema"] >= 1.15 else "speaker-locked")
                if shot == "fit":
                    p = FocusPoint(t=t-start, x=cxg, y=cyg, source="group-fit", confidence=.78,
                                   group_left=gl, group_right=gr, group_top=gt, group_bottom=gb,
                                   target_id="group-" + ",".join(str(c["id"]) for c in visible[:min(4,len(visible))]), shot="fit",
                                   reason="group/context safe fit")
                else:
                    p = FocusPoint(t=t-start, x=current["x"], y=current["y"], source=source,
                                   confidence=min(1.0, .58 + min(.42, current["score"] / 3.0)),
                                   subject_left=current["sl"], subject_right=current["sr"],
                                   subject_top=current["st"], subject_bottom=current["sb"],
                                   target_id=str(current["id"]), shot="solo")
                    fd = decide_framing(p, source_aspect=float(meta.width or 16) / max(1.0, float(meta.height or 9)), target_aspect=target_aspect)
                    if fd.mode == "adaptive_fit":
                        p = FocusPoint(t=p.t, x=fd.center_x, y=fd.center_y, source=p.source, confidence=p.confidence,
                                       subject_left=p.subject_left, subject_right=p.subject_right, subject_top=p.subject_top, subject_bottom=p.subject_bottom,
                                       group_left=p.group_left, group_right=p.group_right, group_top=p.group_top, group_bottom=p.group_bottom,
                                       target_id=p.target_id, shot="fit", reason=fd.reason)
                raw.append(p)
                last_valid = p.__dict__.copy()

                timeline.append({
                    "t": round(t-start,2), "target_id": p.target_id, "shot": p.shot,
                    "active_speaker": active_speaker,
                    "faces": [{"id": c["id"], "x": round(c["x"],3), "y": round(c["y"],3),
                               "motion": round(c["motion_ema"],2), "left": round(c["sl"],3), "right": round(c["sr"],3),
                               "top": round(c["st"],3), "bottom": round(c["sb"],3)} for c in visible[:8]],
                })

            elif mode == "Two Person" and len(visible) >= 1:
                group = visible[:2]
                cx, cy, gl, gr, gt, gb = _safe_group(group, dw, dh)
                p = FocusPoint(t=t-start, x=cx, y=cy, source="two-face-safe", confidence=.9,
                               group_left=gl, group_right=gr, group_top=gt, group_bottom=gb,
                               target_id="two", shot="two")
                raw.append(p); last_valid=p.__dict__.copy()

            elif visible:
                best = visible[0]
                p = FocusPoint(t=t-start, x=best["x"], y=best["y"], source="face", confidence=.65,
                               subject_left=best["sl"], subject_right=best["sr"], subject_top=best["st"], subject_bottom=best["sb"],
                               target_id=str(best["id"]), shot="solo")
                raw.append(p); last_valid=p.__dict__.copy()
            elif last_valid is not None:
                q = dict(last_valid); q["t"] = t-start; q["source"] = "locked-hold"; q["confidence"] = max(.30, float(q.get("confidence",.55)) - .015)
                raw.append(FocusPoint(**{k: q.get(k) for k in FocusPoint.__dataclass_fields__.keys()}))
            prev_gray = gray
    finally:
        cap.release()

    if not raw:
        raise RuntimeError("Tidak ada wajah yang terdeteksi sepanjang timeline")

    raw_dict = [p.__dict__.copy() for p in raw]
    ai_shots = []
    if mode not in ("Manual", "Center") and bool(ai_config.get("ai_camera_director", False)) and timeline:
        orchestrator = _provider_from_config(ai_config)
        if orchestrator is not None:
            # Visual Camera Director: Gemini sees representative frames plus a
            # deterministic map of local subject IDs/positions. The AI chooses the
            # editorial target; local tracking supplies the actual coordinates.
            try:
                from chopster.clipper.visual_analyzer import select_visual_timestamps, extract_frame_images
                from chopster.clipper.camera_director import ai_camera_director_visual
                timestamps = select_visual_timestamps(path, camera_timeline=timeline, max_frames=12, duration=duration)
                root = None; imgs = []
                try:
                    root, imgs = extract_frame_images(path, timestamps, max_width=640)
                    ai_shots = ai_camera_director_visual(
                        orchestrator, timeline,
                        " ".join(getattr(s, "text", "") for s in getattr(transcript, "segments", []) or []),
                        [str(x) for x in imgs], timestamps, visual_context=visual_context or {}, max_tokens=7000,
                    )
                finally:
                    if root:
                        import shutil
                        shutil.rmtree(root, ignore_errors=True)
            except Exception:
                ai_shots = []
            # If visual AI did not produce a plan, use the central textual director
            # over the same timeline as a secondary fallback, still without raw coordinates.
            if not ai_shots:
                ai_shots = ai_camera_director(orchestrator, timeline, " ".join(getattr(s, "text", "") for s in getattr(transcript, "segments", []) or []))
            if not ai_shots:
                ai_shots = _local_motion_shots(timeline)
            # AI/local target changes alter local x/y and subject boxes; stabilization
            # then generates a bounded move and locks the new shot.
            raw_dict = apply_ai_shots(raw_dict, ai_shots, timeline=timeline, source_aspect=float(meta.width or 16) / max(1.0, float(meta.height or 9)), target_aspect=target_aspect)

    if evidence_out is not None:
        evidence_out.clear()
        evidence_out.update({
            "timeline": timeline,
            "ai_shots": ai_shots,
            "target_aspect": target_aspect,
            "source_aspect": float(meta.width or 16) / max(1.0, float(meta.height or 9)),
            "sample_step": sample_step,
            "target_samples": target_samples,
            "adaptive_framing": any(decide_framing(p.__dict__, source_aspect=float(meta.width or 16) / max(1.0, float(meta.height or 9)), target_aspect=target_aspect).mode == "adaptive_fit" for p in raw),
        })

    if progress_cb:
        progress_cb(100, "Person/Face tracking — subject map siap")
    stable = stabilize_camera_path(raw_dict, min_shot_duration=2.0, transition_duration=.28,
                                   max_speed=.48, max_step=.10)
    out: list[FocusPoint] = []
    for p in stable:
        out.append(FocusPoint(
            t=float(p["t"]), x=float(p["x"]), y=float(p["y"]), source=str(p.get("source","center")),
            confidence=float(p.get("confidence",.5)), subject_left=p.get("subject_left"), subject_right=p.get("subject_right"),
            subject_top=p.get("subject_top"), subject_bottom=p.get("subject_bottom"), group_left=p.get("group_left"),
            group_right=p.get("group_right"), group_top=p.get("group_top"), group_bottom=p.get("group_bottom"),
            target_id=p.get("target_id"), shot=p.get("shot","solo"), reason=p.get("reason","")
        ))
    return out
