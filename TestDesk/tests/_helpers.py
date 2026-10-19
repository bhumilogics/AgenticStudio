"""Shared test scaffolding: a miniature copy of the project on disk, so the
isolation tests can prove -- by hashing files -- that a test-mode scenario
leaves the 'real' app untouched."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path

REAL_ROOT = Path(__file__).resolve().parents[2]


def build_fake_project(root: Path) -> Path:
    """root/ gets: the real TestDesk package, the real core/job_store.py, a
    jobs/ + agents/ + workdir/, and an app DB pre-loaded with a sentinel row."""
    proj = root / "proj"
    shutil.copytree(REAL_ROOT / "TestDesk", proj / "TestDesk", ignore=shutil.ignore_patterns("tests", "data", "__pycache__"))
    (proj / "core").mkdir(parents=True)
    shutil.copy(REAL_ROOT / "core" / "job_store.py", proj / "core" / "job_store.py")
    (proj / "jobs").mkdir()
    (proj / "jobs" / "demo.yaml").write_text("id: demo\nname: Demo\nagents: [helper]\n", encoding="utf-8")
    (proj / "agents").mkdir()
    (proj / "agents" / "helper.md").write_text("---\nname: helper\n---\nhi\n", encoding="utf-8")
    (proj / "workdir").mkdir()
    (proj / "workdir" / "seed.txt").write_text("seed", encoding="utf-8")
    (proj / "data").mkdir()
    db = proj / "data" / "jobs.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sentinel (v TEXT)")
    conn.execute("INSERT INTO sentinel VALUES ('app data')")
    conn.commit()
    conn.close()
    return proj


def snapshot(proj: Path) -> dict[str, str]:
    """{relative path: sha256} for every file in the fake project (excluding the TestDesk package)."""
    out = {}
    for p in sorted(proj.rglob("*")):
        if p.is_file() and "TestDesk" not in p.relative_to(proj).parts and "__pycache__" not in p.parts:
            out[str(p.relative_to(proj))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out
