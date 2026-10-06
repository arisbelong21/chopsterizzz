"""Transcript manager — faster-whisper with CPU/CUDA auto, word timestamps, SRT/VTT export."""
from __future__ import annotations

import json
import gc
import hashlib
import os
import subprocess
import sys
import time
from functools import lru_cache
from threading import RLock

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chopster.app.paths import user_data_dir

_CUDA_PREFLIGHT_CACHE: tuple[bool, str] | None = None
_CUDA_PREFLIGHT_CACHE_KEY: tuple[int, str, str] | None = None
_WHISPER_MODEL_LOAD_LOCK = RLock()
_ACTIVE_WHISPER_MODEL_KEY: tuple[str, str, str, int, int, str] | None = None


@lru_cache(maxsize=1)
def _cached_whisper_model(
    model_name: str,
    device: str,
    compute_type: str,
    cpu_threads: int,
    num_workers: int,
    download_root: str,
):
    """Keep only the last selected model resident to avoid repeated load cost and excess VRAM."""
    from faster_whisper import WhisperModel  # type: ignore

    kwargs: dict[str, Any] = {
        "device": device,
        "compute_type": compute_type,
        "cpu_threads": cpu_threads,
        "num_workers": num_workers,
    }
    if download_root:
        kwargs["download_root"] = download_root
    return WhisperModel(model_name, **kwargs)


def _load_whisper_model(
    model_name: str,
    device: str,
    compute_type: str,
    cpu_threads: int,
    num_workers: int,
    download_root: str = "",
):
    # lru_cache protects its mapping, but serializing the miss prevents concurrent
    # jobs from loading multiple copies of a large CPU/GPU model at once.
    global _ACTIVE_WHISPER_MODEL_KEY
    key = (model_name, device, compute_type, cpu_threads, num_workers, download_root)
    with _WHISPER_MODEL_LOAD_LOCK:
        if _ACTIVE_WHISPER_MODEL_KEY != key:
            _cached_whisper_model.cache_clear()
            gc.collect()
            _ACTIVE_WHISPER_MODEL_KEY = key
        return _cached_whisper_model(
            model_name, device, compute_type, cpu_threads, num_workers, download_root
        )


def clear_whisper_model_cache() -> None:
    """Release the retained model when the user changes model/device or exits."""
    global _ACTIVE_WHISPER_MODEL_KEY
    with _WHISPER_MODEL_LOAD_LOCK:
        _cached_whisper_model.cache_clear()
        _ACTIVE_WHISPER_MODEL_KEY = None
        gc.collect()


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list[dict] = field(default_factory=list)  # {start, end, word}


@dataclass
class Transcript:
    language: str
    segments: list[Segment] = field(default_factory=list)
    raw_text: str = ""
    # AI-prepared subtitle cues: [{start, end, text}]. Optional so old projects remain valid.
    subtitle_cues: list[dict] = field(default_factory=list)
    prepared_by: str = ""
    prepared_signature: str = ""

    def to_dict(self) -> dict:
        return {
            "language": self.language,
            "segments": [{"start": s.start, "end": s.end, "text": s.text, "words": s.words} for s in self.segments],
            "raw_text": self.raw_text or " ".join(s.text for s in self.segments),
            "subtitle_cues": list(self.subtitle_cues or []),
            "prepared_by": self.prepared_by or "",
            "prepared_signature": self.prepared_signature or "",
        }

    @staticmethod
    def from_dict(data: dict) -> "Transcript":
        segs = [Segment(start=s["start"], end=s["end"], text=s["text"], words=s.get("words", [])) for s in data.get("segments", [])]
        cues = data.get("subtitle_cues") if isinstance(data.get("subtitle_cues"), list) else []
        return Transcript(
            language=data.get("language", "auto"),
            segments=segs,
            raw_text=data.get("raw_text", ""),
            subtitle_cues=cues,
            prepared_by=str(data.get("prepared_by") or ""),
            prepared_signature=str(data.get("prepared_signature") or ""),
        )


def _base_transcript_copy(transcript: "Transcript") -> "Transcript":
    base = Transcript.from_dict(transcript.to_dict())
    base.subtitle_cues = []
    base.prepared_by = ""
    base.prepared_signature = ""
    return base


