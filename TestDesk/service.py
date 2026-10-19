"""
Orchestration: validate a scenario, run it in test or prod mode, judge it.

    test -> execute_test_mode(): build a sandbox, run TestDesk/runner.py in a
            subprocess pointed at it, read back the result, delete the sandbox.
    prod -> execute_prod_mode(): call the Studio's normal job-run function
            (interface/api.py's _run_job_sync, injected by routes.py), so the
            Run lives in the app DB exactly like one started from the Jobs page.

Either way the *report* (verdict + checks + trimmed events) is saved to
TestDesk's own DB (TestDesk/store.py).

Stdlib only; routes.py injects the pieces that need the rest of the app.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

from . import store
from .sandbox import build_process_env, create_sandbox, destroy_sandbox

PROJECT_ROOT = Path(__file__).resolve().parent.parent

MODES = ("test", "prod")
TARGET_TYPES = ("job", "agent")
EXPECT_STATUSES = ("done", "error", "awaiting_human")

DEFAULT_OPTIONS = {
    "block_external": True,     # test mode: switch off HTTP writes, shell, Gmail, AWS, browser
    "auto_approve": True,       # test mode: approve human-approval pauses so the scenario can finish
    "keep_sandbox": False,      # test mode: leave the sandbox directory behind for inspection
    "timeout_seconds": 900,
}
MAX_CONCURRENT_TEST_RUNS = 3

_active = 0
_active_lock = threading.Lock()


class ScenarioError(ValueError):
    """A scenario that can't be saved or run; the message is shown to the user."""


# --------------------------------------------------------------- validation

def normalize_scenario(raw: dict, *, known_jobs: set[str], known_agents: set[str]) -> dict:
    """Clean + validate user input into the shape store.create_scenario takes.
    Raises ScenarioError with a human-readable message."""
    task = (raw.get("task") or "").strip()
    if not task:
        raise ScenarioError("describe the scenario to test (the 'scenario' text is empty)")

    job_yaml = (raw.get("job_yaml") or "").strip()
    target_type = (raw.get("target_type") or "job").strip()
    target_id = (raw.get("target_id") or "").strip()

    if job_yaml:
        import yaml

        try:
            data = yaml.safe_load(job_yaml)
        except Exception as e:  # noqa: BLE001 -- yaml raises assorted errors
            raise ScenarioError(f"draft Job YAML is not valid YAML: {e}") from e
        if not isinstance(data, dict) or not data.get("id"):
            raise ScenarioError("draft Job YAML must be a mapping with an `id`")
        target_type, target_id = "job", str(data["id"])
    else:
        if target_type not in TARGET_TYPES:
            raise ScenarioError(f"target_type must be one of {TARGET_TYPES}")
        pool = known_jobs if target_type == "job" else known_agents
        if not target_id:
            raise ScenarioError(f"pick the {target_type} to test")
        if target_id not in pool:
            raise ScenarioError(f"unknown {target_type} {target_id!r}")

    expect_status = (raw.get("expect_status") or "done").strip()
    if expect_status not in EXPECT_STATUSES:
        raise ScenarioError(f"expect_status must be one of {EXPECT_STATUSES}")

    exp = raw.get("expectations") or []
    if isinstance(exp, str):
        exp = exp.splitlines()
    expectations = [e.strip() for e in exp if isinstance(e, str) and e.strip()][:20]

    opts = dict(DEFAULT_OPTIONS)
    for k, v in (raw.get("options") or {}).items():
        if k in DEFAULT_OPTIONS:
            opts[k] = v
    for flag in ("block_external", "auto_approve", "keep_sandbox"):
        opts[flag] = bool(opts[flag])
    try:
        opts["timeout_seconds"] = max(30, min(3600, int(opts["timeout_seconds"])))
    except (TypeError, ValueError):
        raise ScenarioError("timeout_seconds must be a number") from None

    name = (raw.get("name") or "").strip() or (task[:60] + ("…" if len(task) > 60 else ""))
    return {
        "name": name[:120], "target_type": target_type, "target_id": target_id, "task": task,
        "expectations": expectations, "expect_status": expect_status, "job_yaml": job_yaml, "options": opts,
    }


