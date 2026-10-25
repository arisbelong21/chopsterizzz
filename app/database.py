"""SQLite database — history, projects, clipper jobs."""
from __future__ import annotations

import sqlite3
import threading
from functools import wraps
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

-- Automation (additive migration; CREATE IF NOT EXISTS keeps old DBs safe)
CREATE TABLE IF NOT EXISTS automation_accounts (
    id           TEXT PRIMARY KEY,
    platform     TEXT NOT NULL,
    display_name TEXT DEFAULT '',
    external_id  TEXT DEFAULT '',
    auth_mode    TEXT DEFAULT 'oauth',
    status       TEXT DEFAULT 'connected',
    credential_ref TEXT DEFAULT '',
    metadata     TEXT DEFAULT '{}',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    ts           INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS automation_runs (
    id           TEXT PRIMARY KEY,
    name         TEXT DEFAULT '',
    status       TEXT DEFAULT 'DRAFT',
    schedule_mode TEXT DEFAULT 'now',
    scheduled_at TEXT DEFAULT '',
    repeat_rule  TEXT DEFAULT '',
    recipe_id    TEXT DEFAULT '',
    metadata     TEXT DEFAULT '{}',
    error_message TEXT DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    started_at   TEXT DEFAULT '',
    completed_at TEXT DEFAULT '',
    ts           INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS automation_jobs (
    id             TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL,
    clip_id        TEXT DEFAULT '',
    media_path     TEXT NOT NULL,
    media_title    TEXT DEFAULT '',
    media_fingerprint TEXT DEFAULT '',
    platform       TEXT NOT NULL,
    account_id     TEXT DEFAULT '',
    publish_mode   TEXT DEFAULT 'now',
    scheduled_at   TEXT DEFAULT '',
    content_pack   TEXT DEFAULT '{}',
    status         TEXT DEFAULT 'QUEUED',
    attempt        INTEGER DEFAULT 0,
    max_attempts   INTEGER DEFAULT 3,
    idempotency_key TEXT DEFAULT '',
    external_post_id TEXT DEFAULT '',
    external_url   TEXT DEFAULT '',
    verification_status TEXT DEFAULT '',
    reason         TEXT DEFAULT '',
    error_code     TEXT DEFAULT '',
    error_message  TEXT DEFAULT '',
    progress       INTEGER DEFAULT 0,
    last_activity  TEXT DEFAULT '',
    source         TEXT DEFAULT 'manual',
    rule_id        TEXT DEFAULT '',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    ts             INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS automation_recipes (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    config       TEXT NOT NULL,
    enabled      INTEGER DEFAULT 1,
    last_run_at  TEXT DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    ts           INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS automation_media (
    id           TEXT PRIMARY KEY,
    source       TEXT DEFAULT 'acs',
    media_path   TEXT NOT NULL,
    title        TEXT DEFAULT '',
    project_id   TEXT DEFAULT '',
    clip_id      TEXT DEFAULT '',
    batch_id     TEXT DEFAULT '',
    content_pack TEXT DEFAULT '{}',
    status       TEXT DEFAULT 'ready',
    reject_reason TEXT DEFAULT '',
    created_at   TEXT NOT NULL,
    ts           INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_auto_jobs_run ON automation_jobs(run_id);
CREATE INDEX IF NOT EXISTS idx_auto_jobs_status ON automation_jobs(status);
CREATE INDEX IF NOT EXISTS idx_auto_jobs_sched ON automation_jobs(scheduled_at);
CREATE INDEX IF NOT EXISTS idx_auto_jobs_idem ON automation_jobs(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_auto_media_status ON automation_media(status);

-- Auto-Pilot rules (spec 6): persistent, app-independent rule storage. A rule
-- maps newly finished content (ACS render / CCA clip) onto the single publishing
-- pipeline; it never owns its own queue or scheduler.
CREATE TABLE IF NOT EXISTS automation_rules (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    enabled      INTEGER DEFAULT 0,
    sources      TEXT DEFAULT '["acs","cca"]',
    platforms    TEXT DEFAULT '[]',
    accounts     TEXT DEFAULT '[]',
    config       TEXT DEFAULT '{}',
    stats        TEXT DEFAULT '{}',
    last_run_at  TEXT DEFAULT '',
    next_run_at  TEXT DEFAULT '',
    last_error   TEXT DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    ts           INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_auto_jobs_rule ON automation_jobs(rule_id);
"""


def _serialized(method):
    """Keep each operation on the shared SQLite connection atomic across workers."""
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return locked


class Database:
    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.RLock()
        self.path = path or (user_data_dir() / "chopster.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        # Additive, non-destructive migrations run BEFORE the schema script:
        # CREATE TABLE IF NOT EXISTS cannot add columns to an existing table,
        # and SCHEMA's new indexes reference those columns.
        self._ensure_columns()
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        # migrate legacy history.json if db empty
        self._migrate_legacy()

    # Column additions are appended so existing rows keep their data (spec 12).
    _ADDITIVE_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
        "automation_jobs": (
            ("rule_id", "TEXT DEFAULT ''"),
            ("reason", "TEXT DEFAULT ''"),
        ),
        "automation_media": (("reject_reason", "TEXT DEFAULT ''"),),
    }

    @_serialized
    def _ensure_columns(self) -> None:
        for table, columns in self._ADDITIVE_COLUMNS.items():
            try:
                cur = self._conn.execute(f"PRAGMA table_info({table})")
                existing = {str(r[1]) for r in cur.fetchall()}
            except Exception:
                continue
            for name, decl in columns:
                if name in existing:
                    continue
                try:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                except Exception:
                    continue
        self._conn.commit()

    @_serialized
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

    @_serialized
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

    @_serialized
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

    @_serialized
    def delete_history(self, hid: int) -> None:
        self._conn.execute("DELETE FROM download_history WHERE id=?", (hid,))
        self._conn.commit()

    @_serialized
    def clear_history(self) -> None:
        self._conn.execute("DELETE FROM download_history")
        self._conn.commit()

    @_serialized
    def has_url(self, url: str) -> bool:
        cur = self._conn.execute("SELECT 1 FROM download_history WHERE url=? LIMIT 1", (url,))
        return cur.fetchone() is not None

    # -- projects ---------------------------------------------------

    @_serialized
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

    @_serialized
    def list_projects(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM clipper_projects ORDER BY ts DESC").fetchall()
        return [dict(r) for r in rows]

    @_serialized
    def get_project(self, pid: str) -> dict[str, Any] | None:
        cur = self._conn.execute("SELECT * FROM clipper_projects WHERE id=?", (pid,))
        row = cur.fetchone()
        return dict(row) if row else None

    @_serialized
    def delete_project(self, pid: str) -> None:
        self._conn.execute("DELETE FROM clipper_projects WHERE id=?", (pid,))
        self._conn.commit()

    @_serialized
    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # -- automation ----------------------------------------------------- #
    #
    # All automation persistence funnels through this connection and the
    # RLock decorator above (SQLite RLock requirement from the 7 Oct audit).
    # Tokens are NEVER stored here — only credential *references*.

    _AUTO_ACCOUNT_COLS = {"display_name", "external_id", "auth_mode", "status",
                          "credential_ref", "metadata"}
    _AUTO_RUN_COLS = {"name", "status", "schedule_mode", "scheduled_at", "repeat_rule",
                      "recipe_id", "metadata", "error_message", "started_at", "completed_at"}
    _AUTO_JOB_COLS = {"clip_id", "media_path", "media_title", "media_fingerprint", "platform",
                      "account_id", "publish_mode", "scheduled_at", "content_pack", "status",
                      "attempt", "max_attempts", "idempotency_key", "external_post_id",
                      "external_url", "verification_status", "reason", "error_code",
                      "error_message", "progress", "last_activity", "source", "rule_id"}

    def _auto_update(self, table: str, row_id: str, fields: dict[str, Any],
                     allowed: set[str], id_col: str = "id") -> bool:
        cols = [k for k in fields if k in allowed]
        if not cols:
            return False
        assignments = ", ".join(f"{c}=?" for c in cols)
        params = [fields[c] for c in cols]
        now = time.time()
        cur = self._conn.execute(
            f"UPDATE {table} SET {assignments}, updated_at=?, ts=? WHERE {id_col}=?",
            (*params, __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"), now, row_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    # accounts
    @_serialized
    def automation_upsert_account(self, account_id: str, platform: str, display_name: str = "",
                                  external_id: str = "", auth_mode: str = "oauth",
                                  status: str = "connected", credential_ref: str = "",
                                  metadata: str = "{}") -> None:
        import datetime
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ts = int(time.time())
        self._conn.execute(
            "INSERT INTO automation_accounts(id,platform,display_name,external_id,auth_mode,status,credential_ref,metadata,created_at,updated_at,ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET platform=excluded.platform, display_name=excluded.display_name,"
            " external_id=excluded.external_id, auth_mode=excluded.auth_mode, status=excluded.status,"
            " credential_ref=excluded.credential_ref, metadata=excluded.metadata,"
            " updated_at=excluded.updated_at, ts=excluded.ts",
            (account_id, platform, display_name, external_id, auth_mode, status,
             credential_ref, metadata, now, now, ts),
        )
        self._conn.commit()

    @_serialized
    def automation_update_account(self, account_id: str, **fields: Any) -> bool:
        return self._auto_update("automation_accounts", account_id, fields, self._AUTO_ACCOUNT_COLS)

    @_serialized
    def automation_get_account(self, account_id: str) -> dict[str, Any] | None:
        cur = self._conn.execute("SELECT * FROM automation_accounts WHERE id=?", (account_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    @_serialized
    def automation_list_accounts(self, platform: str = "") -> list[dict[str, Any]]:
        if platform:
            cur = self._conn.execute(
                "SELECT * FROM automation_accounts WHERE platform=? ORDER BY platform", (platform,))
        else:
            cur = self._conn.execute("SELECT * FROM automation_accounts ORDER BY platform")
        return [dict(r) for r in cur.fetchall()]

    @_serialized
    def automation_delete_account(self, account_id: str) -> None:
        self._conn.execute("DELETE FROM automation_accounts WHERE id=?", (account_id,))
        self._conn.commit()

    # runs
    @_serialized
    def automation_create_run(self, run_id: str, name: str = "", status: str = "DRAFT",
                              schedule_mode: str = "now", scheduled_at: str = "",
                              repeat_rule: str = "", recipe_id: str = "",
                              metadata: str = "{}") -> None:
        import datetime
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ts = int(time.time())
        self._conn.execute(
            "INSERT INTO automation_runs(id,name,status,schedule_mode,scheduled_at,repeat_rule,recipe_id,metadata,created_at,updated_at,ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, name, status, schedule_mode, scheduled_at, repeat_rule, recipe_id,
             metadata, now, now, ts),
        )
        self._conn.commit()

    @_serialized
    def automation_delete_run(self, run_id: str) -> None:
        self._conn.execute("DELETE FROM automation_runs WHERE id=?", (run_id,))
        self._conn.commit()

    @_serialized
    def automation_update_run(self, run_id: str, **fields: Any) -> bool:
        return self._auto_update("automation_runs", run_id, fields, self._AUTO_RUN_COLS)

    @_serialized
    def automation_get_run(self, run_id: str) -> dict[str, Any] | None:
        cur = self._conn.execute("SELECT * FROM automation_runs WHERE id=?", (run_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    @_serialized
    def automation_list_runs(self, limit: int = 200) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM automation_runs ORDER BY ts DESC, created_at DESC LIMIT ?", (int(limit),))
        return [dict(r) for r in cur.fetchall()]

    # jobs
    @_serialized
    def automation_add_job(self, job_id: str, run_id: str, media_path: str, platform: str,
                           **fields: Any) -> None:
        import datetime
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ts = int(time.time())
        row = {
            "id": job_id, "run_id": run_id, "media_path": media_path, "platform": platform,
            "created_at": now, "updated_at": now, "ts": ts,
        }
        for key, value in fields.items():
            if key in self._AUTO_JOB_COLS:
                row[key] = value
        cols = ", ".join(row.keys())
        marks = ", ".join("?" for _ in row)
        self._conn.execute(f"INSERT INTO automation_jobs({cols}) VALUES ({marks})", tuple(row.values()))
        self._conn.commit()

    @_serialized
    def automation_update_job(self, job_id: str, **fields: Any) -> bool:
        return self._auto_update("automation_jobs", job_id, fields, self._AUTO_JOB_COLS)

    @_serialized
    def automation_get_job(self, job_id: str) -> dict[str, Any] | None:
        cur = self._conn.execute("SELECT * FROM automation_jobs WHERE id=?", (job_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    @_serialized
    def automation_delete_job(self, job_id: str) -> bool:
        """Remove one job record (finished jobs — the manager enforces which).

        The queue and History read the same table, so deleting here also drops
        the row from both views; the file on disk is never touched.
        """
        cur = self._conn.execute("DELETE FROM automation_jobs WHERE id=?", (job_id,))
        self._conn.commit()
        return bool(cur.rowcount)

    @_serialized
    def automation_list_jobs(self, statuses: list[str] | None = None, run_id: str = "",
                             limit: int = 500) -> list[dict[str, Any]]:
        q = "SELECT * FROM automation_jobs WHERE 1=1"
        params: list[Any] = []
        if run_id:
            q += " AND run_id=?"
            params.append(run_id)
        if statuses:
            marks = ",".join("?" for _ in statuses)
            q += f" AND status IN ({marks})"
            params.extend([str(s) for s in statuses])
        q += " ORDER BY ts DESC, created_at DESC LIMIT ?"
        params.append(int(limit))
        cur = self._conn.execute(q, params)
        return [dict(r) for r in cur.fetchall()]

    @_serialized
    def automation_count_account_jobs_since(self, account_id: str, since: str,
                                            exclude_job_id: str = "") -> int:
        """Count real submission attempts for an account since ``since``.

        QUEUED/CANCELLED rows are excluded because they never reached the
        platform, and Test Mode rows (``reason='test_mode'``) are excluded
        because a simulation never posts for real. Only rows that actually
        entered the upload pipeline count against the optional conservative
        Browser Session soft cap.
        """
        if not account_id:
            return 0
        q = ("SELECT COUNT(*) FROM automation_jobs WHERE account_id=?"
             " AND created_at>=? AND status IN ('UPLOADING','PROCESSING','VERIFYING',"
             "'PUBLISHED','SCHEDULED','ACTION_REQUIRED','FAILED')"
             " AND COALESCE(reason,'')<>'test_mode'")
        params: list[Any] = [str(account_id), str(since)]
        if exclude_job_id:
            q += " AND id<>?"
            params.append(str(exclude_job_id))
        cur = self._conn.execute(q, params)
        row = cur.fetchone()
        return int(row[0]) if row else 0

    @_serialized
    def automation_claim_job(self, job_id: str, from_statuses: list[str], to_status: str) -> bool:
        """CAS-style claim so two dispatcher passes can never double-run a job."""
        marks = ",".join("?" for _ in from_statuses)
        cur = self._conn.execute(
            f"UPDATE automation_jobs SET status=?, updated_at=?, ts=?"
            f" WHERE id=? AND status IN ({marks})",
            (to_status, __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             time.time(), job_id, *[str(s) for s in from_statuses]),
        )
        self._conn.commit()
        return cur.rowcount > 0

    @_serialized
    def automation_due_jobs(self, now: str, limit: int = 20) -> list[dict[str, Any]]:
        # ``SCHEDULED`` means two different things: a job waiting for its own
        # slot (created by a scheduled/staggered run) and a job that was already
        # uploaded and will simply go live later on the platform. Only the first
        # may be claimed, so the terminal "live later" rows are marked with
        # ``reason='scheduled_live'`` and excluded here — claiming them again
        # would upload the same video a second time.
        cur = self._conn.execute(
            "SELECT * FROM automation_jobs WHERE status IN ('QUEUED','SCHEDULED')"
            " AND COALESCE(reason,'')<>'scheduled_live'"
            " AND (scheduled_at='' OR scheduled_at IS NULL OR scheduled_at<=?)"
            " ORDER BY created_at ASC LIMIT ?",
            (now, int(limit)),
        )
        return [dict(r) for r in cur.fetchall()]

    @_serialized
    def automation_job_statuses(self, run_id: str) -> list[str]:
        cur = self._conn.execute("SELECT status FROM automation_jobs WHERE run_id=?", (run_id,))
        return [str(r[0]) for r in cur.fetchall()]

    @_serialized
    def automation_find_idempotency(self, key: str, exclude_job_id: str = "") -> list[dict[str, Any]]:
        if not key:
            return []
        # Every status that means "this media is already on its way to, or on,
        # the platform" counts. The claim step parks a job in AUTH_CHECK before
        # the runner touches it, so leaving AUTH_CHECK/PREPARING out used to open
        # a race: two twin jobs claimed in the same dispatcher tick each looked at
        # the other, saw a status that was not listed here, and both uploaded.
        #
        # QUEUED/RETRYING are deliberately *not* listed. A twin that stepped
        # aside is put back to QUEUED, so counting that as a duplicate would make
        # two twins defer to each other forever and neither would ever publish.
        # Only the twin that is genuinely in flight counts; the one that stepped
        # aside re-checks later, sees PUBLISHED, and cancels itself as duplicate.
        #
        # ACTION_REQUIRED counts too, but only when the park reason says the media
        # may already have reached the platform (verify_unknown / draft_ready).
        # Without that, the first twin could park *after* its upload and the
        # second twin would then re-upload the very same video.
        q = ("SELECT * FROM automation_jobs WHERE idempotency_key=?"
             " AND (status IN ('PUBLISHED','SCHEDULED','AUTH_CHECK','PREPARING',"
             "'UPLOADING','PROCESSING','VERIFYING')"
             " OR (status='ACTION_REQUIRED' AND COALESCE(reason,'')"
             " IN ('verify_unknown','draft_ready')))")
        params: list[Any] = [key]
        if exclude_job_id:
            q += " AND id<>?"
            params.append(exclude_job_id)
        cur = self._conn.execute(q, params)
        return [dict(r) for r in cur.fetchall()]

    @_serialized
    def automation_job_counts(self) -> dict[str, int]:
        cur = self._conn.execute("SELECT status, COUNT(*) FROM automation_jobs GROUP BY status")
        return {str(r[0]): int(r[1]) for r in cur.fetchall()}

    #: Statuses that mean "still waiting in the queue" — neither finished nor
    #: abandoned. A repeat request for the same media+platform+account while one
    #: of these exists would only duplicate the row, so creation is refused.
    PENDING_JOB_STATUSES = ("QUEUED", "SCHEDULED", "RETRYING", "PAUSED", "ACTION_REQUIRED",
                            "AUTH_CHECK", "PREPARING", "UPLOADING", "PROCESSING", "VERIFYING")

    @_serialized
    def automation_find_pending(self, idempotency_key: str,
                                exclude_job_id: str = "") -> list[dict[str, Any]]:
        """Jobs sharing ``idempotency_key`` that are still pending (not terminal).

        The creation-time counterpart of :meth:`automation_find_idempotency`: it
        answers "is this media already in the queue?" — including jobs parked for
        the user — so a repeat request can be refused instead of adding a
        duplicate card.
        """
        if not idempotency_key:
            return []
        marks = ",".join("?" for _ in self.PENDING_JOB_STATUSES)
        q = (f"SELECT * FROM automation_jobs WHERE idempotency_key=?"
             f" AND status IN ({marks})")
        params: list[Any] = [idempotency_key, *self.PENDING_JOB_STATUSES]
        if exclude_job_id:
            q += " AND id<>?"
            params.append(exclude_job_id)
        q += " ORDER BY created_at ASC, rowid ASC"
        cur = self._conn.execute(q, params)
        return [dict(r) for r in cur.fetchall()]

    # recipes
    @_serialized
    def automation_save_recipe(self, recipe_id: str, name: str, config: str,
                               enabled: int = 1) -> None:
        import datetime
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ts = int(time.time())
        self._conn.execute(
            "INSERT INTO automation_recipes(id,name,config,enabled,created_at,updated_at,ts)"
            " VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET name=excluded.name, config=excluded.config,"
            " enabled=excluded.enabled, updated_at=excluded.updated_at, ts=excluded.ts",
            (recipe_id, name, config, int(enabled), now, now, ts),
        )
        self._conn.commit()

    @_serialized
    def automation_list_recipes(self) -> list[dict[str, Any]]:
        cur = self._conn.execute("SELECT * FROM automation_recipes ORDER BY name")
        return [dict(r) for r in cur.fetchall()]

    @_serialized
    def automation_get_recipe(self, recipe_id: str) -> dict[str, Any] | None:
        cur = self._conn.execute("SELECT * FROM automation_recipes WHERE id=?", (recipe_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    @_serialized
    def automation_set_recipe_enabled(self, recipe_id: str, enabled: int,
                                      last_run_at: str = "") -> bool:
        cur = self._conn.execute(
            "UPDATE automation_recipes SET enabled=?, last_run_at=CASE WHEN ?<>'' THEN ? ELSE last_run_at END,"
            " updated_at=?, ts=? WHERE id=?",
            (int(enabled), last_run_at, last_run_at,
             __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             time.time(), recipe_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    @_serialized
    def automation_delete_recipe(self, recipe_id: str) -> None:
        self._conn.execute("DELETE FROM automation_recipes WHERE id=?", (recipe_id,))
        self._conn.commit()

    # auto-pilot rules (spec 6) - persistent, survives an app restart
    @_serialized
    def automation_save_rule(self, rule_id: str, name: str, *, enabled: int = 0,
                             sources: str = '["acs","cca"]', platforms: str = "[]",
                             accounts: str = "[]", config: str = "{}", stats: str = "{}",
                             last_run_at: str = "", next_run_at: str = "",
                             last_error: str = "") -> None:
        import datetime
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._conn.execute(
            "INSERT INTO automation_rules(id,name,enabled,sources,platforms,accounts,config,stats,"
            "last_run_at,next_run_at,last_error,created_at,updated_at,ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET name=excluded.name, enabled=excluded.enabled,"
            " sources=excluded.sources, platforms=excluded.platforms, accounts=excluded.accounts,"
            " config=excluded.config, stats=excluded.stats, next_run_at=excluded.next_run_at,"
            " last_error=excluded.last_error, updated_at=excluded.updated_at, ts=excluded.ts",
            (rule_id, name, int(enabled), sources, platforms, accounts, config, stats,
             last_run_at, next_run_at, last_error, now, now, int(time.time())),
        )
        self._conn.commit()

    @_serialized
    def automation_list_rules(self) -> list[dict[str, Any]]:
        cur = self._conn.execute("SELECT * FROM automation_rules ORDER BY ts DESC, name")
        return [dict(r) for r in cur.fetchall()]

    @_serialized
    def automation_get_rule(self, rule_id: str) -> dict[str, Any] | None:
        cur = self._conn.execute("SELECT * FROM automation_rules WHERE id=?", (rule_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    _AUTO_RULE_COLS = {"name", "enabled", "sources", "platforms", "accounts", "config",
                       "stats", "last_run_at", "next_run_at", "last_error"}

    @_serialized
    def automation_update_rule(self, rule_id: str, **fields: Any) -> bool:
        return self._auto_update("automation_rules", rule_id, fields, self._AUTO_RULE_COLS)

    @_serialized
    def automation_delete_rule(self, rule_id: str) -> None:
        self._conn.execute("DELETE FROM automation_rules WHERE id=?", (rule_id,))
        self._conn.commit()

    @_serialized
    def automation_rule_jobs_today(self, rule_id: str, day_prefix: str) -> int:
        """Count jobs a rule created today - used for per-day caps (spec 6.2)."""
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM automation_jobs WHERE rule_id=? AND created_at LIKE ?"
            " AND status<>'CANCELLED'",
            (rule_id, f"{day_prefix}%"),
        )
        return int(cur.fetchone()[0])

    @_serialized
    def automation_rule_media_today(self, rule_id: str, day_prefix: str) -> int:
        """Distinct content items the rule published today (per-day cap, spec 6.2)."""
        cur = self._conn.execute(
            "SELECT COUNT(DISTINCT media_fingerprint) FROM automation_jobs"
            " WHERE rule_id=? AND created_at LIKE ? AND status<>'CANCELLED'"
            " AND media_fingerprint<>''",
            (rule_id, f"{day_prefix}%"),
        )
        return int(cur.fetchone()[0])

    @_serialized
    def automation_rule_jobs(self, rule_id: str, limit: int = 200) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM automation_jobs WHERE rule_id=? ORDER BY ts DESC LIMIT ?",
            (rule_id, int(limit)),
        )
        return [dict(r) for r in cur.fetchall()]

    @_serialized
    def automation_rule_has_job(self, rule_id: str, fingerprint: str, platform: str,
                                account_id: str) -> bool:
        """Repeated ACS/CCA events must not enqueue the same target twice (spec 7.4)."""
        if not (rule_id and fingerprint and platform):
            return False
        cur = self._conn.execute(
            "SELECT 1 FROM automation_jobs WHERE rule_id=? AND media_fingerprint=?"
            " AND platform=? AND account_id=? AND status NOT IN ('CANCELLED','FAILED') LIMIT 1",
            (rule_id, fingerprint, platform, account_id),
        )
        return cur.fetchone() is not None

    # media (render-completed handoff, spec §29/§30)
    @_serialized
    def automation_add_media(self, media_id: str, media_path: str, source: str = "acs",
                             title: str = "", project_id: str = "", clip_id: str = "",
                             batch_id: str = "", content_pack: str = "{}") -> None:
        import datetime
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._conn.execute(
            "INSERT OR IGNORE INTO automation_media(id,source,media_path,title,project_id,clip_id,batch_id,content_pack,created_at,ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (media_id, source, media_path, title, project_id, clip_id, batch_id,
             content_pack, now, int(time.time())),
        )
        self._conn.commit()

    @_serialized
    def automation_media_by_path(self, media_path: str) -> dict[str, Any] | None:
        cur = self._conn.execute("SELECT * FROM automation_media WHERE media_path=?", (media_path,))
        row = cur.fetchone()
        return dict(row) if row else None

    @_serialized
    def automation_list_media(self, status: str = "ready", limit: int = 200,
                              source: str = "") -> list[dict[str, Any]]:
        q = "SELECT * FROM automation_media WHERE 1=1"
        params: list[Any] = []
        if status:
            q += " AND status=?"
            params.append(status)
        if source:
            q += " AND source=?"
            params.append(source)
        q += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        cur = self._conn.execute(q, params)
        return [dict(r) for r in cur.fetchall()]

    @_serialized
    def automation_update_media(self, media_id: str, status: str,
                                reject_reason: str | None = None) -> bool:
        if reject_reason is None:
            cur = self._conn.execute("UPDATE automation_media SET status=? WHERE id=?",
                                     (status, media_id))
        else:
            cur = self._conn.execute(
                "UPDATE automation_media SET status=?, reject_reason=? WHERE id=?",
                (status, str(reject_reason)[:400], media_id))
        self._conn.commit()
        return cur.rowcount > 0

    @_serialized
    def automation_delete_media(self, media_id: str) -> None:
        self._conn.execute("DELETE FROM automation_media WHERE id=?", (media_id,))
        self._conn.commit()

    @_serialized
    def automation_history(self, limit: int = 300) -> list[dict[str, Any]]:
        """Jobs joined with their run names for the History table (spec §135)."""
        cur = self._conn.execute(
            "SELECT j.*, r.name AS run_name FROM automation_jobs j"
            " LEFT JOIN automation_runs r ON r.id=j.run_id"
            " ORDER BY j.ts DESC, j.created_at DESC LIMIT ?", (int(limit),))
        return [dict(r) for r in cur.fetchall()]
