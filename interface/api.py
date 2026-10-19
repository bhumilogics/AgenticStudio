"""
FastAPI interface.

    uvicorn interface.api:app --reload

Single-agent (unchanged shape, now agent-selectable):
    POST /tasks                          {"task": "...", "agent": "planner"} -> start
    POST /tasks/{thread_id}/resume        {"decision": "approve"}     -> resume
    GET  /tasks/{thread_id}                                          -> current state
    GET  /tasks/{thread_id}/events                                   -> audit trail
    GET  /tools                                                      -> tool registry

Auth (core/auth.py + `users` table in core/job_store.py):
    POST   /auth/register                {"email","password"}    -> creates a user (first ever registration
                                                                      becomes admin automatically); returns a token
    POST   /auth/login                   {"email","password"}    -> {"token","user"}
    GET    /auth/me                      (Bearer token)           -> the caller's own user record
    GET    /auth/users                   (Bearer token, admin)    -> list all users

    Every route below except /health, /auth/register and /auth/login requires
    `Authorization: Bearer <token>`. Mutating an Agent/Job definition, or
    creating/editing/deleting a Schedule, requires the `admin` role; running a
    Job, resuming a Run, and reading things back is open to any authenticated
    user, but list/read endpoints for Jobs/Runs are scoped to the caller's own
    (created_by / triggered_by_user) rows unless the caller is an admin.

Admin user management also supports creating users, updating email addresses and
roles, changing passwords, and deleting accounts.

Agents (agents/*.md):
    GET    /agents                       -> registry
    GET    /agents/{name}                -> one agent's full parsed definition
    POST   /agents/draft                 {"name","description"} -> NL-generated draft, NOT saved
    PUT    /agents/{name}                {"markdown": "..."}    -> validate + save                [admin]
    DELETE /agents/{name}                                                                          [admin]

Jobs (jobs/*.yaml + DB mirror in core/job_store.py):
    GET    /jobs                         -> registry (synced from jobs/*.yaml on read; owner-scoped)
    GET    /jobs/{id}                    -> full definition
    GET    /jobs/{id}/graph              -> {nodes, edges} for the workflow diagram
    POST   /jobs/draft                   {"id","description"} -> NL-generated draft, NOT saved
    PUT    /jobs/{id}                    {"yaml": "..."}       -> validate + save + sync to DB      [admin]
    DELETE /jobs/{id}                                                                               [admin]
    POST   /jobs/{id}/run                {"task": "..."}       -> start a multi-agent Run
    GET    /jobs/{id}/runs               -> run history for this Job (owner-scoped)

Runs (one execution of a Job, across every agent):
    GET  /runs/{run_id}                  -> status, current agent, final answer (owner-scoped)
                                             includes "stale": true if status="running" and no
                                             heartbeat (core.job_store.update_run) in over
                                             settings.stale_run_seconds -- see mark_failed below
    POST /runs/{run_id}/resume           {"decision": "approve"} -> resume a paused agent, continue the Job
    POST /runs/{run_id}/mark_failed                              -> manually fail a stuck "running" Run
                                             (owner or admin; 409 if the run isn't "running")
    GET  /runs/{run_id}/events           ?level=&agent=&task=    -> the Log Explorer's data source

    /runs/{run_id} and /runs/{run_id}/events both tolerate being passed a
    thread_id (f"{run_id}-{agent_name}", core/supervisor.py's internal
    LangGraph checkpoint key) by mistake instead of a run_id -- easy to do
    since thread_id is what's printed alongside run_id in the console trace
    logs. See core.job_store.resolve_run_id().

TestDesk (TestDesk/routes.py -- scenarios run in a throwaway sandbox or for real):
    /testdesk/targets, /testdesk/scenarios, /testdesk/run, /testdesk/results  (see that module)

Schedules (persisted; run against a Job on a cron):                                          [admin for all of these]
    GET    /schedules
    POST   /schedules                    {"job_id","cron","task"} (task = default task text for each fire)
    PUT    /schedules/{id}               {"enabled": false}
    DELETE /schedules/{id}
    POST   /schedules/{id}/run-now
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from typing import Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.messages import HumanMessage
from pydantic import BaseModel

from core.config import settings
from core.graph import build_graph
from core.state import initial_state
from observability import configure_logging, get_events, stream_and_trace
from observability.logging_config import get_logger

configure_logging()
logger = get_logger("agenticstudio.api")

app = FastAPI(title="AgenticStudio", description="Autonomous general-purpose agent framework")

# The GUI (frontend/) is a static page fetched from a different origin
# (file://, or a plain `python -m http.server`) than uvicorn's, so it needs
# CORS. Tighten allow_origins to the real deployed origin before this goes
# anywhere multi-tenant.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# One compiled graph per agent role, built lazily and cached -- avoids
# rebuilding (re-parsing the .md + rebinding tools) on every request.
_graphs: dict[str, object] = {}

# Backing scheduler for /schedules -- see startup_event() below, which loads
# every persisted, enabled schedule from core.job_store on process start.
_scheduler = None


def _get_graph(agent: str = "planner"):
    if agent not in _graphs:
        from core.agent_loader import AgentLoadError, load_agent

        try:
            agent_def = load_agent(agent)
        except AgentLoadError as e:
            raise HTTPException(status_code=400, detail=f"unknown or invalid agent {agent!r}: {e}")
        _graphs[agent] = build_graph(agent_def=agent_def)
    return _graphs[agent]


# ------------------------------------------------------------------- auth
#
# A user record is looked up fresh on every request from the Bearer token's
# `sub` claim (rather than trusting the token's embedded role forever) so a
# role change or account deletion takes effect on the very next request
# without waiting for the token to expire.


class CurrentUser(BaseModel):
    id: str
    email: str
    role: str


async def get_current_user(authorization: Optional[str] = Header(default=None)) -> CurrentUser:
    from core.auth import AuthError, decode_token
    from core.job_store import get_user_by_id

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        claims = decode_token(token)
    except AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))

    row = get_user_by_id(claims.get("sub", ""))
    if not row:
        raise HTTPException(status_code=401, detail="user no longer exists")
    return CurrentUser(id=row["id"], email=row["email"], role=row["role"])


async def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="this action requires the admin role")
    return user


class RegisterRequest(BaseModel):
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


class AdminCreateUserRequest(BaseModel):
    email: str
    password: str
    role: Literal["end_user", "admin"] = "end_user"


class AdminUpdateUserRequest(BaseModel):
    email: Optional[str] = None
    role: Optional[Literal["end_user", "admin"]] = None


class AdminChangePasswordRequest(BaseModel):
    password: str


@app.post("/auth/register")
async def register_endpoint(req: RegisterRequest):
    from core.auth import AuthError, create_token, register_user

    try:
        user = await asyncio.to_thread(register_user, req.email, req.password)
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    token = create_token(user["id"], user["email"], user["role"])
    return {"token": token, "user": user}


@app.post("/auth/login")
async def login_endpoint(req: LoginRequest):
    from core.auth import AuthError, authenticate, create_token

    try:
        row = await asyncio.to_thread(authenticate, req.email, req.password)
    except AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    token = create_token(row["id"], row["email"], row["role"])
    return {"token": token, "user": {"id": row["id"], "email": row["email"], "role": row["role"]}}


@app.get("/auth/me")
async def me_endpoint(user: CurrentUser = Depends(get_current_user)):
    return user


@app.get("/auth/users")
async def list_users_endpoint(_: CurrentUser = Depends(require_admin)):
    from core.job_store import list_users

    return {"users": list_users()}


@app.post("/auth/users", status_code=201)
async def create_user_endpoint(req: AdminCreateUserRequest, _: CurrentUser = Depends(require_admin)):
    from core.auth import AuthError, register_user

    email = req.email.strip()
    if not email or not req.password:
        raise HTTPException(status_code=422, detail="email and password are required")
    try:
<<<<<<< Updated upstream
        return await asyncio.to_thread(register_user, email, req.password, req.role)
=======
        user = await asyncio.to_thread(register_user, email, req.password, req.role)
>>>>>>> Stashed changes
    except AuthError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="email is already registered")
<<<<<<< Updated upstream
=======
    return user
>>>>>>> Stashed changes


@app.patch("/auth/users/{user_id}")
async def update_user_endpoint(
    user_id: str,
    req: AdminUpdateUserRequest,
    _: CurrentUser = Depends(require_admin),
):
    from core.job_store import get_user_by_id, update_user

    if req.email is None and req.role is None:
        raise HTTPException(status_code=422, detail="provide an email or role to update")
    existing = await asyncio.to_thread(get_user_by_id, user_id)
    if not existing:
        raise HTTPException(status_code=404, detail="user not found")
    email = req.email.strip() if req.email is not None else existing["email"]
    if not email:
        raise HTTPException(status_code=422, detail="email is required")
    role = req.role or existing["role"]
    try:
        updated = await asyncio.to_thread(update_user, user_id, email, role)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="email is already registered")
    if not updated:
        raise HTTPException(status_code=404, detail="user not found")
    return {"id": user_id, "email": email, "role": role}


@app.put("/auth/users/{user_id}/password")
async def change_user_password_endpoint(
    user_id: str,
    req: AdminChangePasswordRequest,
    _: CurrentUser = Depends(require_admin),
):
    from core.auth import hash_password
    from core.job_store import update_user_password

    if not req.password:
        raise HTTPException(status_code=422, detail="password is required")
    password_hash = await asyncio.to_thread(hash_password, req.password)
    updated = await asyncio.to_thread(update_user_password, user_id, password_hash)
    if not updated:
        raise HTTPException(status_code=404, detail="user not found")
    return {"updated": user_id}


@app.delete("/auth/users/{user_id}")
async def delete_user_endpoint(user_id: str, user: CurrentUser = Depends(require_admin)):
    from core.job_store import delete_user

    if user_id == user.id:
        raise HTTPException(status_code=409, detail="you cannot delete your own account")
    try:
        deleted = await asyncio.to_thread(delete_user, user_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="user not found")
    return {"deleted": user_id}


# --------------------------------------------------------------- lifecycle

@app.on_event("startup")
def _on_startup():
    """Bring up the persisted scheduler: every enabled row in the
    schedules table (core/job_store.py) gets registered as an APScheduler
    cron job, so schedules created before a restart keep firing after one --
    the gap Phase 1's in-memory-only ScheduledAgent had."""
    global _scheduler
    from core.job_store import list_schedules, mark_orphaned_runs_as_error
    from interface.scheduler import ScheduledAgent

    # Any run still "running" from before this process started is
    # guaranteed orphaned (its background thread died with the old
    # process) -- close those out now rather than leaving them stuck
    # forever with no way to resume or dismiss them. See
    # core/job_store.py's mark_orphaned_runs_as_error() docstring.
    orphaned = mark_orphaned_runs_as_error()
    if orphaned:
        logger.warning("orphaned_runs_marked_error", count=orphaned)

    _scheduler = ScheduledAgent()
    for row in list_schedules(enabled_only=True):
        try:
            _scheduler.add_job_cron(row["id"], row["job_id"], cron=row["cron_expr"])
        except Exception as e:
            logger.error("schedule_restore_failed", schedule_id=row["id"], error=str(e))
    _scheduler.start()


