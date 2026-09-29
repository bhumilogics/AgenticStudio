"""
APScheduler wiring.

Two independent things can be scheduled:

  - a single agent role directly (add_cron_job) -- unchanged from Phase 1,
    kept for a role you want to run standalone on a cron without a full Job
  - a whole Job, i.e. its agents in sequence via core/supervisor.py
    (add_job_cron) -- what interface/api.py's /schedules endpoints use, and
    what actually satisfies "scheduling task and overall job must be there"

Persistence: interface/api.py is the source of truth for *which* schedules
exist (core/job_store.py's `schedules` table) and registers/unregisters
this class's in-memory APScheduler jobs to match on every create/update/
delete, and again on process startup (see api.py's startup event) --
restarting the API doesn't lose a schedule the way Phase 1's ScheduledAgent
did (it only held jobs in memory).

    from interface.scheduler import ScheduledAgent

    sched = ScheduledAgent()
    sched.add_job_cron("sched-1", job_id="qa_regression_job", cron="0 7 * * *")
    sched.start()
"""

from __future__ import annotations

import uuid

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from core.config import settings
from core.graph import build_graph
from core.state import initial_state
from observability import get_logger, stream_and_trace

logger = get_logger("agenticstudio.scheduler")


class ScheduledAgent:
    def __init__(self):
        self._scheduler = BackgroundScheduler()
        self._agent_graphs: dict[str, object] = {}

    # ---------------------------------------------------- single-agent path

    def _graph_for(self, agent: str):
        if agent not in self._agent_graphs:
            from core.agent_loader import load_agent

            self._agent_graphs[agent] = build_graph(agent_def=load_agent(agent))
        return self._agent_graphs[agent]

    def _run_task(self, task: str, thread_id_prefix: str, agent: str = "planner"):
        from langchain_core.messages import HumanMessage

        thread_id = f"{thread_id_prefix}-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": thread_id}}
        state = initial_state(task, settings.max_iterations, settings.max_tool_retries)
        state["messages"] = [HumanMessage(content=task)]
        graph = self._graph_for(agent)

        logger.info("scheduled_run_started", thread_id=thread_id, task=task, agent=agent)
        try:
            for _ in stream_and_trace(graph, state, config):
                pass
            final = graph.get_state(config).values
            logger.info("scheduled_run_finished", thread_id=thread_id, status=final.get("status"))
        except Exception as e:
            logger.error("scheduled_run_failed", thread_id=thread_id, error=str(e))

    def add_cron_job(self, job_id: str, task: str, cron: str, agent: str = "planner") -> None:
        """Schedule one agent role directly (no Job wrapper). `cron` is a
        standard 5-field cron expression, e.g. '0 7 * * *'."""
        trigger = CronTrigger.from_crontab(cron)
        self._scheduler.add_job(
            self._run_task, trigger=trigger, id=job_id,
            kwargs={"task": task, "thread_id_prefix": job_id, "agent": agent},
        )
        logger.info("cron_job_added", job_id=job_id, cron=cron, agent=agent)

    def run_now(self, task: str, job_id_prefix: str = "adhoc", agent: str = "planner") -> None:
        self._run_task(task, job_id_prefix, agent=agent)

    # ------------------------------------------------------------ Job path

    def _run_job(self, job_id: str, schedule_id: str):
        """What fires on a Job's cron: builds the default task text from the
        Job's own description (a scheduled fire doesn't get a one-off task
        typed in by a person, unlike a manual /jobs/{id}/run), then runs the
        whole agent sequence via core/supervisor.py exactly like a manual
        run would, and records it as a Run (core/job_store.py) so it shows
        up in the same run history and Log Explorer."""
        import json

        from core.job_loader import load_job
        from core.job_store import create_run, update_run, update_schedule
        from core.notifications import notify_job_result
        from core.supervisor import run_job

        try:
            job = load_job(job_id)
        except Exception as e:
            logger.error("scheduled_job_load_failed", job_id=job_id, schedule_id=schedule_id, error=str(e))
            return

        task = job.description or f"Run the {job.name} Job."
        run_id = create_run(job_id, task=task, triggered_by="schedule")
        logger.info("scheduled_job_run_started", job_id=job_id, run_id=run_id, schedule_id=schedule_id)

        def on_step(agent_name, thread_id, final_state):
            update_run(run_id, current_agent=agent_name)

        try:
            result = run_job(job, task, run_id, on_step=on_step)
            dag_state = result.pop("_dag_state", None)
            if result["status"] == "done":
                update_run(run_id, status="done", current_agent=None, final_answer=result["final_answer"], dag_state=json.dumps(dag_state))
            elif result["status"] == "error":
                update_run(run_id, status="error", current_agent=result.get("failed_agent"), error=result["error"], dag_state=json.dumps(dag_state))
            else:
                update_run(run_id, status="awaiting_human", current_agent=result["paused_agent"], dag_state=json.dumps(dag_state))
            update_schedule(schedule_id, last_run_at=__import__("time").time())
            notify_job_result(job, run_id, result)
            logger.info("scheduled_job_run_finished", job_id=job_id, run_id=run_id, status=result["status"])
        except Exception as e:
            update_run(run_id, status="error", error=str(e))
            logger.error("scheduled_job_run_failed", job_id=job_id, run_id=run_id, error=str(e))

    def add_job_cron(self, schedule_id: str, job_id: str, cron: str) -> None:
        """Register (or replace) a Job's cron schedule. `schedule_id` is the
        core.job_store schedules row id -- reused as the APScheduler job id
        so remove_job_cron() can find it again."""
        trigger = CronTrigger.from_crontab(cron)
        self._scheduler.add_job(
            self._run_job, trigger=trigger, id=schedule_id,
            kwargs={"job_id": job_id, "schedule_id": schedule_id},
            replace_existing=True,
        )
        logger.info("job_cron_added", schedule_id=schedule_id, job_id=job_id, cron=cron)

    def remove_job_cron(self, schedule_id: str) -> None:
        try:
            self._scheduler.remove_job(schedule_id)
        except Exception:
            pass  # not registered (already removed, or never started) -- fine

    def run_job_now(self, job_id: str, schedule_id: str = "adhoc") -> None:
        self._run_job(job_id, schedule_id)

    # --------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._scheduler.start()
        logger.info("scheduler_started")

    def shutdown(self) -> None:
        self._scheduler.shutdown()
        logger.info("scheduler_stopped")
