"""
Subprocess entrypoint for ONE test-mode scenario.

    python -m TestDesk.runner <spec.json>        (cwd = project root)

The parent (TestDesk/service.py) has already built a sandbox
(TestDesk/sandbox.py) and started this process with the sandbox's environment
overrides. Before anything from `core` is imported -- core/config.py reads
several paths from the environment AT IMPORT -- this module checks that every
location the Studio writes to resolves inside the sandbox, and refuses to run
if even one does not. That check is the guarantee behind "test mode never
touches the app DB"; tests/test_isolation.py exercises it.

The spec JSON:
    {"sandbox_root", "out_path", "target_type": "job"|"agent", "target_id",
     "task", "job_yaml": "" , "auto_approve": true, "max_approvals": 10,
     "engine": "real"|"fake", "fake": {...}}

The result is written to `out_path` as JSON (see _result()). The engine
"fake" exists only for tests (and only if TESTDESK_ALLOW_FAKE_ENGINE=1): it
drives the real persistence layer (core/job_store.py) without an LLM or
LangGraph, which is what lets the isolation guarantee be tested on a machine
that cannot run the agents.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

MAX_EVENTS = 400
MAX_PAYLOAD_CHARS = 1500


class IsolationError(RuntimeError):
    pass


def assert_isolated(sandbox_root: str) -> None:
    from TestDesk.sandbox import ISOLATED_PATH_VARS

    root = Path(sandbox_root).resolve()
    if os.environ.get("AGENTICSTUDIO_TESTDESK_SANDBOX") != str(root):
        raise IsolationError("AGENTICSTUDIO_TESTDESK_SANDBOX does not match the sandbox root")
    for var in ISOLATED_PATH_VARS:
        val = os.environ.get(var)
        if not val:
            raise IsolationError(f"{var} is not set -- the Studio would fall back to its real default location")
        p = Path(val).resolve()
        if p != root and root not in p.parents:
            raise IsolationError(f"{var}={val} is outside the sandbox {root}")


def _result(**kw) -> dict:
    base = {
        "ok": False, "engine": None, "run_status": None, "final_answer": None, "error": None,
        "failed_agent": None, "paused_agent": None, "run_id": None, "events": [], "approvals": [],
        "steps": [], "db_path": os.environ.get("AGENTICSTUDIO_JOB_DB"),
    }
    base.update(kw)
    return base


def _trim_events(events: list[dict]) -> list[dict]:
    out = []
    for e in reversed(events):  # get_events() is newest-first; the report reads oldest-first
        payload = e.get("payload")
        if isinstance(payload, str) and len(payload) > MAX_PAYLOAD_CHARS:
            payload = payload[:MAX_PAYLOAD_CHARS] + " …[truncated]"
        out.append({k: e.get(k) for k in ("ts", "level", "agent_name", "task_id", "node", "event_type")} | {"payload": payload})
    return out[-MAX_EVENTS:]


# ------------------------------------------------------------------ engines

def _build_job(spec: dict):
    import yaml

    from core.job_loader import JobDefinition, TaskDefinition, load_job, parse_job_yaml

    if spec.get("job_yaml"):
        data = yaml.safe_load(spec["job_yaml"]) or {}
        job = parse_job_yaml(spec["job_yaml"], str(data.get("id") or spec["target_id"]), source="<scenario draft>")
    elif spec["target_type"] == "agent":
        name = spec["target_id"]
        job = JobDefinition(
            id=f"testdesk_agent_{name}", name=f"TestDesk: {name}", description="Ad-hoc single-agent scenario",
            agents=[name], tasks=[TaskDefinition(id="scenario", agent=name, name="Scenario", instructions="")],
        )
    else:
        job = load_job(spec["target_id"])  # AGENTICSTUDIO_JOBS_DIR -> the sandbox copy
    return job.model_copy(update={"webhook_url": ""})  # never notify out of a test


def _real_engine(spec: dict) -> dict:
    from core.job_store import create_run, sync_job_to_store, update_run
    from core.supervisor import run_job, run_job_resume
    from observability import configure_logging, get_events

    configure_logging()
    job = _build_job(spec)
    sync_job_to_store(job)
    run_id = create_run(job.id, task=spec["task"], triggered_by="testdesk")
    steps: list[str] = []

    def on_step(agent_name, thread_id, final_state):
        steps.append(agent_name)
        update_run(run_id, current_agent=agent_name)

    result = run_job(job, spec["task"], run_id, on_step=on_step)
    approvals: list[str] = []
    while (
        result["status"] == "awaiting_human"
        and spec.get("auto_approve", True)
        and len(approvals) < int(spec.get("max_approvals", 10))
    ):
        dag_state = result.pop("_dag_state", None)
        paused = result["paused_agent"]
        approvals.append(paused)
        result = run_job_resume(job, run_id, spec["task"], paused, "approve", on_step=on_step, dag_state=dag_state)
    result.pop("_dag_state", None)

    update_run(
        run_id, status=result["status"], current_agent=result.get("paused_agent") or result.get("failed_agent"),
        final_answer=result.get("final_answer"), error=result.get("error"),
    )
    events = get_events(run_id=run_id, limit=2000)
    return _result(
        ok=True, engine="real", run_status=result["status"], final_answer=result.get("final_answer"),
        error=result.get("error"), failed_agent=result.get("failed_agent"), paused_agent=result.get("paused_agent"),
        run_id=run_id, events=_trim_events(events), approvals=approvals, steps=steps,
    )


def _fake_engine(spec: dict) -> dict:
    """Test-only. Loads core/job_store.py straight from its file (it is pure
    stdlib) so `core/__init__` -- which imports LangGraph -- is never needed."""
    import importlib.util

    if os.environ.get("TESTDESK_ALLOW_FAKE_ENGINE") != "1":
        raise RuntimeError("fake engine is disabled (set TESTDESK_ALLOW_FAKE_ENGINE=1 -- tests only)")
    fake = spec.get("fake") or {}
    path = Path(__file__).resolve().parent.parent / "core" / "job_store.py"
    mod_spec = importlib.util.spec_from_file_location("testdesk_fake_job_store", path)
    js = importlib.util.module_from_spec(mod_spec)
    mod_spec.loader.exec_module(js)

    from types import SimpleNamespace

    job_id = spec["target_id"]
    # runs.job_id is a foreign key, so the Job row has to exist first (the real engine does the same)
    js.sync_job_to_store(SimpleNamespace(id=job_id, name=job_id, description="", trigger_type="manual", agents=[], tasks=[]))
    run_id = js.create_run(job_id, task=spec["task"], triggered_by="testdesk")
    time.sleep(float(fake.get("delay", 0)))
    # leave footprints in every place a sandbox must contain
    (Path(os.environ["AGENTICSTUDIO_WORKDIR"]) / "testdesk_footprint.txt").write_text("written by scenario", encoding="utf-8")
    (Path(os.environ["AGENTICSTUDIO_JOBS_DIR"]) / "testdesk_footprint.yaml").write_text("id: x\n", encoding="utf-8")
    status = fake.get("status", "done")
    answer = fake.get("final_answer", f"fake answer for: {spec['task']}")
    js.update_run(run_id, status=status if status in ("done", "error") else "awaiting_human", final_answer=answer, error=fake.get("error"))
    approvals = ["fake_agent"] * int(fake.get("approvals", 0))
    return _result(
        ok=True, engine="fake", run_status=status, final_answer=answer, error=fake.get("error"), run_id=run_id,
        approvals=approvals, steps=["fake_agent"],
        events=[{"ts": time.time(), "level": "INFO", "agent_name": "fake_agent", "task_id": None, "node": "fake",
                 "event_type": "fake_step", "payload": "fake engine"}],
    )


# --------------------------------------------------------------------- main

def main(argv: list[str]) -> int:
    spec = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    out_path = Path(spec["out_path"])

    def write(res: dict) -> None:
        out_path.write_text(json.dumps(res, default=str), encoding="utf-8")

    try:
        assert_isolated(spec["sandbox_root"])
    except IsolationError as e:
        write(_result(error=f"ISOLATION CHECK FAILED -- scenario not run: {e}", run_status="error"))
        return 3

    try:
        engine = spec.get("engine", "real")
        res = _fake_engine(spec) if engine == "fake" else _real_engine(spec)
    except Exception as e:
        res = _result(error=f"{type(e).__name__}: {e}", run_status="error", engine=spec.get("engine", "real"))
        res["traceback"] = traceback.format_exc()[-4000:]
    write(res)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