def _windows_cuda_dll_preflight() -> tuple[bool, str]:
    """Check the exact CUDA/cuDNN DLLs that CTranslate2 will need on Windows.

    ``get_supported_compute_types('cuda')`` can succeed even when the later
    cuDNN symbol lookup fails.  That false positive is what can lead to a native
    process crash when WhisperModel is created.  This check only loads the DLLs
    and resolves required symbols; it never creates a CUDA model in the UI process.
    """
    if os.name != "nt":
        return True, "non-Windows"
    try:
        import ctypes
        candidates: list[Path] = []
        path_parts = [Path(x) for x in os.environ.get("PATH", "").split(os.pathsep) if x]
        candidates.extend(path_parts)
        # Common per-user / pip-wheel locations.
        site_roots = []
        try:
            import site
            site_roots.extend(Path(x) for x in site.getsitepackages() if x)
        except Exception:
            pass
        try:
            site_roots.append(Path(__import__('sys').prefix) / 'Lib' / 'site-packages')
        except Exception:
            pass
        for root in site_roots:
            candidates.extend([
                root / 'nvidia' / 'cudnn' / 'bin',
                root / 'nvidia' / 'cublas' / 'bin',
                root / 'nvidia' / 'cuda_runtime' / 'bin',
            ])
        # Common system CUDA/cuDNN locations.
        pf = os.environ.get('ProgramFiles', r'C:\\Program Files')
        candidates.extend([
            Path(pf) / 'NVIDIA' / 'CUDNN',
            Path(pf) / 'NVIDIA GPU Computing Toolkit' / 'CUDA',
        ])
        existing = []
        for c in candidates:
            if c.exists() and c.is_dir():
                try:
                    existing.append(c)
                    os.add_dll_directory(str(c))
                except Exception:
                    pass
        def load_any(names: list[str]):
            last = None
            for name in names:
                try:
                    return ctypes.WinDLL(name), name
                except OSError as exc:
                    last = exc
                # Also try explicit candidate paths to avoid PATH surprises.
                for d in existing:
                    f = d / name
                    if f.exists():
                        try:
                            return ctypes.WinDLL(str(f)), str(f)
                        except OSError as exc:
                            last = exc
            return None, last

        cudnn, err = load_any(['cudnn_ops64_9.dll'])
        if cudnn is None:
            return False, 'cuDNN 9 tidak ditemukan: cudnn_ops64_9.dll'
        if not hasattr(cudnn, 'cudnnCreateTensorDescriptor'):
            return False, 'cuDNN 9 ditemukan tetapi symbol cudnnCreateTensorDescriptor tidak tersedia (versi DLL tidak cocok)'
        cublas, berr = load_any(['cublas64_12.dll'])
        if cublas is None:
            return False, 'cuBLAS CUDA 12 tidak ditemukan: cublas64_12.dll'
        return True, 'cuDNN 9 + cuBLAS CUDA 12 terdeteksi'
    except Exception as exc:
        return False, f'Pemeriksaan DLL CUDA gagal: {exc}'