# --------------------------------------------------------------- judging

def evaluate(scenario: dict, run: dict) -> tuple[str, list[dict]]:
    """Turn a finished run into (verdict, checks).

    verdict:
      pass            every check held
      fail            the scenario ran, and at least one check did not hold
                      (including: the Job ended in a status other than expected)
      awaiting_human  prod mode stopped at an approval gate; resume it from the Run page
      error           TestDesk itself couldn't carry the scenario out (runner crash,
                      timeout, failed isolation check) -- says nothing about the Job
    """
    checks: list[dict] = []
    if not run.get("ok"):
        return "error", [{"name": "scenario executed", "ok": False, "detail": run.get("error") or "the runner did not report a result"}]

    status, want = run.get("run_status"), scenario.get("expect_status") or "done"
    if status == "awaiting_human" and want != "awaiting_human":
        return "awaiting_human", [{"name": "run finished", "ok": False,
                                   "detail": f"stopped at a human-approval gate (agent: {run.get('paused_agent')}); approve it from the Run page to continue"}]

    ok = status == want
    detail = f"run ended {status!r}" + ("" if ok else f", expected {want!r}")
    if not ok and run.get("error"):
        detail += f" -- {run['error']}"
    checks.append({"name": f"run status is {want}", "ok": ok, "detail": detail})

    answer = (run.get("final_answer") or "")
    for phrase in scenario.get("expectations") or []:
        hit = phrase.lower() in answer.lower()
        checks.append({"name": f"answer mentions “{phrase}”", "ok": hit,
                       "detail": "found" if hit else ("not found in the final answer" if answer else "there was no final answer")})
    return ("pass" if all(c["ok"] for c in checks) else "fail"), checks


# ------------------------------------------------------------- test mode

def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=15)
        else:
            proc.kill()
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass


def execute_test_mode(
    scenario: dict,
    *,
    project_root: Path | str = PROJECT_ROOT,
    base_dir: Path | str | None = None,
    python: str | None = None,
    engine: str = "real",
    fake: dict | None = None,
) -> dict:
    """Run `scenario` once in a fresh sandbox. Returns the runner's result dict
    plus `sandbox_path` (only if kept / not fully deleted) and `cleanup_ok`.
    Never raises for a scenario failure -- infrastructure problems come back as
    ok=False with an `error`."""
    opts = {**DEFAULT_OPTIONS, **(scenario.get("options") or {})}
    sb = create_sandbox(project_root, base_dir=base_dir, keep=opts["keep_sandbox"], block_external=opts["block_external"])
    result: dict = {}
    try:
        out_path = sb.root / "result.json"
        spec = {
            "sandbox_root": sb.path, "out_path": str(out_path), "engine": engine, "fake": fake or {},
            "target_type": scenario["target_type"], "target_id": scenario["target_id"], "task": scenario["task"],
            "job_yaml": scenario.get("job_yaml") or "", "auto_approve": opts["auto_approve"], "max_approvals": 10,
        }
        spec_path = sb.root / "spec.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")

        env = build_process_env(sb)
        if engine == "fake":
            env["TESTDESK_ALLOW_FAKE_ENGINE"] = "1"
        proc = subprocess.Popen(
            [python or sys.executable, "-m", "TestDesk.runner", str(spec_path)],
            cwd=str(project_root), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        timed_out = False
        try:
            output, _ = proc.communicate(timeout=opts["timeout_seconds"])
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc)
            output, _ = proc.communicate()
        tail = (output or "")[-3000:]

        if timed_out:
            result = {"ok": False, "run_status": "error", "error": f"timed out after {opts['timeout_seconds']}s -- the scenario was stopped"}
        elif out_path.exists():
            result = json.loads(out_path.read_text(encoding="utf-8"))
            if not result.get("ok") and tail.strip():
                result["runner_output"] = tail
        else:
            result = {"ok": False, "run_status": "error", "error": f"runner exited with code {proc.returncode} without a result", "runner_output": tail}
    finally:
        result["sandbox_path"] = None
        result["cleanup_ok"] = True
        if sb.keep:
            result["sandbox_path"] = sb.path
        elif not destroy_sandbox(sb):
            result["sandbox_path"], result["cleanup_ok"] = sb.path, False
    return result


