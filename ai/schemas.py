"""JSON schema validation for AI outputs."""
from __future__ import annotations

from typing import Any


def validate_highlight(data: Any, duration: float) -> list[dict]:
    if not isinstance(data, dict):
        raise ValueError("AI response bukan object JSON")
    cands = data.get("candidates")
    if not isinstance(cands, list):
        raise ValueError("Field 'candidates' harus berupa list")
    out: list[dict] = []
    for i, c in enumerate(cands[:20]):
        if not isinstance(c, dict):
            continue
        try:
            title = str(c.get("title") or f"Kandidat {i+1}")
            start = float(c.get("start", 0))
            end = float(c.get("end", 0))
            excerpt = str(c.get("excerpt") or "")
            reason = str(c.get("reason") or "")
            hook = str(c.get("hook") or "")
            context_required = str(c.get("context_required") or "")
            weaknesses = str(c.get("weaknesses") or "")
            score = int(c.get("score", 50))
        except Exception as exc:
            raise ValueError(f"Kandidat {i+1} field tidak valid: {exc}")
        if not (0 <= start < end <= duration + 1):
            raise ValueError(f"Kandidat {i+1} timestamp di luar durasi video")
        if end - start < 5 or end - start > 180:
            raise ValueError(f"Kandidat {i+1} durasi harus 5-180 detik")
        score = max(0, min(100, score))
        out.append({
            "title": title,
            "start": start,
            "end": end,
            "duration": end - start,
            "excerpt": excerpt,
            "reason": reason,
            "hook": hook,
            "context_required": context_required,
            "weaknesses": weaknesses,
            "score": score,
        })
    if not out:
        raise ValueError("Tidak ada kandidat valid dari AI")
    return sorted(out, key=lambda x: x["score"], reverse=True)


def validate_caption(data: Any) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Caption response bukan object JSON")
    caps = data.get("captions")
    if not isinstance(caps, list) or not caps:
        raise ValueError("Field 'captions' harus berupa list tidak kosong")
    titles = data.get("titles")
    if not isinstance(titles, list):
        titles = [data.get("title")] if data.get("title") else []
    titles = [str(x).strip() for x in titles if str(x).strip()]
    if not titles:
        titles = ["Konten menarik — lihat selengkapnya"]
    description_short = str(data.get("description_short") or data.get("description") or "").strip()
    description_long = str(data.get("description_long") or data.get("description") or "").strip()
    result = {
        "titles": titles[:10],
        "title": titles[0],
        "captions": [str(c).strip() for c in caps if str(c).strip()][:10],
        "description_short": description_short,
        "description_long": description_long,
        "description": description_short,
        "hashtags": [str(h).strip() for h in (data.get("hashtags") or []) if str(h).strip()][:20],
        "keywords": [str(k).strip() for k in (data.get("keywords") or []) if str(k).strip()][:20],
        "pinned_comment": str(data.get("pinned_comment") or "").strip(),
        "thumbnail_text": [str(x).strip() for x in (data.get("thumbnail_text") or []) if str(x).strip()][:10],
        # Preserve non-core metadata used by the UI and downstream clip context.
        "source": str(data.get("source") or "").strip(),
        "highlight_start": data.get("highlight_start"),
        "highlight_end": data.get("highlight_end"),
        "highlight_score": data.get("highlight_score"),
        "highlight_hook": data.get("highlight_hook"),
    }
    if data.get("ai_error"):
        result["ai_error"] = str(data.get("ai_error"))[:500]
    return result
