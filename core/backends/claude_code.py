"""
Claude Code CLI backend.

Shells out to the locally installed `claude` CLI in non-interactive
("headless") mode instead of calling the Anthropic API directly. Auth
comes from `claude login` (an OAuth flow against your Claude Pro/Max
subscription, run once, credentials cached locally by the CLI itself) --
no API key, no scraped session, no stored password.

Deliberately narrow scope: this is a single-turn, tool-free "ask a
question, get text back" call -- NOT a general chat model, and NOT a
replacement for the project's own LangGraph tool-execution loop. Claude
Code has its own built-in agentic tool use (it can read/write files, run
bash on its own); if that ran here it would compete with `tools/` and
`core/graph.py`'s tool_executor node. So every invocation explicitly
disables Claude Code's own tools.

IMPORTANT -- two separate problems, two separate fixes:

1. `--append-system-prompt` (tried briefly) only ADDS to Claude Code's
   default system prompt -- it does not replace it. With `--tools ""`
   the model then genuinely has zero built-in tools and honestly
   reports that ("I don't have access to any tools..."), ignoring our
   injected protocol entirely. Only a FULL override (`--system-prompt`,
   or the file-based `--system-prompt-file` this module tries first)
   replaces Claude Code's identity/tool framing outright, which is what
   lets our own tool-call protocol be the only framing the model sees.

2. Our full system prompt is PLANNER_SYSTEM_PROMPT + the tool-call
   protocol + every tool's rendered name/description/JSON schema across
   the whole `tools/` registry -- easily tens of KB once more than a
   handful of tools are loaded. Windows' CreateProcess has a hard
   ~32,767-character command-line limit, so putting that much text
   directly on argv (as `--system-prompt <text>`, or the transcript via
   the positional `--print <prompt>` argument) risks silent
   truncation/mangling by the OS before `claude` ever sees it -- which
   would produce exactly the earlier symptom (short test prompts
   worked, the real large prompt didn't).

So: the system prompt is written to a temp file and passed via
`--system-prompt-file <path>` (a full override, read from disk, no argv
size limit) when the installed CLI supports that flag; `complete()`
detects an "unknown option" failure and transparently falls back to
passing the same content inline via `--system-prompt <text>` if it
doesn't. The conversation transcript goes over stdin either way, with
no positional `prompt` argument at all -- the `[prompt]` in `claude
--help`'s usage line (`claude [options] [command] [prompt]`) is
bracketed, i.e. optional.

Verify the exact flag names below against `claude --help` for the
version you install -- CLI flags are more prone to drift release-to-
release than the Anthropic API is, this is not guaranteed to be exactly
right for every version.

Usage: wired into both the planner (core/llm.get_llm_with_tools, gated
by settings.llm_backend == "claude-code") and the reflector's optional
LLM-backed reflection path (core/graph.py, gated by
settings.reflector_backend == "claude-code").
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from ..config import settings

logger = logging.getLogger("agenticstudio.claude_code")


class ClaudeCodeUnavailable(RuntimeError):
    """Raised when the `claude` binary can't be found or invoked."""


@dataclass
class ClaudeCodeResult:
    text: str
    raw: dict | None  # parsed --output-format json payload, if we got one
    exit_code: int


def _binary_path() -> str:
    binary = settings.claude_code_binary
    resolved = shutil.which(binary)
    if not resolved:
        raise ClaudeCodeUnavailable(
            f"'{binary}' not found on PATH. Install it (see .env.example / project README) "
            f"and run `claude login`, or set AGENTICSTUDIO_CLAUDE_CODE_BIN to its full path."
        )
    return resolved


_BASE_FLAGS = [
    "--print",                          # non-interactive; no positional prompt --
                                         # the transcript is read from stdin instead
    "--output-format", "json",
    "--tools", "",                      # documented way to disable ALL built-in tools
    "--restricted",                     # ignore your personal user/project/local settings files
    "--strict-mcp-config",              # with no --mcp-config passed: zero MCP servers load
                                         # (without this, your account's own Gmail/Docs MCP
                                         # servers leak in and the model reports THOSE as its
                                         # available tools instead of our text-described ones)
    "--no-session-persistence",         # each call is stateless; we track state ourselves
    "--permission-prompts", "none",     # never hang waiting for an approval nobody can answer
]

# Substrings that indicate the CLI rejected a flag outright (as opposed to
# failing for some other reason after accepting it) -- seen across common
# CLI arg parsers (commander.js, argparse, clap, etc.).
_UNKNOWN_FLAG_MARKERS = ("unknown option", "unrecognized", "not recognized", "invalid option")


