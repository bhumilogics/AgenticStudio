"""
tools/ auto-discovery registry.

Drop a new module in this folder that exposes either:
  - a module-level `TOOLS: list[BaseTool]`, or
  - a `get_tools() -> list[BaseTool]` factory function (preferred when the
    tool needs config/credentials, so it can skip cleanly if unavailable)

...and it is picked up automatically — no edits needed here or in the
LangGraph tool node.

Usage:
    from tools import get_all_tools
    tools = get_all_tools()                       # every available tool
    tools = get_all_tools(categories=["search_web", "code_execution"])
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from pathlib import Path
from typing import Iterable

logger = logging.getLogger("agenticstudio.tools")

_PACKAGE_DIR = Path(__file__).parent
_SKIP_MODULES = {"__init__"}


def _iter_tool_modules() -> Iterable[str]:
    for module_info in pkgutil.iter_modules([str(_PACKAGE_DIR)]):
        if module_info.name in _SKIP_MODULES:
            continue
        yield module_info.name


def get_all_tools(categories: list[str] | None = None, verbose: bool = True) -> list:
    """
    Import every tool module in this package and collect all available tools.

    A module that fails to import (missing package) or whose get_tools()
    raises (missing API key) is skipped with a logged warning rather than
    crashing the whole registry — partial availability is expected.
    """
    all_tools: list = []

    for name in _iter_tool_modules():
        if categories is not None and name not in categories:
            continue

        try:
            module = importlib.import_module(f"{__name__}.{name}")
        except ImportError as e:
            if verbose:
                logger.warning("tools.%s: skipped (missing dependency: %s)", name, e)
            continue

        module_tools = []
        if hasattr(module, "get_tools"):
            try:
                module_tools = module.get_tools() or []
            except Exception as e:  # missing API key, bad config, etc.
                if verbose:
                    logger.warning("tools.%s: skipped (%s)", name, e)
                continue
        elif hasattr(module, "TOOLS"):
            module_tools = module.TOOLS

        if module_tools:
            if verbose:
                names = ", ".join(getattr(t, "name", str(t)) for t in module_tools)
                logger.info("tools.%s: loaded [%s]", name, names)
            all_tools.extend(module_tools)

    return all_tools


def get_tools_by_name(*names: str) -> list:
    """Pull specific tools out of the full registry by their `.name` attribute."""
    wanted = set(names)
    return [t for t in get_all_tools(verbose=False) if getattr(t, "name", None) in wanted]


def list_categories() -> list[str]:
    """List available tool module names (categories) in this folder."""
    return sorted(_iter_tool_modules())
