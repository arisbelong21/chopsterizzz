"""AI + local social content pack generator."""
from __future__ import annotations

import json
import re
from typing import Any

from chopster.clipper.transcript_manager import Transcript


def _local_captions(transcript: Transcript, platform: str, tone: str, count: int, style: str, language: str, highlight_context: list[dict] | None = None) -> dict:
    text = " ".join(s.text for s in transcript.segments).strip()
    snippet = text[:2400]
    words = re.findall(r"\b\w{4,}\b", snippet.lower())
    freq: dict[str, int] = {}
    for w in words:
        freq[w] = freq.get(w, 0) + 1
    top = sorted(freq, key=lambda k: (-freq[k], k))[:8]
    context = highlight_context or []
    top_candidate = context[0] if context else None
    topic = ", ".join(top[:3]) or "obrolan ini"
    if top_candidate:
        topic = str(top_candidate.get("hook") or top_candidate.get("excerpt") or topic).strip()[:120]

    titles = [
        f"{topic}: ini bagian yang paling bikin penasaran",
        f"Kenapa {topic} jadi bahan obrolan?",
        f"Bagian {topic} yang ternyata lebih dalam dari kelihatannya",
        f"Satu obrolan tentang {topic} yang layak kamu dengar",
        f"Jawaban soal {topic} ternyata bukan seperti yang dikira",
    ][:max(1, count)]
    captions = [
        f"Bagian ini bikin sudut pandang soal {topic} jadi beda. Kamu setuju atau justru punya pengalaman lain?",
        f"Ada satu poin di obrolan ini yang gampang banget kelewat. Coba dengarkan sampai bagian akhirnya.",
        f"Menurut kamu, bagian mana yang paling masuk akal dari obrolan tentang {topic}?",
        f"Bukan sekadar potongan podcast — konteksnya justru ada di bagian ini.",
        f"Kalau kamu di posisi mereka, kamu akan jawab apa?",
    ][:max(1, count)]
    hashtag_words = [w for w in top[:8] if len(w) >= 4]
    hashtags = [f"#{w}" for w in hashtag_words]
    desc_short = f"Potongan obrolan tentang {topic}. Simak konteksnya, lalu tulis pendapatmu di komentar."
    desc_long = (
        f"Di potongan podcast ini, pembahasan berfokus pada {topic}. "
        "Video membahas sudut pandang dan percakapan yang muncul dari konteks obrolan, jadi bagian ini paling tepat "
        "dilihat sebagai potongan diskusi, bukan kesimpulan tunggal.\n\n"
        f"Topik yang terdengar dalam percakapan mencakup: {', '.join(top[:6]) or 'beberapa topik utama dalam podcast'}. "
        "Perhatikan bagaimana pembicara membangun argumen, memberi contoh, lalu merespons sudut pandang satu sama lain. "
        "Kalau kamu menonton versi lengkapnya, konteks sebelum dan sesudah potongan ini dapat membantu memahami pembahasannya secara utuh.\n\n"
        "Apa pendapatmu? Bagian mana yang paling kamu setujui atau justru ingin kamu bantah? Tulis alasanmu di komentar."
    )
    return {
        "titles": titles,
        "title": titles[0],
        "captions": captions,
        "hashtags": hashtags,
        "keywords": top,
        "description_short": desc_short,
        "description_long": desc_long,
        "description": desc_short,
        "pinned_comment": "Kamu lebih setuju atau tidak setuju dengan poin di video ini? Jelaskan alasannya.",
        "thumbnail_text": [topic[:24], "Menurut Kamu?", "Bagian Ini Penting", "Dengar Sampai Akhir"],
        "highlight_start": top_candidate.get("start") if top_candidate else None,
        "highlight_end": top_candidate.get("end") if top_candidate else None,
        "highlight_score": top_candidate.get("score") if top_candidate else None,
        "highlight_hook": top_candidate.get("hook") if top_candidate else None,
        "source": "local",
    }


