"""
Persistence for everything a Job needs beyond its own YAML file: the DB
mirror of a Job's shape (for fast listing/joins), its Runs, its Schedules,
and (Phase 5) its Users.

Kept in SQLite -- same choice observability/tracer.py and
core/checkpointer.py already made -- rather than jumping straight to
Postgres: this repo is still single-machine, and SQLite is zero-setup for
anyone cloning it. The spec doc's "upgrade to Postgres" note is about
scaling to concurrent multi-user writes later; nothing here is Postgres-
specific, so that upgrade is a connection-string change plus an Alembic
migration, not a rewrite.

    from core.job_store import sync_job_to_store, create_run, update_run, list_runs

    sync_job_to_store(job_def)                        # upsert jobs/job_agents/tasks
    run_id = create_run(job_def.id, triggered_by="manual")
    update_run(run_id, status="done", current_agent=None)
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    trigger_type TEXT NOT NULL DEFAULT 'manual',
    status TEXT NOT NULL DEFAULT 'published',
    created_by TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS job_agents (
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    agent_name TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (job_id, agent_name)
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    agent_name TEXT NOT NULL,
    name TEXT NOT NULL,
    instructions TEXT,
    depends_on TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (job_id, id)
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    task TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
    current_agent TEXT,
    started_at REAL NOT NULL,
    finished_at REAL,
    triggered_by TEXT NOT NULL DEFAULT 'manual',
    triggered_by_user TEXT,
    error TEXT,
    final_answer TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_job ON runs(job_id);

CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    cron_expr TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run_at REAL,
    next_run_at REAL,
    created_by TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_schedules_job ON schedules(job_id);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'end_user',
    created_at REAL NOT NULL
);
"""


# A run started before core/supervisor.py's DAG rewrite (fan-out/parallel
# tasks, retry-then-escalate) has no `dag_state` column. Added the same way
# observability/tracer.py migrates run_events: ALTER TABLE ADD COLUMN,
# duplicate-column errors swallowed, run against every existing DB on the
# very next connection this process makes.
_MIGRATION_COLUMNS = [
    ("runs", "dag_state", "TEXT"),  # JSON: {"node_outputs": {...}, "paused_node_id": "..."}
    # Heartbeat: bumped by every update_run() call (each agent hop calls it
    # via on_step), so a "running" row's staleness can be judged by how long
    # it's been since anything last happened, not just when it started --
    # see Settings.stale_run_seconds and is_run_stale() below.
    ("runs", "updated_at", "REAL"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, col, coltype in _MIGRATION_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise


def _db_path() -> str:
    return os.environ.get("AGENTICSTUDIO_JOB_DB", "./data/jobs.sqlite")


@contextmanager
def _connect():
    path = _db_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------- jobs

def sync_job_to_store(job_def) -> None:
    """Upsert a JobDefinition (core.job_loader.JobDefinition) into
    jobs/job_agents/tasks. Safe to call every time a Job is loaded from
    its YAML -- the file is the source of truth, this is just an indexed
    mirror the API/UI can query without re-parsing YAML on every request."""
    now = time.time()
    with _connect() as conn:
        existing = conn.execute("SELECT created_at FROM jobs WHERE id = ?", (job_def.id,)).fetchone()
        created_at = existing["created_at"] if existing else now
        conn.execute(
            "INSERT INTO jobs (id, name, description, trigger_type, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'published', ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, description=excluded.description, "
            "trigger_type=excluded.trigger_type, updated_at=excluded.updated_at",
            (job_def.id, job_def.name, job_def.description, job_def.trigger_type, created_at, now),
        )
        conn.execute("DELETE FROM job_agents WHERE job_id = ?", (job_def.id,))
        conn.executemany(
            "INSERT INTO job_agents (job_id, agent_name, position) VALUES (?, ?, ?)",
            [(job_def.id, name, i) for i, name in enumerate(job_def.agents)],
        )
        conn.execute("DELETE FROM tasks WHERE job_id = ?", (job_def.id,))
        conn.executemany(
            "INSERT INTO tasks (id, job_id, agent_name, name, instructions, depends_on) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (t.id, job_def.id, t.agent, t.name, t.instructions, json.dumps(t.depends_on))
                for t in job_def.tasks
            ],
        )


def list_jobs() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


def get_job_row(job_id: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None


def delete_job(job_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))  # cascades job_agents/tasks/runs/schedules


# --------------------------------------------------------------------- runs

def create_run(job_id: str, task: str, triggered_by: str = "manual", triggered_by_user: str | None = None) -> str:
    run_id = str(uuid.uuid4())
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO runs (id, job_id, task, status, started_at, updated_at, triggered_by, triggered_by_user) "
            "VALUES (?, ?, ?, 'running', ?, ?, ?, ?)",
            (run_id, job_id, task, now, now, triggered_by, triggered_by_user),
        )
    return run_id


def update_run(run_id: str, **fields) -> None:
    """update_run(run_id, status='done', current_agent=None, final_answer='...')

    Every call is treated as a heartbeat: `updated_at` is stamped to now
    unless the caller passes its own value, so a "running" row's staleness
    (is_run_stale()) reflects when it last actually did something, not just
    when it started."""
    if not fields:
        return
    if fields.get("status") in ("done", "error") and "finished_at" not in fields:
        fields["finished_at"] = time.time()
    fields.setdefault("updated_at", time.time())
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _connect() as conn:
        conn.execute(f"UPDATE runs SET {cols} WHERE id = ?", (*fields.values(), run_id))