# ------------------------------------------------------------ single-agent

class StartTaskRequest(BaseModel):
    task: str
    thread_id: Optional[str] = None
    agent: str = "planner"


class ResumeRequest(BaseModel):
    decision: str  # "approve" or "deny: <reason>"


def _serialize_state(config: dict, agent: str = "planner") -> dict:
    graph = _get_graph(agent)
    snapshot = graph.get_state(config)
    values = dict(snapshot.values)
    values.pop("messages", None)
    return {
        "status": values.get("status"),
        "final_answer": values.get("final_answer"),
        "error": values.get("error"),
        "iterations": values.get("iterations"),
        "paused_before": list(snapshot.next) if snapshot.next else [],
    }


@app.post("/tasks")
async def start_task(req: StartTaskRequest, _: CurrentUser = Depends(get_current_user)):
    thread_id = req.thread_id or str(uuid.uuid4())
    graph = _get_graph(req.agent)
    config = {"configurable": {"thread_id": thread_id}}

    state = initial_state(req.task, settings.max_iterations, settings.max_tool_retries)
    state["messages"] = [HumanMessage(content=req.task)]

    def _run():
        for _ in stream_and_trace(graph, state, config):
            pass

    await asyncio.to_thread(_run)
    return {"thread_id": thread_id, "agent": req.agent, **_serialize_state(config, req.agent)}


