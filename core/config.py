"""
Central configuration, read from environment (optionally loaded from a
.env file — see .env.example at the project root).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from dotenv import load_dotenv

# Load .env (if present) before any of the os.environ.get() calls below run.
# Must happen at import time, here, since `settings = Settings()` at the
# bottom of this module reads the environment immediately on first import.
load_dotenv()

# Some .env parsers (observed with the python-dotenv version this project's
# requirements.txt pins) do NOT reliably strip a trailing "  # comment" on a
# line whose value is blank -- e.g. `AGENTICSTUDIO_TOOL_CATEGORIES=    #
# comma-separated...` was seen coming back as the literal string
# "# comma-separated..." instead of "" -- NOTE: it also strips the leading
# whitespace before handing that back, so the raw value starts directly
# with "#" at index 0 (confirmed: `repr()` showed no leading space). A
# lookbehind-style "\s+#.*$" pattern does NOT match that (there's no
# whitespace immediately before the '#' once dotenv has already eaten it),
# so this matches either a '#' at the very start of the string OR one
# preceded by whitespace anywhere in it. Every value read from the
# environment goes through this to strip that class of bug out at the
# source, regardless of what the .env parser did or didn't do.
_INLINE_COMMENT_RE = re.compile(r"(?:^|\s)#.*$")


def _env(name: str, default: str | None = "") -> str | None:
    raw = os.environ.get(name, default)
    if raw is None:
        return None
    return _INLINE_COMMENT_RE.sub("", raw).strip()


def _bool(name: str, default: bool = False) -> bool:
    return _env(name, str(default)).lower() in {"1", "true", "yes", "on"}


def _list(name: str, default: str) -> list[str]:
    raw = _env(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass
class Settings:
    # --- LLM backend ---
    model: str = field(default_factory=lambda: _env("AGENTICSTUDIO_MODEL", "claude-sonnet-5"))
    temperature: float = field(default_factory=lambda: float(_env("AGENTICSTUDIO_TEMPERATURE", "0")))
    max_tokens: int = field(default_factory=lambda: int(_env("AGENTICSTUDIO_MAX_TOKENS", "4096")))

    # --- Autonomy policy ---
    # "autonomous"  -> never stops for approval
    # "checkpoint"  -> stops before tools listed in risky_tools
    # "supervised"  -> stops before every tool call
    autonomy_level: str = field(default_factory=lambda: _env("AGENTICSTUDIO_AUTONOMY", "checkpoint"))
    risky_tools: list[str] = field(
        default_factory=lambda: _list(
            "AGENTICSTUDIO_RISKY_TOOLS",
            "shell_exec,python_repl,python_ast_repl,send_gmail_message,requests_post,requests_patch,"
            "requests_put,requests_delete,file_delete,move_file,aws_lambda",
        )
    )

    # --- Loop limits ---
    max_iterations: int = field(default_factory=lambda: int(_env("AGENTICSTUDIO_MAX_ITERATIONS", "25")))
    max_tool_retries: int = field(default_factory=lambda: int(_env("AGENTICSTUDIO_MAX_TOOL_RETRIES", "2")))

    # --- Persistence ---
    checkpoint_db_path: str = field(
        default_factory=lambda: _env("AGENTICSTUDIO_CHECKPOINT_DB", "./data/checkpoints.sqlite")
    )
    events_db_path: str = field(
        default_factory=lambda: _env("AGENTICSTUDIO_EVENTS_DB", "./data/events.sqlite")
    )

    # --- Observability ---
    log_level: str = field(default_factory=lambda: _env("AGENTICSTUDIO_LOG_LEVEL", "INFO"))
    log_json: bool = field(default_factory=lambda: _bool("AGENTICSTUDIO_LOG_JSON", True))

    # --- Tool selection ---
    tool_categories: list[str] | None = field(
        default_factory=lambda: (_list("AGENTICSTUDIO_TOOL_CATEGORIES", "") or None)
    )

    # --- Planner LLM backend ---
    # "api"          -> Anthropic API via langchain-anthropic (needs ANTHROPIC_API_KEY)
    # "claude-code"  -> Claude Code CLI, subscription auth, no API key
    llm_backend: str = field(default_factory=lambda: _env("AGENTICSTUDIO_LLM_BACKEND", "api"))

    # --- Reflector LLM backend ---
    # "heuristic"    -> keyword-based error detection only (no LLM call, default)
    # "api"          -> use the same Anthropic API backend as the planner
    # "claude-code"  -> shell out to the Claude Code CLI (subscription auth,
    #                   no per-token API billing) for the reflection call
    reflector_backend: str = field(
        default_factory=lambda: _env("AGENTICSTUDIO_REFLECTOR_BACKEND", "heuristic")
    )

    # --- Claude Code CLI backend (core/backends/claude_code.py) ---
    claude_code_binary: str = field(default_factory=lambda: _env("AGENTICSTUDIO_CLAUDE_CODE_BIN", "claude"))
    claude_code_model: str | None = field(default_factory=lambda: _env("AGENTICSTUDIO_CLAUDE_CODE_MODEL", None))
    claude_code_timeout: int = field(
        default_factory=lambda: int(_env("AGENTICSTUDIO_CLAUDE_CODE_TIMEOUT", "60"))
    )


settings = Settings()
