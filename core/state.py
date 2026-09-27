"""
Shared LangGraph state schema.

`messages` is the running conversation (task, plan, tool calls, tool
results, replies) — the reducer `add_messages` appends rather than
overwrites, which is what lets the planner/tool-executor/reflector loop
share one growing transcript.

Everything else is small bookkeeping the reflector/router nodes use to
decide what happens next.
"""

from __future__ import annotations

from typing import Annotated, Literal, Optional

from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]

    task: str
    plan: list[str]

    status: Literal["planning", "acting", "reflecting", "awaiting_human", "done", "error"]

    iterations: int
    max_iterations: int

    consecutive_tool_errors: int
    max_tool_retries: int

    pending_approval_tool: Optional[str]
    human_response: Optional[str]

    final_answer: Optional[str]
    error: Optional[str]


def initial_state(task: str, max_iterations: int, max_tool_retries: int) -> AgentState:
    return AgentState(
        messages=[],
        task=task,
        plan=[],
        status="planning",
        iterations=0,
        max_iterations=max_iterations,
        consecutive_tool_errors=0,
        max_tool_retries=max_tool_retries,
        pending_approval_tool=None,
        human_response=None,
        final_answer=None,
        error=None,
    )
