"""
The throwaway world a test-mode scenario runs in.

Everything the Studio persists or writes is located through an environment
variable, and the scenario runs in a *separate process* (TestDesk/runner.py)
whose environment points every one of them into one temp directory:

    AGENTICSTUDIO_JOB_DB         jobs / runs / schedules / users   (core/job_store.py, read per call)
    AGENTICSTUDIO_CHECKPOINT_DB  LangGraph checkpoints             (core/config.py, read at import)
    AGENTICSTUDIO_EVENTS_DB      run events / audit trail          (core/config.py, read at import)
    AGENTICSTUDIO_LOG_DIR        log files                         (observability/logging_config.py)
    AGENTICSTUDIO_WORKDIR        file tools' root                  (tools/file_ops.py)
    AGENTICSTUDIO_MEMORY_DIR     long-term memory store            (tools/retrieval.py)
    AGENTICSTUDIO_JOBS_DIR       jobs/*.yaml                       (core/job_loader.py, read at import)
    AGENTICSTUDIO_AGENTS_DIR     agents/*.md                       (core/agent_loader.py, read at import)

A separate process (rather than swapping os.environ inside the API server) is
what makes this safe: the API keeps serving real requests against the real DB
while a scenario runs, and the import-time settings above can't leak across.

What a sandbox does NOT isolate is the outside world. A scenario that calls a
live LLM still costs tokens, and a tool that reaches out (HTTP POST, shell,
Gmail, AWS) would really do it -- so `block_external` (default on) switches
those tools off for the run. See BLOCKED_ENV.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

# Values that disable outward-reaching tools. They are set to a *value*
# rather than removed, because core/config.py calls load_dotenv() (which
# fills in any variable that is merely absent from the environment from
# .env -- it would silently put real credentials back). Empty string counts
# as "not set" to tools/_env.require_env.
BLOCKED_ENV = {
    "AGENTICSTUDIO_ALLOW_HTTP_REQUESTS": "0",       # requests_post / put / patch / delete
    "AGENTICSTUDIO_ALLOW_UNSANDBOXED_EXEC": "0",    # shell_exec / python_repl
    "AGENTICSTUDIO_ENABLE_BROWSER": "0",
    "AGENTICSTUDIO_DB_URI": "",                     # the SQL tool's database (tools/data_db.py)
    "GMAIL_CREDENTIALS_PATH": "",
    "GMAIL_TOKEN_PATH": "",
    "AWS_ACCESS_KEY_ID": "",
    "AWS_SECRET_ACCESS_KEY": "",
    "AWS_SESSION_TOKEN": "",
    "AWS_PROFILE": "",
}

# The env vars that name a place the Studio writes. runner.py refuses to start
# unless every one of these resolves inside the sandbox.
ISOLATED_PATH_VARS = (
    "AGENTICSTUDIO_JOB_DB",
    "AGENTICSTUDIO_CHECKPOINT_DB",
    "AGENTICSTUDIO_EVENTS_DB",
    "AGENTICSTUDIO_LOG_DIR",
    "AGENTICSTUDIO_WORKDIR",
    "AGENTICSTUDIO_MEMORY_DIR",
    "AGENTICSTUDIO_JOBS_DIR",
    "AGENTICSTUDIO_AGENTS_DIR",
)

_COPY_LIMIT_BYTES = 50 * 1024 * 1024


@dataclass
class Sandbox:
    root: Path
    env: dict[str, str] = field(default_factory=dict)
    keep: bool = False

    @property
    def path(self) -> str:
        return str(self.root)


def _copy_tree(src: Path, dst: Path, limit: int | None = None) -> None:
    """Copy src -> dst, skipping everything if src is missing or bigger than
    `limit` (a runaway workdir shouldn't make every test slow)."""
    limit = _COPY_LIMIT_BYTES if limit is None else limit
    dst.mkdir(parents=True, exist_ok=True)
    if not src.is_dir():
        return
    total = sum(p.stat().st_size for p in src.rglob("*") if p.is_file())
    if total > limit:
        return
    shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def create_sandbox(
    project_root: str | os.PathLike,
    *,
    base_dir: str | os.PathLike | None = None,
    keep: bool = False,
    block_external: bool = True,
    copy_workdir: bool = True,
) -> Sandbox:
    """Build a fresh sandbox directory and the environment overrides that
    point the Studio at it. The jobs/ and agents/ definitions are COPIED in,
    so a scenario sees the same roles and Jobs as the real app but any draft
    Job it adds (or edit it makes) lands only in the copy. The app DB is not
    copied: a scenario starts with empty tables and its own run history."""
    project_root = Path(project_root).resolve()
    root = Path(tempfile.mkdtemp(prefix="testdesk_", dir=str(base_dir) if base_dir else None)).resolve()

    jobs_dir, agents_dir = root / "jobs", root / "agents"
    workdir, memory_dir, log_dir = root / "workdir", root / "memory", root / "logs"
    (root / "data").mkdir()
    _copy_tree(project_root / "jobs", jobs_dir)
    _copy_tree(project_root / "agents", agents_dir)
    if copy_workdir:
        _copy_tree(project_root / "workdir", workdir)
    else:
        workdir.mkdir()
    memory_dir.mkdir()
    log_dir.mkdir()

    env = {
        "AGENTICSTUDIO_JOB_DB": str(root / "data" / "jobs.sqlite"),
        "AGENTICSTUDIO_CHECKPOINT_DB": str(root / "data" / "checkpoints.sqlite"),
        "AGENTICSTUDIO_EVENTS_DB": str(root / "data" / "events.sqlite"),
        "AGENTICSTUDIO_LOG_DIR": str(log_dir),
        "AGENTICSTUDIO_WORKDIR": str(workdir),
        "AGENTICSTUDIO_MEMORY_DIR": str(memory_dir),
        "AGENTICSTUDIO_JOBS_DIR": str(jobs_dir),
        "AGENTICSTUDIO_AGENTS_DIR": str(agents_dir),
        "AGENTICSTUDIO_TESTDESK_SANDBOX": str(root),
    }
    if block_external:
        env.update(BLOCKED_ENV)
    return Sandbox(root=root, env=env, keep=keep)


def build_process_env(sb: Sandbox, base: dict[str, str] | None = None) -> dict[str, str]:
    """The full environment for the runner subprocess: the parent's env
    (API keys etc. stay, so the LLM backend still works) with the sandbox
    overrides layered on top."""
    env = dict(os.environ if base is None else base)
    env.update(sb.env)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def destroy_sandbox(sb: Sandbox, retries: int = 5) -> bool:
    """Delete the sandbox. Returns True if it is gone. SQLite files can be
    briefly locked on Windows right after the process holding them exits, so
    retry a few times before giving up (the caller reports a leftover path)."""
    for i in range(retries):
        shutil.rmtree(sb.root, ignore_errors=True)
        if not sb.root.exists():
            return True
        time.sleep(0.2 * (i + 1))
    return not sb.root.exists()
