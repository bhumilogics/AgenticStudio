"""
Persists every graph node transition (and, where available, token usage)
to a small SQLite table, and logs it via structlog. This is the "why did
it do that" audit trail, and what a dashboard/Log Explorer reads from.

Schema (v2): each event now carries a `level` (DEBUG/INFO/WARN/ERROR) plus
optional `job_id` / `run_id` / `agent_name` / `task_id` correlation columns,
so a multi-agent Job's whole story -- across every role -- can be pulled by
one `run_id`, and the admin Log Explorer can filter by level/module the way
the spec calls for. `thread_id` is kept as-is for backward compatibility: a
single-agent call still passes just a thread_id (job_id/run_id/agent_name
stay NULL); core/supervisor.py passes all four so each hop is attributable
to one Job run and one role.

The migration from the v1 schema (thread_id/ts/node/event_type/payload/
input_tokens/output_tokens only) is handled inline in _connect(): existing
DBs get the new columns ALTERed in once, quietly, the first time this module
runs against them; a fresh DB just gets the full v2 CREATE TABLE.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager

from core.config import settings

from .logging_config import get_logger

logger = get_logger("agenticstudio.trace")

_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    ts REAL NOT NULL,
    node TEXT NOT NULL,
    event_type TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'INFO',
    job_id TEXT,
    run_id TEXT,
    agent_name TEXT,
    task_id TEXT,
    payload TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER
);
"""

# Only safe to run AFTER _migrate() has ensured level/job_id/run_id/agent_name/
# task_id exist -- an index on a column a v1 (pre-log-level) table doesn't
# have yet would fail before the ALTER TABLEs below get a chance to add it.
_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_run_events_thread ON run_events(thread_id);
CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events(run_id);
CREATE INDEX IF NOT EXISTS idx_run_events_level ON run_events(level);
"""

# Columns a v1 (pre-log-level) DB won't have yet. ALTER TABLE ADD COLUMN is
# additive-only in SQLite (no IF NOT EXISTS), so each is tried once and a
# "duplicate column" error is swallowed -- the column already being there
# is the success case on every run after the first.
_V2_COLUMNS = [
    ("level", "TEXT NOT NULL DEFAULT 'INFO'"),
    ("job_id", "TEXT"),
    ("run_id", "TEXT"),
    ("agent_name", "TEXT"),
    ("task_id", "TEXT"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for name, coltype in _V2_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE run_events ADD COLUMN {name} {coltype}")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise


@contextmanager
def _connect():
    db_path = settings.events_db_path
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_TABLE_SCHEMA)
        _migrate(conn)
        conn.executescript(_INDEX_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------- live server console
#
# Everything an agent does is persisted to run_events (the Log Explorer reads
# it) and also printed here, as one readable line per event, so the terminal
# running uvicorn shows a live play-by-play. Turn it off with
# AGENTICSTUDIO_LIVE_CONSOLE=0.

def _console_enabled() -> bool:
    return os.environ.get("AGENTICSTUDIO_LIVE_CONSOLE", "1").strip().lower() not in {"0", "false", "no", "off"}


def _clip(value, n: int) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def summarize_payload(payload: dict | None) -> str:
    """One short human-readable line for an event's payload (mirrors summarizeEvent() in frontend/app.js)."""
    p = payload or {}
    msgs = p.get("messages")
    if isinstance(msgs, list) and msgs:
        m = msgs[-1]
        return f"{m.get('type', 'Message')}: {_clip(m.get('content'), 220)}"
    if p.get("error"):
        return "error: " + _clip(p["error"], 220)
    if p.get("input"):
        return "input: " + _clip(p["input"], 160)
    return _clip(json.dumps(p, default=str), 200) if p else ""


def console_line(level: str, text: str, run_id: str | None = None, agent: str | None = None) -> None:
    """Print one timestamped line to the server terminal and flush it at once."""
    if not _console_enabled():
        return
    stamp = time.strftime("%H:%M:%S")
    where = " ".join(x for x in ((f"run {run_id[:8]}" if run_id else ""), (agent or "")) if x)
    line = f"[{stamp}] {level:<5} {where + '  ' if where else ''}{text}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:  # a legacy Windows console codepage
        print(line.encode("ascii", "replace").decode("ascii"), flush=True)
    except Exception:  # noqa: BLE001 -- console output must never break a run
        pass


