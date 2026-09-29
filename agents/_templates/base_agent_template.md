---
name: my_new_agent
display_name: My New Agent
role: One-line description of what this role is for
reports_to: null
hands_off_to: null
model: claude-sonnet-5
llm_backend: null        # null -> use the project-wide AGENTICSTUDIO_LLM_BACKEND
tools: []                # tool category/module names from tools/ -- see GET /tools
autonomy: null           # null -> use the project-wide AGENTICSTUDIO_AUTONOMY
risky_tools: []          # tool names that always require human approval for this agent
max_iterations: null     # null -> use the project-wide AGENTICSTUDIO_MAX_ITERATIONS
max_tool_retries: null   # null -> use the project-wide AGENTICSTUDIO_MAX_TOOL_RETRIES
version: 1
---

## Role

One or two sentences: who this agent is and what it is responsible for.

## Instructions

1. First thing this agent should do given a task.
2. How it should decide what to do next.
3. When it should hand off to another role instead of continuing itself.
4. When it should stop and ask a human instead of guessing.

## Definition of done

- A clear, checkable statement of what "finished" looks like for this role.
- Another one, if there's more than one condition.
