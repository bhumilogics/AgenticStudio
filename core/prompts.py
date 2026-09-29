"""
Agent prompts are no longer hardcoded here.

Every role's instructions now live as a Markdown file under agents/,
parsed by core/agent_loader.py into an AgentDefinition:

    agents/planner.md    -- the default single-agent planner (was
                             PLANNER_SYSTEM_PROMPT)
    agents/reflector.md  -- the tool-failure classifier (was
                             REFLECTOR_SYSTEM_PROMPT)
    agents/qa_manager.md, agents/qa_lead.md,
    agents/qa_automation_engineer.md, agents/qa_test_executor.md
                         -- the example multi-agent QA team

To read or edit a role's prompt: edit its .md file directly, or write it
through the natural-language pipeline (core/nl_to_md.py) once that lands.
Nothing in core/graph.py imports from this module anymore -- it's kept only
as a pointer for anyone still looking here out of habit.

This file is reserved, going forward, for prompts that genuinely belong to
*code* rather than to any one agent role -- e.g. a future supervisor's
routing prompt (core/supervisor.py) or the natural-language-to-.md writer's
own meta-prompt (core/nl_to_md.py) -- since those aren't agent personas
themselves, just orchestration/tooling logic.
"""

from __future__ import annotations
