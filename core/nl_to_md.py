"""
Natural language -> structured Agent (.md) / Job (.yaml) definitions.

An end user should never have to hand-write YAML frontmatter or a Job's
agent/task graph by hand. This module is one small, single-purpose LLM call
(reusing core/llm.py's existing backend switch -- no new LLM dependency, no
extra API key requirement beyond what the rest of the project already
needs) whose only job is turning a plain-English description into a draft
that validates against the exact same schema core/agent_loader.py /
core/job_loader.py already enforce for hand-written files.

Nothing here writes to disk. draft_agent_from_nl() / draft_job_from_nl()
return a DraftResult -- the raw generated text plus whether it actually
validated -- for the caller (the API, then the UI) to show for confirmation.
Only agent_loader.save_agent_markdown() / job_loader.save_job_yaml() write a
file, and only once a human approves the draft (interface/api.py wires this
as: POST /agents or /jobs with {"nl": "..."} returns a draft and saves
nothing; a follow-up PUT with the (possibly edited) text is what saves it).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Union

from .agent_loader import AgentDefinition, AgentLoadError, list_agents, parse_agent_markdown
from .job_loader import JobDefinition, JobLoadError, parse_job_yaml

AGENT_WRITER_SYSTEM_PROMPT = """You convert a plain-English description of an \
agent role into a single Markdown file with YAML frontmatter, in EXACTLY this \
shape and nothing else (no commentary, no code fences -- just the raw file \
content, starting with the "---" line):

---
name: <lowercase_snake_case_slug, matching the name you were given exactly>
display_name: <Human Readable Name>
role: <one-line summary of what this role is for>
reports_to: <one of the existing agent names below, or null>
hands_off_to: <one of the existing agent names below, or null>
model: claude-sonnet-5
llm_backend: null
tools: [<zero or more names from the tool list below>]
autonomy: null
risky_tools: []
max_iterations: null
max_tool_retries: null
version: 1
---

## Role
One or two sentences.

## Instructions
1. Numbered, concrete steps this role follows.

## Definition of done
- What "finished" looks like for this role.

Rules:
- Only use reports_to / hands_off_to values from this list of already-existing \
agents (or null if none fits): {existing_agents}.
- Only use tool names from this list: {tool_categories}.
- If the description leaves something unclear, make the most reasonable \
choice rather than omitting the field -- the file must always be complete \
and parse as valid YAML frontmatter + Markdown body."""

JOB_WRITER_SYSTEM_PROMPT = """You convert a plain-English description of a \
multi-agent workflow into a single YAML Job definition, in EXACTLY this shape \
and nothing else (no commentary, no code fences -- just the raw YAML):

id: <lowercase_snake_case_slug, matching the id you were given exactly>
name: <Human Readable Name>
description: <one or two sentences>
trigger_type: manual
agents:
  - <agent_name_1>
  - <agent_name_2>
tasks:
  - id: <slug>
    agent: <one of the agents listed under `agents` above>
    name: <short name>
    instructions: <what this agent should do for this task>
    depends_on: []

Rules:
- Only use agent names from this list of already-existing agents: {existing_agents}. \
If the description names a role that doesn't exist yet, pick the closest \
existing one rather than inventing a new agent name.
- `agents` must list the roles in the order they should run (a straight \
hand-off chain, first to last).
- Every task's `agent` value must be one of the names listed in `agents`.
- `depends_on` may reference other task ids in this same list, or be empty."""


@dataclass
class DraftResult:
    text: str  # the raw generated .md / .yaml, defensively stripped of code fences
    parsed: Optional[Union[AgentDefinition, JobDefinition]]  # set only if valid
    error: Optional[str]  # validation error message if it didn't validate
    valid: bool


def _strip_code_fences(text: str) -> str:
    """Models reliably wrap output in ```markdown / ```yaml fences even when
    told not to -- strip them defensively rather than failing the draft over
    formatting the instructions already asked it not to use."""
    text = text.strip()
    m = re.match(r"^```[a-zA-Z]*\n(.*)\n```\s*$", text, re.DOTALL)
    return m.group(1).strip() if m else text


def _complete(system_prompt: str, user_text: str) -> str:
    """Runs the conversion through whichever backend core/llm.py's settings
    already point at (api or claude-code) -- this module never talks to a
    model directly."""
    from .config import settings

    if settings.llm_backend == "claude-code":
        from .backends.claude_code import complete as claude_code_complete

        result = claude_code_complete(system_prompt, user_text, timeout=settings.claude_code_timeout)
        return result.text

    from langchain_core.messages import HumanMessage, SystemMessage

    from .llm import get_llm

    response = get_llm().invoke([SystemMessage(content=system_prompt), HumanMessage(content=user_text)])
    return response.content if isinstance(response.content, str) else str(response.content)


def draft_agent_from_nl(description: str, name: str) -> DraftResult:
    """Generate + validate (but do not save) an agent .md draft for `name`
    from a plain-English `description`."""
    from tools import list_categories

    system_prompt = AGENT_WRITER_SYSTEM_PROMPT.format(
        tool_categories=", ".join(list_categories()) or "(none available)",
        existing_agents=", ".join(list_agents()) or "(none yet)",
    )
    raw = _complete(system_prompt, f"Agent name to use: {name}\n\nDescription:\n{description}")
    text = _strip_code_fences(raw)
    try:
        parsed = parse_agent_markdown(text, name, source=f"<nl draft for {name}>")
        return DraftResult(text=text, parsed=parsed, error=None, valid=True)
    except AgentLoadError as e:
        return DraftResult(text=text, parsed=None, error=str(e), valid=False)


def draft_job_from_nl(description: str, job_id: str) -> DraftResult:
    """Generate + validate (but do not save) a Job .yaml draft for `job_id`
    from a plain-English `description`."""
    system_prompt = JOB_WRITER_SYSTEM_PROMPT.format(existing_agents=", ".join(list_agents()) or "(none yet)")
    raw = _complete(system_prompt, f"Job id to use: {job_id}\n\nDescription:\n{description}")
    text = _strip_code_fences(raw)
    try:
        parsed = parse_job_yaml(text, job_id, source=f"<nl draft for {job_id}>")
        return DraftResult(text=text, parsed=parsed, error=None, valid=True)
    except JobLoadError as e:
        return DraftResult(text=text, parsed=None, error=str(e), valid=False)
