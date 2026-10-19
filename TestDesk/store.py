"""
TestDesk's own persistence: saved scenarios and the reports of past runs.

Deliberately a SEPARATE SQLite file (TestDesk/data/testdesk.sqlite, or
$TESTDESK_DB) from the app DB (data/jobs.sqlite). A test-mode run is pointed
at a throwaway copy of the app's databases and then deletes it -- the report
has to outlive that, and must never live somewhere the sandbox could touch.

Stdlib only.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scenarios (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    target_type TEXT NOT NULL DEFAULT 'job',      -- 'job' | 'agent'
    target_id TEXT NOT NULL,
    task TEXT NOT NULL,                            -- the scenario, in the user's words
    expectations TEXT NOT NULL DEFAULT '[]',       -- JSON list: phrases the final answer must contain
    expect_status TEXT NOT NULL DEFAULT 'done',
    job_yaml TEXT NOT NULL DEFAULT '',             -- optional draft Job definition (test mode only)
    options TEXT NOT NULL DEFAULT '{}',            -- JSON: block_external, auto_approve, keep_sandbox, timeout_seconds
    owner_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS results (
    id TEXT PRIMARY KEY,
    scenario_id TEXT,
    scenario_name TEXT NOT NULL,
    scenario TEXT NOT NULL,                        -- JSON snapshot of the scenario as it was run
    mode TEXT NOT NULL,                            -- 'test' | 'prod'
    state TEXT NOT NULL DEFAULT 'running',         -- 'running' | 'finished'
    verdict TEXT,                                  -- 'pass' | 'fail' | 'error' | 'awaiting_human'
    run_status TEXT,                               -- the Job run's own final status
    final_answer TEXT,
    error TEXT,
    checks TEXT NOT NULL DEFAULT '[]',             -- JSON list of {name, ok, detail}
    events TEXT NOT NULL DEFAULT '[]',             -- JSON list (trimmed run events)
    approvals TEXT NOT NULL DEFAULT '[]',          -- JSON list of agents auto-approved
    run_id TEXT,                                   -- prod mode: the real Run in the app DB
    sandbox_path TEXT,                             -- only set when the sandbox was kept
    owner_id TEXT,
    started_at REAL NOT NULL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_results_scenario ON results(scenario_id);
"""

_JSON_COLS = {"expectations", "options", "scenario", "checks", "events", "approvals"}


def db_path() -> str:
    return os.environ.get("TESTDESK_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "testdesk.sqlite"))


@contextmanager
def _connect():
    path = db_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _row(r: sqlite3.Row | None) -> Optional[dict]:
    if r is None:
        return None
    d = dict(r)
    for k in _JSON_COLS & d.keys():
        try:
            d[k] = json.loads(d[k]) if d[k] else ([] if k != "options" and k != "scenario" else {})
        except json.JSONDecodeError:
            d[k] = [] if k in ("expectations", "checks", "events", "approvals") else {}
    return d


# ---------------------------------------------------------------- scenarios

def create_scenario(*, name: str, target_type: str, target_id: str, task: str, expectations: list[str],
                    expect_status: str, job_yaml: str, options: dict, owner_id: Optional[str]) -> str:
    sid, now = str(uuid.uuid4()), time.time()
    with _connect() as c:
        c.execute(
            "INSERT INTO scenarios (id,name,target_type,target_id,task,expectations,expect_status,job_yaml,options,owner_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, name, target_type, target_id, task, json.dumps(expectations), expect_status, job_yaml,
             json.dumps(options), owner_id, now, now),
        )
    return sid


def update_scenario(scenario_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields = {k: (json.dumps(v) if k in _JSON_COLS else v) for k, v in fields.items()}
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _connect() as c:
        c.execute(f"UPDATE scenarios SET {cols} WHERE id = ?", (*fields.values(), scenario_id))


def get_scenario(scenario_id: str) -> Optional[dict]:
    with _connect() as c:
        return _row(c.execute("SELECT * FROM scenarios WHERE id = ?", (scenario_id,)).fetchone())


def list_scenarios(owner_id: Optional[str] = None) -> list[dict]:
    with _connect() as c:
        if owner_id:
            rows = c.execute("SELECT * FROM scenarios WHERE owner_id = ? ORDER BY updated_at DESC", (owner_id,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM scenarios ORDER BY updated_at DESC").fetchall()
        return [_row(r) for r in rows]


def delete_scenario(scenario_id: str) -> None:
    with _connect() as c:
        c.execute("DELETE FROM scenarios WHERE id = ?", (scenario_id,))


# ------------------------------------------------------------------ results

def create_result(*, scenario: dict, mode: str, owner_id: Optional[str]) -> str:
    rid = str(uuid.uuid4())
    with _connect() as c:
        c.execute(
            "INSERT INTO results (id,scenario_id,scenario_name,scenario,mode,state,owner_id,started_at) VALUES (?,?,?,?,?,'running',?,?)",
            (rid, scenario.get("id"), scenario.get("name") or "(unsaved scenario)", json.dumps(scenario), mode, owner_id, time.time()),
        )
    return rid


def finish_result(result_id: str, **fields: Any) -> None:
    fields = {k: (json.dumps(v, default=str) if k in _JSON_COLS else v) for k, v in fields.items()}
    fields["state"] = "finished"
    fields["finished_at"] = time.time()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _connect() as c:
        c.execute(f"UPDATE results SET {cols} WHERE id = ?", (*fields.values(), result_id))


def get_result(result_id: str) -> Optional[dict]:
    with _connect() as c:
        return _row(c.execute("SELECT * FROM results WHERE id = ?", (result_id,)).fetchone())


def list_results(owner_id: Optional[str] = None, scenario_id: Optional[str] = None, limit: int = 50) -> list[dict]:
    """Summary rows (no events / final_answer bodies) for the history table."""
    where, params = [], []
    if owner_id:
        where.append("owner_id = ?"); params.append(owner_id)
    if scenario_id:
        where.append("scenario_id = ?"); params.append(scenario_id)
    sql = ("SELECT id,scenario_id,scenario_name,mode,state,verdict,run_status,run_id,owner_id,started_at,finished_at FROM results"
           + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY started_at DESC LIMIT ?")
    with _connect() as c:
        return [dict(r) for r in c.execute(sql, (*params, limit)).fetchall()]


def mark_orphaned_results_as_error() -> int:
    """Startup hygiene, same idea as job_store.mark_orphaned_runs_as_error():
    a result still 'running' when this process starts has no thread behind it."""
    with _connect() as c:
        cur = c.execute(
            "UPDATE results SET state='finished', verdict='error', finished_at=?, "
            "error='Orphaned: the server restarted while this scenario was running.' WHERE state='running'",
            (time.time(),),
        )
        return cur.rowcount