@app.post("/tasks/{thread_id}/resume")
async def resume(thread_id: str, req: ResumeRequest, agent: str = "planner", _: CurrentUser = Depends(get_current_user)):
    from langgraph.types import Command

    graph = _get_graph(agent)
    config = {"configurable": {"thread_id": thread_id}}
    if not graph.get_state(config).values:
        raise HTTPException(status_code=404, detail="unknown thread_id")

    def _run():
        for _ in stream_and_trace(graph, Command(resume=req.decision), config):
            pass

    await asyncio.to_thread(_run)
    return {"thread_id": thread_id, "agent": agent, **_serialize_state(config, agent)}


@app.get("/tasks/{thread_id}")
async def get_task(thread_id: str, agent: str = "planner", _: CurrentUser = Depends(get_current_user)):
    config = {"configurable": {"thread_id": thread_id}}
    graph = _get_graph(agent)
    if not graph.get_state(config).values:
        raise HTTPException(status_code=404, detail="unknown thread_id")
    return {"thread_id": thread_id, "agent": agent, **_serialize_state(config, agent)}


@app.get("/tasks/{thread_id}/events")
async def get_task_events(thread_id: str, limit: int = 200, _: CurrentUser = Depends(get_current_user)):
    return {"thread_id": thread_id, "events": get_events(thread_id=thread_id, limit=limit)}


