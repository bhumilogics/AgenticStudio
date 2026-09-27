"""
Persists every graph node transition (and, where available, token usage)
to a small SQLite table, and logs it via structlog. This is the "why did
it do that" audit trail, and what a dashboard reads from.
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

_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    ts REAL NOT NULL,
    node TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER
);
CREATE INDEX IF NOT EXISTS idx_run_events_thread ON run_events(thread_id);
"""


@contextmanager
def _connect():
    db_path = settings.events_db_path
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def record_event(
    thread_id: str,
    node: str,
    event_type: str,
    payload: dict | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO run_events (thread_id, ts, node, event_type, payload, input_tokens, output_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (thread_id, time.time(), node, event_type, json.dumps(payload or {}, default=str), input_tokens, output_tokens),
        )
    logger.info("graph_event", thread_id=thread_id, node=node, event_type=event_type)


def get_events(thread_id: str | None = None, limit: int = 500) -> list[dict]:
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        if thread_id:
            rows = conn.execute(
                "SELECT * FROM run_events WHERE thread_id = ? ORDER BY id DESC LIMIT ?", (thread_id, limit)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM run_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
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


def stream_and_trace(graph, input_, config: dict):
    """
    Wraps graph.stream(..., stream_mode="updates"): logs + persists each
    node's update as it happens, and yields it through unchanged so callers
    (CLI/API) can still consume it live.
    """
    thread_id = config.get("configurable", {}).get("thread_id", "unknown")
    record_event(thread_id, node="_run", event_type="started", payload={"input": str(input_)[:2000]})

    try:
        for update in graph.stream(input_, config=config, stream_mode="updates"):
            for node_name, node_update in update.items():
                in_tok, out_tok = _extract_usage({node_name: node_update})
                record_event(
                    thread_id,
                    node=node_name,
                    event_type="update",
                    payload=_safe_payload(node_update),
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                )
            yield update
    except Exception as e:
        record_event(thread_id, node="_run", event_type="exception", payload={"error": str(e)})
        raise

    record_event(thread_id, node="_run", event_type="finished")


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
