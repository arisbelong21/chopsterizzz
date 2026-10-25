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


#: Content-pack house style for the offline provider: CAPSLOCK titles, a hook +
#: CTA in every description, and hashtags taken from the transcript itself
#: instead of a generic list (the "kelihatan bot" Studio complaint).
_PACK_TITLE_SHAPES = {
    "id": (
        "{topic}: YANG BIKIN BERHENTI SCROLL",
        "KENAPA {topic} JADI BAHAN PERHATIAN?",
        "{topic} - BAGIAN YANG JARANG DIBAHAS",
        "INI YANG TERJADI SAAT {topic} DIBAHAS",
    ),
    "en": (
        "{topic}: THE PART THAT STOPS THE SCROLL",
        "WHY {topic} GOT EVERYONE'S ATTENTION",
        "{topic} - THE PART NOBODY TALKS ABOUT",
        "WHAT HAPPENS WHEN {topic} GETS DISCUSSED",
    ),
}
_PACK_CTAS = {
    "id": (
        "Tulis pendapatmu di komentar.",
        "Follow untuk bagian berikutnya.",
        "Bagikan kalau kamu setuju.",
    ),
    "en": (
        "Drop your take in the comments.",
        "Follow for the next part.",
        "Share it if you agree.",
    ),
}
_PACK_STOPWORDS = frozenset({
    "yang", "dengan", "untuk", "dari", "pada", "dalam", "akan", "tidak", "bukan",
    "juga", "karena", "tapi", "tetapi", "atau", "ini", "itu", "saja", "bisa",
    "harus", "sudah", "masih", "lebih", "sangat", "kita", "kami", "mereka", "kamu",
    "adalah", "saat", "ketika", "kalau", "jika", "agar", "supaya", "oleh",
    "tentang", "banyak", "begitu", "secara", "tersebut", "video", "bagian",
    "shot", "shorts", "clip", "podcast", "highlight", "conversation",
    "the", "and", "for", "with", "that", "this", "from", "you", "your", "are",
    "was", "were", "have", "has", "had", "will", "would", "could", "should",
    "about", "into", "just", "like", "when", "what", "who", "how", "why",
})
_PACK_TRANSCRIPT_RE = re.compile(r"Transkrip/konteks:\s*(.*)", re.S | re.IGNORECASE)
#: The prompt's own JSON-format block follows the transcript and must never be
#: mined for keywords ("#json", "#description_long" is the bot smell).
_PACK_TAIL_RE = re.compile(r"\n\s*Format JSON:.*", re.S | re.IGNORECASE)
#: The caption prompt declares the output language on a "Bahasa: xx" line.
_PACK_LANG_RE = re.compile(r"Bahasa:\s*([A-Za-z-]+)", re.IGNORECASE)
_PACK_EN_MARKERS = frozenset({
    "the", "and", "you", "your", "this", "that", "with", "what", "when", "why",
    "how", "who", "are", "were", "have", "has", "had", "will", "would", "could",
    "should", "about", "because", "people", "think", "really", "just", "like",
})


def _pack_capitalise(text: str) -> str:
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    return clean[:1].upper() + clean[1:] if clean else clean


def _pack_caps(text: str, limit: int = 90) -> str:
    """CAPSLOCK pack text, long enough to read but free of trailing punctuation."""
    clean = re.sub(r"\s+", " ", str(text or "")).strip().upper()
    if len(clean) > limit:
        cut = clean[:limit]
        clean = cut[:cut.rfind(" ")].strip() if " " in cut else cut
    return clean


def _pack_sentence(text: str, limit: int) -> str:
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    if not clean:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", clean)
    return (parts[0] if parts else clean).strip()[:limit]


def _detect_pack_language(prompt: str, body: str) -> str:
    """Resolve the pack's output language ('id' or 'en').

    Order of evidence: the prompt's explicit "Bahasa: xx" line wins; otherwise a light
    stopword scan of the transcript body picks the dominant language, defaulting to 'id'
    (Chopster's house default) only when the evidence is genuinely mixed or empty.
    """
    m = _PACK_LANG_RE.search(prompt or "")
    if m:
        code = (m.group(1) or "").strip().lower().split("-")[0]
        if code in ("en", "id"):
            return code
    tokens = re.findall(r"\b[a-z]+\b", (body or "").lower())
    if not tokens:
        return "id"
    id_hits = sum(1 for t in tokens if t in _PACK_STOPWORDS and t not in _PACK_EN_MARKERS)
    en_hits = sum(1 for t in tokens if t in _PACK_EN_MARKERS)
    if en_hits > id_hits:
        return "en"
    return "id"


