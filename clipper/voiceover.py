"""Optional local voiceover. Uses pyttsx3 if installed; otherwise reports a clear install hint."""
from __future__ import annotations
from pathlib import Path

def generate_voiceover(text: str, output: str|Path, rate: int=170, voice_name: str=""):
    try:
        import pyttsx3
    except Exception as exc:
        raise RuntimeError("Voiceover lokal membutuhkan pyttsx3. Install dependency lalu coba lagi.") from exc
    out=Path(output); out.parent.mkdir(parents=True,exist_ok=True)
    engine=pyttsx3.init()
    engine.setProperty("rate", int(rate))
    if voice_name:
        for v in engine.getProperty("voices"):
            if voice_name.lower() in (getattr(v,"name","") or "").lower(): engine.setProperty("voice",v.id); break
    engine.save_to_file(text,out.as_posix()); engine.runAndWait()
    return out
