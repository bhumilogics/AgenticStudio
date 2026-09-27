"""
Code & Execution tools.

SECURITY NOTE: ShellTool and PythonREPLTool execute arbitrary code/commands
on the host with the framework's own permissions. In this project they must
only ever be handed to the agent inside the Docker sandbox layer described
in the architecture plan -- never registered for an ungated/production graph
run. They're included here so they're available in dev, gated behind
AGENTICSTUDIO_ALLOW_UNSANDBOXED_EXEC=1.

pip installs:
    pip install langchain-experimental        # PythonREPLTool, PythonAstREPLTool
    pip install langchain-community            # ShellTool
    pip install bearly-sdk                      # BearlyInterpreterTool (BEARLY_API_KEY)
    pip install e2b-code-interpreter            # E2BDataAnalysisTool (E2B_API_KEY)
"""

from __future__ import annotations

import os

from ._env import require_env

_UNSANDBOXED_OK = os.environ.get("AGENTICSTUDIO_ALLOW_UNSANDBOXED_EXEC") == "1"


def get_tools() -> list:
    tools = []

    if not _UNSANDBOXED_OK:
        # Refuse to load raw shell/python execution unless explicitly opted
        # in -- these should normally only run inside the Docker sandbox
        # node, not the host process. Sandboxed variants (E2B, Bearly) are
        # loaded below regardless, since they're remote-executed.
        pass
    else:
        try:
            from langchain_experimental.tools import PythonREPLTool

            tools.append(PythonREPLTool(name="python_repl"))
        except ImportError:
            pass

        try:
            from langchain_experimental.tools.python.tool import PythonAstREPLTool

            tools.append(PythonAstREPLTool(name="python_ast_repl"))
        except ImportError:
            pass

        try:
            from langchain_community.tools import ShellTool

            tools.append(ShellTool(name="shell_exec"))
        except ImportError:
            pass

    # --- Bearly (remote sandboxed interpreter -- safe to load unconditionally) ---
    try:
        require_env("BEARLY_API_KEY")
        from langchain_community.tools import BearlyInterpreterTool

        bearly = BearlyInterpreterTool(api_key=require_env("BEARLY_API_KEY"))
        tools.append(bearly.as_tool())
    except Exception:
        pass

    # --- E2B (remote sandboxed code interpreter -- safe to load unconditionally) ---
    try:
        require_env("E2B_API_KEY")
        from langchain_community.tools import E2BDataAnalysisTool

        tools.append(E2BDataAnalysisTool(api_key=require_env("E2B_API_KEY"), name="e2b_code_interpreter"))
    except Exception:
        pass

    return tools
