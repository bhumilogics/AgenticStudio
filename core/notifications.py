"""
Fire-and-forget webhook notification when a Job run finishes.

A Job can set `webhook_url` in its YAML (core/job_loader.JobDefinition).
When a run of that Job reaches a terminal-for-now state (done, error, or
awaiting_human), interface/api.py's _run_job_sync() and
interface/scheduler.py's _run_job() both call notify_job_result() right
after they persist the run's final status -- so both the manual-run and
the scheduled-run paths behave identically.

Uses urllib.request (stdlib) rather than the `requests` package that's
already a project dependency, purely so this module has zero import-time
cost for anyone who never sets a webhook_url; it's a small, self-contained
POST, not worth pulling in a session/connection-pooled client for.

Failure to deliver a webhook (network error, non-2xx response, timeout)
is logged and swallowed -- a notification failing must never fail or
retry-loop the run itself.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from observability.logging_config import get_logger

logger = get_logger("agenticstudio.notifications")

_TIMEOUT_SECONDS = 5


def notify_job_result(job, run_id: str, result: dict) -> None:
    """`job` is a core.job_loader.JobDefinition; `result` is whatever
    core.supervisor.run_job/run_job_resume returned (status/final_answer/
    error/failed_agent/paused_agent). No-op if the Job has no webhook_url."""
    url = getattr(job, "webhook_url", "") or ""
    if not url.strip():
        return

    payload = {
        "job_id": job.id,
        "job_name": job.name,
        "run_id": run_id,
        "status": result.get("status"),
        "final_answer": result.get("final_answer"),
        "error": result.get("error"),
        "failed_agent": result.get("failed_agent"),
        "paused_agent": result.get("paused_agent"),
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            logger.info("webhook_delivered", job_id=job.id, run_id=run_id, url=url, status_code=resp.status)
    except urllib.error.URLError as e:
        logger.warning("webhook_failed", job_id=job.id, run_id=run_id, url=url, error=str(e))
    except Exception as e:  # never let a notification bug break the run's own result
        logger.warning("webhook_failed_unexpected", job_id=job.id, run_id=run_id, url=url, error=str(e))
