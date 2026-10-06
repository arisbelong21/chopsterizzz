"""Project manager — filesystem + DB index for Clipper projects."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from chopster.app.paths import user_data_dir


@dataclass
class ClipEntry:
    index: int
    start: float
    end: float
    title: str = ""
    status: str = "planned"  # planned | exported | failed
    output: str = ""
    selected: bool = True

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class Project:
    id: str
    name: str
    source_path: str
    source_meta: dict[str, Any] = field(default_factory=dict)
    clips: list[ClipEntry] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    # extended fields preserved for future modules
    extra: dict[str, Any] = field(default_factory=dict)

    def to_manifest(self) -> dict[str, Any]:
        return {
            "version": 1,
            "id": self.id,
            "name": self.name,
            "source_path": self.source_path,
            "source_meta": self.source_meta,
            "clips": [asdict(c) for c in self.clips],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "extra": self.extra,
        }

    @staticmethod
    def from_manifest(data: dict[str, Any]) -> "Project":
        clips = [ClipEntry(**c) for c in data.get("clips", [])]
        return Project(
            id=data.get("id", ""),
            name=data.get("name", ""),
            source_path=data.get("source_path", ""),
            source_meta=data.get("source_meta", {}),
            clips=clips,
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            extra=data.get("extra", {}),
        )


def projects_root() -> Path:
    # honor custom project_dir if set
    try:
        from chopster.app.configuration import Configuration
        cfg = Configuration()
        custom = (cfg.get("project_dir") or "").strip()
        if custom:
            p = Path(custom)
            p.mkdir(parents=True, exist_ok=True)
            return p
    except Exception:
        pass
    p = user_data_dir() / "projects"
    p.mkdir(parents=True, exist_ok=True)
    return p


def project_dir(pid: str) -> Path:
    return projects_root() / pid


def manifest_path(pid: str) -> Path:
    return project_dir(pid) / "project.json"


def _now_str() -> str:
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def create_project(name: str, source_path: str = "", source_meta: dict | None = None) -> Project:
    pid = uuid.uuid4().hex[:12]
    now = _now_str()
    proj = Project(
        id=pid,
        name=name.strip() or f"Project {pid[:6]}",
        source_path=source_path,
        source_meta=source_meta or {},
        created_at=now,
        updated_at=now,
    )
    save_project(proj)
    # index in DB
    try:
        from chopster.app.database import Database
        db = Database()
        db.upsert_project(pid, proj.name, source_path, json.dumps(source_meta or {}, ensure_ascii=False))
        db.close()
    except Exception:
        pass
    return proj


def save_project(proj: Project) -> None:
    proj.updated_at = _now_str()
    d = project_dir(proj.id)
    d.mkdir(parents=True, exist_ok=True)
    # atomic write
    tmp = manifest_path(proj.id).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(proj.to_manifest(), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(manifest_path(proj.id))
    # ensure subfolders
    for sub in ("clips", "subtitles", "transcripts", "thumbnails", "captions", "exports", "analysis"):
        (d / sub).mkdir(exist_ok=True)
    # update DB index
    try:
        from chopster.app.database import Database
        db = Database()
        db.upsert_project(proj.id, proj.name, proj.source_path, json.dumps(proj.source_meta, ensure_ascii=False), json.dumps(proj.extra, ensure_ascii=False))
        db.close()
    except Exception:
        pass


def load_project(pid: str) -> Project | None:
    mp = manifest_path(pid)
    if not mp.exists():
        return None
    try:
        data = json.loads(mp.read_text(encoding="utf-8"))
        return Project.from_manifest(data)
    except Exception:
        return None


def list_projects() -> list[Project]:
    root = projects_root()
    out: list[Project] = []
    for pid_dir in root.iterdir():
        if not pid_dir.is_dir():
            continue
        mp = pid_dir / "project.json"
        if mp.exists():
            proj = load_project(pid_dir.name)
            if proj:
                out.append(proj)
    # fallback DB list for display
    out.sort(key=lambda p: p.updated_at, reverse=True)
    return out


def delete_project(pid: str) -> None:
    import shutil
    d = project_dir(pid)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    try:
        from chopster.app.database import Database
        db = Database()
        db.delete_project(pid)
        db.close()
    except Exception:
        pass


def relink_source(proj: Project, new_path: str) -> None:
    from chopster.clipper.media_probe import probe
    try:
        meta = probe(new_path)
        proj.source_meta = {
            "duration": meta.duration,
            "width": meta.width,
            "height": meta.height,
            "fps": meta.fps,
            "vcodec": meta.vcodec,
            "acodec": meta.acodec,
            "has_audio": meta.has_audio,
            "has_video": meta.has_video,
        }
    except Exception:
        proj.source_meta = {}
    proj.source_path = new_path
    save_project(proj)
