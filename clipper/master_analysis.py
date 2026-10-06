"""Unified Master Analysis pipeline for Chopster.

The master analysis is the single evidence bus for downstream intelligent tools.
Transcript, scenes, person/camera analysis and one central AI editorial review are
cached separately but synchronized into one manifest. All remote AI calls pass
through AIOrchestrator and have deterministic local fallbacks.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable

from chopster.clipper.analysis_cache import load_artifact, save_artifact, status as cache_status
from chopster.clipper.media_probe import probe
from chopster.clipper.scene_detector import detect_scenes
from chopster.clipper.gemini_scene_vision import analyze_scene_visuals
from chopster.clipper.transcript_manager import Transcript, load_cached_transcript, transcribe, save_cached_transcript
from chopster.clipper.subtitle_ai import prepare_transcript_and_subtitles, transcript_prepare_signature
from chopster.clipper.face_tracking import detect_face_focus
from chopster.ai.provider_interface import AIRequest, AIResponse
from chopster.clipper.visual_analyzer import extract_frame_images, select_visual_timestamps

CAMERA_ALGORITHM_VERSION = "8.5.0"
VISION_ALGORITHM_VERSION = "8.5.0"


def _ai_signature(config: dict[str, Any]) -> str:
    raw = "|".join([
        str(config.get("ai_provider") or "none"),
        str(config.get("ai_endpoint") or ""),
        str(config.get("ai_model") or ""),
        str(config.get("ai_vision_model") or ""),
        str(config.get("ai_vision_preference") or ""),
        str(config.get("ai_local_vision", True)),
        str(config.get("ai_auto_visual", True)),
        str(config.get("target_aspect") or 9/16),
        str(config.get("ai_camera_director", True)),
        CAMERA_ALGORITHM_VERSION,
        VISION_ALGORITHM_VERSION,
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _local_master_advice(transcript: Transcript, scenes: list, camera: list, duration: float, target_aspect: float) -> dict[str, Any]:
    text = " ".join(s.text for s in transcript.segments[:400]).strip()
    words = [w.strip(".,!?;:()[]{}\"").lower() for w in text.split()]
    freq: dict[str, int] = {}
    for w in words:
        if len(w) >= 5 and w.isalpha():
            freq[w] = freq.get(w, 0) + 1
    topics = [w for w, _ in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[:8]]
    camera_targets = [str(x.get("target_id")) for x in camera if isinstance(x, dict) and x.get("target_id")]
    return {
        "summary": f"Master lokal: {len(transcript.segments)} segmen transcript, {len(scenes)} scene, {len(camera)} titik kamera.",
        "top_topics": topics,
        "viral_signals": ["question", "contrast", "specific claim", "reaction"],
        "editing_rules": ["Jaga konteks setup → payoff", "Hindari jump cut di tengah kalimat", "Pertahankan reaction penting"],
        "camera_rules": ["LOCK target selama shot", "Jangan mengikuti gerakan kepala kecil", "Gunakan fit untuk group yang terlalu lebar"],
        "recommended_aspect": f"{target_aspect:.4f}",
        "focus_targets": list(dict.fromkeys(camera_targets))[:8],
        "confidence": 0.55,
        "source": "local",
    }


def _build_evidence(transcript: Transcript, scenes: list, camera: list, duration: float, target_aspect: float) -> dict[str, Any]:
    rows = list(transcript.segments or [])
    if len(rows) > 160:
        chosen=[]
        for i in range(160):
            idx=round(i*(len(rows)-1)/max(1,159))
            if not chosen or idx != chosen[-1]:
                chosen.append(idx)
        rows=[rows[i] for i in chosen]
    transcript_sample=[{"start":round(float(s.start),2),"end":round(float(s.end),2),"text":str(s.text)} for s in rows]
    scene_sample=[]
    for sc in (scenes or [])[:240]:
        try:
            if isinstance(sc, (list,tuple)) and len(sc)>=2:
                scene_sample.append({"start":float(sc[0]),"end":float(sc[1])})
            elif isinstance(sc, dict):
                scene_sample.append({"start":float(sc.get("start",0)),"end":float(sc.get("end",0)),"score":sc.get("score")})
        except Exception:
            continue
    camera_sample=[]
    prev=None
    for row in camera or []:
        key=(str(row.get("target_id")),str(row.get("shot")))
        if key != prev:
            camera_sample.append({k:row.get(k) for k in ("t","target_id","shot","active_speaker","reason") if k in row})
            prev=key
    return {"duration":duration,"target_aspect":target_aspect,"transcript":transcript_sample,"scenes":scene_sample,"camera_changes":camera_sample[:180]}


def _ask_master_advisor(config: dict[str, Any], evidence: dict[str, Any], transcript: Transcript, scenes: list, camera: list) -> dict[str, Any]:
    from chopster.ai.orchestrator import orchestrator_from_config
    orch = orchestrator_from_config(config)
    prompt = (
        "Kamu adalah Master Editor Brain untuk Chopster. Kamu menerima evidence lokal dari seluruh video. "
        "Buat rekomendasi editorial yang bisa dipakai mesin lain. Jangan membuat timestamp di luar evidence. "
        "Fokus pada hook, setup-payoff, reaction, context, dan keputusan kamera yang konservatif. "
        "Kembalikan JSON object saja dengan field: summary, top_topics(array), viral_signals(array), "
        "editing_rules(array), camera_rules(array), recommended_aspect, focus_targets(array), confidence, notes(array).\n\n" +
        json.dumps(evidence, ensure_ascii=False)
    )
    tr_fallback = _local_master_advice(transcript, scenes, camera, float(evidence.get("duration",0)), float(evidence.get("target_aspect",9/16)))
    req = AIRequest(
        prompt=prompt,
        system="Kamu adalah editor AI yang konservatif. Jangan mengarang data yang tidak ada.",
        model=config.get("ai_model") or "",
        temperature=float(config.get("ai_temperature") or .15),
        max_tokens=max(5000, int(config.get("ai_max_tokens") or 8000)),
        timeout=int(config.get("ai_timeout") or 90),
    )
    data, resp = orch.generate_json(req, local_fallback=lambda: tr_fallback)
    if not isinstance(data, dict):
        data = tr_fallback
    data.setdefault("source", (resp.raw or {}).get("provider") if isinstance(resp.raw, dict) else "remote")
    return data



def _local_visual_review(timestamps: list[float], camera_timeline: list[dict], target_aspect: float) -> dict[str, Any]:
    transitions=[]
    prev=None
    for row in camera_timeline or []:
        key=(str(row.get("target_id")), str(row.get("shot")))
        if key != prev:
            transitions.append({
                "time": float(row.get("t") or 0),
                "target_id": row.get("target_id"),
                "shot": row.get("shot"),
            })
            prev=key
    return {
        "summary": "Visual AI tidak tersedia; review lokal memakai hasil face/body tracking dan camera locks.",
        "frames_reviewed": len(timestamps),
        "frame_timestamps": list(timestamps),
        "subjects": [],
        "composition_notes": [
            "Jaga breathing room pada portrait.",
            "Gunakan fit/group jika subject terlalu besar atau terlalu lebar.",
        ],
        "active_speaker_notes": [],
        "shot_changes": transitions[:20],
        "target_aspect": float(target_aspect),
        "source": "local",
    }


def _ask_visual_reviewer(
    config: dict[str, Any],
    source_path: str,
    timestamps: list[float],
    scenes: list,
    camera_timeline: list[dict],
    target_aspect: float,
    visual_frames: list[str],
) -> dict[str, Any]:
    from chopster.ai.orchestrator import orchestrator_from_config
    orch=orchestrator_from_config(config)
    if not visual_frames:
        return _local_visual_review(timestamps, camera_timeline, target_aspect)
    labels="\n".join(f"Frame {i+1} = {float(t):.1f}s" for i,t in enumerate(timestamps))
    prompt=(
        "Kamu adalah Visual Editor Brain untuk Chopster. Periksa frame video berikut dan gabungkan dengan evidence lokal. "
        "Fokus pada jumlah orang, posisi wajah/tubuh, ukuran subject terhadap frame, apakah portrait 9:16 terlalu dekat, "
        "apakah group harus memakai fit, siapa yang tampak aktif/relevan, dan apakah ada reaction visual. "
        "Jangan membuat timestamp baru. Kembalikan JSON object dengan field: summary, subjects(array), "
        "composition_notes(array), active_speaker_notes(array), shot_changes(array), confidence, source. "
        "Setiap subject boleh memiliki id/relation/position/size jika terlihat.\n\n"
        + labels
        + "\nTARGET ASPECT=" + f"{target_aspect:.6f}"
        + "\nSCENES=" + json.dumps((scenes or [])[:80], ensure_ascii=False)[:7000]
        + "\nCAMERA TIMELINE=" + json.dumps((camera_timeline or [])[:160], ensure_ascii=False)[:12000]
    )
    local=lambda: AIResponse(
        text=json.dumps(_local_visual_review(timestamps, camera_timeline, target_aspect), ensure_ascii=False),
        raw={"provider":"local-fallback", "fallback":True},
    )
    try:
        resp=orch.generate_vision(
            prompt,
            visual_frames,
            system="Kamu adalah analis visual konservatif untuk video editor.",
            model="",
            temperature=.15,
            max_tokens=7000,
            timeout=int(config.get("ai_timeout") or 90),
            local_fallback=local,
        )
        raw=(resp.text or "").strip()
        if "```" in raw:
            m=re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.S | re.I)
            if m:
                raw=m.group(1).strip()
        data=json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("visual review bukan object")
        data.setdefault("frame_timestamps", timestamps)
        data.setdefault("target_aspect", target_aspect)
        raw_meta = resp.raw if isinstance(resp.raw, dict) else {}
        data.setdefault("source", raw_meta.get("provider") or "remote-vision")
        # Preserve the embedded Local Vision evidence so downstream Camera Director
        # and Master Editor Brain can inspect both AI layers, even when remote AI wins.
        local_blob = raw_meta.get("local_vision_evidence")
        if local_blob:
            try:
                data["local_vision_evidence"] = json.loads(local_blob) if isinstance(local_blob, str) else local_blob
            except Exception:
                data["local_vision_evidence"] = local_blob
        data["ai_chain"] = raw_meta.get("provider_chain") or [str(data.get("source") or "remote-vision")]
        return data
    except Exception as exc:
        fallback=_local_visual_review(timestamps, camera_timeline, target_aspect)
        fallback["error"]=str(exc)[:300]
        fallback["source"]="local-fallback"
        return fallback

def ensure_master_analysis(
    project_dir: str | Path,
    source_path: str | Path,
    *,
    model: str = "tiny",
    language: str = "auto",
    word_timestamps: bool = False,
    device: str = "auto",
    mode: str = "Smart",
    ai_config: dict[str, Any] | None = None,
    progress_cb: Callable[[int, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    force_camera: bool = False,
    max_camera_samples: int | None = 1800,
    target_aspect: float = 9/16,
) -> dict[str, Any]:
    """Ensure transcript, scenes, camera, AI advisor and master manifest exist/current."""
    path = str(Path(source_path).resolve())
    ai_config = dict(ai_config or {})
    ai_config["target_aspect"] = float(target_aspect or 9/16)
    ai_sig = _ai_signature(ai_config)
    subtitle_max_words = int(ai_config.get("subtitle_max_words") or 8)
    transcript_ai_sig = transcript_prepare_signature(ai_config, subtitle_max_words)
    meta = probe(path)
    duration = float(meta.duration)

    def emit(p: int, msg: str) -> None:
        if progress_cb:
            progress_cb(max(0, min(100, int(p))), str(msg))

    def cancelled() -> bool:
        return bool(cancel_check and cancel_check())

    # Transcript. The in-project artifact is keyed by the source file only, so keep a
    # small provenance map on it: when the user changes the Whisper model/language/
    # word-timestamp settings, Master Analysis must re-transcribe instead of silently
    # reusing the previously cached transcript for this project.
    tr_data = load_artifact(project_dir, "transcript", path)
    transcript_prepared_source = "local"
    transcribe_provenance = {
        "model": str(model or ""),
        "language": str(language or "auto"),
        "word_timestamps": bool(word_timestamps),
        "device": str(device or "auto"),
    }
    stored_prov = dict(tr_data.get("_transcribe_provenance") or {}) if isinstance(tr_data, dict) else {}
    # Artifacts saved before this change carry no provenance; keep reusing them so
    # existing projects are not forced into an unexpected re-transcription.
    prov_compatible = (not stored_prov) or all(stored_prov.get(k) == v for k, v in transcribe_provenance.items())
    if tr_data is not None and prov_compatible:
        tr = Transcript.from_dict(tr_data)
        emit(18, f"Transcript cache ✓ — {len(tr.segments)} segmen")
    else:
        tr = load_cached_transcript(path, model, language, word_timestamps)
        if tr is not None:
            save_artifact(project_dir, "transcript", dict(tr.to_dict(), _transcribe_provenance=transcribe_provenance), path)
            emit(18, f"Transcript global cache ✓ — {len(tr.segments)} segmen")
        else:
            emit(5, "Master Analysis — transkripsi audio…")
            def tprog(p: int, msg: str) -> None:
                emit(5 + int(max(0, min(100, p)) * 0.25), f"Master Analysis — {msg}")
            tr = transcribe(path, model=model, language=language, device=device, word_timestamps=word_timestamps, progress_cb=tprog)
            save_cached_transcript(path, model, language, word_timestamps, tr)
            save_artifact(project_dir, "transcript", dict(tr.to_dict(), _transcribe_provenance=transcribe_provenance), path)
    if str(getattr(tr, "prepared_signature", "") or "") != transcript_ai_sig:
        emit(24, "Master Analysis — menyelaraskan transcript dengan AI Settings/local fallback…")
        tr, transcript_prepared_source = prepare_transcript_and_subtitles(tr, ai_config, max_words_per_line=subtitle_max_words)
        save_artifact(project_dir, "transcript", dict(tr.to_dict(), _transcribe_provenance=transcribe_provenance), path)
    else:
        transcript_prepared_source = str(getattr(tr, "prepared_by", "") or "local")
    if cancelled(): return {"cancelled": True}

    # Visual scene detection is isolated and NEVER changes camera state.
    scenes = load_artifact(project_dir, "scenes", path)
    if scenes is None:
        emit(34, "Master Analysis — scene detection satu pass…")
        scenes = detect_scenes(path, progress_cb=lambda p,m: emit(34 + int(max(0,min(100,p))*0.15), f"Master Analysis — {m}"), cancel_check=cancel_check)
        save_artifact(project_dir, "scenes", scenes, path)
    else:
        emit(49, f"Scene cache ✓ — {len(scenes)} scene")
    if cancelled(): return {"cancelled": True}

    # Gemini visual scene/frame understanding is independent of transcript. FFmpeg
    # detects deterministic cut boundaries; embedded Gemini inspects representative
    # frames to describe the shot, relevant subject and framing safety.
    scene_vision_artifact = load_artifact(project_dir, "scene_vision", path)
    scene_vision = None
    if (isinstance(scene_vision_artifact, dict) and
        scene_vision_artifact.get("ai_signature") == ai_sig and scene_vision_artifact.get("algorithm_version") == VISION_ALGORITHM_VERSION and
        abs(float(scene_vision_artifact.get("target_aspect") or 0) - float(target_aspect or 9/16)) < 1e-4):
        scene_vision = scene_vision_artifact
        emit(50, f"Gemini Scene Vision cache ✓ — {len(scene_vision.get('scenes') or [])} scene")
    elif bool(ai_config.get("ai_auto_visual", True)):
        emit(50, "Master Analysis — Gemini membaca representative frame setiap scene…")
        try:
            from chopster.ai.orchestrator import orchestrator_from_config
            orch = orchestrator_from_config(ai_config)
            scene_vision = analyze_scene_visuals(
                orch, path, scenes or [], target_aspect=float(target_aspect or 9/16),
                max_scenes=12, timeout=min(60, int(ai_config.get("ai_timeout") or 45)),
            )
        except Exception as exc:
            scene_vision = {
                "source": "local-fallback", "target_aspect": float(target_aspect or 9/16),
                "scenes": [], "error": str(exc)[:500],
            }
        scene_vision["ai_signature"] = ai_sig
        scene_vision["algorithm_version"] = VISION_ALGORITHM_VERSION
        save_artifact(project_dir, "scene_vision", scene_vision, path)
    else:
        scene_vision = {"source": "local-fallback", "target_aspect": float(target_aspect or 9/16), "scenes": [], "ai_signature": ai_sig}
    if cancelled(): return {"cancelled": True}

    # Person/camera analysis. It can internally ask the central AI director, but never a raw provider.
    camera_artifact = load_artifact(project_dir, "camera", path)
    camera = None
    evidence: dict[str, Any] = {}
    if isinstance(camera_artifact, dict) and isinstance(camera_artifact.get("points"), list):
        cached_aspect = float(camera_artifact.get("target_aspect") or 0)
        cached_mode = str(camera_artifact.get("mode") or "Smart")
        if abs(cached_aspect - float(target_aspect or 9/16)) < 1e-4 and cached_mode == str(mode) and camera_artifact.get("camera_algorithm_version") == CAMERA_ALGORITHM_VERSION:
            camera = camera_artifact.get("points")
            evidence = {
                "timeline": list(camera_artifact.get("timeline") or []),
                "ai_shots": list(camera_artifact.get("ai_shots") or []),
                "target_aspect": float(target_aspect or 9/16),
                "sample_step": camera_artifact.get("sample_step"),
                "target_samples": len(camera or []),
            }
    if camera is None or force_camera:
        if mode not in ("Manual", "Center"):
            emit(54, "Master Analysis — person/body/face tracking & camera locks…")
            try:
                focus_points = detect_face_focus(
                    path, 0.0, duration, 0.9, max_camera_samples,
                    mode=mode, transcript=tr, ai_config=ai_config, evidence_out=evidence,
                    progress_cb=lambda p,m: emit(54 + int(max(0,min(100,p))*0.16), f"Master Analysis — {m}"),
                )
                camera = [p.__dict__ for p in focus_points]
                save_artifact(project_dir, "camera", {
                    "points": camera,
                    "target_aspect": float(target_aspect or 9/16),
                    "mode": str(mode),
                    "timeline": evidence.get("timeline") or [],
                    "ai_shots": evidence.get("ai_shots") or [],
                    "sample_step": evidence.get("sample_step"),
                    "camera_algorithm_version": CAMERA_ALGORITHM_VERSION,
                }, path)
            except Exception as exc:
                camera = []
                emit(70, f"Camera analysis fallback — {str(exc)[:160]}")
        else:
            camera = []
    else:
        emit(70, f"Camera cache ✓ — {len(camera)} focus points")
    if cancelled(): return {"cancelled": True}

    if not evidence:
        evidence = {"timeline": [], "ai_shots": [], "target_aspect": float(target_aspect or 9/16), "sample_step": None, "target_samples": len(camera or [])}

    # Visual evidence pass: sampled frames are inspected by the same AI orchestrator.
    visual_review = None
    old_master = load_artifact(project_dir, "master", path)
    if isinstance(old_master, dict) and old_master.get("ai_signature") == ai_sig and isinstance(old_master.get("visual_review"), dict) and old_master.get("visual_review", {}).get("target_aspect") == float(target_aspect or 9/16):
        visual_review = old_master.get("visual_review")
        emit(76, "AI Vision cache ✓")
    elif bool(ai_config.get("ai_auto_visual", True)):
        emit(74, "Master Analysis — membaca frame representative dengan AI Vision…")
        timestamps=select_visual_timestamps(path, scenes=scenes or [], camera_timeline=evidence.get("timeline") or [], max_frames=8, duration=duration)
        root=None
        imgs=[]
        try:
            root, imgs = extract_frame_images(path, timestamps, max_width=560)
            visual_review=_ask_visual_reviewer(ai_config, path, timestamps, scenes or [], evidence.get("timeline") or [], float(target_aspect or 9/16), [str(x) for x in imgs])
        except Exception as exc:
            visual_review=_local_visual_review(timestamps, evidence.get("timeline") or [], float(target_aspect or 9/16))
            visual_review["error"]=str(exc)[:300]
            visual_review["source"]="local-fallback"
        finally:
            if root:
                import shutil
                shutil.rmtree(root,ignore_errors=True)
    else:
        visual_review=_local_visual_review([], evidence.get("timeline") or [], float(target_aspect or 9/16))

    # Optional visual Camera Director refinement. Local coordinates remain authoritative.
    ai_shots = evidence.get("ai_shots") or []
    if (
        mode not in ("Manual", "Center")
        and bool(ai_config.get("ai_camera_director", False))
        and bool(ai_config.get("ai_auto_visual", True))
        and camera
        and not evidence.get("ai_shots")
    ):
        try:
            timestamps=select_visual_timestamps(path, scenes=scenes or [], camera_timeline=evidence.get("timeline") or [], max_frames=6, duration=duration)
            root=None; imgs=[]
            try:
                root, imgs = extract_frame_images(path, timestamps, max_width=560)
                from chopster.clipper.camera_director import ai_camera_director_visual, apply_ai_shots
                from chopster.ai.orchestrator import orchestrator_from_config
                orch=orchestrator_from_config(ai_config)
                ai_shots=ai_camera_director_visual(orch, evidence.get("timeline") or [], " ".join(getattr(s,"text","") for s in tr.segments), [str(x) for x in imgs], timestamps, visual_context=visual_review or {})
                if ai_shots:
                    camera=apply_ai_shots(camera, ai_shots, timeline=evidence.get("timeline") or [])
                    camera=stabilize_camera_path(camera, min_shot_duration=1.5, transition_duration=.28, max_speed=.48, max_step=.10)
                    save_artifact(project_dir,"camera",{
                        "points": camera,
                        "target_aspect": float(target_aspect or 9/16),
                        "mode": str(mode),
                        "timeline": evidence.get("timeline") or [],
                        "ai_shots": ai_shots,
                        "sample_step": evidence.get("sample_step"),
                        "camera_algorithm_version": CAMERA_ALGORITHM_VERSION,
                    }, path)
                    evidence["ai_shots"]=ai_shots
            finally:
                if root:
                    import shutil
                    shutil.rmtree(root,ignore_errors=True)
        except Exception as exc:
            evidence.setdefault("ai_errors",[]).append(f"visual camera: {str(exc)[:300]}")

    # Central AI editorial review — the unified editor brain receives transcript, scenes, camera and visual evidence.
    ai_advice = None
    if isinstance(old_master, dict) and old_master.get("ai_signature") == ai_sig and isinstance(old_master.get("ai_advice"), dict):
        ai_advice = old_master.get("ai_advice")
        emit(80, "AI Editor Brain cache ✓")
    else:
        emit(78, "Master Analysis — AI Editor Brain membaca evidence lokal…")
        evidence_payload = _build_evidence(tr, scenes or [], evidence.get("timeline") or [], duration, float(target_aspect or 9/16))
        evidence_payload["visual_review"] = visual_review or {}
        evidence_payload["scene_vision"] = scene_vision or {}
        try:
            ai_advice = _ask_master_advisor(ai_config, evidence_payload, tr, scenes or [], evidence.get("timeline") or [])
        except Exception as exc:
            ai_advice = _local_master_advice(tr, scenes or [], evidence.get("timeline") or [], duration, float(target_aspect or 9/16))
            ai_advice["error"] = str(exc)[:300]
        emit(90, f"AI Editor Brain: {ai_advice.get('source','local')} ✓")

    master = {
        "version": 8,
        "camera_algorithm_version": CAMERA_ALGORITHM_VERSION,
        "vision_algorithm_version": VISION_ALGORITHM_VERSION,
        "ai_signature": ai_sig,
        "transcript_ai_signature": transcript_ai_sig,
        "transcript_prepared_source": transcript_prepared_source,
        "duration": duration,
        "transcript_segments": len(tr.segments),
        "scene_count": len(scenes or []),
        "camera_points": len(camera or []),
        "target_aspect": float(target_aspect or 9/16),
        "scenes": scenes or [],
        "scene_vision": scene_vision or {},
        "camera": camera or [],
        "camera_evidence": evidence.get("timeline") or [],
        "ai_shots": evidence.get("ai_shots") or [],
        "visual_review": visual_review or {},
        "ai_advice": ai_advice or {},
        "source": path,
        "cache": cache_status(project_dir, path),
    }
    save_artifact(project_dir, "master", master, path)
    emit(100, "Master Analysis selesai — evidence + AI Editor Brain tersimpan")
    return {"cancelled": False, "transcript": tr.to_dict(), "scenes": scenes or [], "scene_vision": scene_vision or {}, "focus": camera or [], "master": master}
