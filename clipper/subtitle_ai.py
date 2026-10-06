"""AI-assisted transcript cleanup and subtitle segmentation.

The remote model is the optional AI configured in Settings (CleanAPIs/Kimi/Claude/etc.).
Embedded Gemini is intentionally not used for this text task. If Settings AI is blank or
fails, a deterministic local segmentation fallback is returned.
"""
from __future__ import annotations

import hashlib
import json
import re
from difflib import SequenceMatcher
from typing import Any

from chopster.ai.provider_interface import AIRequest
from chopster.clipper.transcript_manager import Segment, Transcript


def _tokens(text: str) -> list[str]:
    return re.findall(r"[\wÀ-ÿ']+", str(text or "").lower())


def _token_similarity(a: str, b: str) -> float:
    aa, bb = _tokens(a), _tokens(b)
    if not aa and not bb:
        return 1.0
    if not aa or not bb:
        return 0.0
    return SequenceMatcher(None, aa, bb).ratio()


def _clean_local(text: str) -> str:
    t = re.sub(r"\s+", " ", str(text or "").strip())
    if not t:
        return ""
    # Safe punctuation normalization only; do not invent words.
    t = re.sub(r"\s+([,.!?;:])", r"\1", t)
    if t and t[-1] not in ".!?…,:;":
        t += "."
    return t


