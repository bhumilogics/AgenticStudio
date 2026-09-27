"""
LLM backend factory.

Two backends for the tool-calling (planner) path, chosen by
AGENTICSTUDIO_LLM_BACKEND:
    "api"          -> ChatAnthropic via the Anthropic API (needs ANTHROPIC_API_KEY)
    "claude-code"  -> shells out to the Claude Code CLI (subscription auth,
                      no API key) -- see core/backends/claude_code.py

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


def get_llm_with_tools(tools: list, model: str | None = None):
    """
    Returns the tool-calling-capable LLM the planner node should use,
    picked by settings.llm_backend:
      - "api": real Anthropic tool-calling (llm.bind_tools(tools))
      - "claude-code": a wrapper around the Claude Code CLI that gets
        tool-call intent out of it via a JSON-in-text protocol (see
        core/backends/claude_code.py) since the headless CLI has no
        native structured tool-calling to bind to.
    """
    if settings.llm_backend == "claude-code":
        from .backends.claude_code import ClaudeCodeToolCallingLLM

        return ClaudeCodeToolCallingLLM(tools=tools, model=model)

    llm = get_llm(model=model)
    return llm.bind_tools(tools)
