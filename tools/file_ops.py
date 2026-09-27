"""
File & Document tools.

Scoped to AGENTICSTUDIO_WORKDIR (defaults to ./workdir) via LangChain's
built-in `root_dir` sandboxing on FileManagementToolkit -- the agent can
never read/write outside that directory.

pip installs:
    pip install langchain-community
"""

from __future__ import annotations

import os


def get_tools() -> list:
    tools = []

    try:
        from langchain_community.agent_toolkits import FileManagementToolkit

        root_dir = os.environ.get("AGENTICSTUDIO_WORKDIR", "./workdir")
        os.makedirs(root_dir, exist_ok=True)

        toolkit = FileManagementToolkit(
            root_dir=root_dir,
            selected_tools=[
                "read_file",
                "write_file",
                "list_directory",
                "copy_file",
                "move_file",
                "file_delete",
                "file_search",
            ],
        )
        tools.extend(toolkit.get_tools())
    except ImportError:
        pass

    return tools