@app.get("/tools")
async def list_tools(category: Optional[str] = None, _: CurrentUser = Depends(get_current_user)):
    from tools import get_all_tools, list_categories

    tools = get_all_tools(categories=[category] if category else None, verbose=False)
    return {
        "categories": list_categories(),
        "tools": [{"name": getattr(t, "name", "?"), "description": getattr(t, "description", "")} for t in tools],
    }


# ----------------------------------------------------------------- agents

class AgentDraftRequest(BaseModel):
    name: str
    description: str


class AgentSaveRequest(BaseModel):
    markdown: str


@app.get("/agents")
async def list_agents_endpoint(_: CurrentUser = Depends(get_current_user)):
    from core.agent_loader import AgentLoadError, list_agents, load_agent

    out = []
    for name in list_agents():
        try:
            a = load_agent(name)
            out.append(
                {
                    "name": a.name, "display_name": a.display_name, "role": a.role,
                    "reports_to": a.reports_to, "hands_off_to": a.hands_off_to,
                    "model": a.model, "tools": a.tools, "version": a.version,
                }
            )
        except AgentLoadError as e:
            out.append({"name": name, "error": str(e)})
    return {"agents": out}


@app.get("/agents/{name}")
async def get_agent_endpoint(name: str, _: CurrentUser = Depends(get_current_user)):
    from core.agent_loader import AgentLoadError, load_agent

    try:
        a = load_agent(name)
    except AgentLoadError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return a.model_dump()


@app.post("/agents/draft")
async def draft_agent_endpoint(req: AgentDraftRequest, _: CurrentUser = Depends(get_current_user)):
    """Runs the NL -> .md pipeline and returns the draft for confirmation.
    Nothing is written to disk here -- PUT /agents/{name} saves it."""
    from core.nl_to_md import draft_agent_from_nl

    def _run():
        return draft_agent_from_nl(req.description, req.name)

    result = await asyncio.to_thread(_run)
    return {"valid": result.valid, "markdown": result.text, "error": result.error}


