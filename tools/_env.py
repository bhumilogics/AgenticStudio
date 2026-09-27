"""Small shared helper: read an env var or raise a clean, catchable error."""

from __future__ import annotations

import os


class MissingConfig(RuntimeError):
    """Raised when a tool's required env var / credential isn't set.

    tools/__init__.py catches this (and ImportError) per-module, so a tool
    missing its API key is silently skipped rather than crashing startup.
    """


def require_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    raise MissingConfig(f"none of {names} set in environment")


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)
