# tools/

Every LangChain tool/toolkit from the plan, organized one module per
category. This registry is what the LangGraph tool-executor node will
call into.

## Files

| Module | Category | Needs config? |
|---|---|---|
| `search_web.py` | DuckDuckGo, Tavily, Google/Serper/Bing, SearX, Brave, Wikipedia, Arxiv, PubMed, web page fetch | Only the paid ones (Tavily/Google/Bing/Serper/Brave) |
| `code_execution.py` | Python REPL, Shell, Bearly, E2B | Shell/PythonREPL gated behind `AGENTICSTUDIO_ALLOW_UNSANDBOXED_EXEC=1`; Bearly/E2B need API keys |
| `file_ops.py` | Read/write/list/copy/move/delete/search files | No — sandboxed to `AGENTICSTUDIO_WORKDIR` (default `./workdir`) |
| `data_db.py` | SQL DB, HTTP requests, OpenAPI, JSON, pandas dataframe | Yes, per sub-tool (see docstring) |
| `productivity.py` | Gmail, Slack, GitHub, GitLab, Jira, Office365, Zapier | Yes, all of them |
| `cloud_infra.py` | AWS Lambda, Amadeus, Playwright browser | Yes, all of them |
| `retrieval.py` | Long-term memory (ChromaDB) as a retriever tool | No — local embeddings by default |
| `math_utils.py` | Calculator, Wolfram Alpha, current datetime | Only Wolfram Alpha |
| `multimodal.py` | DALL-E image gen, Azure Cognitive Services | Yes, both |
| `human_loop.py` | Ask-a-human approval gate | No |

## How discovery works

`tools/__init__.py` scans this folder, imports every module, and collects
tools from either a module-level `TOOLS` list or a `get_tools()` factory.
A module that fails to import (dependency not installed) or whose
`get_tools()` raises `MissingConfig` (env var not set) is **skipped
silently** — you always get "everything that's currently configured,"
never a crash.

```python
from tools import get_all_tools, list_categories

tools = get_all_tools()                 # everything available right now
list_categories()                        # ['cloud_infra', 'code_execution', ...]
tools = get_all_tools(categories=["search_web", "file_ops"])   # just these
```

Wire the result straight into the LangGraph tool node:

```python
from langgraph.prebuilt import ToolNode
from tools import get_all_tools

tool_node = ToolNode(get_all_tools())
```

## Adding a new tool

Drop a new `.py` file in this folder exposing a `get_tools()` function (or
a `TOOLS` list). No changes needed anywhere else — `__init__.py` picks it
up on the next `get_all_tools()` call.

## Security notes

- `code_execution.py`'s raw `ShellTool`/`PythonREPLTool` are **off by
  default**. In this project they're meant to run only inside the Docker
  sandbox layer, not the host process — see the architecture plan.
- `data_db.py`'s generic HTTP request tools are off by default
  (`AGENTICSTUDIO_ALLOW_HTTP_REQUESTS=1` to enable) since they let the
  agent call arbitrary URLs.
- `file_ops.py` is sandboxed to a root directory via LangChain's own
  `root_dir` mechanism — it cannot escape `AGENTICSTUDIO_WORKDIR`.
