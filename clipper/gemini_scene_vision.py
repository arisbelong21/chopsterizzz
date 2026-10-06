"""Gemini-only scene/frame visual understanding for Chopster.

FFmpeg remains the deterministic cut detector. Gemini is responsible for the
visual understanding of representative frames for each detected scene: subject
count, active/relevant person, shot type, framing safety, and a preferred frame
for review. This keeps scene segmentation reliable while giving the editor a
true Gemini vision layer.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from chopster.clipper.visual_analyzer import extract_frame_images
from chopster.ai.provider_interface import AIResponse


def _local(rows: list[dict[str, Any]], target_aspect: float, error: str = "") -> dict[str, Any]:
    return {
        "source": "local-fallback",
        "target_aspect": float(target_aspect),
        "scenes": rows,
        "error": error,
        "algorithm_version": "8.5.0",
    }


def analyze_scene_visuals(
    orchestrator,
    source_path: str | Path,
    segments: list[tuple[float, float]],
    *,
    target_aspect: float = 9 / 16,
    max_scenes: int = 24,
    timeout: int = 45,
    camera_timeline: list[dict] | None = None,
) -> dict[str, Any]:
    """Ask the embedded Gemini Vision layer to understand representative scene frames."""
    chosen = list(segments or [])[: max(1, int(max_scenes))]
    timestamps = [round((float(s) + float(e)) * 0.5, 3) for s, e in chosen]
    base_rows = [
        {"scene": i + 1, "start": float(s), "end": float(e), "timestamp": timestamps[i]}
        for i, (s, e) in enumerate(chosen)
    ]
    if not base_rows:
        return _local([], target_aspect)

    root = None
    frames: list[Path] = []
    try:
        root, frames = extract_frame_images(str(source_path), timestamps, max_width=640)
        if not frames:
            return _local(base_rows, target_aspect, "frame extraction kosong")

        labels = "\n".join(
            f"Frame {i + 1} = scene {i + 1}, timestamp {timestamps[i]:.3f}s"
            for i in range(len(frames))
        )
        frame_maps=[]
        for i,t in enumerate(timestamps):
            row=min(camera_timeline,key=lambda r: abs(float(r.get("t",0.0))-t)) if camera_timeline else {}
            frame_maps.append({
                "frame":i+1,"time":t,
                "candidate_subjects":[
                    {"id":str(f.get("id")),"x":round(float(f.get("x",.5)),3),"y":round(float(f.get("y",.5)),3),
                     "left":round(float(f.get("left",0)),3),"right":round(float(f.get("right",1)),3),
                     "top":round(float(f.get("top",0)),3),"bottom":round(float(f.get("bottom",1)),3),
                     "motion":round(float(f.get("motion",0)),2)}
                    for f in (row.get("faces") or [])
                ]
            })
        prompt = (
            "Analisis frame representative scene berikut sebagai Gemini Visual Director. Jangan membuat timestamp baru. "
            "Untuk setiap frame, tentukan subject_count, active_subject_id memakai ID lokal dari FRAME SUBJECT MAP, "
            "shot_type (solo/two/group/wide/close), framing_safety, apakah wajah terlalu close, dan preferred_frame. "
            "Kembalikan JSON object dengan field scenes[]. Setiap item wajib memuat scene, subject_count, "
            "active_subject_id, shot_type, framing, risk, preferred_frame, confidence. Jangan membuat ID baru.\n\n"
            + labels
            + "\nFRAME SUBJECT MAP:\n" + json.dumps(frame_maps, ensure_ascii=False)
            + f"\nTARGET_ASPECT={float(target_aspect):.6f}"
        )
        fallback = AIResponse(
            text=json.dumps(_local(base_rows, target_aspect), ensure_ascii=False),
            raw={"provider": "local-fallback", "fallback": True},
        )
        resp = orchestrator.generate_vision(
            prompt,
            [str(p) for p in frames],
            system="Gunakan visual evidence frame secara konservatif. Jangan mengarang orang/timestamp yang tidak terlihat.",
            model="",
            temperature=0.1,
            max_tokens=5000,
            timeout=timeout,
            local_fallback=lambda: fallback,
        )
        raw = (resp.text or "").strip()
        if "```" in raw:
            import re
            m = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.S | re.I)
            if m:
                raw = m.group(1).strip()
        data = json.loads(raw)
        rows = data.get("scenes") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise ValueError("Gemini scene vision tidak mengembalikan scenes[]")
        meta = resp.raw if isinstance(resp.raw, dict) else {}
        return {
            "source": meta.get("provider") or "embedded-gemini-vision",
            "ai_layer": meta.get("ai_layer") or "embedded-gemini",
            "model": meta.get("gemini_model") or meta.get("_chopster_model") or "auto",
            "target_aspect": float(target_aspect),
            "scenes": rows,
            "frame_timestamps": timestamps,
            "frame_maps": frame_maps,
            "attempts": meta.get("gemini_attempts") or [],
            "algorithm_version": "8.5.0",
        }
    except Exception as exc:
        return _local(base_rows, target_aspect, str(exc)[:500])
    finally:
        if root:
            import shutil
            shutil.rmtree(root, ignore_errors=True)