def _pack_json(prompt: str) -> dict[str, Any]:
    """Deterministic content pack built from the transcript inside the prompt.

    The pack's titles, CTAs and descriptions follow the transcript's own language
    (Indonesian or English) so an English video never receives an Indonesian pack.
    """
    match = _PACK_TRANSCRIPT_RE.search(prompt or "")
    body = (match.group(1) if match else (prompt or "").split("\n\n")[-1])
    body = _PACK_TAIL_RE.sub("", str(body or ""))
    body = re.sub(r"\s+", " ", str(body or "")).strip()
    lang = _detect_pack_language(prompt, body)
    words = [w for w in re.findall(r"\b[^\W\d_]{4,}\b", body.lower()) if w not in _PACK_STOPWORDS]
    freq: dict[str, int] = {}
    for word in words:
        freq[word] = freq.get(word, 0) + 1
    top = sorted(freq, key=lambda key: (-freq[key], key))[:8]
    default_topic = "video ini" if lang == "id" else "this video"
    topic = _pack_caps(" ".join(top[:3]) or body[:60] or default_topic, 48)
    hook_fallback = (
        f"Bagian ini membahas {topic.lower()}." if lang == "id"
        else f"This part covers {topic.lower()}."
    )
    hook = _pack_capitalise(_pack_sentence(body, 150)) or hook_fallback
    cta = _PACK_CTAS[lang][len(words) % len(_PACK_CTAS[lang])]
    details = ", ".join(top[:6]) or topic.lower()
    title_shapes = _PACK_TITLE_SHAPES[lang]
    if lang == "id":
        captions = [
            f"{hook} Bagian mana yang paling kamu setujui? {cta}",
            f"Ada satu poin soal {topic.lower()} yang gampang kelewat. {cta}",
            f"{hook} Tonton sampai habis, lalu bilang mana yang paling masuk akal.",
        ]
        description_long = (
            f"{hook}\n\nPembahasan bergerak di sekitar {details}. "
            "Konteks sebelum dan sesudah potongan ini membantu memahami poinnya secara utuh.\n\n"
            f"Kata kunci yang menandai bagian ini: {details}.\n\n{cta}"
        )
        pinned = f"Bagian {topic.lower()} mana yang menurutmu paling penting? Jelaskan alasannya."
        thumbs = ["BAGIAN INI", "MENURUT KAMU?", "BAGIAN INI PENTING", "DENGAR SAMPAI AKHIR"]
        hashtags_default = "#shortvideo"
    else:
        captions = [
            f"{hook} Which part do you agree with the most? {cta}",
            f"There's one point about {topic.lower()} that's easy to miss. {cta}",
            f"{hook} Watch it to the end, then tell us which part makes the most sense.",
        ]
        description_long = (
            f"{hook}\n\nThe discussion centres on {details}. "
            "The context before and after this cut helps the point land in full.\n\n"
            f"Keywords that mark this part: {details}.\n\n{cta}"
        )
        pinned = f"Which part of {topic.lower()} matters most to you? Explain why."
        thumbs = ["THIS PART", "WHAT DO YOU THINK?", "THIS PART MATTERS", "LISTEN TO THE END"]
        hashtags_default = "#shorts"
    return {
        "titles": [_pack_caps(title_shapes[i % len(title_shapes)].format(topic=topic), 100)
                   for i in range(3)],
        "captions": captions,
        "description_short": f"{hook} Fokusnya {details}. {cta}" if lang == "id" else f"{hook} It focuses on {details}. {cta}",
        "description_long": description_long,
        "hashtags": [f"#{w}" for w in top[:10]] or [hashtags_default],
        "keywords": top,
        "pinned_comment": pinned,
        "thumbnail_text": [_pack_caps(topic, 24) or ("BAGIAN INI" if lang == "id" else "THIS PART"), *thumbs[1:]],
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
            # Built from the transcript inside the prompt: generic canned titles
            # and hashtags are what made the pack "kelihatan bot".
            return AIResponse(text=json.dumps(_pack_json(text), ensure_ascii=False),
                              raw={"provider":"local-ai","model":"embedded-heuristic"})
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