@app.put("/agents/{name}")
async def save_agent_endpoint(name: str, req: AgentSaveRequest, _: CurrentUser = Depends(require_admin)):
    from core.agent_loader import AgentLoadError, save_agent_markdown

    try:
        a = save_agent_markdown(name, req.markdown)
    except AgentLoadError as e:
        raise HTTPException(status_code=422, detail=str(e))
    _graphs.pop(name, None)  # force a rebuild next time this role runs
    return a.model_dump()


@app.delete("/agents/{name}")
async def delete_agent_endpoint(name: str, _: CurrentUser = Depends(require_admin)):
    from core.agent_loader import delete_agent
    from core.job_loader import list_job_ids, load_job

    in_use = [jid for jid in list_job_ids() if name in load_job(jid).agents]
    if in_use:
        raise HTTPException(status_code=409, detail=f"agent {name!r} is used by job(s) {in_use} -- update or delete those first")

    delete_agent(name)
    _graphs.pop(name, None)
    return {"deleted": name}


# ------------------------------------------------------------------- jobs

class JobDraftRequest(BaseModel):
    id: str
    description: str


class JobSaveRequest(BaseModel):
    yaml: str


class RunJobRequest(BaseModel):
    task: str


def _sync_all_jobs_to_store():
    from core.job_loader import list_job_ids, load_job
    from core.job_store import sync_job_to_store

    for jid in list_job_ids():
        sync_job_to_store(load_job(jid))


@app.get("/jobs")
async def list_jobs_endpoint(_: CurrentUser = Depends(get_current_user)):
    from core.job_store import list_jobs

    _sync_all_jobs_to_store()
    return {"jobs": list_jobs()}


@app.get("/jobs/{job_id}")
async def get_job_endpoint(job_id: str, _: CurrentUser = Depends(get_current_user)):
    from core.job_loader import JobLoadError, load_job

    try:
        job = load_job(job_id)
    except JobLoadError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return job.model_dump()


@app.get("/jobs/{job_id}/graph")
async def get_job_graph_endpoint(job_id: str, _: CurrentUser = Depends(get_current_user)):
    from core.job_loader import JobLoadError, load_job

    try:
        job = load_job(job_id)
    except JobLoadError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return job.agent_graph()


@app.post("/jobs/draft")
async def draft_job_endpoint(req: JobDraftRequest, _: CurrentUser = Depends(get_current_user)):
    from core.nl_to_md import draft_job_from_nl

    def _run():
        return draft_job_from_nl(req.description, req.id)

    result = await asyncio.to_thread(_run)
    return {"valid": result.valid, "yaml": result.text, "error": result.error}


@app.put("/jobs/{job_id}")
async def save_job_endpoint(job_id: str, req: JobSaveRequest, _: CurrentUser = Depends(require_admin)):
    from core.job_loader import JobLoadError, save_job_yaml
    from core.job_store import sync_job_to_store

    try:
        job = save_job_yaml(job_id, req.yaml)
    except JobLoadError as e:
        raise HTTPException(status_code=422, detail=str(e))
    sync_job_to_store(job)
    return job.model_dump()


@app.delete("/jobs/{job_id}")
async def delete_job_endpoint(job_id: str, _: CurrentUser = Depends(require_admin)):
    from core.job_loader import delete_job_yaml
    from core.job_store import delete_job as delete_job_row

    delete_job_yaml(job_id)
    delete_job_row(job_id)
    return {"deleted": job_id}