def is_run_stale(row: dict, now: float | None = None) -> bool:
    """A "running" row is stale once longer than settings.stale_run_seconds
    has passed since its last heartbeat (updated_at, falling back to
    started_at for a row from before that column existed). Only meaningful
    for status="running" -- a terminal row is never stale."""
    if row.get("status") != "running":
        return False
    from core.config import settings

    last_seen = row.get("updated_at") or row.get("started_at") or 0
    return ((now if now is not None else time.time()) - last_seen) > settings.stale_run_seconds


def _with_stale(row: dict) -> dict:
    row["stale"] = is_run_stale(row)
    return row


def get_run(run_id: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return _with_stale(dict(row)) if row else None


def list_runs(job_id: str | None = None, limit: int = 100) -> list[dict]:
    with _connect() as conn:
        if job_id:
            rows = conn.execute(
                "SELECT * FROM runs WHERE job_id = ? ORDER BY started_at DESC LIMIT ?", (job_id, limit)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        return [_with_stale(dict(r)) for r in rows]


def resolve_run_id(candidate: str) -> Optional[str]:
    """Best-effort recovery when `candidate` is actually a LangGraph
    thread_id (f"{run_id}-{agent_name}", built in core/supervisor.py) rather
    than a run_id -- easy to confuse since thread_id is what shows up
    alongside run_id in the console trace logs (observability/tracer.py),
    and people copy the wrong one into the Log Explorer or a Run detail URL.

    Returns `candidate` unchanged if it's already a real run_id, the
    recovered run_id if stripping a trailing "-<agent_name>" segment
    resolves to one, or None if neither matches anything. Agent names are
    snake_case (no hyphens) and a run_id is a uuid4 (hyphens only inside
    it), so splitting on the *last* hyphen reliably separates the two."""
    if get_run(candidate):
        return candidate
    if "-" in candidate:
        prefix = candidate.rsplit("-", 1)[0]
        if get_run(prefix):
            return prefix
    return None


def mark_orphaned_runs_as_error() -> int:
    """Call once at process startup. Any run still `status='running'` at
    that point is *guaranteed* orphaned -- this is a fresh process, so
    nothing is actually executing it -- most commonly because the previous
    process died or was restarted (e.g. `uvicorn --reload`) mid-run, which
    kills the background thread running it without ever calling update_run()
    to close out its row. Left alone, such a row stays "running" forever
    with no way to resume or dismiss it from the UI. Returns how many rows
    were cleaned up, for a one-line startup log."""
    now = time.time()
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE runs SET status = 'error', "
            "error = 'Orphaned: the server restarted (or crashed) while this run was in progress.', "
            "finished_at = ?, updated_at = ? WHERE status = 'running'",
            (now, now),
        )
        return cur.rowcount


# ---------------------------------------------------------------- schedules

def create_schedule(job_id: str, cron_expr: str, created_by: str | None = None, enabled: bool = True) -> str:
    schedule_id = str(uuid.uuid4())
    with _connect() as conn:
        conn.execute(
            "INSERT INTO schedules (id, job_id, cron_expr, enabled, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (schedule_id, job_id, cron_expr, int(enabled), created_by, time.time()),
        )
    return schedule_id


def list_schedules(enabled_only: bool = False) -> list[dict]:
    with _connect() as conn:
        q = "SELECT * FROM schedules"
        if enabled_only:
            q += " WHERE enabled = 1"
        rows = conn.execute(q + " ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


def get_schedule(schedule_id: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
        return dict(row) if row else None


def update_schedule(schedule_id: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _connect() as conn:
        conn.execute(f"UPDATE schedules SET {cols} WHERE id = ?", (*fields.values(), schedule_id))


def delete_schedule(schedule_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))


# -------------------------------------------------------------------- users
# Deliberately minimal (Phase 5 flags this as a stub, not production auth):
# password hashing uses hashlib.pbkdf2_hmac (stdlib, no bcrypt dependency)
# and there's no session/token revocation store. Good enough to gate the
# Admin screens behind a role; not audited for a real multi-tenant deploy.

def create_user(email: str, password_hash: str, role: str = "end_user") -> str:
    user_id = str(uuid.uuid4())
    with _connect() as conn:
        conn.execute(
            "INSERT INTO users (id, email, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, email, password_hash, role, time.time()),
        )
    return user_id


def get_user_by_email(email: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def list_users() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT id, email, role, created_at FROM users ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]


def update_user(user_id: str, email: str, role: str) -> bool:
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            return False
        if row["role"] == "admin" and role != "admin":
            admin_count = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]
            if admin_count <= 1:
                raise ValueError("cannot demote the last admin")
        conn.execute("UPDATE users SET email = ?, role = ? WHERE id = ?", (email, role, user_id))
        return True


def update_user_password(user_id: str, password_hash: str) -> bool:
    with _connect() as conn:
        cursor = conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
        return cursor.rowcount > 0


def delete_user(user_id: str) -> bool:
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            return False
        if row["role"] == "admin":
            admin_count = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]
            if admin_count <= 1:
                raise ValueError("cannot delete the last admin")
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return True
