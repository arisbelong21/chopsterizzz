"""Persistent, versioned analysis cache for Chopster.

Transcript/scene artifacts can survive camera algorithm upgrades. Camera and master
artifacts carry their own implementation version so a framing upgrade does not force
an expensive retranscription.
"""
from __future__ import annotations
import hashlib, json, time
from pathlib import Path
from typing import Any

CACHE_VERSION = 4
ARTIFACT_VERSIONS = {
    "transcript": 3,
    "scenes": 3,
    "camera": 7,
    "master": 7,
}


def source_signature(path: str | Path) -> str:
    p = Path(path).resolve(); st = p.stat()
    raw = f"{p}|{st.st_size}|{st.st_mtime_ns}".encode()
    return hashlib.sha256(raw).hexdigest()[:24]


def analysis_dir(project_dir: str | Path) -> Path:
    d = Path(project_dir) / "analysis"
    d.mkdir(parents=True, exist_ok=True)
    return d


def artifact_path(project_dir: str | Path, name: str) -> Path:
    return analysis_dir(project_dir) / f"{name}.json"


def _accepted_versions(name: str) -> set[tuple[int, int | None]]:
    expected = int(ARTIFACT_VERSIONS.get(name, 1))
    # Legacy v3 files remain readable for transcript/scenes, while old camera/master
    # artifacts are intentionally invalidated after the stable-framing rewrite.
    if name in ("transcript", "scenes"):
        return {(CACHE_VERSION, expected), (3, None), (2, None)}
    return {(CACHE_VERSION, expected)}


def load_artifact(project_dir: str | Path, name: str, source_path: str | Path | None = None) -> Any | None:
    p = artifact_path(project_dir, name)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        current = int(data.get("cache_version") or 0)
        artifact_version = data.get("artifact_version")
        accepted = _accepted_versions(name)
        valid = (current, int(artifact_version) if artifact_version is not None else None) in accepted
        if current in (2, 3) and name in ("transcript", "scenes"):
            valid = True
        if not valid:
            return None
        if source_path is not None and data.get("source_signature") != source_signature(source_path):
            return None
        return data.get("data")
    except Exception:
        return None


def save_artifact(project_dir: str | Path, name: str, data: Any, source_path: str | Path | None = None) -> Path:
    p = artifact_path(project_dir, name)
    payload = {
        "cache_version": CACHE_VERSION,
        "artifact_version": int(ARTIFACT_VERSIONS.get(name, 1)),
        "created_at": time.time(),
        "source_signature": source_signature(source_path) if source_path else None,
        "data": data,
    }
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
    return p


def status(project_dir: str | Path, source_path: str | Path) -> dict[str, bool]:
    return {n: load_artifact(project_dir, n, source_path) is not None
            for n in ("transcript", "scenes", "camera", "master")}
