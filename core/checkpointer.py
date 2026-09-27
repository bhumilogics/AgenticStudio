"""
Session/state persistence for the graph, via LangGraph's SQLite
checkpointer. This is what makes a run resumable across process
restarts (and what lets the FastAPI /resume endpoint pick a paused,
awaiting-human thread back up).
"""

from __future__ import annotations

import os
import sqlite3

from .config import settings


def get_checkpointer():
    from langgraph.checkpoint.sqlite import SqliteSaver

    db_path = settings.checkpoint_db_path
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

    conn = sqlite3.connect(db_path, check_same_thread=False)
    return SqliteSaver(conn)
