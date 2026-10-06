"""SQLite database — history, projects, clipper jobs."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from chopster.app.paths import user_data_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS download_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT NOT NULL,
    platform    TEXT DEFAULT '',
    title       TEXT DEFAULT '',
    status      TEXT DEFAULT '',
    file_path   TEXT DEFAULT '',
    file_size   INTEGER DEFAULT 0,
    format_sel  TEXT DEFAULT '',
    error       TEXT DEFAULT '',
    created_at  TEXT NOT NULL,
    ts          INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS clipper_projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    source_path TEXT DEFAULT '',
    source_meta TEXT DEFAULT '',
    settings    TEXT DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    ts          INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS clipper_clips (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES clipper_projects(id) ON DELETE CASCADE,
    clip_no     INTEGER NOT NULL,
    start_sec   REAL NOT NULL,
    end_sec     REAL NOT NULL,
    title       TEXT DEFAULT '',
    status      TEXT DEFAULT 'planned',
    output_path TEXT DEFAULT '',
    FOREIGN KEY(project_id) REFERENCES clipper_projects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_history_ts ON download_history(ts DESC);
CREATE INDEX IF NOT EXISTS idx_history_url ON download_history(url);
CREATE INDEX IF NOT EXISTS idx_clips_project ON clipper_clips(project_id);
"""


class Database:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (user_data_dir() / "chopster.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        # migrate legacy history.json if db empty
        self._migrate_legacy()

    def _migrate_legacy(self) -> None:
        cur = self._conn.execute("SELECT COUNT(*) FROM download_history")
        if cur.fetchone()[0] > 0:
            return
        # look for history.json in app_dir and user_data_dir
        from chopster.app.paths import app_dir
        import json
        for p in [app_dir() / "history.json", user_data_dir() / "history.json"]:
            if not p.exists():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(data, list):
                    continue
                for item in data:
                    self.add_history(
                        url=item.get("url", ""),
                        platform=item.get("platform", ""),
                        title=item.get("title", ""),
                        status=item.get("status", ""),
                        file_path=item.get("path", ""),
                    )
                break
            except Exception:
                continue

    # -- history ----------------------------------------------------

    def add_history(self, url: str, platform: str = "", title: str = "",
                    status: str = "", file_path: str = "", file_size: int = 0,
                    format_sel: str = "", error: str = "") -> int:
        import datetime
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        ts = int(time.time())
        cur = self._conn.execute(
            "INSERT INTO download_history(url,platform,title,status,file_path,file_size,format_sel,error,created_at,ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (url, platform, title, status, file_path, file_size, format_sel, error, now, ts),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore

    def list_history(self, limit: int = 500, search: str = "", platform: str = "", status: str = "") -> list[dict[str, Any]]:
        q = "SELECT * FROM download_history WHERE 1=1"
        params: list[Any] = []
        if search:
            q += " AND (url LIKE ? OR title LIKE ? OR platform LIKE ?)"
            like = f"%{search}%"
            params.extend([like, like, like])
        if platform:
            q += " AND platform = ?"
            params.append(platform)
        if status:
            q += " AND status = ?"
            params.append(status)
        q += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def delete_history(self, hid: int) -> None:
        self._conn.execute("DELETE FROM download_history WHERE id=?", (hid,))
        self._conn.commit()

    def clear_history(self) -> None:
        self._conn.execute("DELETE FROM download_history")
        self._conn.commit()

    def has_url(self, url: str) -> bool:
        cur = self._conn.execute("SELECT 1 FROM download_history WHERE url=? LIMIT 1", (url,))
        return cur.fetchone() is not None

    # -- projects ---------------------------------------------------

    def upsert_project(self, pid: str, name: str, source_path: str = "", source_meta: str = "", settings: str = "") -> None:
        import datetime
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ts = int(time.time())
        self._conn.execute(
            "INSERT INTO clipper_projects(id,name,source_path,source_meta,settings,created_at,updated_at,ts)"
            " VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET name=excluded.name, source_path=excluded.source_path,"
            " source_meta=excluded.source_meta, settings=excluded.settings, updated_at=excluded.updated_at, ts=excluded.ts",
            (pid, name, source_path, source_meta, settings, now, now, ts),
        )
        self._conn.commit()

    def list_projects(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM clipper_projects ORDER BY ts DESC").fetchall()
        return [dict(r) for r in rows]

    def get_project(self, pid: str) -> dict[str, Any] | None:
        cur = self._conn.execute("SELECT * FROM clipper_projects WHERE id=?", (pid,))
        row = cur.fetchone()
        return dict(row) if row else None

    def delete_project(self, pid: str) -> None:
        self._conn.execute("DELETE FROM clipper_projects WHERE id=?", (pid,))
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
