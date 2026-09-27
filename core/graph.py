"""
The LangGraph orchestration core.

    START -> planner -> (route) -> tool_executor -> reflector -> (route) -> planner ...
                      \\-> human_approval -> (route) -> tool_executor / planner
                      \\-> END (final answer)

- planner: calls the LLM (tools bound). Produces/revises the plan, calls
  tools, or emits a "FINAL ANSWER:" to finish.
- human_approval: an interrupt point. Reached when the planner wants to
  call a tool that the autonomy policy says needs a checkpoint. Pauses
  the graph (via LangGraph's `interrupt()`) until resumed with an
  approve/deny decision.
- tool_executor: a prebuilt LangGraph ToolNode -- actually calls the tool(s).
- reflector: inspects the tool result(s). Decides continue / retry /
  escalate (too many failures -> stop and surface to a human rather than
  loop forever).

This is intentionally a plain StateGraph rather than `create_react_agent`,
so the retry/escalate/approval policy is visible and editable here rather
than hidden inside a prebuilt helper.
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, interrupt

from .checkpointer import get_checkpointer
from .config import settings
from .llm import get_llm_with_tools
from .prompts import PLANNER_SYSTEM_PROMPT, REFLECTOR_SYSTEM_PROMPT
from .state import AgentState

logger = logging.getLogger("agenticstudio.graph")


def _last_ai_message(state: AgentState) -> AIMessage | None:
    for msg in reversed(state["messages"]):
        if isinstance(msg, AIMessage):
            return msg
    return None


def _pending_tool_calls(state: AgentState) -> list[dict]:
    ai_msg = _last_ai_message(state)
    if ai_msg is None:
        return []
    return getattr(ai_msg, "tool_calls", None) or []


def _looks_like_error(content) -> bool:
    text = content if isinstance(content, str) else str(content)
    lowered = text.lower()
    return any(marker in lowered for marker in ("error", "traceback", "exception", "failed"))


def _format_recent_tool_results(tool_messages: list[ToolMessage]) -> str:
    lines = []
    for m in tool_messages:
        lines.append(f"tool_call_id={getattr(m, 'tool_call_id', '?')}\nresult: {m.content}")
    return "\n\n".join(lines)


def _parse_reflection_decision(text: str) -> str:
    """Map the reflector LLM's one-word answer onto our three routes,
    defaulting to the safe choice (retry) if it said something else."""
    lowered = (text or "").strip().lower()
    for candidate in ("escalate", "retry", "continue"):
        if candidate in lowered:
            return candidate
    return "retry"


def _llm_backed_reflection(tool_messages: list[ToolMessage]) -> str | None:
    """
    Ask an LLM to classify the recent tool failure(s) as continue / retry /
    escalate, using whichever backend settings.reflector_backend names.
    Returns None (caller falls back to the heuristic-only path) on any
    backend failure -- a broken reflection backend should never take the
    whole run down.
    """
    backend = settings.reflector_backend
    if backend == "heuristic":
        return None

    prompt = _format_recent_tool_results(tool_messages)

    if backend == "claude-code":
        from .backends.claude_code import ClaudeCodeUnavailable, complete

        try:
            result = complete(REFLECTOR_SYSTEM_PROMPT, prompt, timeout=settings.claude_code_timeout)
            return _parse_reflection_decision(result.text)
        except ClaudeCodeUnavailable as e:
            logger.warning("claude-code reflector backend unavailable (%s) -- falling back to heuristic", e)
            return None
        except Exception as e:
            logger.warning("claude-code reflector backend failed (%s) -- falling back to heuristic", e)
            return None

    if backend == "api":
        from .llm import get_llm

        try:
            response = get_llm().invoke([SystemMessage(content=REFLECTOR_SYSTEM_PROMPT), HumanMessage(content=prompt)])
            content = response.content if isinstance(response.content, str) else str(response.content)
            return _parse_reflection_decision(content)
        except Exception as e:
            logger.warning("api reflector backend failed (%s) -- falling back to heuristic", e)
            return None

    logger.warning("unknown reflector_backend %r -- falling back to heuristic", backend)
    return None


def build_graph(tools: list | None = None, risky_tools: list[str] | None = None):
    """
    Build and compile the agent graph.

    `tools` defaults to tools.get_all_tools() (everything currently
    configured). `risky_tools` defaults to settings.risky_tools.
    """
    if tools is None:
        from tools import get_all_tools

        tools = get_all_tools(categories=settings.tool_categories)

    risky_tools = set(risky_tools if risky_tools is not None else settings.risky_tools)

    llm_with_tools = get_llm_with_tools(tools)
    tool_node = ToolNode(tools)

    # ---------------------------------------------------------------- planner
    def planner(state: AgentState) -> dict:
        messages = state["messages"]
        if not messages:
            messages = [SystemMessage(content=PLANNER_SYSTEM_PROMPT)]
        elif not any(isinstance(m, SystemMessage) for m in messages):
            messages = [SystemMessage(content=PLANNER_SYSTEM_PROMPT), *messages]

        # Fold a pending human response back in as plain context, if present.
        if state.get("human_response"):
            from langchain_core.messages import HumanMessage

            messages = [*messages, HumanMessage(content=f"[human input] {state['human_response']}")]

        response = llm_with_tools.invoke(messages)

        update: dict = {
            "messages": [response],
            "iterations": state["iterations"] + 1,
            "human_response": None,
        }

        if not response.tool_calls:
            content = response.content if isinstance(response.content, str) else str(response.content)
            if content.strip().upper().startswith("FINAL ANSWER"):
                update["status"] = "done"
                update["final_answer"] = content.split(":", 1)[-1].strip()
            else:
                # Model spoke without calling a tool and without
                # signaling completion -- treat as done to avoid looping
                # forever on a chatty non-tool response.
                update["status"] = "done"
                update["final_answer"] = content
        else:
            update["status"] = "acting"

        return update

    def route_after_planner(state: AgentState) -> str:
        if state["status"] == "done":
            return END
        if state["iterations"] >= state["max_iterations"]:
            return "give_up"

        pending = _pending_tool_calls(state)
        needs_approval = settings.autonomy_level == "supervised" or any(
            tc["name"] in risky_tools for tc in pending
        )
        return "human_approval" if needs_approval else "tool_executor"

    # --------------------------------------------------------- human_approval
    def human_approval(state: AgentState) -> dict:
        pending = _pending_tool_calls(state)
        calls_desc = [{"tool": tc["name"], "args": tc["args"]} for tc in pending]

        decision = interrupt(
            {
                "reason": "checkpoint required before executing tool call(s)",
                "pending_tool_calls": calls_desc,
                "instructions": 'Resume with Command(resume="approve") or Command(resume="deny: <reason>")',
            }
        )

        decision_str = str(decision or "").strip().lower()
        if decision_str.startswith("approve"):
            return {"status": "acting"}

        # Denied: synthesize tool-error-shaped messages so the planner sees
        # exactly which calls were blocked and why, then loop back to plan
        # again rather than executing them.
        reason = decision_str.split(":", 1)[-1].strip() if ":" in decision_str else "denied by human"
        denial_messages = [
            ToolMessage(content=f"Denied by human: {reason}", tool_call_id=tc["id"]) for tc in pending
        ]
        return {"messages": denial_messages, "status": "planning"}

    def route_after_human(state: AgentState) -> str:
        return "tool_executor" if state["status"] == "acting" else "planner"

    # ------------------------------------------------------------ reflector
    def reflector(state: AgentState) -> dict:
        recent_tool_messages = [m for m in state["messages"][-10:] if isinstance(m, ToolMessage)]
        had_error = any(_looks_like_error(m.content) for m in recent_tool_messages)

        if not had_error:
            return {"status": "planning", "consecutive_tool_errors": 0}

        errors_so_far = state["consecutive_tool_errors"] + 1

        # Heuristic already found an error -- optionally ask an LLM (via
        # settings.reflector_backend) to classify it rather than always
        # just retrying. None (backend unset/unavailable/failed) keeps the
        # original heuristic-only behavior.
        decision = _llm_backed_reflection(recent_tool_messages) or "retry"

        if decision == "escalate" or errors_so_far > state["max_tool_retries"]:
            return {
                "status": "error",
                "consecutive_tool_errors": errors_so_far,
                "error": "Repeated tool failures exceeded max_tool_retries; escalating."
                if errors_so_far > state["max_tool_retries"]
                else "Reflector escalated after inspecting the tool failure.",
            }

        logger.warning("tool error, retry %s/%s (reflector decision=%s)", errors_so_far, state["max_tool_retries"], decision)
        return {"status": "planning", "consecutive_tool_errors": errors_so_far}

    def route_after_reflector(state: AgentState) -> str:
        return "planner" if state["status"] == "planning" else END

    # -------------------------------------------------------------- give_up
    def give_up(state: AgentState) -> dict:
        return {"status": "error", "error": "max_iterations reached without a final answer"}

    # ------------------------------------------------------------- assemble
    graph = StateGraph(AgentState)
    graph.add_node("planner", planner)
    graph.add_node("human_approval", human_approval)
    graph.add_node("tool_executor", tool_node)
    graph.add_node("reflector", reflector)
    graph.add_node("give_up", give_up)

    graph.add_edge(START, "planner")
    graph.add_conditional_edges(
        "planner", route_after_planner, {"tool_executor": "tool_executor", "human_approval": "human_approval", "give_up": "give_up", END: END}
    )
    graph.add_conditional_edges("human_approval", route_after_human, {"tool_executor": "tool_executor", "planner": "planner"})
    graph.add_edge("tool_executor", "reflector")
    graph.add_conditional_edges("reflector", route_after_reflector, {"planner": "planner", END: END})
    graph.add_edge("give_up", END)

    return graph.compile(checkpointer=get_checkpointer())


def run_task(task: str, thread_id: str, graph=None) -> dict:
    """Synchronous convenience runner: invoke the graph to completion (or
    until it pauses on a human_approval interrupt) and return final state."""
    from .state import initial_state

    graph = graph or build_graph()
    config = {"configurable": {"thread_id": thread_id}}
    state = initial_state(task, settings.max_iterations, settings.max_tool_retries)
    state["messages"] = [SystemMessage(content=PLANNER_SYSTEM_PROMPT)]
    from langchain_core.messages import HumanMessage

    state["messages"].append(HumanMessage(content=task))

    return graph.invoke(state, config=config)


def resume_task(thread_id: str, decision: str, graph=None) -> dict:
    """Resume a thread paused at human_approval with 'approve' or 'deny: <reason>'."""
    graph = graph or build_graph()
    config = {"configurable": {"thread_id": thread_id}}
    return graph.invoke(Command(resume=decision), config=config)
