"""
Human-in-the-loop tools.

Backs the framework's "checkpoint" autonomy level: the graph can call this
tool to pause and ask a person before an irreversible/risky action instead
of proceeding on its own. In a real deployment, swap the default stdin
prompt_func for whatever your interface uses (a Slack DM, a web approval
UI, etc.) by passing prompt_func to HumanInputRun.

pip installs:
    pip install langchain-community
"""

from __future__ import annotations


def get_tools() -> list:
    tools = []

    try:
        from langchain_community.tools import HumanInputRun

        tools.append(
            HumanInputRun(
                name="ask_human",
                description=(
                    "Ask a human for approval or input before taking a risky, "
                    "costly, or irreversible action. Use this whenever the "
                    "autonomy policy requires a checkpoint."
                ),
            )
        )
    except ImportError:
        pass

    return tools
