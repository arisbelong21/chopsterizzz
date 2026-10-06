"""ASS subtitle generation + helpers."""
from __future__ import annotations

from pathlib import Path
import re

from chopster.clipper.subtitle_templates import SubtitleStyle


def _ass_header(style: SubtitleStyle) -> str:
    return f"""[Script Info]
Title: {style.name}
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{style.font},{style.font_size},{style.text_color},{style.highlight_color},{style.outline_color},{style.back_color},{-1 if style.bold else 0},{-1 if style.italic else 0},0,0,100,100,0,0,1,{style.outline},{style.shadow},{style.alignment},24,24,{style.margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _fmt_ass_time(sec: float) -> str:
    sec = max(0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    cs = int((sec - int(sec)) * 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape_text(text: str) -> str:
    return (str(text or "").replace("\\", r"\\").replace("{", r"\\{").replace("}", r"\\}").replace("\n", r"\\N"))


def _chunk_words(words: list[dict], max_words: int) -> list[tuple[float, float, str, list[dict]]]:
    cleaned=[]
    for w in words:
        try:
            ws=float(w.get("start", 0.0)); we=float(w.get("end", ws)); wt=str(w.get("word", "")).strip()
        except Exception:
            continue
        if wt and we>ws:
            cleaned.append({"start":ws,"end":we,"word":wt})
    if not cleaned:
        return []
    n=max(1,int(max_words or 8))
    out=[]
    for i in range(0,len(cleaned),n):
        group=cleaned[i:i+n]
        out.append((float(group[0]["start"]), float(group[-1]["end"]), " ".join(x["word"] for x in group), group))
    return out


def _line_cues(seg, style: SubtitleStyle) -> list[tuple[float,float,str,list[dict]]]:
    words=list(getattr(seg,"words",[]) or [])
    if words:
        chunks=_chunk_words(words, style.max_words_per_line)
        if chunks:
            return chunks
    txt=str(getattr(seg,"text","")).strip()
    if not txt:
        return []
    return [(float(seg.start), float(seg.end), txt, [])]


def _animation_tags(style: SubtitleStyle, start: float, end: float, text: str, words: list[dict]) -> str:
    a=str(style.animation or "none").lower()
    bs="\\"
    if a=="fade":
        return f"{{{bs}fad(120,120)}}"
    if a in {"pop", "word_pop"}:
        return f"{{{bs}t(0,110,{bs}fscx120{bs}fscy120){bs}t(110,230,{bs}fscx100{bs}fscy100)}}"
    if a=="bounce":
        return f"{{{bs}t(0,100,{bs}fscy128){bs}t(100,230,{bs}fscy100){bs}t(230,330,{bs}fscy108){bs}t(330,430,{bs}fscy100)}}"
    # \\move uses a stable bottom-center default. This remains valid even when the
    # player chooses another alignment because the style itself still controls layout.
    if a in {"slide_up", "slide_left", "slide_right"}:
        if int(style.alignment) in (1,4,7):
            x="180"
        elif int(style.alignment) in (3,6,9):
            x="1740"
        else:
            x="960"
        y=str(max(120,1080-int(style.margin_v)))
        if a=="slide_up":
            return f"{{{bs}move({x},{int(y)+90},{x},{y},0,220)}}"
        if a=="slide_left":
            return f"{{{bs}move({int(x)+120},{y},{x},{y},0,240)}}"
        return f"{{{bs}move({int(x)-120},{y},{x},{y},0,240)}}"
    if a=="typewriter":
        if words:
            tags=[]
            count=len(words)
            for i,w in enumerate(words):
                delay=max(50,int(((i+1)/max(1,count))*180))
                tags.append(f"{{{bs}alpha&HFF&{bs}t(0,{delay},{bs}alpha&H00&)}}{_ass_escape_text(w['word'])}")
            return " ".join(tags)
        # Word timestamps are optional. Reveal plain cue text character-by-
        # character when only segment timing is available.
        chars=re.findall(r"\\N|.", text, flags=re.S)
        count=max(1,len(chars)); duration=max(120,min(900,int(max(0.1,end-start)*250)))
        return "".join(
            f"{{{bs}alpha&HFF&{bs}t(0,{max(40,int((i+1)*duration/count))},{bs}alpha&H00&)}}{char}"
            for i,char in enumerate(chars)
        )
    if a=="keyword" and not words:
        return f"{{{bs}c{style.highlight_color}}}"
    if a=="karaoke" and not words:
        return f"{{{bs}fad(80,80)}}"
    return ""


def _word_pop_events(style: SubtitleStyle, words: list[dict]) -> list[str]:
    bs="\\"; out=[]
    for w in words:
        a=max(0.0,float(w["start"])); b=max(a+0.05,float(w["end"]))
        text=_ass_escape_text(w["word"])
        tags=f"{{{bs}c{style.highlight_color}{bs}t(0,90,{bs}fscx125{bs}fscy125){bs}t(90,180,{bs}fscx100{bs}fscy100)}}"
        out.append((a,b,tags+text))
    return out


def build_ass(transcript, style: SubtitleStyle, out_path: str | Path) -> Path:
    """Build a rich ASS subtitle track from transcript segments and optional word timestamps."""
    p=Path(out_path); p.parent.mkdir(parents=True,exist_ok=True)
    lines=[_ass_header(style)]
    mode=str(getattr(style,"subtitle_mode","line") or "line").lower()
    animation=str(style.animation or "none").lower()

    subtitle_cues = list(getattr(transcript, "subtitle_cues", []) or [])
    if mode == "line" and subtitle_cues:
        for cue in subtitle_cues:
            st=float(cue.get("start", 0.0)); en=float(cue.get("end", st)); esc=_ass_escape_text(str(cue.get("text") or "").strip())
            if not esc or en <= st:
                continue
            tags=_animation_tags(style,st,en,esc,[])
            event_text=tags if animation=="typewriter" else tags+esc
            lines.append(f"Dialogue: 0,{_fmt_ass_time(st)},{_fmt_ass_time(en)},Default,,0,0,0,,{event_text}")
        p.write_text("\n".join(lines)+"\n",encoding="utf-8")
        return p

    for seg in transcript.segments:
        words=list(getattr(seg,"words",[]) or [])
        if mode=="word" and words:
            for st,en,txt in _word_pop_events(style, words) if animation in {"word_pop","pop"} else [(float(w["start"]),float(w["end"]),_ass_escape_text(w["word"])) for w in words]:
                lines.append(f"Dialogue: 1,{_fmt_ass_time(st)},{_fmt_ass_time(en)},Default,,0,0,0,,{txt}")
            continue

        if mode=="karaoke" and words:
            parts=[]
            for w in words:
                dur_cs=max(1,int((float(w["end"])-float(w["start"])) * 100))
                parts.append(f"{{\\k{dur_cs}}}{_ass_escape_text(w['word'])}")
            txt=" ".join(parts)
            if animation=="typewriter":
                txt=_animation_tags(style,float(seg.start),float(seg.end),txt,words)
            elif animation in {"fade","pop","bounce","word_pop"}:
                txt=_animation_tags(style,float(seg.start),float(seg.end),txt,words)+txt
            lines.append(f"Dialogue: 0,{_fmt_ass_time(float(seg.start))},{_fmt_ass_time(float(seg.end))},Default,,0,0,0,,{txt}")
            continue

        cues=_line_cues(seg,style)
        for st,en,txt,chunk_words in cues:
            esc=_ass_escape_text(txt)
            if animation=="keyword" and chunk_words:
                parts=[]
                for idx,w in enumerate(chunk_words):
                    word=_ass_escape_text(w["word"])
                    if idx % 4 == 0:
                        parts.append(f"{{\\c{style.highlight_color}}}{word}{{\\c{style.text_color}}}")
                    else:
                        parts.append(word)
                esc=" ".join(parts)
            elif animation=="word_pop" and chunk_words:
                # One compact line with timed emphasis across each word.
                parts=[]
                for idx,w in enumerate(chunk_words):
                    word=_ass_escape_text(w["word"])
                    dur=max(1,int((float(w["end"])-float(w["start"])) * 100))
                    parts.append(f"{{\\k{dur}}}{word}")
                esc=" ".join(parts)
            elif animation=="karaoke" and chunk_words:
                parts=[]
                for w in chunk_words:
                    dur=max(1,int((float(w["end"])-float(w["start"])) * 100))
                    parts.append(f"{{\\k{dur}}}{_ass_escape_text(w['word'])}")
                esc=" ".join(parts)
            tags=_animation_tags(style,st,en,esc,chunk_words)
            # For typewriter, _animation_tags already contains the revealed text.
            if animation=="typewriter":
                final=tags
            else:
                final=tags+esc
            lines.append(f"Dialogue: 0,{_fmt_ass_time(st)},{_fmt_ass_time(en)},Default,,0,0,0,,{final}")
    p.write_text("\n".join(lines)+"\n",encoding="utf-8")
    return p


def build_srt_from_transcript(transcript, out_path: str | Path) -> Path:
    from chopster.clipper.transcript_manager import transcript_to_srt
    return transcript_to_srt(transcript,out_path)