def record_event(
    thread_id: str,
    node: str,
    event_type: str,
    level: str = "INFO",
    job_id: str | None = None,
    run_id: str | None = None,
    agent_name: str | None = None,
    task_id: str | None = None,
    payload: dict | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO run_events "
            "(thread_id, ts, node, event_type, level, job_id, run_id, agent_name, task_id, payload, input_tokens, output_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                thread_id,
                time.time(),
                node,
                event_type,
                level,
                job_id,
                run_id,
                agent_name,
                task_id,
                json.dumps(payload or {}, default=str),
                input_tokens,
                output_tokens,
            ),
        )
    console_line(
        level, f"{node} > {event_type}  {summarize_payload(payload)}".rstrip(), run_id=run_id, agent=agent_name,
    )
    log_fn = {"DEBUG": logger.debug, "INFO": logger.info, "WARN": logger.warning, "ERROR": logger.error}.get(level, logger.info)
    log_fn("graph_event", thread_id=thread_id, node=node, event_type=event_type, job_id=job_id, run_id=run_id, agent_name=agent_name)


def get_events(
    thread_id: str | None = None,
    run_id: str | None = None,
    job_id: str | None = None,
    agent_name: str | None = None,
    task_id: str | None = None,
    level: str | None = None,
    limit: int = 500,
    since_id: int | None = None,
) -> list[dict]:
    """The Log Explorer's data source: every filter is optional and ANDed
    together. `run_id` is what correlates one multi-agent Job run's events
    across every agent's own thread_id."""
    clauses, params = [], []
    for col, val in (
        ("thread_id", thread_id),
        ("run_id", run_id),
        ("job_id", job_id),
        ("agent_name", agent_name),
        ("task_id", task_id),
        ("level", level),
    ):
        if val is not None:
            clauses.append(f"{col} = ?")
            params.append(val)
    if since_id is not None:  # live tail: only events newer than the last one the caller saw, oldest first
        clauses.append("id > ?")
        params.append(since_id)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM run_events {where} ORDER BY id {'ASC' if since_id is not None else 'DESC'} LIMIT ?", (*params, limit)
        ).fetchall()
        return [dict(r) for r in rows]


def _extract_usage(update: dict) -> tuple[int | None, int | None]:
    for value in update.values():
        messages = value.get("messages") if isinstance(value, dict) else None
        if not messages:
            continue
        for msg in messages:
            usage = getattr(msg, "usage_metadata", None)
            if usage:
                return usage.get("input_tokens"), usage.get("output_tokens")
    return None, None


def _level_for_update(node_name: str, node_update: dict) -> str:
    """Best-effort severity classification from what the node actually
    returned -- not every caller bothers to pass an explicit level."""
    status = node_update.get("status") if isinstance(node_update, dict) else None
    if status == "error":
        return "ERROR"
    if node_name == "reflector" and (node_update.get("consecutive_tool_errors") or 0) > 0:
        return "WARN"
    if node_name == "human_approval":
        return "WARN"
    return "INFO"


def stream_and_trace(
    graph,
    input_,
    config: dict,
    job_id: str | None = None,
    run_id: str | None = None,
    agent_name: str | None = None,
    task_id: str | None = None,
):
    """
    Wraps graph.stream(..., stream_mode="updates"): logs + persists each
    node's update as it happens, and yields it through unchanged so callers
    (CLI/API/core.supervisor) can still consume it live.

    A single-agent caller (main.py, the CLI, /tasks) passes only `graph`,
    `input_`, `config` -- job_id/run_id/agent_name/task_id stay NULL and the
    thread_id alone still identifies the run, exactly as before. A
    multi-agent Job run (core/supervisor.py) passes all four so this hop's
    events are attributable to one Job run and one role, not just a
    LangGraph thread_id.
    """
    thread_id = config.get("configurable", {}).get("thread_id", "unknown")
    common = dict(job_id=job_id, run_id=run_id, agent_name=agent_name, task_id=task_id)

    record_event(thread_id, node="_run", event_type="started", level="DEBUG", payload={"input": str(input_)[:2000]}, **common)

    try:
        for update in graph.stream(input_, config=config, stream_mode="updates"):
            for node_name, node_update in update.items():
                in_tok, out_tok = _extract_usage({node_name: node_update})
                record_event(
                    thread_id,
                    node=node_name,
                    event_type="update",
                    level=_level_for_update(node_name, node_update if isinstance(node_update, dict) else {}),
                    payload=_safe_payload(node_update),
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    **common,
                )
            yield update
    except Exception as e:
        record_event(thread_id, node="_run", event_type="exception", level="ERROR", payload={"error": str(e)}, **common)
        raise

    record_event(thread_id, node="_run", event_type="finished", level="DEBUG", **common)


def _safe_payload(node_update: dict) -> dict:
    out = {}
    for k, v in node_update.items():
        if k == "messages":
            out["messages"] = [
                {"type": type(m).__name__, "content": str(getattr(m, "content", ""))[:1000]} for m in v
            ]
        else:
            try:
                json.dumps(v, default=str)
                out[k] = v
            except TypeError:
                out[k] = str(v)
    return out