def transcript_prepare_signature(config: dict[str, Any] | None, max_words_per_line: int = 8) -> str:
    cfg = dict(config or {})
    try:
        from chopster.ai.config_helpers import normalize_provider, settings_ai_enabled
        provider = normalize_provider(cfg.get("ai_provider"))
        endpoint = str(cfg.get("ai_endpoint") or "").strip()
        model = str(cfg.get("ai_model") or "").strip()
        has_settings_ai = settings_ai_enabled(provider, endpoint, cfg.get("ai_api_key"))
    except Exception:
        provider = str(cfg.get("ai_provider") or "none").strip().lower()
        endpoint = str(cfg.get("ai_endpoint") or "").strip()
        model = str(cfg.get("ai_model") or "").strip()
        has_settings_ai = provider not in {"", "none", "local"} and bool(endpoint and cfg.get("ai_api_key"))
    payload = {
        "layer": "settings-ai" if has_settings_ai else "local",
        "provider": provider if has_settings_ai else "local",
        "endpoint": endpoint if has_settings_ai else "",
        "model": model if has_settings_ai else "",
        "max_words": int(max_words_per_line or 8),
        "version": 2,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _local_cues(transcript: Transcript, max_words: int = 8) -> list[dict]:
    cues: list[dict] = []
    n = max(1, int(max_words or 8))
    for seg in transcript.segments:
        words = [str(w.get("word", "")).strip() for w in (seg.words or []) if str(w.get("word", "")).strip()]
        if words:
            for i in range(0, len(words), n):
                chunk = words[i:i+n]
                # Match timestamps from the word entries if available.
                w_objs = [w for w in (seg.words or []) if str(w.get("word", "")).strip()][i:i+n]
                cues.append({
                    "start": float(w_objs[0].get("start", seg.start)),
                    "end": float(w_objs[-1].get("end", seg.end)),
                    "text": " ".join(chunk).strip(),
                })
        else:
            text = _clean_local(seg.text)
            if text:
                cues.append({"start": float(seg.start), "end": float(seg.end), "text": text})
    return cues


def _chunk_ranges(total: int, size: int = 60):
    for start in range(0, total, size):
        yield start, min(total, start + size)


def _fallback_chunk_payload(segments: list[Segment], start: int, end: int, max_words: int) -> dict[str, Any]:
    local = Transcript(language="auto", segments=segments[start:end])
    for seg in local.segments:
        seg.text = _clean_local(seg.text)
    local.subtitle_cues = _local_cues(local, max_words=max_words)
    # Convert local segment positions into source-global indices.
    rows = [{"index": start + i, "text": seg.text} for i, seg in enumerate(local.segments)]
    cues = []
    for cue in local.subtitle_cues:
        # Find the smallest global range covering this cue.
        hit = [i for i, seg in enumerate(segments[start:end], start) if float(seg.start) <= float(cue["end"]) and float(seg.end) >= float(cue["start"])]
        if hit:
            cues.append({"start_index": min(hit), "end_index": max(hit), "text": cue["text"]})
    return {"segments": rows, "cues": cues}


def _validate_chunk(data: Any, segments: list[Segment], start: int, end: int) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("AI subtitle result bukan object")
    out_segments = []
    raw_rows = data.get("segments") or []
    if not isinstance(raw_rows, list):
        raise ValueError("segments tidak valid")
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        try:
            idx = int(row.get("index"))
        except Exception:
            continue
        if idx < start or idx >= end:
            continue
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        original = segments[idx].text
        # Keep AI wording only when it stays close to the spoken words.
        if _token_similarity(original, text) < 0.78:
            continue
        out_segments.append({"index": idx, "text": text})
    # If AI omitted too many rows, use local rows for the missing ones.
    seen = {r["index"] for r in out_segments}
    for idx in range(start, end):
        if idx not in seen:
            out_segments.append({"index": idx, "text": _clean_local(segments[idx].text)})
    out_segments.sort(key=lambda r: r["index"])

    cues = []
    for row in data.get("cues") or []:
        if not isinstance(row, dict):
            continue
        try:
            a, b = int(row.get("start_index")), int(row.get("end_index"))
        except Exception:
            continue
        if a < start or b < a or b >= end:
            continue
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        source = " ".join(segments[i].text for i in range(a, b + 1))
        if _token_similarity(source, text) < 0.72:
            continue
        max_words = max(1, len(_tokens(text)))
        if max_words > 14:
            # Keep line lengths under control even if the model ignored the prompt.
            continue
        cues.append({"start_index": a, "end_index": b, "text": text})
    if not cues:
        raise ValueError("AI tidak menghasilkan subtitle cues valid")
    return {"segments": out_segments, "cues": cues}


def prepare_transcript_and_subtitles(transcript: Transcript, config: dict[str, Any], max_words_per_line: int = 8) -> tuple[Transcript, str]:
    """Use the Settings AI to clean transcript text and produce subtitle cues.

    The model may change punctuation/casing and sentence/phrase grouping, but the
    source word content and timestamps remain anchored to Whisper. Returns a copy
    plus a source label ('settings-ai', 'local', or 'mixed').
    """
    out = Transcript.from_dict(transcript.to_dict())
    if not out.segments:
        out.subtitle_cues = []
        out.prepared_by = "local"
        out.prepared_signature = transcript_prepare_signature(config, max_words_per_line)
        return out, "local"

    from chopster.ai.orchestrator import orchestrator_from_config
    orch = orchestrator_from_config(config)
    chunk_size = 60
    all_cues: list[dict] = []
    source_hits: set[str] = set()

    for start, end in _chunk_ranges(len(out.segments), chunk_size):
        payload = [
            {"index": i, "start": float(out.segments[i].start), "end": float(out.segments[i].end), "text": out.segments[i].text}
            for i in range(start, end)
        ]
        prompt = (
            "Rapikan transcript dan susun subtitle untuk video sosial.\n"
            "ATURAN WAJIB:\n"
            "1) Pertahankan kata-kata yang benar-benar diucapkan; jangan menambah fakta atau kalimat baru.\n"
            "2) Boleh memperbaiki kapitalisasi, tanda baca, dan pemenggalan agar enak dibaca.\n"
            f"3) Subtitle cue ideal maksimal {int(max_words_per_line)} kata per cue dan jangan memotong frasa secara aneh.\n"
            "4) Gunakan index segment sumber sebagai anchor timestamp. Jangan membuat timestamp sendiri.\n"
            "5) Untuk setiap cue berikan start_index dan end_index (inclusive).\n"
            "6) Kembalikan JSON SAJA dengan bentuk: {\"segments\":[{\"index\":0,\"text\":\"...\"}],\"cues\":[{\"start_index\":0,\"end_index\":0,\"text\":\"...\"}]}\n\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        local = lambda s=start, e=end: json.dumps(_fallback_chunk_payload(out.segments, s, e, max_words_per_line), ensure_ascii=False)
        try:
            resp = orch.generate(
                AIRequest(
                    prompt=prompt,
                    system="Kamu editor transcript dan subtitle yang sangat presisi. Pertahankan kata yang diucapkan dan jangan mengarang.",
                    model=str(config.get("ai_model") or ""),
                    temperature=0.10,
                    max_tokens=12000,
                    timeout=int(config.get("ai_timeout") or 90),
                ),
                local_fallback=local,
            )
            raw = resp.text
            raw_meta = resp.raw if isinstance(resp.raw, dict) else {}
            chunk_source = "settings-ai" if raw_meta.get("ai_layer") == "settings" else "local"
            if "```" in raw:
                m = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.S | re.I)
                if m:
                    raw = m.group(1).strip()
            data = _validate_chunk(json.loads(raw), out.segments, start, end)
            source_hits.add(chunk_source)
        except Exception:
            data = _fallback_chunk_payload(out.segments, start, end, max_words_per_line)
            source_hits.add("local")
        for row in data["segments"]:
            out.segments[row["index"]].text = row["text"]
        for cue in data["cues"]:
            a, b = cue["start_index"], cue["end_index"]
            out_cue = {
                "start": float(out.segments[a].start),
                "end": float(out.segments[b].end),
                "text": str(cue["text"]).strip(),
            }
            all_cues.append(out_cue)

    merged: list[dict] = []
    for cue in sorted(all_cues, key=lambda x: (float(x["start"]), float(x["end"]))):
        if merged and abs(float(merged[-1]["start"]) - float(cue["start"])) < 0.001 and merged[-1]["text"] == cue["text"]:
            continue
        merged.append(cue)
    out.subtitle_cues = merged
    out.raw_text = " ".join(s.text for s in out.segments)
    if not source_hits or source_hits == {"local"}:
        source = "local"
    elif source_hits == {"settings-ai"}:
        source = "settings-ai"
    else:
        source = "mixed"
    out.prepared_by = source
    out.prepared_signature = transcript_prepare_signature(config, max_words_per_line)
    return out, source