def _cuda_preflight(timeout: float = 12.0) -> tuple[bool, str]:
    """Safely probe CTranslate2 CUDA support without risking the main process.

    On Windows we first check the concrete cuDNN/cuBLAS DLLs and symbols that
    caused the observed crash. Only after that succeeds do we ask CTranslate2
    for its CUDA compute types in a child process.
    """
    global _CUDA_PREFLIGHT_CACHE, _CUDA_PREFLIGHT_CACHE_KEY
    disable_flag = os.environ.get("CHOPSTER_DISABLE_CUDA", "").strip().lower()
    run_impl = subprocess.run
    run_mod = getattr(run_impl, "__module__", "")
    cache_allowed = run_mod == "subprocess"
    cache_key = (id(run_impl), os.name, disable_flag)
    if cache_allowed and _CUDA_PREFLIGHT_CACHE is not None and _CUDA_PREFLIGHT_CACHE_KEY == cache_key:
        return _CUDA_PREFLIGHT_CACHE
    if disable_flag in {"1", "true", "yes", "on"}:
        _CUDA_PREFLIGHT_CACHE = (False, "CUDA dinonaktifkan (CHOPSTER_DISABLE_CUDA)")
        _CUDA_PREFLIGHT_CACHE_KEY = cache_key if cache_allowed else None
        return _CUDA_PREFLIGHT_CACHE
    if getattr(sys, "frozen", False):
        # A frozen EXE cannot safely run a `python -c ...` child probe: on a frozen
        # build sys.executable IS the application EXE, so that child would launch a
        # second GUI window. Keep the safety guarantee and pick CPU instead of
        # risking a native CUDA init in the EXE process.
        _CUDA_PREFLIGHT_CACHE = (False, "Build EXE: probe CUDA terisolasi tidak tersedia; Device Auto memakai CPU.")
        _CUDA_PREFLIGHT_CACHE_KEY = cache_key if cache_allowed else None
        return _CUDA_PREFLIGHT_CACHE
    if os.name == "nt":
        ok, detail = _windows_cuda_dll_preflight()
        if not ok:
            _CUDA_PREFLIGHT_CACHE = (False, detail)
            _CUDA_PREFLIGHT_CACHE_KEY = cache_key if cache_allowed else None
            return _CUDA_PREFLIGHT_CACHE
    code = (
        "import ctranslate2\n"
        "types = ctranslate2.get_supported_compute_types('cuda')\n"
        "print(','.join(sorted(str(x) for x in types)))\n"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=max(2.0, float(timeout)),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        _CUDA_PREFLIGHT_CACHE = (False, f"CUDA preflight gagal dijalankan: {exc}")
        _CUDA_PREFLIGHT_CACHE_KEY = cache_key if cache_allowed else None
        return _CUDA_PREFLIGHT_CACHE
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "proses CUDA berhenti").strip().splitlines()
        detail = detail[-1] if detail else f"kode {proc.returncode}"
        _CUDA_PREFLIGHT_CACHE = (False, detail[:300])
        _CUDA_PREFLIGHT_CACHE_KEY = cache_key if cache_allowed else None
        return _CUDA_PREFLIGHT_CACHE
    supported = (proc.stdout or "").strip()
    if not supported:
        _CUDA_PREFLIGHT_CACHE = (False, "CTranslate2 tidak melaporkan compute type CUDA")
        _CUDA_PREFLIGHT_CACHE_KEY = cache_key if cache_allowed else None
        return _CUDA_PREFLIGHT_CACHE
    _CUDA_PREFLIGHT_CACHE = (True, supported)
    _CUDA_PREFLIGHT_CACHE_KEY = cache_key if cache_allowed else None
    return _CUDA_PREFLIGHT_CACHE


def _resolve_device(requested: str = "auto") -> str:
    req = (requested or "auto").strip().lower()
    if req == "cpu":
        return "cpu"
    ok, detail = _cuda_preflight()
    if req == "cuda":
        if ok:
            return "cuda"
        raise RuntimeError(f"CUDA/cuDNN tidak siap: {detail}. Pilih Device=Auto atau CPU.")
    # Auto only selects CUDA after the exact Windows DLL/symbol check and the
    # isolated CTranslate2 probe. Missing/mismatched cuDNN therefore means CPU,
    # never a native CUDA initialization inside the main application process.
    return "cuda" if ok else "cpu"


def _transcribe_runtime_profile(duration: float, model_name: str, device_resolved: str, word_timestamps: bool) -> dict[str, Any]:
    duration = float(duration or 0.0)
    cpu = str(device_resolved or "cpu") == "cpu"
    long_form = duration >= 20 * 60
    very_long = duration >= 45 * 60
    model_low = str(model_name or "").lower()
    heavy_model = model_low in {"medium", "large-v2", "large-v3"}
    cpu_threads = max(2, min(8, (os.cpu_count() or 4)))
    num_workers = 1
    if not word_timestamps and cpu and not very_long:
        num_workers = 2
    if cpu and long_form:
        beam_size = 1
        best_of = 1
        vad_parameters = {"min_silence_duration_ms": 320, "speech_pad_ms": 120}
    elif cpu:
        beam_size = 2
        best_of = 2
        vad_parameters = {"min_silence_duration_ms": 380, "speech_pad_ms": 150}
    else:
        beam_size = 3
        best_of = 3
        vad_parameters = {"min_silence_duration_ms": 420, "speech_pad_ms": 180}
    note = "balanced"
    if cpu and long_form:
        note = "fast-long-cpu"
    elif cpu and heavy_model and word_timestamps:
        note = "cpu-word-timestamps"
    elif not cpu:
        note = "gpu"
    return {
        "cpu_threads": cpu_threads,
        "num_workers": num_workers,
        "beam_size": beam_size,
        "best_of": best_of,
        "vad_parameters": vad_parameters,
        "note": note,
    }

