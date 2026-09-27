"""
FastAPI interface.

    uvicorn interface.api:app --reload

    POST /tasks                          {"task": "..."}            -> start
    POST /tasks/{thread_id}/resume        {"decision": "approve"}     -> resume
    GET  /tasks/{thread_id}                                          -> current state
    GET  /tasks/{thread_id}/events                                   -> audit trail
    GET  /tools                                                      -> registry
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from core.config import settings
from core.graph import build_graph
from core.prompts import PLANNER_SYSTEM_PROMPT
from core.state import initial_state
from observability import configure_logging, get_events, stream_and_trace
from observability.logging_config import get_logger

configure_logging()
logger = get_logger("agenticstudio.api")

app = FastAPI(title="AgenticStudio", description="Autonomous general-purpose agent framework")

_graph = None


def _get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


class StartTaskRequest(BaseModel):
    task: str
    thread_id: Optional[str] = None


class ResumeRequest(BaseModel):
    decision: str  # "approve" or "deny: <reason>"


def _serialize_state(config: dict) -> dict:
    graph = _get_graph()
    snapshot = graph.get_state(config)
    values = dict(snapshot.values)
    values.pop("messages", None)  # avoid dumping raw LC message objects over the wire
    return {
        "status": values.get("status"),
        "final_answer": values.get("final_answer"),
        "error": values.get("error"),
        "iterations": values.get("iterations"),
        "paused_before": list(snapshot.next) if snapshot.next else [],
    }


@app.post("/tasks")
async def start_task(req: StartTaskRequest):
    thread_id = req.thread_id or str(uuid.uuid4())
    graph = _get_graph()
    config = {"configurable": {"thread_id": thread_id}}

    state = initial_state(req.task, settings.max_iterations, settings.max_tool_retries)
    state["messages"] = [SystemMessage(content=PLANNER_SYSTEM_PROMPT), HumanMessage(content=req.task)]

    def _run():
        for _ in stream_and_trace(graph, state, config):
            pass

    await asyncio.to_thread(_run)
    return {"thread_id": thread_id, **_serialize_state(config)}


@app.post("/tasks/{thread_id}/resume")
async def resume(thread_id: str, req: ResumeRequest):
    from langgraph.types import Command

    graph = _get_graph()
    config = {"configurable": {"thread_id": thread_id}}

    if not graph.get_state(config).values:
        raise HTTPException(status_code=404, detail="unknown thread_id")

    def _run():
        for _ in stream_and_trace(graph, Command(resume=req.decision), config):
            pass

    await asyncio.to_thread(_run)
    return {"thread_id": thread_id, **_serialize_state(config)}


@app.get("/tasks/{thread_id}")
async def get_task(thread_id: str):
    config = {"configurable": {"thread_id": thread_id}}
    graph = _get_graph()
    if not graph.get_state(config).values:
        raise HTTPException(status_code=404, detail="unknown thread_id")
    return {"thread_id": thread_id, **_serialize_state(config)}


@app.get("/tasks/{thread_id}/events")
async def get_task_events(thread_id: str, limit: int = 200):
    return {"thread_id": thread_id, "events": get_events(thread_id=thread_id, limit=limit)}


@app.get("/tools")
async def list_tools(category: Optional[str] = None):
    from tools import get_all_tools, list_categories

    tools = get_all_tools(categories=[category] if category else None, verbose=False)
    return {
        "categories": list_categories(),
        "tools": [{"name": getattr(t, "name", "?"), "description": getattr(t, "description", "")} for t in tools],
    }


@app.get("/health")
async def health():
    return {"status": "ok"}
