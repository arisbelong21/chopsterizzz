"""Highlight analyzer — transcript heuristic + optional AI refinement."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from chopster.clipper.transcript_manager import Transcript
from chopster.clipper.advanced_features import format_timecode
from chopster.ai.schemas import validate_highlight

# Heuristic keywords
HOOK_PATTERNS = [r"\b(kenapa|mengapa|bagaimana|rahasia|jangan|ternyata|fakta|viral|menyesal|penting)\b", r"\b(wow|gila|luar biasa|mengejutkan|heboh)\b"]
QUESTION_RE = re.compile(r"\?")
EXCLAMATION_RE = re.compile(r"!")
STRONG_RE = re.compile(r"\b(sangat|sekali|paling|ter.*|jangan|harus|wajib|gratis|mudah|cepat)\b", re.I)

@dataclass
class Candidate:
    title: str
    start: float
    end: float
    excerpt: str
    reason: str
    hook: str
    context_required: str
    weaknesses: str
    score: int
    source: str = "heuristic"  # heuristic | ai | combined

    @property
    def duration(self) -> float:
        return max(0, self.end - self.start)


def _score_segment(seg, transcript: Transcript, duration: float) -> tuple[int, str, str]:
    text = seg.text.strip()
    low = text.lower()
    score = 30
    reasons: list[str] = []
    if QUESTION_RE.search(text): score += 12; reasons.append("pertanyaan yang mendorong rasa ingin tahu")
    if EXCLAMATION_RE.search(text): score += 6; reasons.append("emosi/penekanan")
    if any(re.search(p, text, re.I) for p in HOOK_PATTERNS): score += 10; reasons.append("kata hook kuat")
    if STRONG_RE.search(text): score += 8; reasons.append("pernyataan tegas")
    if re.search(r"\b\d+(?:[.,]\d+)?\b", text): score += 5; reasons.append("angka/fakta spesifik")
    if re.search(r"\b(tapi|namun|ternyata|justru|padahal|sebenarnya|ternyata)\b", low): score += 6; reasons.append("kontras/kejutan")
    if re.search(r"\b(aku|kamu|kalian|orang|semua|siapa|apa|mengapa|kenapa)\b", low): score += 3
    if 3 <= (seg.end-seg.start) <= 12: score += 5
    if len(text) >= 55: score += 4
    if seg.start < duration * .12: score += 4; reasons.append("bagian awal video")
    # reward segments surrounded by supporting context
    neighbor_count=sum(1 for s in transcript.segments if s.start>=max(0,seg.start-12) and s.end<=min(duration,seg.end+12))
    if neighbor_count>=3: score += 4; reasons.append("punya konteks pendukung")
    hook = text[:100].strip() + ("…" if len(text)>100 else "")
    reason = "; ".join(reasons) or "isi transcript cukup jelas untuk dipotong"
    return max(0,min(100,score)), reason, hook


def analyze_transcript(transcript: Transcript, duration: float, top_k: int = 8) -> list[Candidate]:
    """Transcript-first potential-virality heuristic. It does not predict actual views."""
    if not transcript.segments or duration <= 0:return []
    scored=[]
    for seg in transcript.segments:
        score,reason,hook=_score_segment(seg,transcript,duration); scored.append((score,seg,reason,hook))
    scored.sort(key=lambda x:x[0],reverse=True)
    candidates=[]; used=[]
    for score,seg,reason,hook in scored:
        if len(candidates)>=top_k:break
        # default Shorts window: 30s, aligned around the high-scoring segment
        target=30.0
        center=(seg.start+seg.end)/2
        start=max(0.0,center-target/2); end=min(duration,start+target)
        if end-start<target and start>0:start=max(0,end-target)
        overlap=False
        for us,ue in used:
            ov=max(0,min(end,ue)-max(start,us))
            if ov>target*.55: overlap=True; break
        if overlap:continue
        contained=[x for x in transcript.segments if x.start<end and x.end>start]
        excerpt=" ".join(x.text.strip() for x in contained)[:260]
        # Score the full window lightly; avoid making long windows look artificially perfect
        density=min(8,len(contained))
        window_score=min(100,score+density)
        candidates.append(Candidate(
            title=f"{format_timecode(start)}–{format_timecode(end)} • {hook[:45]}",
            start=start,end=end,excerpt=excerpt or seg.text[:260],
            reason=reason,hook=hook,
            context_required="Cek apakah setup → payoff masih lengkap di window ini.",
            weaknesses="Skor hanya heuristic transcript; performa aktual bergantung pada penonton/platform.",
            score=int(window_score),source="heuristic"))
        used.append((start,end))
    return sorted(candidates,key=lambda c:c.score,reverse=True)

def _sample_transcript_lines(transcript: Transcript, max_segments: int = 180) -> str:
    """Cover the full video instead of truncating to the first N transcript segments."""
    rows = list(transcript.segments or [])
    if not rows:
        return ""
    if len(rows) <= max_segments:
        chosen = rows
    else:
        idxs=[]
        for i in range(max_segments):
            idx=round(i*(len(rows)-1)/max(1,max_segments-1))
            if not idxs or idx != idxs[-1]:
                idxs.append(idx)
        chosen=[rows[i] for i in idxs]
    return "\n".join(f"[{s.start:.1f}-{s.end:.1f}] {s.text}" for s in chosen)


def _scope_candidates(items: list[Candidate], scope_start: float, scope_end: float, top_k: int = 10) -> list[Candidate]:
    scoped=[]
    for c in items or []:
        start=max(float(scope_start), float(c.start))
        end=min(float(scope_end), float(c.end))
        if end - start < 3.0:
            continue
        scoped.append(Candidate(
            title=f"{format_timecode(start)}–{format_timecode(end)} • {c.hook[:45]}",
            start=start,end=end,excerpt=c.excerpt,reason=c.reason,hook=c.hook,
            context_required=c.context_required,weaknesses=c.weaknesses,score=int(c.score),source=c.source,
        ))
    return _dedupe_candidates(scoped, top_k=top_k)


def _dedupe_candidates(items: list[Candidate], top_k: int = 10) -> list[Candidate]:
    ordered=sorted(items,key=lambda c:int(c.score),reverse=True)
    out=[]
    for c in ordered:
        if c.end <= c.start:
            continue
        too_close=False
        for e in out:
            overlap=max(0.0,min(c.end,e.end)-max(c.start,e.start))
            union=max(c.end,e.end)-min(c.start,e.start)
            if union>0 and overlap/union >= 0.65:
                too_close=True; break
        if not too_close:
            out.append(c)
        if len(out)>=top_k:
            break
    return out


def _parse_ai_candidates(resp_text: str, duration: float) -> list[Candidate]:
    text=(resp_text or "").strip()
    if "```" in text:
        m=re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```",text,re.S|re.I)
        if m:text=m.group(1)
    data=json.loads(text)
    if isinstance(data,list):
        data={"candidates":data}
    elif isinstance(data,dict) and "candidates" not in data and "highlights" in data:
        # The visual review prompt uses `highlights`; the shared validator uses
        # `candidates`. Normalize the envelope, never bypass field validation.
        data={**data,"candidates":data["highlights"]}
    validated=validate_highlight(data,duration)
    return [Candidate(title=v["title"],start=v["start"],end=v["end"],excerpt=v["excerpt"],reason=v["reason"],hook=v["hook"],context_required=v["context_required"],weaknesses=v["weaknesses"],score=v["score"],source="ai") for v in validated]


def analyze_with_ai(transcript: Transcript, duration: float, config: dict) -> list[Candidate]:
    """Analyze the entire timeline through the central AI Orchestrator.

    Every AI call shares the same circuit breaker and local fallback. Master
    Analysis context is included when available so the model sees the same
    evidence bus used by Camera Director and Content Pack.
    """
    from chopster.ai.orchestrator import orchestrator_from_config
    from chopster.ai.prompts import HIGHLIGHT_SYSTEM, highlight_prompt
    from chopster.ai.provider_interface import AIRequest

    rows=list(transcript.segments or [])
    if not rows:
        return []
    orch=orchestrator_from_config(config)
    lang=config.get("transcribe_language") or config.get("language") or "id"
    instruction=str(config.get("ai_instruction") or "")
    master_context=config.get("master_context") or {}
    chunk_size=90; overlap=8; all_candidates=[]
    starts=list(range(0,len(rows),max(1,chunk_size-overlap)))

    for ci,begin in enumerate(starts):
        chunk=rows[begin:min(len(rows),begin+chunk_size)]
        if not chunk: continue
        text="\n".join(f"[{x.start:.1f}-{x.end:.1f}] {x.text}" for x in chunk)
        if len(text)>15000:
            text=text[:15000]
        scope_start=float(chunk[0].start); scope_end=float(chunk[-1].end)
        master_hint=json.dumps(master_context,ensure_ascii=False)[:5000] if master_context else "{}"
        prompt=highlight_prompt(text,duration,language=lang,instruction=instruction,scope_start=scope_start,scope_end=scope_end,chunk_index=ci+1,chunk_count=len(starts))
        prompt += ("\n\nMASTER EDITOR BRAIN CONTEXT:\n" + master_hint +
                   "\nUse this only as editorial guidance; timestamps must remain inside the supplied chunk.")
        req=AIRequest(prompt=prompt,system=HIGHLIGHT_SYSTEM,model=config.get("ai_model") or "",temperature=0.2,max_tokens=8192,timeout=int(config.get("ai_timeout") or 60))

        def local_json(chunk_rows=chunk, scope_a=scope_start, scope_b=scope_end):
            # Reuse deterministic local scorer for this chunk when the remote AI
            # is unavailable, but keep every clip strictly inside the current chunk.
            local_tr=Transcript(language=transcript.language,segments=list(chunk_rows),raw_text=" ".join(x.text for x in chunk_rows))
            local=_scope_candidates(analyze_transcript(local_tr, duration, top_k=4), scope_a, scope_b, top_k=4)
            return json.dumps([c.__dict__ for c in local], ensure_ascii=False)

        try:
            resp=orch.generate(req, local_fallback=local_json)
            parsed=_scope_candidates(_parse_ai_candidates(resp.text,duration), scope_start, scope_end, top_k=6)
            all_candidates.extend(parsed)
        except Exception:
            # The orchestrator normally returns local fallback already; keep a
            # final hard fallback to the whole-transcript heuristic.
            continue

    local_coverage=analyze_transcript(transcript,duration,top_k=12)
    if not all_candidates:
        return local_coverage
    candidates=_dedupe_candidates(all_candidates+local_coverage,top_k=20)

    # Targeted visual second pass through the same orchestrator.
    if candidates and config.get("source_path") and bool(config.get("ai_auto_visual",True)):
        try:
            from chopster.clipper.visual_analyzer import extract_frame_images
            top=candidates[:6]; marks=[]
            for c in top:
                marks += [max(0.0,min(float(duration),float(c.start))), max(0.0,min(float(duration),float(c.end)))]
            seen=set(); marks=[m for m in marks if not (round(m,1) in seen or seen.add(round(m,1)))][:8]
            root,imgs=extract_frame_images(config["source_path"],marks,max_width=480)
            try:
                if imgs:
                    candidate_text="\n".join(f"#{i+1} {format_timecode(c.start)}–{format_timecode(c.end)} score={c.score} hook={c.hook}" for i,c in enumerate(top))
                    vp=("Verifikasi kandidat highlight dari seluruh timeline menggunakan transcript dan frame video. "
                        "Periksa reaksi wajah, siapa yang sedang aktif berbicara, perubahan scene, objek dan payoff. "
                        "Jangan mengubah timestamp dan jangan membuat timestamp baru. Kembalikan JSON highlights dengan field yang sama.\n\nKANDIDAT:\n"+candidate_text)
                    vresp=orch.generate_vision(vp,[str(x) for x in imgs],system=HIGHLIGHT_SYSTEM,model=config.get("ai_model") or "",temperature=0.15,max_tokens=8192,timeout=int(config.get("ai_timeout") or 60),local_fallback=lambda: '{"candidates": []}')
                    visual=_parse_ai_candidates(vresp.text,duration) if vresp.text.strip() else []
                    if visual:
                        for c in visual: c.source="ai+vision"
                        candidates=_dedupe_candidates(visual+top,top_k=10)
            finally:
                import shutil; shutil.rmtree(root,ignore_errors=True)
        except Exception:
            pass
    return candidates[:10]