def _run_job_sync(
    job_id: str,
    task: str,
    triggered_by: str = "manual",
    triggered_by_user: Optional[str] = None,
    run_id: Optional[str] = None,
) -> dict:
    import json

    from core.job_loader import load_job
    from core.job_store import create_run, update_run
    from core.notifications import notify_job_result
    from core.supervisor import run_job

    from observability import console_line

    job = load_job(job_id)
    if run_id is None:  # callers that already created the Run row (the background path below) pass its id in
        run_id = create_run(job_id, task=task, triggered_by=triggered_by, triggered_by_user=triggered_by_user)
    console_line("INFO", f"RUN STARTED  job={job_id}  by={triggered_by}  task: {' '.join(task.split())[:120]}", run_id=run_id)

    def on_step(agent_name, thread_id, final_state):
        update_run(run_id, current_agent=agent_name)

    result = run_job(job, task, run_id, on_step=on_step)
    dag_state = result.pop("_dag_state", None)

    if result["status"] == "done":
        update_run(run_id, status="done", current_agent=None, final_answer=result["final_answer"], dag_state=json.dumps(dag_state))
    elif result["status"] == "error":
        update_run(run_id, status="error", current_agent=result.get("failed_agent"), error=result["error"], dag_state=json.dumps(dag_state))
    else:  # awaiting_human
        update_run(run_id, status="awaiting_human", current_agent=result["paused_agent"], dag_state=json.dumps(dag_state))

    notify_job_result(job, run_id, result)
    console_line(
        "ERROR" if result["status"] == "error" else "INFO",
        f"RUN {result['status'].upper()}" + (f"  at {result.get('failed_agent')}: {result.get('error')}" if result["status"] == "error" else "")
        + (f"  -- waiting for approval of {result.get('paused_agent')}" if result["status"] == "awaiting_human" else ""),
        run_id=run_id,
    )
    return {"run_id": run_id, **result}


def _check_run_owner(row: dict, user: "CurrentUser") -> None:
    """A Run belongs to whoever triggered it (triggered_by_user); a run
    fired by a schedule (triggered_by_user=None) is visible to admins only.
    Admins can see and act on every run."""
    if user.role == "admin":
        return
    if row.get("triggered_by_user") != user.id:
        raise HTTPException(status_code=404, detail="unknown run_id")


@app.post("/jobs/{job_id}/run")
async def run_job_endpoint(job_id: str, req: RunJobRequest, user: CurrentUser = Depends(get_current_user)):
    from core.job_loader import JobLoadError

    import threading

    from core.job_loader import load_job
    from core.job_store import create_run, update_run

    # A Job run can take many minutes (every agent calls a model and tools), so
    # start it in the background and answer immediately with its run_id. The UI
    # goes straight to the Run page, which polls /runs/{id} for live progress.
    try:
        load_job(job_id)  # 404 now if the Job doesn't exist or doesn't parse
    except JobLoadError as e:
        raise HTTPException(status_code=404, detail=str(e))
    run_id = create_run(job_id, task=req.task, triggered_by="manual", triggered_by_user=user.id)

    def _background():
        try:
            _run_job_sync(job_id, req.task, "manual", user.id, run_id=run_id)
        except Exception as e:  # never leave the row stuck at "running"
            logger.error("background_run_failed", run_id=run_id, error=str(e))
            update_run(run_id, status="error", error=f"{type(e).__name__}: {e}")

    threading.Thread(target=_background, name=f"run-{run_id[:8]}", daemon=True).start()
    return {"run_id": run_id, "status": "running"}


@app.get("/jobs/{job_id}/runs")
async def list_job_runs_endpoint(job_id: str, user: CurrentUser = Depends(get_current_user)):
    from core.job_store import list_runs

    rows = list_runs(job_id)
    if user.role != "admin":
        rows = [r for r in rows if r.get("triggered_by_user") == user.id]
    return {"runs": rows}


# ------------------------------------------------------------------- runs

def _get_run_or_resolve(run_id: str) -> tuple[dict, str]:
    """get_run(run_id), tolerating a thread_id passed where a run_id
    belongs (core.job_store.resolve_run_id) -- returns (row, the run_id
    actually used). Raises 404 if neither the literal id nor a resolved one
    exists."""
    from core.job_store import get_run, resolve_run_id

    row = get_run(run_id)
    if row:
        return row, run_id
    resolved = resolve_run_id(run_id)
    if resolved:
        row = get_run(resolved)
        if row:
            logger.warning("run_id_thread_id_confusion", given=run_id, resolved=resolved)
            return row, resolved
    raise HTTPException(status_code=404, detail="unknown run_id")


@app.get("/runs/{run_id}")
async def get_run_endpoint(run_id: str, user: CurrentUser = Depends(get_current_user)):
    row, _ = _get_run_or_resolve(run_id)
    _check_run_owner(row, user)
    return row


