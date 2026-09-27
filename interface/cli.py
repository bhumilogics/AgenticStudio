"""
Typer CLI.

    python -m interface.cli run "research X and summarize it"
    python -m interface.cli resume <thread_id> approve
    python -m interface.cli resume <thread_id> deny "too risky"
    python -m interface.cli tools list
    python -m interface.cli tools list --category search_web
"""

from __future__ import annotations

import uuid

import typer
from rich.console import Console
from rich.table import Table

from core.config import settings
from core.graph import build_graph, resume_task
from observability import configure_logging, stream_and_trace
from observability.logging_config import get_logger

app = typer.Typer(help="AgenticStudio — autonomous general-purpose agent framework")
tools_app = typer.Typer(help="Inspect the tools/ registry")
app.add_typer(tools_app, name="tools")

console = Console()
logger = get_logger("agenticstudio.cli")


@app.callback()
def _startup():
    configure_logging()


@app.command()
def run(
    task: str = typer.Argument(..., help="The task to give the agent"),
    thread_id: str = typer.Option(None, help="Resume/continue a specific thread instead of starting a new one"),
):
    """Run a task to completion (or until it pauses for human approval)."""
    from langchain_core.messages import HumanMessage, SystemMessage
    from core.prompts import PLANNER_SYSTEM_PROMPT
    from core.state import initial_state

    thread_id = thread_id or str(uuid.uuid4())
    console.print(f"[bold cyan]thread:[/bold cyan] {thread_id}")

    graph = build_graph()
    state = initial_state(task, settings.max_iterations, settings.max_tool_retries)
    state["messages"] = [SystemMessage(content=PLANNER_SYSTEM_PROMPT), HumanMessage(content=task)]
    config = {"configurable": {"thread_id": thread_id}}

    final_state = _stream_to_console(graph, state, config, thread_id)
    _print_outcome(final_state, thread_id)


@app.command()
def resume(
    thread_id: str = typer.Argument(...),
    decision: str = typer.Argument(..., help='"approve" or "deny: <reason>"'),
):
    """Resume a thread that's paused awaiting human approval."""
    graph = build_graph()
    config = {"configurable": {"thread_id": thread_id}}
    from langgraph.types import Command

    final_state = _stream_to_console(graph, Command(resume=decision), config, thread_id)
    _print_outcome(final_state, thread_id)


@app.command()
def status(thread_id: str = typer.Argument(...)):
    """Show a thread's current checkpointed state."""
    graph = build_graph()
    snapshot = graph.get_state({"configurable": {"thread_id": thread_id}})
    console.print(snapshot.values)
    if snapshot.next:
        console.print(f"[yellow]paused before:[/yellow] {snapshot.next}")


@tools_app.command("list")
def tools_list(category: str = typer.Option(None, help="Only show this category")):
    """List every currently-available tool (skips ones missing config)."""
    from tools import get_all_tools, list_categories

    if category:
        tools = get_all_tools(categories=[category], verbose=False)
    else:
        tools = get_all_tools(verbose=False)

    table = Table(title="Available tools")
    table.add_column("name")
    table.add_column("description", overflow="fold")
    for t in tools:
        table.add_row(getattr(t, "name", "?"), (getattr(t, "description", "") or "")[:100])
    console.print(table)
    console.print(f"[dim]categories: {', '.join(list_categories())}[/dim]")


def _stream_to_console(graph, input_, config, thread_id):
    final_state = None
    for update in stream_and_trace(graph, input_, config):
        for node_name, node_update in update.items():
            console.print(f"[green]{node_name}[/green] -> {node_update.get('status', '')}")
        final_state = update
    return graph.get_state(config).values


def _print_outcome(final_state: dict, thread_id: str):
    status_ = final_state.get("status")
    if status_ == "done":
        console.print("\n[bold green]FINAL ANSWER[/bold green]")
        console.print(final_state.get("final_answer", ""))
    elif status_ == "awaiting_human":
        console.print("\n[bold yellow]Paused for approval.[/bold yellow]")
        console.print(f'Resume with: python -m interface.cli resume {thread_id} approve')
    elif status_ == "error":
        console.print(f"\n[bold red]Stopped with error:[/bold red] {final_state.get('error')}")
    else:
        # Graph may also be paused mid-stream at the human_approval interrupt
        # without status having been updated yet -- check get_state().next.
        console.print(f"\n[bold yellow]Run ended in state:[/bold yellow] {status_}")
        console.print(f"If paused for approval: python -m interface.cli resume {thread_id} approve")


if __name__ == "__main__":
    app()
