"""FFmpeg-based audio enhancement utilities."""
from __future__ import annotations
import subprocess, os
from pathlib import Path
from chopster.downloader.ffmpeg import find_ffmpeg


def enhance_audio(source: str|Path, output: str|Path, mode: str="Voice Clear", cancel_check=None):
    ff=find_ffmpeg()
    if not ff: raise RuntimeError("FFmpeg tidak ditemukan")
    filters={
        "Voice Clear":"highpass=f=80,lowpass=f=12000,afftdn=nf=-25,loudnorm=I=-16:TP=-1.5:LRA=11",
        "Podcast":"highpass=f=70,lowpass=f=14000,afftdn=nf=-22,acompressor=threshold=-18dB:ratio=3:attack=5:release=80,loudnorm=I=-16:TP=-1.5:LRA=11",
        "Shorts":"highpass=f=80,afftdn=nf=-24,acompressor=threshold=-20dB:ratio=3,loudnorm=I=-14:TP=-1.5:LRA=8",
    }
    out=Path(output); out.parent.mkdir(parents=True,exist_ok=True); tmp=out.with_suffix(out.suffix+".tmp")
    cmd=[ff,"-hide_banner","-loglevel","error","-y","-i",str(source),"-af",filters.get(mode,filters["Voice Clear"]),"-c:v","copy","-c:a","aac","-b:a","160k",str(tmp)]
    if cancel_check and cancel_check(): return False,"Dibatalkan"
    kw={"stdout":subprocess.PIPE,"stderr":subprocess.PIPE,"text":True,"encoding":"utf-8","errors":"replace"}
    if os.name=="nt": kw["creationflags"]=getattr(subprocess,"CREATE_NO_WINDOW",0)
    p=subprocess.run(cmd,**kw,timeout=900)
    if p.returncode!=0: return False,(p.stderr or "FFmpeg gagal")[-2000:]
    tmp.replace(out); return True,str(out)