@app.post("/runs/{run_id}/mark_failed")
async def mark_run_failed_endpoint(run_id: str, user: CurrentUser = Depends(get_current_user)):
    """Manually fail a Run stuck at status="running" -- the exposed
    alternative to auto-failing it, for the case that isn't a guaranteed
    orphan (see mark_orphaned_runs_as_error(), run once at startup): a run
    that's simply gone quiet for longer than settings.stale_run_seconds
    (row["stale"]) but might still genuinely be working, so the server
    won't fail it on its own. 409 if it isn't "running" -- nothing to do."""
    from core.job_store import get_run, update_run

    row, run_id = _get_run_or_resolve(run_id)
    _check_run_owner(row, user)
    if row["status"] != "running":
        raise HTTPException(
            status_code=409,
            detail=f"run {run_id} is not running (status={row['status']!r}) -- nothing to mark failed",
        )
    update_run(
        run_id,
        status="error",
        error=f"Manually marked as failed by {user.email}.",
    )
    return get_run(run_id)


@app.post("/runs/{run_id}/resume")
async def resume_run_endpoint(run_id: str, req: ResumeRequest, user: CurrentUser = Depends(get_current_user)):
    import json

    from core.job_loader import load_job
    from core.job_store import update_run
    from core.notifications import notify_job_result
    from core.supervisor import run_job_resume

    row, run_id = _get_run_or_resolve(run_id)
    _check_run_owner(row, user)
    if row["status"] != "awaiting_human":
        raise HTTPException(status_code=409, detail=f"run {run_id} is not awaiting approval (status={row['status']!r})")

    job = load_job(row["job_id"])
    paused_agent = row["current_agent"]
    dag_state = json.loads(row["dag_state"]) if row.get("dag_state") else None

    def on_step(agent_name, thread_id, final_state):
        update_run(run_id, current_agent=agent_name)

    def _run():
        return run_job_resume(job, run_id, row["task"], paused_agent, req.decision, on_step=on_step, dag_state=dag_state)

    result = await asyncio.to_thread(_run)
    new_dag_state = result.pop("_dag_state", None)

    if result["status"] == "done":
        update_run(run_id, status="done", current_agent=None, final_answer=result["final_answer"], dag_state=json.dumps(new_dag_state))
    elif result["status"] == "error":
        update_run(run_id, status="error", current_agent=result.get("failed_agent"), error=result["error"], dag_state=json.dumps(new_dag_state))
    else:
        update_run(run_id, status="awaiting_human", current_agent=result["paused_agent"], dag_state=json.dumps(new_dag_state))

    notify_job_result(job, run_id, result)
    return {"run_id": run_id, **result}


@app.get("/runs/{run_id}/events")
async def get_run_events_endpoint(
    run_id: str,
    level: Optional[str] = None,
    agent: Optional[str] = None,
    task: Optional[str] = None,
    limit: int = 500,
    since_id: Optional[int] = None,
    user: CurrentUser = Depends(get_current_user),
):
    row, run_id = _get_run_or_resolve(run_id)
    _check_run_owner(row, user)

    events = get_events(run_id=run_id, level=level, agent_name=agent, task_id=task, limit=limit, since_id=since_id)
    return {"run_id": run_id, "events": events}


@app.get("/logs/live")
async def live_logs_endpoint(
    since_id: Optional[int] = None,
    level: Optional[str] = None,
    run_id: Optional[str] = None,
    agent: Optional[str] = None,
    limit: int = 200,
    user: CurrentUser = Depends(get_current_user),
):
    """Live-tail of run events for the Log Explorer's console. The first call
    (no since_id) returns the latest `limit` events oldest-first; each later call
    passes the previous response's last_id and gets only what is new. Admins see
    every run; other users see only events of runs they started."""
    from core.job_store import list_runs

    limit = max(1, min(limit, 1000))
    if run_id:
        row, run_id = _get_run_or_resolve(run_id)
        _check_run_owner(row, user)
    if since_id is None:
        events = list(reversed(get_events(run_id=run_id, level=level, agent_name=agent, limit=limit)))
    else:
        events = get_events(run_id=run_id, level=level, agent_name=agent, limit=limit, since_id=since_id)
    last_id = max((e["id"] for e in events), default=since_id or 0)  # advance past events this user can't see, too
    if user.role != "admin":
        owned = {r["id"] for r in list_runs(limit=500) if r.get("triggered_by_user") == user.id}
        events = [e for e in events if e.get("run_id") in owned]
    return {"events": events, "last_id": last_id}


