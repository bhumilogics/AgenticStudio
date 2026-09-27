"""
Data / Database tools.

pip installs:
    pip install langchain-community sqlalchemy   # SQLDatabaseToolkit
    pip install langchain-community requests      # Requests* tools
    pip install pandas                             # PandasDataFrameTool
"""

from __future__ import annotations

import os

from ._env import require_env


def get_tools() -> list:
    tools = []

    # --- SQL database (query, list tables, get schema, check syntax) ---
    try:
        db_uri = require_env("AGENTICSTUDIO_DB_URI")  # e.g. "sqlite:///./data/agent.db"
        from langchain_community.agent_toolkits.sql.toolkit import SQLDatabaseToolkit
        from langchain_community.utilities import SQLDatabase
        from langchain_anthropic import ChatAnthropic

        db = SQLDatabase.from_uri(db_uri)
        llm = ChatAnthropic(model=os.environ.get("AGENTICSTUDIO_MODEL", "claude-sonnet-5"))
        toolkit = SQLDatabaseToolkit(db=db, llm=llm)
        tools.extend(toolkit.get_tools())
    except Exception:
        pass

    # --- Generic HTTP requests (GET/POST/PATCH/PUT/DELETE) ---
    # Gated: lets the agent call arbitrary URLs. Enable explicitly.
    if os.environ.get("AGENTICSTUDIO_ALLOW_HTTP_REQUESTS") == "1":
        try:
            from langchain_community.agent_toolkits.openapi.toolkit import RequestsToolkit
            from langchain_community.utilities.requests import TextRequestsWrapper

            toolkit = RequestsToolkit(
                requests_wrapper=TextRequestsWrapper(headers={}),
                allow_dangerous_requests=True,
            )
            tools.extend(toolkit.get_tools())
        except ImportError:
            pass

    # --- OpenAPI toolkit: point at a spec file/URL via env var to enable ---
    openapi_spec_path = os.environ.get("AGENTICSTUDIO_OPENAPI_SPEC")
    if openapi_spec_path:
        try:
            import yaml
            from langchain_community.agent_toolkits.openapi.toolkit import OpenAPIToolkit
            from langchain_community.tools.json.tool import JsonSpec
            from langchain_community.utilities.requests import TextRequestsWrapper
            from langchain_anthropic import ChatAnthropic

            with open(openapi_spec_path) as f:
                raw_spec = yaml.safe_load(f)
            json_spec = JsonSpec(dict_=raw_spec, max_value_length=4000)
            llm = ChatAnthropic(model=os.environ.get("AGENTICSTUDIO_MODEL", "claude-sonnet-5"))
            toolkit = OpenAPIToolkit.from_llm(llm, json_spec, TextRequestsWrapper(headers={}), allow_dangerous_requests=True)
            tools.extend(toolkit.get_tools())
        except Exception:
            pass

    # --- JSON toolkit: point at a JSON file via env var to enable ---
    json_path = os.environ.get("AGENTICSTUDIO_JSON_FILE")
    if json_path:
        try:
            import json as _json
            from langchain_community.agent_toolkits.json.toolkit import JsonToolkit
            from langchain_community.tools.json.tool import JsonSpec

            with open(json_path) as f:
                data = _json.load(f)
            toolkit = JsonToolkit(spec=JsonSpec(dict_=data))
            tools.extend(toolkit.get_tools())
        except Exception:
            pass

    # --- Pandas dataframe query tool: point at a CSV via env var to enable ---
    csv_path = os.environ.get("AGENTICSTUDIO_CSV_FILE")
    if csv_path:
        try:
            import pandas as pd
            from langchain_experimental.agents.agent_toolkits import create_pandas_dataframe_agent
            from langchain_anthropic import ChatAnthropic
            from langchain_core.tools import tool

            df = pd.read_csv(csv_path)
            llm = ChatAnthropic(model=os.environ.get("AGENTICSTUDIO_MODEL", "claude-sonnet-5"))
            pandas_agent = create_pandas_dataframe_agent(llm, df, allow_dangerous_code=True, verbose=False)

            @tool("query_dataframe")
            def query_dataframe(question: str) -> str:
                """Ask a natural-language question about the configured CSV dataset."""
                return pandas_agent.invoke({"input": question})["output"]

            tools.append(query_dataframe)
        except Exception:
            pass

    return tools
