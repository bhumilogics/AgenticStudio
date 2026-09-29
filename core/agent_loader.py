"""
Agent definitions as data, not code.

Every agent role is a Markdown file with YAML frontmatter under `agents/`
(schema + example: agents/_templates/base_agent_template.md). This module
parses those files into validated `AgentDefinition` objects so core/graph.py
(and core/supervisor.py) never has to import a hardcoded prompt string the
way core/prompts.py used to require.

    from core.agent_loader import load_agent, list_agents

    qa_lead = load_agent("qa_lead")
    qa_lead.instructions   # the markdown body -- the actual system prompt
    qa_lead.hands_off_to   # "qa_automation_engineer" -- what the workflow
                           # diagram / supervisor read to route between roles

A field left null/absent in the frontmatter falls back to the project-wide
setting in core/config.py (see AgentDefinition.resolved_*), so an agent file
only has to state what makes IT different from the defaults.

Frontmatter is parsed by hand with PyYAML rather than the `python-frontmatter`
package -- one fewer third-party dependency, and the format (a `---`-fenced
YAML block at the top of the file, body after) is simple enough not to need
a dedicated library.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator

from .config import settings

AGENTS_DIR = Path(os.environ.get("AGENTICSTUDIO_AGENTS_DIR", "./agents"))

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*\n?(.*)\Z", re.DOTALL)


class AgentLoadError(RuntimeError):
    """Raised for a missing, malformed, or schema-invalid agent .md file."""


class AgentDefinition(BaseModel):
    name: str
    display_name: str = ""
    role: str = ""
    reports_to: Optional[str] = None
    hands_off_to: Optional[str] = None

    model: str = "claude-sonnet-5"
    llm_backend: Optional[str] = None  # None -> settings.llm_backend
    tools: list[str] = Field(default_factory=list)
    autonomy: Optional[str] = None  # None -> settings.autonomy_level
    risky_tools: list[str] = Field(default_factory=list)
    max_iterations: Optional[int] = None  # None -> settings.max_iterations
    max_tool_retries: Optional[int] = None  # None -> settings.max_tool_retries

    version: int = 1
    instructions: str  # the markdown body: the actual system prompt

    @field_validator("name")
    @classmethod
    def _validate_slug(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(
                f"agent name must be a lowercase slug matching {_NAME_RE.pattern!r}, got {v!r}"
            )
        return v

    @field_validator("instructions")
    @classmethod
    def _validate_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("agent definition body (instructions) must not be empty")
        return v

    # --- resolved_* fields fold in the project-wide default when unset ---

    @property
    def resolved_llm_backend(self) -> str:
        return self.llm_backend or settings.llm_backend

    @property
    def resolved_autonomy(self) -> str:
        return self.autonomy or settings.autonomy_level

    @property
    def resolved_risky_tools(self) -> set[str]:
        return set(self.risky_tools) if self.risky_tools else set(settings.risky_tools)

    @property
    def resolved_max_iterations(self) -> int:
        return self.max_iterations if self.max_iterations is not None else settings.max_iterations

    @property
    def resolved_max_tool_retries(self) -> int:
        return self.max_tool_retries if self.max_tool_retries is not None else settings.max_tool_retries


def parse_agent_markdown(text: str, expected_name: str, source: str = "<string>") -> AgentDefinition:
    """
    Parse the raw text of an agent .md file (frontmatter + body) into an
    AgentDefinition. Split out from load_agent() so both the filesystem
    loader and the NL-authoring pipeline (core/nl_to_md.py, which validates
    a freshly-generated draft before it's ever written to disk) share one
    parser and one set of error messages.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise AgentLoadError(f"{source}: expected a '---' YAML frontmatter block at the top of the file")

    raw_frontmatter, body = m.group(1), m.group(2)
    try:
        data = yaml.safe_load(raw_frontmatter) or {}
    except yaml.YAMLError as e:
        raise AgentLoadError(f"{source}: invalid YAML frontmatter -- {e}") from e

    if not isinstance(data, dict):
        raise AgentLoadError(f"{source}: frontmatter must be a YAML mapping, got {type(data).__name__}")

    data.setdefault("name", expected_name)
    if data["name"] != expected_name:
        raise AgentLoadError(
            f"{source}: frontmatter name {data['name']!r} does not match expected name {expected_name!r}"
        )
    data["instructions"] = body.strip()

    try:
        return AgentDefinition(**data)
    except Exception as e:
        raise AgentLoadError(f"{source}: invalid agent definition -- {e}") from e


def _path_for(name: str) -> Path:
    return AGENTS_DIR / f"{name}.md"


@lru_cache(maxsize=128)
def load_agent(name: str) -> AgentDefinition:
    """
    Parse agents/<name>.md into a validated AgentDefinition.

    Cached per process (agent files change rarely and are read on every
    graph build). Call reload_agent(name) after editing a file on disk
    within the same running process -- e.g. right after the NL-authoring
    pipeline or an admin edit writes a new version.
    """
    path = _path_for(name)
    if not path.exists():
        raise AgentLoadError(
            f"no agent definition at {path} -- run list_agents() to see what's available"
        )
    return parse_agent_markdown(path.read_text(encoding="utf-8"), name, source=str(path))


def reload_agent(name: str) -> AgentDefinition:
    """Bust the cache for one agent and re-parse it from disk."""
    load_agent.cache_clear()  # functools.lru_cache has no per-key eviction; clear all
    return load_agent(name)


def list_agents() -> list[str]:
    """Every agent name available under AGENTS_DIR (skips _templates/ and any _-prefixed file)."""
    if not AGENTS_DIR.exists():
        return []
    return sorted(p.stem for p in AGENTS_DIR.glob("*.md") if not p.stem.startswith("_"))


def load_all_agents() -> dict[str, AgentDefinition]:
    return {name: load_agent(name) for name in list_agents()}


def delete_agent(name: str) -> None:
    """Remove agents/<name>.md and bust the load cache. The caller (the API)
    is responsible for checking nothing still references this agent (a Job's
    `agents` list) before calling this -- deleting an agent a published Job
    depends on is a modeling error, not something this function guesses at."""
    path = _path_for(name)
    if path.exists():
        path.unlink()
    load_agent.cache_clear()


def save_agent_markdown(name: str, markdown_text: str) -> AgentDefinition:
    """
    Validate `markdown_text` as an agent definition for `name`, and only if
    it validates, write it to agents/<name>.md and bust the load cache.
    Used by the NL-authoring pipeline and the admin "edit raw .md" UI --
    never writes a definition that wouldn't parse.
    """
    agent_def = parse_agent_markdown(markdown_text, name, source=f"<draft for {name}>")
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    _path_for(name).write_text(markdown_text, encoding="utf-8")
    load_agent.cache_clear()
    return agent_def