def transcribe(
    path: str | Path,
    model: str = "tiny",
    model_dir: str = "",
    language: str = "auto",
    device: str = "auto",
    word_timestamps: bool = False,
    progress_cb=None,
) -> Transcript:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(str(p))
    try:
        device_resolved = _resolve_device(device)
    except RuntimeError as exc:
        if (device or "auto").strip().lower() == "cuda":
            if progress_cb:
                progress_cb(8, f"GPU tidak aman dipakai — beralih ke CPU. ({exc})")
            device_resolved = "cpu"
        else:
            raise
    compute_type = "float16" if device_resolved == "cuda" else "int8"
    duration = 0.0
    try:
        from chopster.clipper.media_probe import probe as _probe
        duration = float(_probe(p).duration)
    except Exception:
        duration = 0.0
    profile = _transcribe_runtime_profile(duration, model, device_resolved, bool(word_timestamps))
    if progress_cb:
        progress_cb(2, "Transkripsi: memeriksa Faster-Whisper…")
    try:
        from faster_whisper import WhisperModel  # type: ignore  # verify optional runtime is installed
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisper belum terpasang. Jalankan: pip install faster-whisper\n"
            "Atau install Whisper lain yang kompatibel."
        ) from exc
    model_name = model.strip() or "tiny"
    model_load_kwargs: dict[str, Any] = {
        "device": device_resolved,
        "compute_type": compute_type,
        "cpu_threads": int(profile["cpu_threads"]),
        "num_workers": int(profile["num_workers"]),
    }
    if model_dir and Path(model_dir).exists():
        cand = Path(model_dir) / model_name
        if cand.exists():
            model_name = str(cand)
        else:
            model_load_kwargs["download_root"] = str(Path(model_dir))
    model_download_root = str(model_load_kwargs.get("download_root") or "")
    try:
        if progress_cb:
            extra = " • mode cepat video panjang" if profile["note"] == "fast-long-cpu" else (" • word timestamps aktif" if bool(word_timestamps) else "")
            progress_cb(8, f"Transkripsi: memuat model {model_name} ({device_resolved}){extra}…")
        whisper = _load_whisper_model(
            model_name,
            device_resolved,
            compute_type,
            int(profile["cpu_threads"]),
            int(profile["num_workers"]),
            model_download_root,
        )
    except Exception as exc:
        if device_resolved == "cuda":
            if progress_cb:
                progress_cb(10, f"GPU gagal dipakai — beralih ke CPU. ({str(exc)[:180]})")
            try:
                cpu_profile = _transcribe_runtime_profile(duration, model_name, "cpu", bool(word_timestamps))
                whisper = _load_whisper_model(
                    model_name,
                    "cpu",
                    "int8",
                    int(cpu_profile["cpu_threads"]),
                    int(cpu_profile["num_workers"]),
                    model_download_root,
                )
                device_resolved = "cpu"
                profile = cpu_profile
            except Exception as cpu_exc:
                raise RuntimeError(f"Gagal memuat model Whisper di GPU dan CPU: GPU={exc}; CPU={cpu_exc}") from cpu_exc
        else:
            raise RuntimeError(f"Gagal memuat model Whisper '{model_name}': {exc}") from exc

    transcribe_input = p
    try:
        from chopster.clipper.analysis_cache import source_signature
        audio_cache = (user_data_dir() / "cache" / "audio") / f"{source_signature(p)}.wav"
        if audio_cache.exists() and audio_cache.stat().st_size > 44:
            transcribe_input = audio_cache
    except Exception:
        transcribe_input = p

    lang = None if language in ("auto", "", None) else language
    if progress_cb:
        detail = f"threads={profile['cpu_threads']} • beam={profile['beam_size']}"
        if bool(word_timestamps):
            detail += " • word timestamps"
        progress_cb(15, f"Transkripsi: model siap, membaca audio… ({detail})")
    segments_iter, info = whisper.transcribe(
        str(transcribe_input),
        language=lang,
        word_timestamps=bool(word_timestamps),
        vad_filter=True,
        vad_parameters=profile["vad_parameters"],
        beam_size=int(profile["beam_size"]),
        best_of=int(profile["best_of"]),
        temperature=0.0,
        condition_on_previous_text=False,
    )
    transcript = Transcript(language=info.language if hasattr(info, "language") else (language or "auto"))
    count = 0
    for seg in segments_iter:
        count += 1
        words = []
        if word_timestamps and hasattr(seg, "words") and seg.words:
            for w in seg.words:
                words.append({"start": float(w.start), "end": float(w.end), "word": w.word})
        seg_end = float(seg.end)
        transcript.segments.append(Segment(start=float(seg.start), end=seg_end, text=seg.text.strip(), words=words))
        if progress_cb and duration > 0:
            pct = min(99, max(16, int(seg_end / duration * 82) + 16))
            progress_cb(pct, f"Transkripsi: {pct}% — {count} segmen")
    transcript.raw_text = " ".join(s.text for s in transcript.segments)
    if progress_cb:
        progress_cb(100, f"Transkripsi selesai — {len(transcript.segments)} segmen")
    return transcript


