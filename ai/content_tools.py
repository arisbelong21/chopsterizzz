"""Hybrid AI content helpers for transcript cleanup, B-roll and voiceover scripts."""
from __future__ import annotations
import json
import re
from typing import Any

from chopster.ai.provider_interface import AIRequest


def _orchestrator_from_config(config: dict[str, Any]):
    from chopster.ai.orchestrator import orchestrator_from_config
    return orchestrator_from_config(config)


def _extract_json(text: str) -> Any:
    raw = (text or "").strip()
    if "```" in raw:
        m = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.S | re.I)
        if m:
            raw = m.group(1).strip()
    return json.loads(raw)




def _local_broll(transcript_text: str, count: int = 10) -> list[dict[str, str]]:
    words = [w.strip(".,!?;:()[]{}\"").lower() for w in str(transcript_text).split()]
    freq: dict[str, int] = {}
    for w in words:
        if len(w) >= 5 and w.isalpha():
            freq[w] = freq.get(w, 0) + 1
    topics = [w for w, _ in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[:max(3, count)]] or ["topik utama"]
    ideas = []
    templates = [
        ("keyword", "B-roll detail sesuai keyword utama: {topic}", "Memberi visual konkret tanpa mengubah konteks pembicaraan."),
        ("reaction", "Close-up reaksi natural pembicara saat poin penting: {topic}", "Memperkuat emosi dan retention tanpa menambah fakta baru."),
        ("context", "Visual pendukung yang merepresentasikan konteks {topic}", "Membantu penonton memahami topik dalam hitungan detik."),
        ("detail", "Insert objek/lingkungan yang relevan dengan {topic}", "Membuat ritme visual lebih hidup di antara talking-head shots."),
    ]
    for i in range(max(1, count)):
        kind, suggestion, why = templates[i % len(templates)]
        topic = topics[i % len(topics)]
        ideas.append({"keyword": f"{kind}:{topic}", "suggestion": suggestion.format(topic=topic), "why": why})
    return ideas[:count]


def generate_broll_ai(transcript_text: str, config: dict[str, Any], count: int = 10) -> list[dict[str, str]]:
    from chopster.ai.orchestrator import orchestrator_from_config
    prompt = f"Buat {int(count)} ide B-roll untuk video Shorts/Reels berdasarkan transcript berikut.\n\n"
    prompt += "Kembalikan JSON array saja dengan field: keyword, suggestion, why. Jangan menambahkan markdown.\n\n"
    prompt += transcript_text[:9000]
    if config.get("master_context"):
        prompt += "\n\nMASTER EDITOR BRAIN CONTEXT\n" + json.dumps(config.get("master_context"), ensure_ascii=False)[:4000]
    orch = orchestrator_from_config(config)
    # Always use the central orchestrator. Settings AI is optional; embedded Gemini
    # remains the default remote brain and the local generator is the last fallback.
    req = AIRequest(prompt=prompt, system="Kamu adalah AI video editor. Buat ide B-roll yang konkret, mudah divisualisasikan, relevan dengan kalimat sumber, dan tidak mengarang fakta.", model=config.get("ai_model") or "", temperature=0.4, max_tokens=4096, timeout=int(config.get("ai_timeout") or 60))
    local_json = lambda: json.dumps(_local_broll(transcript_text, count), ensure_ascii=False)
    try:
        resp = orch.generate(req, local_fallback=local_json)
        data = _extract_json(resp.text)
        rows = data if isinstance(data, list) else data.get("items") if isinstance(data, dict) else []
        parsed = [{"keyword": str(x.get("keyword") or "B-roll"), "suggestion": str(x.get("suggestion") or ""), "why": str(x.get("why") or "")} for x in rows if isinstance(x, dict)][:count]
        return parsed or _local_broll(transcript_text, count)
    except Exception:
        return _local_broll(transcript_text, count)


def polish_transcript(transcript, config: dict[str, Any]):
    """Improve punctuation/readability while preserving segment boundaries.

    All remote calls go through the central orchestrator; a no-op local copy is
    returned when the provider is unavailable.
    """
    from chopster.ai.orchestrator import orchestrator_from_config
    from chopster.ai.provider_interface import AIRequest
    orch = orchestrator_from_config(config)
    lines = [f"{i}\t{s.text}" for i, s in enumerate(transcript.segments)]
    prompt = "Rapikan transcript berikut tanpa mengubah nomor baris atau makna. Pertahankan bahasa asli. Kembalikan JSON array objek {index,text} saja.\n\n" + "\n".join(lines[:120])
    original = transcript.to_dict()
    def local_json():
        return json.dumps([{"index": i, "text": str(s.text).strip()} for i, s in enumerate(transcript.segments)], ensure_ascii=False)
    try:
        resp = orch.generate(AIRequest(prompt=prompt, system="Kamu editor transcript video. Jangan menghapus isi penting.", model=config.get("ai_model") or "", temperature=0.15, max_tokens=8192, timeout=int(config.get("ai_timeout") or 60)), local_fallback=local_json)
        rows = _extract_json(resp.text)
    except Exception:
        return transcript
    if not isinstance(rows, list):
        return transcript
    by_index = {int(x.get("index")): str(x.get("text") or "").strip() for x in rows if isinstance(x, dict) and str(x.get("index", "")).isdigit()}
    for i, seg in enumerate(transcript.segments):
        if i in by_index and by_index[i]:
            seg.text = by_index[i]
    transcript.raw_text = " ".join(s.text for s in transcript.segments)
    return transcript


def generate_voiceover_script(source_text: str, config: dict[str, Any], language: str = "id") -> str:
    from chopster.ai.orchestrator import orchestrator_from_config
    from chopster.ai.provider_interface import AIRequest
    orch = orchestrator_from_config(config)
    prompt = f"Tulis naskah voice-over {language} yang natural dan enak didengar untuk video pendek dari bahan berikut. Pertahankan fakta dan maksud. Jangan pakai bullet, label, emoji, atau penjelasan tambahan.\n\n{source_text[:6000]}"
    try:
        resp = orch.generate(
            AIRequest(prompt=prompt, system="Kamu penulis naskah voice-over Shorts yang ringkas dan natural.", model=config.get("ai_model") or "", temperature=0.55, max_tokens=4096, timeout=int(config.get("ai_timeout") or 60)),
            local_fallback=lambda: source_text[:6000],
        )
        return resp.text.strip() or source_text
    except Exception:
        return source_text

