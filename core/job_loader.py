"""
Job definitions as data.

A Job is the parent unit an end user triggers: which agent roles
participate, in what order they hand off, and what Tasks each one owns.
Its shape lives in a YAML file under jobs/, parsed here into a validated
JobDefinition, then mirrored into the DB (core/job_store.py) so the running
app has one place to read from regardless of whether a Job came from a
file, the API, or the natural-language pipeline (core/nl_to_md.py).

    from core.job_loader import load_job
    from core.job_store import sync_job_to_store

    job = load_job("qa_regression_job")
    sync_job_to_store(job)   # upserts jobs / job_agents / tasks rows

v1 keeps a Job's agent graph as a straight chain (`agents`, in order) --
this is deliberately the simplest thing that satisfies "QA Manager -> QA
Lead -> Automation Engineer -> Test Executor": fan-out (parallel agents) and
conditional edges (skip a role based on a prior result) are a documented
extension, not implemented here, so the supervisor (core/supervisor.py)
that reads `agent_graph()` stays simple enough to trust.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from .agent_loader import list_agents

JOBS_DIR = Path(os.environ.get("AGENTICSTUDIO_JOBS_DIR", "./jobs"))


class JobLoadError(RuntimeError):
    """Raised for a missing, malformed, or schema-invalid job .yaml file."""


class TaskDefinition(BaseModel):
    id: str
    agent: str
    name: str
    instructions: str = ""
    depends_on: list[str] = Field(default_factory=list)


class JobDefinition(BaseModel):
    id: str
    name: str
    description: str = ""
    trigger_type: str = "manual"  # manual | schedule | webhook
    agents: list[str] = Field(default_factory=list)  # ordered role sequence
    tasks: list[TaskDefinition] = Field(default_factory=list)
    webhook_url: str = ""  # optional; POSTed a JSON status summary when a run of this Job finishes

    @field_validator("agents")
    @classmethod
    def _nonempty(cls, v):
        if not v:
            raise ValueError("a Job needs at least one agent in its `agents` sequence")
        return v

    def agent_graph(self) -> dict:
        """Nodes + edges for the workflow diagram (and for the supervisor's
        own routing) -- a straight chain in `agents` order."""
        nodes = [{"id": a, "position": i} for i, a in enumerate(self.agents)]
        edges = [{"from": self.agents[i], "to": self.agents[i + 1]} for i in range(len(self.agents) - 1)]
        return {"nodes": nodes, "edges": edges}

    def tasks_for(self, agent_name: str) -> list[TaskDefinition]:
        return [t for t in self.tasks if t.agent == agent_name]


def parse_job_yaml(text: str, expected_id: str, source: str = "<string>") -> JobDefinition:
    """
    Parse + validate a Job. Split out from load_job() so the filesystem
    loader and the NL-authoring pipeline share one parser and error format
    (mirrors core.agent_loader.parse_agent_markdown).
    """
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise JobLoadError(f"{source}: invalid YAML -- {e}") from e

    if not isinstance(data, dict):
        raise JobLoadError(f"{source}: job file must be a YAML mapping")

    data.setdefault("id", expected_id)
    if data["id"] != expected_id:
        raise JobLoadError(f"{source}: id {data['id']!r} does not match expected {expected_id!r}")

    try:
        job = JobDefinition(**data)
    except Exception as e:
        raise JobLoadError(f"{source}: invalid job definition -- {e}") from e

    known_agents = set(list_agents())
    unknown = [a for a in job.agents if a not in known_agents]
    if unknown:
        raise JobLoadError(f"{source}: references unknown agent(s) {unknown} -- not found under agents/")

    stray = {t.agent for t in job.tasks} - set(job.agents)
    if stray:
        raise JobLoadError(f"{source}: task(s) assigned to agent(s) {stray} not in this Job's `agents` list")

    task_ids = [t.id for t in job.tasks]
    if len(task_ids) != len(set(task_ids)):
        raise JobLoadError(f"{source}: duplicate task id(s) in {task_ids}")

    return job


def _path_for(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.yaml"


def load_job(job_id: str) -> JobDefinition:
    path = _path_for(job_id)
    if not path.exists():
        raise JobLoadError(f"no job definition at {path}")
    return parse_job_yaml(path.read_text(encoding="utf-8"), job_id, source=str(path))


def list_job_ids() -> list[str]:
    if not JOBS_DIR.exists():
        return []
    return sorted(p.stem for p in JOBS_DIR.glob("*.yaml"))


def load_all_jobs() -> dict[str, JobDefinition]:
    return {jid: load_job(jid) for jid in list_job_ids()}


def delete_job_yaml(job_id: str) -> None:
    """Remove jobs/<job_id>.yaml. The caller (the API) also removes the DB
    mirror via core.job_store.delete_job(job_id) -- kept as two calls rather
    than one so a Job with run history can be archived (DB row kept, file
    removed) without this module needing to know about job_store at all."""
    path = _path_for(job_id)
    if path.exists():
        path.unlink()


def save_job_yaml(job_id: str, yaml_text: str) -> JobDefinition:
    """Validate, then write jobs/<job_id>.yaml -- mirrors
    agent_loader.save_agent_markdown. Never writes a Job that doesn't parse
    or that references an agent that doesn't exist."""
    job = parse_job_yaml(yaml_text, job_id, source=f"<draft for {job_id}>")
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    _path_for(job_id).write_text(yaml_text, encoding="utf-8")
    return job
