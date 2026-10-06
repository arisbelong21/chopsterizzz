"""Small isolated SQLite store for Auto Clip Studio projects."""
from __future__ import annotations
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any
from chopster.app.paths import user_data_dir

DB_PATH = user_data_dir() / "AutoClipStudio" / "projects.sqlite3"

class ProjectStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS projects(
                id TEXT PRIMARY KEY, title TEXT NOT NULL, source TEXT,
                source_type TEXT, created REAL NOT NULL, updated REAL NOT NULL,
                data_json TEXT NOT NULL)""")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def save(self, data: dict[str, Any], project_id: str | None = None) -> str:
        now = time.time(); pid = project_id or uuid.uuid4().hex
        title = str(data.get("title") or "Untitled project")
        source = str(data.get("source_url") or data.get("video_url") or "")
        source_type = str(data.get("source_type") or "unknown")
        payload = json.dumps(data, ensure_ascii=False)
        with self._connect() as db:
            db.execute("""INSERT INTO projects VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title, source=excluded.source,
                source_type=excluded.source_type, updated=excluded.updated,
                data_json=excluded.data_json""",
                (pid, title, source, source_type, now, now, payload))
        return pid

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT id,title,source,source_type,created,updated FROM projects ORDER BY updated DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def load(self, project_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT data_json FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row: raise KeyError(project_id)
        return json.loads(row[0])

    def delete(self, project_id: str) -> None:
        with self._connect() as db: db.execute("DELETE FROM projects WHERE id=?", (project_id,))