def generate_captions(transcript: Transcript | str, config: dict, platform: str = "TikTok", tone: str = "casual", language: str = "id", count: int = 5, style: str = "hooks") -> dict:
    text = transcript.raw_text if isinstance(transcript, Transcript) else str(transcript)
    if isinstance(transcript, Transcript) and not text:
        text = " ".join(s.text for s in transcript.segments)
    highlight_context = config.get("highlight_context") or []
    # When the user has selected/highlighted moments, feed the AI their local
    # transcript context first. This prevents a long podcast's opening minutes
    # from dominating titles/descriptions for a later selected clip.
    if isinstance(transcript, Transcript) and highlight_context:
        focused_parts=[]
        for item in highlight_context[:6]:
            try:
                st=float(item.get("start",0)); en=float(item.get("end",st+30))
            except Exception:
                continue
            excerpt=" ".join(seg.text for seg in transcript.segments if seg.end > max(0,st-12) and seg.start < en+12).strip()
            if excerpt:
                focused_parts.append(f"[{st:.1f}-{en:.1f}] {excerpt}")
        if focused_parts:
            text = "\n\n".join(focused_parts) + "\n\nFULL TRANSCRIPT CONTEXT:\n" + text[:6000]
    # Always route through the central orchestrator. Text features use Settings AI
    # when configured, otherwise they fall back directly to the deterministic
    # local engine. Embedded Gemini remains dedicated to visual reasoning.
    try:
        from chopster.ai.prompts import CAPTION_SYSTEM, caption_prompt
        from chopster.ai.schemas import validate_caption
        from chopster.ai.provider_interface import AIRequest
        prompt = caption_prompt(text[:12000], platform=platform, tone=tone, language=language, count=count, style=style)
        if highlight_context:
            best = highlight_context[0]
            prompt += (
                f"\n\nVIRAL ANALYZER CONTEXT\nStart: {best.get('start',0)}s\nEnd: {best.get('end',0)}s\n"
                f"Score: {best.get('score',0)}/100\nHook: {best.get('hook','')}\n"
                f"Reason: {best.get('reason','')}\nExcerpt: {best.get('excerpt','')}"
            )
        master_context = config.get("master_context") or {}
        if master_context:
            prompt += "\n\nMASTER EDITOR BRAIN CONTEXT\n" + json.dumps(master_context, ensure_ascii=False)[:5000]
            prompt += "\nGunakan konteks ini untuk membuat judul/caption yang spesifik pada clip, bukan generik."
        from chopster.ai.orchestrator import orchestrator_from_config
        orchestrator = orchestrator_from_config(config)
        req = AIRequest(prompt=prompt, system=CAPTION_SYSTEM, model=config.get("ai_model") or "", temperature=float(config.get("ai_temperature") or .2), max_tokens=max(5000, int(config.get("ai_max_tokens") or 8000)), timeout=int(config.get("ai_timeout") or 90))
        def _local_json():
            return json.dumps(_local_captions(tr_local, platform, tone, count, style, language, highlight_context), ensure_ascii=False)
        tr_local = transcript if isinstance(transcript, Transcript) else Transcript(language=language, segments=[])
        if isinstance(transcript, str):
            from chopster.clipper.transcript_manager import Segment
            tr_local = Transcript(language=language, segments=[Segment(start=0, end=10, text=text[:800])], raw_text=text)
        resp = orchestrator.generate(req, local_fallback=_local_json)
        t = resp.text.strip()
        if "```" in t:
            m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.S)
            if m:
                t = m.group(1)
        data = json.loads(t)
        out = validate_caption(data)
        if isinstance(out, dict):
            raw_meta = resp.raw if isinstance(resp.raw, dict) else {}
            out.setdefault("source", raw_meta.get("provider") or "settings-ai")
            if raw_meta.get("ai_error") and not out.get("ai_error"):
                out["ai_error"] = str(raw_meta.get("ai_error"))[:300]
        return out
    except Exception as exc:
        tr = transcript if isinstance(transcript, Transcript) else Transcript(language=language, segments=[])
        if isinstance(transcript, str):
            from chopster.clipper.transcript_manager import Segment
            tr = Transcript(language=language, segments=[Segment(start=0, end=10, text=text[:800])], raw_text=text)
        res = _local_captions(tr, platform, tone, count, style, language, highlight_context)
        res["source"] = "local-fallback"
        res["ai_error"] = str(exc)[:300]
        return res
