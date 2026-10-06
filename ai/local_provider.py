"""Embedded local AI fallback for Chopster.

The local engine is deliberately dependency-light and works without network access.
It uses OpenCV's trained Haar face detector shipped with the OpenCV package plus
editorial heuristics to create structured visual evidence. This is a real local
vision model, not an empty placeholder, but it is intentionally not presented as
a local generative LLM. Remote reasoning remains an enhancement when available.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from chopster.ai.provider_interface import AIProvider, AIRequest, AIResponse


def _aspect_from_prompt(prompt: str) -> float:
    m = re.search(r"TARGET\s*ASPECT\s*=\s*([0-9.]+)", prompt or "", re.I)
    if m:
        try:
            value = float(m.group(1))
            if value > 0:
                return value
        except Exception:
            pass
    # common textual hints
    low=(prompt or "").lower()
    for token in ("9:16", "4:5", "3:4", "1:1", "4:3", "16:9"):
        if token in low:
            a,b=token.split(":")
            return float(a)/float(b)
    return 9/16


def _face_detector():
    import cv2
    cascade_cls = getattr(cv2, "CascadeClassifier", None)
    cv2_data = getattr(cv2, "data", None)
    haar_dir = getattr(cv2_data, "haarcascades", "") if cv2_data is not None else ""
    if cascade_cls is None or not haar_dir:
        return None
    cascade = cascade_cls(str(Path(haar_dir) / "haarcascade_frontalface_default.xml"))
    try:
        if cascade is not None and not cascade.empty():
            return cascade
    except Exception:
        pass
    return None


def _scan_frame(path: str, target_aspect: float) -> dict[str, Any]:
    import cv2
    image = cv2.imread(str(path))
    if image is None:
        return {"path": str(path), "ok": False, "faces": [], "face_count": 0, "error": "frame tidak bisa dibaca"}
    height, width = image.shape[:2]
    gray=cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray=cv2.equalizeHist(gray)
    cascade=_face_detector()
    if cascade is None:
        faces=[]
    else:
        faces=cascade.detectMultiScale(gray, scaleFactor=1.08, minNeighbors=5, minSize=(24,24))
    rows=[]
    for i,(x,y,w,h) in enumerate(sorted(faces, key=lambda b: b[2]*b[3], reverse=True)[:12], start=1):
        rows.append({
            "id": i,
            "x": x/width, "y": y/height, "w": w/width, "h": h/height,
            "cx": (x+w/2)/width, "cy": (y+h/2)/height,
            "area": (w*h)/(width*height),
        })
    if rows:
        left=min(r["x"] for r in rows); right=max(r["x"]+r["w"] for r in rows)
        top=min(r["y"] for r in rows); bottom=max(r["y"]+r["h"] for r in rows)
        group_w=right-left; group_h=bottom-top
        group_cx=(left+right)/2; group_cy=(top+bottom)/2
        largest=max(r["area"] for r in rows)
    else:
        left=right=top=bottom=group_w=group_h=group_cx=group_cy=largest=0.0
    # Editorial framing heuristic: face coverage is the strongest signal for
    # a portrait breathing-room recommendation. It never outputs pixel crop coordinates.
    if len(rows)>=2 and (group_w > 0.88 or largest > 0.24):
        shot="fit"; note="Group/face envelope terlalu besar untuk crop portrait nyaman; gunakan wider fit."
    elif len(rows)==1 and largest > 0.20:
        shot="solo-wide"; note="Wajah sangat dekat; beri breathing room dan jangan center-crop terlalu ketat."
    elif len(rows)>=2:
        shot="two"; note="Dua subject terdeteksi; pertahankan keduanya bila komposisi ratio memungkinkan."
    elif len(rows)==1:
        shot="solo"; note="Satu subject terdeteksi; beri ruang alami di sekitar kepala/bahu."
    else:
        shot="unknown"; note="Tidak ada wajah yang terdeteksi; jangan memaksa camera follow."
    return {
        "path": str(path), "ok": True, "width": width, "height": height,
        "aspect": round(width/max(1,height),6), "target_aspect": target_aspect,
        "faces": rows, "face_count": len(rows),
        "largest_face_area": round(largest,6),
        "group_box": {"x":round(left,4),"y":round(top,4),"w":round(group_w,4),"h":round(group_h,4),"cx":round(group_cx,4),"cy":round(group_cy,4)},
        "shot_recommendation": shot, "composition_note": note,
    }


def analyze_frames(image_paths: list[str], prompt: str = "") -> dict[str, Any]:
    target_aspect=_aspect_from_prompt(prompt)
    frames=[]
    for p in image_paths or []:
        try:
            frames.append(_scan_frame(str(p), target_aspect))
        except Exception as exc:
            frames.append({"path":str(p),"ok":False,"faces":[],"face_count":0,"error":str(exc)[:300]})
    good=[f for f in frames if f.get("ok")]
    face_counts=[int(f.get("face_count",0)) for f in good]
    recommendations=[str(f.get("shot_recommendation")) for f in good if f.get("shot_recommendation")]
    return {
        "source":"local-vision-ai",
        "model":"OpenCV Haar Face Intelligence",
        "target_aspect":target_aspect,
        "frames_reviewed":len(frames),
        "frames":frames,
        "max_faces":max(face_counts or [0]),
        "recommendations":recommendations[:20],
        "summary":("Local Vision mendeteksi " + str(max(face_counts or [0])) + " wajah maksimum; gunakan fit/wider bila wajah terlalu dekat."),
    }


class LocalProvider(AIProvider):
    @property
    def name(self) -> str:
        return "local"

    def is_available(self) -> tuple[bool, str]:
        try:
            import cv2
            required = ["imread", "cvtColor", "equalizeHist"]
            missing = [name for name in required if not hasattr(cv2, name)]
            if missing:
                return False, f"Local Vision tidak siap: OpenCV kurang fitur dasar ({', '.join(missing)})"
            detector = _face_detector()
            if detector is None:
                return True, "Embedded Local Vision AI siap (heuristic mode)"
            return True, "Embedded Local Vision AI siap"
        except Exception as exc:
            return False, f"Local Vision tidak siap: {exc}"

    def generate(self, req: AIRequest) -> AIResponse:
        text = req.prompt or ""
        low = text.lower()
        if "json" in low and ("candidates" in low or "highlights" in low):
            return AIResponse(text=json.dumps({"candidates": []}, ensure_ascii=False), raw={"provider":"local-ai","model":"embedded-heuristic"})
        if "caption" in low or "hashtag" in low or "title" in low or "description" in low:
            return AIResponse(text=json.dumps({
                "titles":["Momen yang Bikin Percakapan Ini Berubah", "Bagian Podcast yang Paling Menarik", "Kenapa Pernyataan Ini Jadi Perdebatan"],
                "captions":["Potongan percakapan dengan konteks tetap utuh. Pilih angle yang paling cocok untuk audiensmu."],
                "description_short":"Highlight percakapan dengan konteks yang tetap dijaga.",
                "description_long":"Ini adalah potongan percakapan yang menampilkan bagian penting dari pembahasan. Gunakan konteks asli video sebagai acuan dan hindari mengambil kesimpulan di luar isi sumber.",
                "hashtags":["#shorts","#podcast","#clip"],"keywords":["podcast","highlight","conversation"],
                "pinned_comment":"Bagian mana yang paling menarik menurut kamu?",
                "thumbnail_text":["WAJIB LIHAT BAGIAN INI", "TERNYATA BEGINI"]
            }, ensure_ascii=False), raw={"provider":"local-ai","model":"embedded-heuristic"})
        if "voice-over" in low or "voiceover" in low:
            source=text.split("\n\n",1)[-1].strip()
            return AIResponse(text=source[:3000], raw={"provider":"local-ai","model":"embedded-heuristic"})
        cleaned = re.sub(r"\s+", " ", text).strip()
        return AIResponse(text=cleaned[:1800], raw={"provider":"local-ai","model":"embedded-heuristic"})

    def generate_vision(self, prompt: str, image_paths: list[str], *, system: str = "", model: str = "", temperature: float = 0.2, max_tokens: int = 4096, timeout: int | None = None) -> AIResponse:
        data=analyze_frames(image_paths, prompt)
        return AIResponse(text=json.dumps(data, ensure_ascii=False), raw={
            "provider":"local-vision-ai", "model":"OpenCV Haar Face Intelligence", "capabilities":["vision","face_detection","composition"],
            "local_evidence":data,
        })
