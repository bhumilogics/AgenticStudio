"""
LLM backend factory.

Two backends for the tool-calling (planner) path:
    "api"          -> ChatAnthropic via the Anthropic API (needs ANTHROPIC_API_KEY)
    "claude-code"  -> shells out to the Claude Code CLI (subscription auth,
                      no API key) -- see core/backends/claude_code.py

Which backend is used is settings.llm_backend by default, but can be
overridden per agent (an agent's `llm_backend:` frontmatter field --
core/agent_loader.py) so, for example, a cheap/simple role can run on the
subscription-auth Claude Code backend while another role in the same Job
uses the API backend for structured tool-calling.

Both return something with a LangChain-compatible `.invoke(messages) ->
AIMessage`, which is all core/graph.py's planner node relies on -- the
graph itself doesn't need to know or care which backend is active.
"""

from __future__ import annotations

from functools import lru_cache

from .config import settings


@lru_cache(maxsize=8)
def get_llm(model: str | None = None, temperature: float | None = None):
    """
    Returns a cached ChatAnthropic instance (Anthropic API backend only --
    used directly by callers that specifically want the API, e.g. the
    reflector's "api" mode, regardless of what the planner's
    AGENTICSTUDIO_LLM_BACKEND is set to).
    """
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(
        model=model or settings.model,
        temperature=settings.temperature if temperature is None else temperature,
        max_tokens=settings.max_tokens,
    )


def get_llm_with_tools(tools: list, model: str | None = None, backend: str | None = None):
    """
    Returns the tool-calling-capable LLM a planner node should use.

    `backend` defaults to settings.llm_backend, but an agent_def can pass
    its own resolved_llm_backend to override it per role:
      - "api": real Anthropic tool-calling (llm.bind_tools(tools))
      - "claude-code": a wrapper around the Claude Code CLI that gets
        tool-call intent out of it via a JSON-in-text protocol (see
        core/backends/claude_code.py) since the headless CLI has no
        native structured tool-calling to bind to.
    """
    backend = backend or settings.llm_backend

    if backend == "claude-code":
        from .backends.claude_code import ClaudeCodeToolCallingLLM

        return ClaudeCodeToolCallingLLM(tools=tools, model=model)

    llm = get_llm(model=model)
    return llm.bind_tools(tools)
