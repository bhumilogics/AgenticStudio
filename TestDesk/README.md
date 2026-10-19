# TestDesk — the Studio's self-test guide

Describe a scenario, choose where it runs, and the Studio performs it — so you can try anything before doing it live.

Open **TestDesk** in the sidebar (`#/testdesk`).

## Two modes

| | **Test** (default) | **Prod** |
|---|---|---|
| Where it runs | A throwaway sandbox: its own temp copy of the jobs/runs DB, LangGraph checkpoints, run events, logs, work folder, `jobs/` and `agents/` | The Studio's normal run path — same as pressing *Run* on the Jobs page |
| What is kept | Only the short **report** (verdict, checks, final answer, trimmed events), in `TestDesk/data/testdesk.sqlite` | The Run, its events and checkpoints stay in the app DB (`data/jobs.sqlite`, …) — visible on Jobs / Log Explorer |
| Can touch the app DB? | No — see *How isolation works* | Yes, by design |
| Targets | A Job, an Agent, or a **draft Job YAML** you haven't saved | A saved Job only |
| Approval gates | Auto-approved (option) so the scenario can finish | Stops at `awaiting_human`; resume from the Run page |

Prod needs a deliberate second click. After a passing test, **“Looks good — set up a real (prod) run”** loads the same scenario in Prod mode.

## A scenario

* **What to test** — your words; this becomes the task text given to the Job.
* **Target** — a Job or an Agent (agents run as a one-step Job).
* **Expected outcome** — `done` (default), `error`, or `awaiting_human`.
* **Final answer should mention** — optional phrases, one per line (case-insensitive). The verdict is **pass** when the run ended as expected and every phrase is in the final answer.
* **Draft Job YAML** (optional, test only) — try a Job definition before saving it. It exists only inside the sandbox.

Verdicts: `pass` · `fail` (it ran, a check didn't hold — including the Job erroring) · `awaiting_human` (prod stopped at an approval gate) · `error` (TestDesk itself couldn't run it: crash, timeout, failed isolation check — says nothing about the Job).

## How isolation works

Every place the Studio writes is located through an environment variable. A test-mode scenario runs in a **separate process** (`python -m TestDesk.runner`) whose environment points all of them into one temp directory (`sandbox.py`):

`AGENTICSTUDIO_JOB_DB`, `_CHECKPOINT_DB`, `_EVENTS_DB`, `_LOG_DIR`, `_WORKDIR`, `_MEMORY_DIR`, `_JOBS_DIR`, `_AGENTS_DIR`

Before importing anything from `core`, the runner checks that **every one** resolves inside the sandbox and refuses to start otherwise (`runner.assert_isolated`). A separate process also means the API keeps serving real requests meanwhile, and import-time settings can't leak. The sandbox is deleted when the scenario ends (unless you tick *keep the sandbox*).

`tests/test_isolation.py` proves it by hashing a miniature app before and after a run, and by feeding the runner bad paths.

### What a sandbox does *not* isolate

* **LLM calls are real** — a scenario costs tokens like any run (Claude Code CLI backend: subscription usage).
* **Outbound tools.** With *Block outbound tools* on (default), these are switched off for the run: HTTP POST/PUT/PATCH/DELETE, shell and Python REPL, browser, Gmail, AWS, the SQL tool's database (`AGENTICSTUDIO_DB_URI`), and the Job's webhook. Untick it only if you mean the scenario to really reach those. Read-only tools (web search, Wikipedia…) still work.
* Anything else a custom tool does by absolute path or its own credentials.

## Files

| File | Role |
|---|---|
| `store.py` | Saved scenarios + reports (separate SQLite file) |
| `sandbox.py` | Builds/destroys the sandbox and its environment |
| `runner.py` | Subprocess entrypoint; isolation guard; real engine + test-only fake engine |
| `service.py` | Validation, judging, test/prod execution, background threads |
| `routes.py` | `/testdesk/*` API, mounted in `interface/api.py` |
| `../frontend/testdesk.js` | The page (must live in `frontend/` to be served) |
| `tests/` | `python -m unittest discover -s TestDesk/tests -t .` (or pytest) |
| `../playwright/tests/testdesk.spec.js` | Browser tests against a stubbed API |

## API

`GET /testdesk/targets` · `GET/POST /testdesk/scenarios` · `PUT/DELETE /testdesk/scenarios/{id}` · `POST /testdesk/run {mode, scenario_id | scenario}` → `{result_id}` (runs in the background) · `GET /testdesk/results` · `GET /testdesk/results/{id}` (poll until `state == "finished"`).

Any signed-in user can use it; scenarios and reports are visible to their owner and to admins. At most 3 scenarios run at once.
