"""
TestDesk -- a "Self test guide" for AgenticStudio.

A user describes a scenario ("run the QA job against this change and tell me
if the plan covers login"), picks a mode, and the Studio performs it:

    test  -> runs in a THROWAWAY sandbox: its own temp copy of every database
             (jobs/runs/users, LangGraph checkpoints, run events), logs, work
             dir, jobs/ and agents/. Nothing it does can reach the app DB, and
             the sandbox is deleted when the scenario finishes. Only the short
             pass/fail report is kept (in TestDesk's own separate DB).
    prod  -> runs through the Studio's normal run path, so the Run, its events
             and its checkpoints are kept in the app DB like any real run.

See TestDesk/README.md.
"""
