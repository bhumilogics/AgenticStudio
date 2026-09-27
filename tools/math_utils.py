"""
Math / Utility tools.

pip installs:
    pip install langchain-community numexpr    # LLMMathTool
    pip install wolframalpha                    # WolframAlphaQueryRun (WOLFRAM_ALPHA_APPID)
"""

from __future__ import annotations

import datetime as _dt

from langchain_core.tools import tool

from ._env import require_env


def get_tools() -> list:
    tools = []

    # --- LLM-backed calculator (handles arbitrary arithmetic expressions) ---
    try:
        from langchain.chains import LLMMathChain
        from langchain_anthropic import ChatAnthropic
        import os

        llm = ChatAnthropic(model=os.environ.get("AGENTICSTUDIO_MODEL", "claude-sonnet-5"))
        math_chain = LLMMathChain.from_llm(llm)

        @tool("calculator")
        def calculator(expression: str) -> str:
            """Evaluate a math expression, e.g. '(37 * 42) / 6'."""
            return math_chain.run(expression)

        tools.append(calculator)
    except ImportError:
        pass

    # --- Wolfram Alpha (formulas, unit conversion, factual computation) ---
    try:
        require_env("WOLFRAM_ALPHA_APPID")
        from langchain_community.tools import WolframAlphaQueryRun
        from langchain_community.utilities.wolfram_alpha import WolframAlphaAPIWrapper

        tools.append(WolframAlphaQueryRun(api_wrapper=WolframAlphaAPIWrapper(), name="wolfram_alpha"))
    except Exception:
        pass

    # --- Date/time (trivial, no external dep -- written directly rather
    #     than depending on a community add-on) ---
    @tool("current_datetime")
    def current_datetime(timezone: str = "UTC") -> str:
        """Return the current date and time. `timezone` is informational only
        (this returns naive UTC unless zoneinfo is available for the name)."""
        try:
            from zoneinfo import ZoneInfo

            now = _dt.datetime.now(ZoneInfo(timezone))
        except Exception:
            now = _dt.datetime.now(_dt.timezone.utc)
        return now.isoformat()

    tools.append(current_datetime)

    return tools