def _run_claude(cmd: list[str], stdin_payload: str, timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, input=stdin_payload, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise ClaudeCodeUnavailable(str(e)) from e


def complete(system_prompt: str, user_prompt: str, timeout: int | None = None) -> ClaudeCodeResult:
    """
    Single non-interactive completion via the Claude Code CLI, with its own
    tool use disabled and its default system prompt fully replaced by ours
    (not appended-to -- see module docstring for why that distinction
    matters). Raises ClaudeCodeUnavailable if the binary isn't
    installed/on PATH, or subprocess.TimeoutExpired / RuntimeError on
    other failures -- callers (core/graph.py, core/llm.py) are expected to
    catch these and fall back rather than crash the run.
    """
    binary = _binary_path()
    timeout = timeout or settings.claude_code_timeout

    if settings.claude_code_model:
        model_flags = ["--model", settings.claude_code_model]
    else:
        model_flags = []

    # Try the file-based full override first -- keeps a potentially large
    # system prompt off argv entirely (no OS command-line length limit to
    # worry about). Falls back to passing it inline if this CLI version
    # doesn't recognize --system-prompt-file.
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as tf:
            tf.write(system_prompt)
            tmp_path = tf.name

        cmd_file = [binary, *_BASE_FLAGS, "--system-prompt-file", tmp_path, *model_flags]
        logger.debug("claude_code trying --system-prompt-file (%d char system prompt)", len(system_prompt))
        proc = _run_claude(cmd_file, user_prompt, timeout)

        stderr_lower = (proc.stderr or "").lower()
        flag_rejected = proc.returncode != 0 and any(m in stderr_lower for m in _UNKNOWN_FLAG_MARKERS)

        if flag_rejected:
            logger.debug(
                "claude_code: --system-prompt-file not supported by this CLI (stderr=%s), "
                "falling back to inline --system-prompt",
                (proc.stderr or "")[:500],
            )
            cmd_inline = [binary, *_BASE_FLAGS, "--system-prompt", system_prompt, *model_flags]
            proc = _run_claude(cmd_inline, user_prompt, timeout)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    stdout = proc.stdout.strip()
    raw = None
    text = stdout

    if stdout:
        try:
            raw = json.loads(stdout)
            # Known shapes vary by CLI version; try the common ones.
            text = (
                raw.get("result")
                or raw.get("response")
                or raw.get("content")
                or stdout
            )
            if isinstance(text, list):  # content-block style
                text = "\n".join(
                    block.get("text", "") for block in text if isinstance(block, dict)
                )
        except json.JSONDecodeError:
            pass  # not JSON -- treat stdout as plain text

    logger.debug(
        "claude_code exit=%s stdout(%d chars)=%s stderr(%d chars)=%s",
        proc.returncode, len(stdout), stdout[:2000], len(proc.stderr or ""), (proc.stderr or "")[:2000],
    )

    if proc.returncode != 0 and not text:
        raise RuntimeError(f"claude CLI exited {proc.returncode}: {proc.stderr.strip()[:2000]}")

    return ClaudeCodeResult(text=text, raw=raw, exit_code=proc.returncode)


# ---------------------------------------------------------------------------
# Tool-calling over the headless CLI.
#
# The CLI has no native structured tool-calling to bind to (that's an API
# feature). So this gets the same *effect* the old-fashioned way: the
# system prompt describes each tool's name/args as JSON schema and asks
# the model to reply with a JSON block naming which tool(s) to call, or
# "FINAL ANSWER: ..." when done. We parse that JSON back into a real
# AIMessage.tool_calls list -- so everything downstream (core/graph.py's
# routing, the ToolNode executor) is none the wiser about how the
# AIMessage was produced.
# ---------------------------------------------------------------------------

_TOOL_PROTOCOL_INSTRUCTIONS = """
You have the following tools available. To call one or more tools, reply
with ONLY a JSON object of this exact shape and nothing else (no prose
before or after it, and do NOT wrap it in <function_calls> tags or any
other markup -- just the raw JSON object on its own):

{"tool_calls": [{"name": "<tool_name>", "args": {"<arg>": <value>, ...}}]}

Call at most one tool per turn unless calls are clearly independent and
safe to run together. When you are completely done and have the answer
the user needs, reply with plain text starting with "FINAL ANSWER:"
instead of JSON -- never both in the same reply.

Available tools:
{tool_specs}
""".strip()


def _render_tool_specs(tools: list) -> str:
    blocks = []
    for t in tools:
        name = getattr(t, "name", "?")
        description = (getattr(t, "description", "") or "").strip()
        try:
            args_schema = t.args  # BaseTool.args -> dict of {arg_name: schema}
        except Exception:
            args_schema = {}
        blocks.append(f"- {name}: {description}\n  args_schema: {json.dumps(args_schema, default=str)}")
    return "\n".join(blocks) if blocks else "(no tools available)"


def _render_transcript(messages: list) -> str:
    """Flatten a LangChain message history into a plain-text transcript
    the CLI can consume as a single prompt (it has no concept of a
    structured multi-role message list)."""
    lines = []
    for m in messages:
        if isinstance(m, SystemMessage):
            continue  # folded into the system prompt separately
        elif isinstance(m, HumanMessage):
            lines.append(f"USER: {m.content}")
        elif isinstance(m, AIMessage):
            content = m.content if isinstance(m.content, str) else str(m.content)
            if content.strip():
                lines.append(f"ASSISTANT: {content}")
            for tc in getattr(m, "tool_calls", None) or []:
                lines.append(f"ASSISTANT (called tool): {tc['name']}({json.dumps(tc.get('args', {}), default=str)})")
        elif isinstance(m, ToolMessage):
            lines.append(f"TOOL RESULT [{getattr(m, 'tool_call_id', '?')}]: {m.content}")
        else:
            lines.append(str(getattr(m, "content", m)))
    return "\n\n".join(lines)


# Claude models are trained to express tool calls as an Anthropic-style
# `<function_calls>[{"name": ..., "args": ...}]</function_calls>` block even
# when told (as our protocol does) to use a bare `{"tool_calls": [...]}`
# object instead -- observed in practice: the CLI replied with exactly that
# XML-ish wrapper around a JSON array, mixed with stray "FINAL ANSWER"-ish
# narration in the same turn. Rather than fight that habit, the parser below
# accepts either shape (and a single bare `{"name": ..., "args": ...}` object
# too), wherever it appears in the reply, and -- when a real tool call is
# found -- prioritizes acting on it over any "FINAL ANSWER" text the model
# also produced in the same turn (a model that already emitted a tool call
# is still mid-task, whatever else it also said).
_FUNCTION_CALLS_TAG_RE = re.compile(r"</?function_calls>", re.IGNORECASE)


def _strip_function_call_tags(text: str) -> str:
    return _FUNCTION_CALLS_TAG_RE.sub("", text)


def _extract_json_candidates(text: str) -> list[str]:
    """Scan for balanced top-level {...} or [...] regions and return each as
    a substring, in the order they appear. Unlike a single greedy regex,
    this correctly separates multiple/nested JSON blocks instead of
    spanning from the first '{' to the very last '}' in the whole text."""
    candidates = []
    stack = []
    start = None
    for i, ch in enumerate(text):
        if ch in "{[":
            if not stack:
                start = i
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
                if not stack and start is not None:
                    candidates.append(text[start : i + 1])
                    start = None
    return candidates


def _normalize_tool_calls(parsed) -> list[dict] | None:
    """Accepts {"tool_calls": [...]}, a bare list of {"name":..,"args":..},
    or a single {"name":..,"args":..} dict. Returns the raw list of
    {"name", "args"} dicts (before id assignment), or None if `parsed`
    doesn't look like any of those shapes."""
    if isinstance(parsed, dict):
        if isinstance(parsed.get("tool_calls"), list):
            return parsed["tool_calls"]
        if "name" in parsed:
            return [parsed]
    elif isinstance(parsed, list) and parsed and all(isinstance(item, dict) and "name" in item for item in parsed):
        return parsed
    return None


def _parse_tool_call_reply(text: str) -> AIMessage:
    cleaned = _strip_function_call_tags(text or "").strip()

    if not cleaned:
        return AIMessage(content="FINAL ANSWER: (empty response)")

    for candidate in _extract_json_candidates(cleaned):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        raw_calls = _normalize_tool_calls(parsed)
        if raw_calls:
            tool_calls = [
                {"name": c["name"], "args": c.get("args", {}), "id": str(uuid.uuid4())}
                for c in raw_calls
                if "name" in c
            ]
            if tool_calls:
                return AIMessage(content="", tool_calls=tool_calls)

    if cleaned.upper().startswith("FINAL ANSWER"):
        return AIMessage(content=cleaned)

    # Couldn't parse a tool call and it didn't say FINAL ANSWER -- treat
    # the raw text as the final answer rather than looping/crashing.
    return AIMessage(content=f"FINAL ANSWER: {cleaned}")


class ClaudeCodeToolCallingLLM:
    """
    Drop-in replacement for `ChatAnthropic(...).bind_tools(tools)` backed by
    the Claude Code CLI instead of the Anthropic API. Only implements
    `.invoke(messages) -> AIMessage`, since that's all core/graph.py's
    planner node calls.
    """

    def __init__(self, tools: list, model: str | None = None):
        self.tools = tools
        self.model = model
        self._tool_specs = _render_tool_specs(tools)

    def invoke(self, messages: list) -> AIMessage:
        # Plain string replacement, not .format() -- the template contains
        # literal JSON braces (e.g. {"tool_calls": ...}) that .format()
        # would misparse as its own placeholders.
        system_prompt = _TOOL_PROTOCOL_INSTRUCTIONS.replace("{tool_specs}", self._tool_specs)
        # Fold in whatever the caller's own system message says (e.g.
        # PLANNER_SYSTEM_PROMPT) so both sets of instructions apply.
        caller_system = "\n\n".join(m.content for m in messages if isinstance(m, SystemMessage) and m.content)
        if caller_system:
            system_prompt = f"{caller_system}\n\n---\n\n{system_prompt}"

        transcript = _render_transcript(messages)

        logger.debug("claude_code system_prompt (%d chars): %s", len(system_prompt), system_prompt[:4000])
        logger.debug("claude_code transcript (%d chars): %s", len(transcript), transcript[:2000])

        result = complete(system_prompt, transcript, timeout=settings.claude_code_timeout)

        logger.debug("claude_code raw reply: %s", result.text[:2000])

        return _parse_tool_call_reply(result.text)
