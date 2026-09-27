"""
APScheduler wiring: run a fixed task on a cron schedule, or fire one on an
ad-hoc event (file change, webhook, etc.) via `run_now`.

    from interface.scheduler import ScheduledAgent

    sched = ScheduledAgent()
    sched.add_cron_job("nightly-report", task="Summarize today's inbox and save a report", cron="0 7 * * *")
    sched.start()
"""

from __future__ import annotations

import uuid

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from core.config import settings
from core.graph import build_graph
from core.prompts import PLANNER_SYSTEM_PROMPT
from core.state import initial_state
from observability import get_logger, stream_and_trace

logger = get_logger("agenticstudio.scheduler")


class ScheduledAgent:
    def __init__(self):
        self._scheduler = BackgroundScheduler()
        self._graph = build_graph()

    def _run_task(self, task: str, thread_id_prefix: str):
        from langchain_core.messages import HumanMessage, SystemMessage

        thread_id = f"{thread_id_prefix}-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": thread_id}}
        state = initial_state(task, settings.max_iterations, settings.max_tool_retries)
        state["messages"] = [SystemMessage(content=PLANNER_SYSTEM_PROMPT), HumanMessage(content=task)]

        logger.info("scheduled_run_started", thread_id=thread_id, task=task)
        try:
            for _ in stream_and_trace(self._graph, state, config):
                pass
            final = self._graph.get_state(config).values
            logger.info("scheduled_run_finished", thread_id=thread_id, status=final.get("status"))
        except Exception as e:
            logger.error("scheduled_run_failed", thread_id=thread_id, error=str(e))

    def add_cron_job(self, job_id: str, task: str, cron: str) -> None:
        """`cron` is a standard 5-field cron expression, e.g. '0 7 * * *'."""
        trigger = CronTrigger.from_crontab(cron)
        self._scheduler.add_job(
            self._run_task, trigger=trigger, id=job_id, kwargs={"task": task, "thread_id_prefix": job_id}
        )
        logger.info("cron_job_added", job_id=job_id, cron=cron)

    def run_now(self, task: str, job_id_prefix: str = "adhoc") -> None:
        """Fire a task immediately, e.g. from a webhook or file-watch handler."""
        self._run_task(task, job_id_prefix)

    def start(self) -> None:
        self._scheduler.start()
        logger.info("scheduler_started")

    def shutdown(self) -> None:
        self._scheduler.shutdown()
        logger.info("scheduler_stopped")