# ------------------------------------------------------------- prod mode

def execute_prod_mode(scenario: dict, *, owner_id: Optional[str], run_job_sync: Callable[..., dict]) -> dict:
    """Run through the Studio's normal path. The Run, its events and its
    checkpoints land in the app DB (visible on the Jobs / Log Explorer pages)."""
    if scenario["target_type"] != "job" or scenario.get("job_yaml"):
        raise ScenarioError("prod mode runs a saved Job; save the Job first (agents and draft Jobs can only be run in test mode)")
    try:
        res = run_job_sync(scenario["target_id"], scenario["task"], "testdesk", owner_id)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "run_status": "error", "error": f"{type(e).__name__}: {e}"}
    return {
        "ok": True, "engine": "app", "run_status": res.get("status"), "final_answer": res.get("final_answer"),
        "error": res.get("error"), "failed_agent": res.get("failed_agent"), "paused_agent": res.get("paused_agent"),
        "run_id": res.get("run_id"), "events": [], "approvals": [], "steps": [],
    }


# --------------------------------------------------------- background run

def active_runs() -> int:
    return _active


def start_run(
    scenario: dict,
    mode: str,
    *,
    owner_id: Optional[str],
    run_job_sync: Callable[..., dict] | None = None,
    project_root: Path | str = PROJECT_ROOT,
    base_dir: Path | str | None = None,
    engine: str = "real",
    fake: dict | None = None,
    wait: bool = False,
) -> str:
    """Create the result row and run the scenario on a background thread.
    Returns the result id immediately (poll store.get_result); `wait=True`
    joins the thread first (tests)."""
    global _active
    if mode not in MODES:
        raise ScenarioError(f"mode must be one of {MODES}")
    if mode == "prod" and (scenario["target_type"] != "job" or scenario.get("job_yaml")):
        raise ScenarioError("prod mode runs a saved Job; save the Job first (agents and draft Jobs can only be run in test mode)")
    with _active_lock:
        if _active >= MAX_CONCURRENT_TEST_RUNS:
            raise ScenarioError(f"{_active} scenarios are already running -- wait for one to finish")
        _active += 1

    result_id = store.create_result(scenario=scenario, mode=mode, owner_id=owner_id)

    def work() -> None:
        global _active
        try:
            if mode == "test":
                run = execute_test_mode(scenario, project_root=project_root, base_dir=base_dir, engine=engine, fake=fake)
            else:
                run = execute_prod_mode(scenario, owner_id=owner_id, run_job_sync=run_job_sync)
            verdict, checks = evaluate(scenario, run)
            store.finish_result(
                result_id, verdict=verdict, run_status=run.get("run_status"), final_answer=run.get("final_answer"),
                error=run.get("error") if verdict in ("error", "fail") else None, checks=checks,
                events=run.get("events") or [], approvals=run.get("approvals") or [], run_id=run.get("run_id") if mode == "prod" else None,
                sandbox_path=run.get("sandbox_path"),
            )
        except Exception as e:  # noqa: BLE001 -- the row must never stay 'running'
            store.finish_result(result_id, verdict="error", error=f"{type(e).__name__}: {e}",
                                checks=[{"name": "scenario executed", "ok": False, "detail": str(e)}])
        finally:
            with _active_lock:
                _active -= 1

    t = threading.Thread(target=work, name=f"testdesk-{result_id[:8]}", daemon=True)
    t.start()
    if wait:
        t.join()
    return result_id