# -------------------------------------------------------------- schedules

class CreateScheduleRequest(BaseModel):
    job_id: str
    cron: str
    task: str = ""  # currently informational; a scheduled fire re-runs the
    # Job's own Task instructions per agent (core/job_loader.py), same as a
    # manual run with no extra task text layered on top


class UpdateScheduleRequest(BaseModel):
    enabled: Optional[bool] = None
    cron: Optional[str] = None


@app.get("/schedules")
async def list_schedules_endpoint(_: CurrentUser = Depends(require_admin)):
    from core.job_store import list_schedules

    return {"schedules": list_schedules()}


@app.post("/schedules")
async def create_schedule_endpoint(req: CreateScheduleRequest, user: CurrentUser = Depends(require_admin)):
    from core.job_loader import JobLoadError, load_job
    from core.job_store import create_schedule

    try:
        load_job(req.job_id)  # 404s if the job doesn't exist / doesn't parse
    except JobLoadError as e:
        raise HTTPException(status_code=404, detail=str(e))

    schedule_id = create_schedule(req.job_id, req.cron, created_by=user.id)
    if _scheduler is not None:
        _scheduler.add_job_cron(schedule_id, req.job_id, cron=req.cron)
    return {"id": schedule_id, "job_id": req.job_id, "cron": req.cron}


@app.put("/schedules/{schedule_id}")
async def update_schedule_endpoint(schedule_id: str, req: UpdateScheduleRequest, _: CurrentUser = Depends(require_admin)):
    from core.job_store import get_schedule, update_schedule

    row = get_schedule(schedule_id)
    if not row:
        raise HTTPException(status_code=404, detail="unknown schedule_id")

    fields = {}
    if req.enabled is not None:
        fields["enabled"] = int(req.enabled)
    if req.cron is not None:
        fields["cron_expr"] = req.cron
    update_schedule(schedule_id, **fields)

    if _scheduler is not None:
        _scheduler.remove_job_cron(schedule_id)
        new_row = get_schedule(schedule_id)
        if new_row["enabled"]:
            _scheduler.add_job_cron(schedule_id, new_row["job_id"], cron=new_row["cron_expr"])

    return get_schedule(schedule_id)


@app.delete("/schedules/{schedule_id}")
async def delete_schedule_endpoint(schedule_id: str, _: CurrentUser = Depends(require_admin)):
    from core.job_store import delete_schedule

    if _scheduler is not None:
        _scheduler.remove_job_cron(schedule_id)
    delete_schedule(schedule_id)
    return {"deleted": schedule_id}


@app.post("/schedules/{schedule_id}/run-now")
async def run_schedule_now_endpoint(schedule_id: str, _: CurrentUser = Depends(require_admin)):
    from core.job_store import get_schedule

    row = get_schedule(schedule_id)
    if not row:
        raise HTTPException(status_code=404, detail="unknown schedule_id")

    def _run():
        from core.job_loader import load_job

        job = load_job(row["job_id"])
        default_task = job.description or f"Run the {job.name} Job."
        return _run_job_sync(row["job_id"], default_task, triggered_by="schedule")

    result = await asyncio.to_thread(_run)
    return result


# ---------------------------------------------------------------- TestDesk
# Self-test guide: run a user-written scenario in a throwaway sandbox (test)
# or through the normal run path (prod). See TestDesk/README.md.
from TestDesk.routes import build_router as _build_testdesk_router  # noqa: E402

app.include_router(_build_testdesk_router(get_current_user, _run_job_sync))


@app.get("/health")
async def health():
    return {"status": "ok"}
