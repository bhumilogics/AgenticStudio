"""
Runs a Job's Tasks as a dependency DAG across its agents, each on its own
compiled LangGraph subgraph (core/graph.build_graph(agent_def=...)).

This replaces the earlier "straight dispatch down job.agents in order"
supervisor with something that still behaves identically for a Job that
has no explicit Tasks (or one Task per agent in a straight chain -- the
common case), but additionally supports what the plain agent-order model
could not:

  - Fan-out / parallel execution: two Tasks that both depend only on the
    same upstream Task (e.g. qa_regression_job.yaml's `automate` and
    `execute`, both depends_on: [plan]) now genuinely run at the same time,
    each on its own thread and its own LangGraph thread_id, instead of one
    waiting on the other just because of `agents` list order.
  - Retry-then-escalate: if an agent's subgraph run for a Task ends in its
    own status="error" (it already exhausted ITS internal tool-retry
    budget -- core/graph.py's reflector loop), the supervisor retries that
    same agent a bounded number of extra times (reusing
    agent_def.resolved_max_tool_retries as the budget), and if still
    failing, escalates by handing the failure to the agent's `reports_to`
    role (agents/*.md frontmatter) as a new task asking it to resolve or
    take over -- rather than failing the whole Job the instant one Task's
    own retries run out. Only if the escalation target ALSO fails (or
    there's no reports_to) does the Job's run actually error out.

DAG construction (see _build_dag()): every explicit Task
(core/job_loader.TaskDefinition) becomes a node keyed by its own id, using
its own `depends_on`. Any agent in job.agents that owns no Task of its own
gets one synthetic pass-through node, wired to depend on the previous
agent's terminal node(s) -- this is what makes a Job with no Tasks at all
(just an ordered `agents` list) behave exactly like the old sequential
chain, node-for-node.

Known v1 limitation, deliberate rather than accidental: if two branches of
the DAG both hit a human_approval pause in the same round, only one is
surfaced as `awaiting_human` (the others are left un-run and are re-
attempted, in whatever order the DAG gives them, on the next dispatch once
the surfaced one is resumed). Multi-pause support is a real gap for a Job
whose parallel branches both need sign-off, not something this file quietly
gets right -- documented rather than hidden.

    from core.supervisor import run_job, run_job_resume

    result = run_job(job, task, run_id, on_step=on_step)
    # {"status": "done", "final_answer": ...}
    # {"status": "error", "error": ..., "failed_agent": ...}
    # {"status": "awaiting_human", "paused_agent": ..., "paused_thread_id": ...}

    result = run_job_resume(job, run_id, task, paused_agent, decision, on_step=on_step)
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from langchain_core.messages import HumanMessage

from .agent_loader import load_agent
from .graph import build_graph
from .job_loader import JobDefinition
from .state import initial_state

_graph_cache: dict[str, object] = {}

_MAX_PARALLEL_WORKERS = 8


def _graph_for_agent(agent_name: str):
    if agent_name not in _graph_cache:
        _graph_cache[agent_name] = build_graph(agent_def=load_agent(agent_name))
    return _graph_cache[agent_name]


# --------------------------------------------------------------- DAG nodes

@dataclass
class _Node:
    id: str
    agent: str
    name: str
    instructions: str
    depends_on: list[str] = field(default_factory=list)


def _build_dag(job: JobDefinition) -> dict[str, _Node]:
    """
    One node per explicit Task, plus a synthetic pass-through node for any
    agent in job.agents that owns no Task of its own. A pass-through node
    (and any Task left with an empty `depends_on` in the YAML) is wired to
    depend on the *terminal* node(s) of the previous agent in job.agents --
    the node(s) of that agent that nothing else belonging to the same agent
    depends on. For a Job with no Tasks at all this produces exactly the
    old straight chain, one synthetic node per agent.
    """
    nodes: dict[str, _Node] = {}
    for t in job.tasks:
        nodes[t.id] = _Node(id=t.id, agent=t.agent, name=t.name, instructions=t.instructions, depends_on=list(t.depends_on))

    prev_terminal_ids: list[str] = []  # [] => "the Job's own task text", no predecessor yet
    for agent_name in job.agents:
        own_ids = [nid for nid, n in nodes.items() if n.agent == agent_name]

        if own_ids:
            for nid in own_ids:
                if not nodes[nid].depends_on:
                    nodes[nid].depends_on = list(prev_terminal_ids)
            depended_on_within_agent = {
                d for nid in own_ids for d in nodes[nid].depends_on if d in own_ids
            }
            prev_terminal_ids = [nid for nid in own_ids if nid not in depended_on_within_agent]
        else:
            nid = f"_agent_{agent_name}"
            nodes[nid] = _Node(id=nid, agent=agent_name, name=f"{agent_name} (pass-through)", instructions="", depends_on=list(prev_terminal_ids))
            prev_terminal_ids = [nid]

    return nodes


def _terminal_node_ids(nodes: dict[str, _Node]) -> list[str]:
    depended_on = {d for n in nodes.values() for d in n.depends_on}
    return [nid for nid in nodes if nid not in depended_on]


def _task_input_for(node: _Node, job_task: str, upstream_outputs: list[str]) -> str:
    """What this node's agent is actually asked to do: its own instructions
    (if it's a real Task) plus whatever its upstream node(s) produced, or
    just the Job's own task text verbatim if this is the first node with no
    upstream and no instructions of its own (a pass-through first agent)."""
    if not node.instructions and not upstream_outputs:
        return job_task

    parts = [f"Job task: {job_task}"]
    if upstream_outputs:
        if len(upstream_outputs) == 1:
            parts.append(f"Input from the previous step:\n{upstream_outputs[0]}")
        else:
            joined = "\n\n".join(f"- {o}" for o in upstream_outputs)
            parts.append(f"Input from {len(upstream_outputs)} previous steps:\n{joined}")
    if node.instructions:
        parts.append(f"Your assigned task ({node.name}):\n{node.instructions}")
    return "\n\n".join(parts)


# ------------------------------------------------------------- agent hops

def _run_one_agent(job: JobDefinition, run_id: str, agent_name: str, agent_task: str, on_step=None) -> dict:
    """Run a single agent's subgraph to completion or to its own
    human_approval pause, tracing every step against this Job run."""
    from observability import stream_and_trace

    agent_def = load_agent(agent_name)
    graph = _graph_for_agent(agent_name)
    thread_id = f"{run_id}-{agent_name}"
    config = {"configurable": {"thread_id": thread_id}}

    state = initial_state(agent_task, agent_def.resolved_max_iterations, agent_def.resolved_max_tool_retries)
    state["messages"] = [HumanMessage(content=agent_task)]

    for _ in stream_and_trace(graph, state, config, job_id=job.id, run_id=run_id, agent_name=agent_name):
        pass

    final_state = graph.get_state(config).values
    if on_step:
        on_step(agent_name, thread_id, final_state)
    return final_state


def _resume_one_agent(job: JobDefinition, run_id: str, agent_name: str, decision: str, on_step=None) -> dict:
    from langgraph.types import Command

    from observability import stream_and_trace

    graph = _graph_for_agent(agent_name)
    thread_id = f"{run_id}-{agent_name}"
    config = {"configurable": {"thread_id": thread_id}}

    for _ in stream_and_trace(graph, Command(resume=decision), config, job_id=job.id, run_id=run_id, agent_name=agent_name):
        pass

    final_state = graph.get_state(config).values
    if on_step:
        on_step(agent_name, thread_id, final_state)
    return final_state


def _safe_run_agent_call(fn, *args, **kwargs) -> dict:
    """Call _run_one_agent/_resume_one_agent, converting an unexpected
    exception into the same {"status": "error", "error": ...} shape a
    graceful in-band failure already returns. Without this, a real crash
    below LangGraph (a backend subprocess dying, a malformed model reply,
    etc. -- exactly what happened with core/backends/claude_code.py's
    Windows stdout-decoding bug) propagates as a raw exception through the
    ThreadPoolExecutor future, which takes down the entire dispatch round
    (and the HTTP request calling it) instead of being handled as one
    task's failure with the normal retry/escalation path."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def _run_node_with_retry_and_escalation(job: JobDefinition, run_id: str, node: _Node, input_text: str, on_step=None) -> dict:
    """
    Run `node`'s agent. On its own status="error" (its internal tool-retry
    budget is already exhausted -- see core/graph.py), retry the same agent
    a few more times (budget = its own resolved_max_tool_retries), then, if
    still failing, hand the failure to its `reports_to` role as a new task.

    Returns one of:
      {"status": "done", "output": "..."}
      {"status": "awaiting_human", "paused_agent": ..., "paused_thread_id": ...}
      {"status": "error", "error": ..., "failed_agent": ...}
    """
    from observability import record_event

    agent_def = load_agent(node.agent)
    max_attempts = 1 + max(agent_def.resolved_max_tool_retries, 0)
    last_error = None

    for attempt in range(1, max_attempts + 1):
        final_state = _safe_run_agent_call(_run_one_agent, job, run_id, node.agent, input_text, on_step)
        status = final_state.get("status")
        if status == "done":
            return {"status": "done", "output": final_state.get("final_answer")}
        if status != "error":
            return {
                "status": "awaiting_human",
                "paused_agent": node.agent,
                "paused_thread_id": f"{run_id}-{node.agent}",
            }
        last_error = final_state.get("error")
        record_event(
            thread_id=f"{run_id}-{node.agent}", node="supervisor", event_type="task_attempt_failed",
            level="WARN", job_id=job.id, run_id=run_id, agent_name=node.agent, task_id=node.id,
            payload={"attempt": attempt, "max_attempts": max_attempts, "error": last_error},
        )

    if agent_def.reports_to:
        escalation_task = (
            f"Agent '{node.agent}' failed the task '{node.name}' after {max_attempts} attempt(s).\n"
            f"Last error: {last_error}\n\nOriginal task input:\n{input_text}\n\n"
            "Please review this failure and either resolve it yourself or provide a final answer/decision."
        )
        record_event(
            thread_id=f"{run_id}-{agent_def.reports_to}", node="supervisor", event_type="escalated",
            level="WARN", job_id=job.id, run_id=run_id, agent_name=agent_def.reports_to, task_id=node.id,
            payload={"escalated_from": node.agent, "error": last_error},
        )
        escalated_state = _safe_run_agent_call(_run_one_agent, job, run_id, agent_def.reports_to, escalation_task, on_step)
        escalated_status = escalated_state.get("status")
        if escalated_status == "done":
            return {"status": "done", "output": escalated_state.get("final_answer")}
        if escalated_status != "error":
            return {
                "status": "awaiting_human",
                "paused_agent": agent_def.reports_to,
                "paused_thread_id": f"{run_id}-{agent_def.reports_to}",
            }
        last_error = escalated_state.get("error")

    return {"status": "error", "error": last_error, "failed_agent": node.agent}


# --------------------------------------------------------------- dispatch

def _dispatch(job: JobDefinition, run_id: str, task: str, nodes: dict[str, _Node], node_outputs: dict[str, str], on_step=None) -> dict:
    """
    Run every node in `nodes` whose dependencies are already satisfied by
    `node_outputs`, round by round, running each round's ready nodes
    concurrently. Mutates `node_outputs` in place as nodes complete, so a
    caller that persists it (core.job_store's runs.dag_state) can resume
    exactly where this left off.
    """
    remaining = {nid: n for nid, n in nodes.items() if nid not in node_outputs}

    while remaining:
        ready = [
            nid for nid, n in remaining.items()
            if all(dep in node_outputs for dep in n.depends_on)
        ]
        if not ready:
            # a real cycle or a dependency on a node that will never run --
            # shouldn't happen given job_loader's validation, but fail loudly
            # rather than spin forever.
            return {
                "status": "error",
                "error": f"no runnable task in {list(remaining)} -- unsatisfiable dependency (cycle or missing task)",
                "failed_agent": None,
                "final_answer": None,
            }

        results: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=min(len(ready), _MAX_PARALLEL_WORKERS)) as pool:
            futures = {}
            for nid in ready:
                node = remaining[nid]
                upstream_outputs = [node_outputs[d] for d in node.depends_on]
                input_text = _task_input_for(node, task, upstream_outputs)
                futures[pool.submit(_run_node_with_retry_and_escalation, job, run_id, node, input_text, on_step)] = nid
            for fut in as_completed(futures):
                results[futures[fut]] = fut.result()

        # Deterministic handling when this round mixes outcomes: prefer
        # surfacing an error, then a pause, over quietly finishing only the
        # nodes that happened to succeed -- but nodes that DID complete this
        # round are kept in node_outputs either way (progress isn't lost).
        errored = {nid: r for nid, r in results.items() if r["status"] == "error"}
        paused = {nid: r for nid, r in results.items() if r["status"] == "awaiting_human"}
        done = {nid: r for nid, r in results.items() if r["status"] == "done"}

        for nid, r in done.items():
            node_outputs[nid] = r["output"]
            del remaining[nid]

        if errored:
            first = sorted(errored)[0]
            return {"status": "error", "error": errored[first]["error"], "failed_agent": errored[first]["failed_agent"], "final_answer": None}
        if paused:
            first = sorted(paused)[0]
            return {
                "status": "awaiting_human",
                "paused_agent": paused[first]["paused_agent"],
                "paused_thread_id": paused[first]["paused_thread_id"],
                "final_answer": None,
            }

    terminal_ids = _terminal_node_ids(nodes)
    outputs = [node_outputs[nid] for nid in terminal_ids if nid in node_outputs]
    if len(outputs) == 1:
        final_answer = outputs[0]
    else:
        final_answer = "\n\n".join(f"[{nodes[nid].name}]\n{node_outputs[nid]}" for nid in terminal_ids if nid in node_outputs)

    return {"status": "done", "final_answer": final_answer, "error": None}


