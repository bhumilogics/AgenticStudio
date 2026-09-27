"""
Minimal observability dashboard: `streamlit run observability/dashboard.py`

Shows recent runs, per-node event timeline, and token usage — reads
straight from the events SQLite DB tracer.py writes to. No separate
service to stand up.
"""

from __future__ import annotations

import json
import os
import sys

import pandas as pd
import streamlit as st

# Ensure the project root (parent of this file's directory) is importable,
# regardless of the cwd `streamlit run` was launched from.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from observability.tracer import get_events  # noqa: E402

st.set_page_config(page_title="AgenticStudio Observability", layout="wide")
st.title("AgenticStudio — Run Observability")

events = get_events(limit=2000)
if not events:
    st.info("No runs recorded yet. Kick off a task, then refresh this page.")
    st.stop()

df = pd.DataFrame(events)
df["ts"] = pd.to_datetime(df["ts"], unit="s")

thread_ids = sorted(df["thread_id"].unique(), reverse=True)
selected_thread = st.sidebar.selectbox("Thread", ["(all)"] + list(thread_ids))

view = df if selected_thread == "(all)" else df[df["thread_id"] == selected_thread]

col1, col2, col3 = st.columns(3)
col1.metric("Events shown", len(view))
col2.metric("Total input tokens", int(view["input_tokens"].fillna(0).sum()))
col3.metric("Total output tokens", int(view["output_tokens"].fillna(0).sum()))

st.subheader("Timeline")
st.dataframe(
    view[["ts", "thread_id", "node", "event_type", "input_tokens", "output_tokens"]].sort_values("ts", ascending=False),
    use_container_width=True,
)

st.subheader("Event detail")
options = view.sort_values("ts", ascending=False)
labels = [f"{row.ts} · {row.node} · {row.event_type}" for row in options.itertuples()]
if labels:
    idx = st.selectbox("Pick an event", range(len(labels)), format_func=lambda i: labels[i])
    row = options.iloc[idx]
    try:
        payload = json.loads(row["payload"]) if row["payload"] else {}
    except Exception:
        payload = {"raw": row["payload"]}
    st.json(payload)
