"""
Convenience entrypoint.

    python main.py "research the current state of X and write a summary"

For the API: uvicorn interface.api:app --reload
For the CLI:  python -m interface.cli --help
For the dashboard: streamlit run observability/dashboard.py
"""

from __future__ import annotations

import sys
import uuid

from core.config import settings
from core.graph import build_graph
from core.state import initial_state
from observability import configure_logging, stream_and_trace


def main():
    configure_logging()

    if len(sys.argv) < 2:
        print('Usage: python main.py "<task description>"')
        sys.exit(1)

    task = " ".join(sys.argv[1:])
    thread_id = str(uuid.uuid4())
    print(f"thread_id: {thread_id}")

    from langchain_core.messages import HumanMessage

    graph = build_graph()
    state = initial_state(task, settings.max_iterations, settings.max_tool_retries)
    # No SystemMessage here -- the planner node prepends its agent's own
    # instructions (agents/planner.md by default) when none is present yet.
    state["messages"] = [HumanMessage(content=task)]
    config = {"configurable": {"thread_id": thread_id}}

    for update in stream_and_trace(graph, state, config):
        for node_name, node_update in update.items():
            print(f"[{node_name}] status={node_update.get('status')}")

    final = graph.get_state(config).values
    print("\n--- outcome ---")
    if final.get("status") == "done":
        print(final.get("final_answer"))
    elif final.get("status") == "error":
        print(f"error: {final.get('error')}")
    else:
        print(f"paused ({final.get('status')}) -- resume with:")
        print(f'  python -m interface.cli resume {thread_id} approve')


if __name__ == "__main__":
    main()