def transcript_to_srt(transcript: Transcript, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    def fmt(t: float) -> str:
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        sec = int(t % 60)
        ms = int((t - int(t)) * 1000)
        return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"
    cues = list(getattr(transcript, "subtitle_cues", []) or [])
    lines: list[str] = []
    if cues:
        for i, cue in enumerate(cues, 1):
            st=float(cue.get("start", 0.0)); en=float(cue.get("end", st))
            text=str(cue.get("text") or "").strip()
            if not text or en <= st:
                continue
            lines += [str(i), f"{fmt(st)} --> {fmt(en)}", text, ""]
    else:
        for i, seg in enumerate(transcript.segments, 1):
            lines += [str(i), f"{fmt(seg.start)} --> {fmt(seg.end)}", seg.text, ""]
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def transcript_to_vtt(transcript: Transcript, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    def fmt(t: float) -> str:
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = int(t % 60)
        ms = int((t - int(t)) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"
    lines: list[str] = ["WEBVTT", ""]
    for seg in transcript.segments:
        lines.append(f"{fmt(seg.start)} --> {fmt(seg.end)}")
        lines.append(seg.text)
        lines.append("")
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def save_transcript_json(transcript: Transcript, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(transcript.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def transcript_cache_key(path: str | Path, model: str, language: str, word_timestamps: bool) -> str:
    """Stable cache key for a source + transcription configuration."""
    p = Path(path).resolve()
    st = p.stat()
    payload = f"{p}|{st.st_size}|{st.st_mtime_ns}|{model}|{language}|{int(bool(word_timestamps))}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def transcript_cache_path(path: str | Path, model: str, language: str, word_timestamps: bool) -> Path:
    d = user_data_dir() / "cache" / "transcripts"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{transcript_cache_key(path, model, language, word_timestamps)}.json"


def load_cached_transcript(path: str | Path, model: str, language: str, word_timestamps: bool) -> Transcript | None:
    try:
        cache = transcript_cache_path(path, model, language, word_timestamps)
        if not cache.exists():
            return None
        return _base_transcript_copy(Transcript.from_dict(json.loads(cache.read_text(encoding="utf-8"))))
    except Exception:
        return None


def save_cached_transcript(path: str | Path, model: str, language: str, word_timestamps: bool, transcript: Transcript) -> Path:
    cache = transcript_cache_path(path, model, language, word_timestamps)
    cache.write_text(json.dumps(_base_transcript_copy(transcript).to_dict(), ensure_ascii=False), encoding="utf-8")
    return cache


def delete_cached_transcript(path: str | Path, model: str, language: str, word_timestamps: bool) -> bool:
    """Delete exactly one transcription cache entry for the selected source/settings."""
    cache = transcript_cache_path(path, model, language, word_timestamps)
    try:
        cache.unlink(missing_ok=True)
        return True
    except Exception:
        return False