def run_job(job: JobDefinition, task: str, run_id: str, on_step=None) -> dict:
    """
    Run every Task/agent in the Job's dependency DAG, starting fresh.

    `on_step(agent_name, thread_id, final_state)` fires after every agent
    hop -- interface/api.py uses it to persist per-hop progress via
    core/job_store.update_run() and to stream status to the UI.
    """
    nodes = _build_dag(job)
    node_outputs: dict[str, str] = {}
    result = _dispatch(job, run_id, task, nodes, node_outputs, on_step=on_step)
    result["_dag_state"] = {"node_outputs": node_outputs}
    return result


def run_job_resume(job: JobDefinition, run_id: str, task: str, paused_agent: str, decision: str, on_step=None, dag_state: dict | None = None) -> dict:
    """
    Resume a Job run that's paused at `paused_agent`'s own human_approval,
    merge its result into whatever DAG progress was already persisted
    (`dag_state`, from the paused run_job()/run_job_resume() call's
    "_dag_state" -- see interface/api.py, which round-trips it through
    core.job_store.update_run(run_id, dag_state=json.dumps(...))), then
    continue dispatching whatever nodes are still runnable.

    `task` is the Job run's original task text (core.job_store.get_run(run_id)["task"]).
    """
    nodes = _build_dag(job)
    node_outputs: dict[str, str] = dict((dag_state or {}).get("node_outputs") or {})

    # Which node was paused at `paused_agent`? Prefer a node still pending
    # whose agent matches; fall back to the first such node found.
    paused_node_id = next((nid for nid, n in nodes.items() if n.agent == paused_agent and nid not in node_outputs), None)

    final_state = _safe_run_agent_call(_resume_one_agent, job, run_id, paused_agent, decision, on_step)
    status = final_state.get("status")
    if status == "error":
        result = {"status": "error", "error": final_state.get("error"), "failed_agent": paused_agent, "final_answer": None}
        result["_dag_state"] = {"node_outputs": node_outputs}
        return result
    if status != "done":
        result = {
            "status": "awaiting_human",
            "paused_agent": paused_agent,
            "paused_thread_id": f"{run_id}-{paused_agent}",
            "final_answer": None,
        }
        result["_dag_state"] = {"node_outputs": node_outputs}
        return result

    if paused_node_id is not None:
        node_outputs[paused_node_id] = final_state.get("final_answer")

    result = _dispatch(job, run_id, task, nodes, node_outputs, on_step=on_step)
    result["_dag_state"] = {"node_outputs": node_outputs}
    return result
