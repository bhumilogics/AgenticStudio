"""
The guarantee that matters: test mode never touches the app.

These run the real sandbox + the real runner subprocess against a miniature
fake project, with the 'fake' engine (no LLM, no LangGraph needed) driving the
REAL core/job_store.py persistence code. The fake project's app DB, jobs/,
agents/ and workdir/ are hashed before and after.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from TestDesk import sandbox, service, store

from ._helpers import build_fake_project, snapshot

SCENARIO = {"id": None, "name": "t", "target_type": "job", "target_id": "demo", "task": "check the login page",
            "expectations": [], "expect_status": "done", "job_yaml": "", "options": {}}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.proj = build_fake_project(self.tmp)
        self.sandboxes = self.tmp / "sandboxes"
        self.sandboxes.mkdir()
        os.environ["TESTDESK_DB"] = str(self.tmp / "testdesk.sqlite")

    def tearDown(self):
        os.environ.pop("TESTDESK_DB", None)
        self._tmp.cleanup()

    def run_test_mode(self, **opts):
        sc = {**SCENARIO, "options": opts.pop("options", {})}
        return service.execute_test_mode(sc, project_root=self.proj, base_dir=self.sandboxes, engine="fake", **opts)


class TestModeLeavesAppUntouched(Base):
    def test_app_files_are_byte_identical_after_a_run(self):
        before = snapshot(self.proj)
        res = self.run_test_mode()
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["run_status"], "done")
        self.assertEqual(snapshot(self.proj), before, "test mode changed something in the app")
        # the sentinel row in the app DB is still the only content
        rows = sqlite3.connect(self.proj / "data" / "jobs.sqlite").execute("SELECT v FROM sentinel").fetchall()
        self.assertEqual(rows, [("app data",)])

    def test_run_was_recorded_in_the_sandbox_not_the_app(self):
        res = self.run_test_mode(options={"keep_sandbox": True})
        sb_root = Path(res["sandbox_path"])
        self.assertTrue(str(Path(res["db_path"]).resolve()).startswith(str(sb_root.resolve())))
        runs = sqlite3.connect(sb_root / "data" / "jobs.sqlite").execute("SELECT job_id, triggered_by, status FROM runs").fetchall()
        self.assertEqual(runs, [("demo", "testdesk", "done")])
        # the scenario's writes landed in the sandbox copies, never the project's
        self.assertTrue((sb_root / "workdir" / "testdesk_footprint.txt").exists())
        self.assertTrue((sb_root / "jobs" / "testdesk_footprint.yaml").exists())
        self.assertFalse((self.proj / "workdir" / "testdesk_footprint.txt").exists())
        self.assertFalse((self.proj / "jobs" / "testdesk_footprint.yaml").exists())
        # ...and the sandbox started as a copy of the project's jobs/agents/workdir
        self.assertTrue((sb_root / "workdir" / "seed.txt").exists())
        self.assertTrue((sb_root / "jobs" / "demo.yaml").exists())
        self.assertTrue((sb_root / "agents" / "helper.md").exists())

    def test_sandbox_is_deleted_by_default(self):
        res = self.run_test_mode()
        self.assertIsNone(res["sandbox_path"])
        self.assertTrue(res["cleanup_ok"])
        self.assertEqual(list(self.sandboxes.iterdir()), [])

    def test_keep_sandbox_keeps_it(self):
        res = self.run_test_mode(options={"keep_sandbox": True})
        self.assertTrue(Path(res["sandbox_path"]).is_dir())

    def test_two_runs_do_not_share_state(self):
        a = self.run_test_mode(options={"keep_sandbox": True})
        b = self.run_test_mode(options={"keep_sandbox": True})
        self.assertNotEqual(a["sandbox_path"], b["sandbox_path"])
        self.assertNotEqual(a["run_id"], b["run_id"])
        for r in (a, b):
            n = sqlite3.connect(Path(r["sandbox_path"]) / "data" / "jobs.sqlite").execute("SELECT count(*) FROM runs").fetchone()[0]
            self.assertEqual(n, 1)

    def test_timeout_stops_the_run_and_still_cleans_up(self):
        res = service.execute_test_mode(
            {**SCENARIO, "options": {"timeout_seconds": 1}}, project_root=self.proj, base_dir=self.sandboxes,
            engine="fake", fake={"delay": 30},
        )
        self.assertFalse(res["ok"])
        self.assertIn("timed out", res["error"])
        self.assertEqual(list(self.sandboxes.iterdir()), [])

    def test_runner_crash_is_reported_not_raised(self):
        res = service.execute_test_mode(SCENARIO, project_root=self.proj, base_dir=self.sandboxes, engine="nonsense")
        # unknown engine falls to the real engine, which cannot import `core` in the fake project
        self.assertEqual(res["run_status"], "error")
        self.assertTrue(res["error"])


class IsolationGuard(Base):
    """runner.py must refuse to run if any Studio path escapes the sandbox."""

    def _run_runner(self, env_patch: dict, drop: list[str] = ()):
        sb = sandbox.create_sandbox(self.proj, base_dir=self.sandboxes)
        env = sandbox.build_process_env(sb)
        env["TESTDESK_ALLOW_FAKE_ENGINE"] = "1"
        env.update(env_patch)
        for k in drop:
            env.pop(k, None)
        out = sb.root / "result.json"
        spec = {"sandbox_root": sb.path, "out_path": str(out), "engine": "fake", "target_type": "job", "target_id": "demo", "task": "x"}
        (sb.root / "spec.json").write_text(json.dumps(spec))
        p = subprocess.run([sys.executable, "-m", "TestDesk.runner", str(sb.root / "spec.json")], cwd=self.proj, env=env, capture_output=True, text=True)
        return p, json.loads(out.read_text()), sb

    def test_db_path_pointing_at_the_app_db_is_refused(self):
        app_db = self.proj / "data" / "jobs.sqlite"
        before = snapshot(self.proj)
        p, res, _ = self._run_runner({"AGENTICSTUDIO_JOB_DB": str(app_db)})
        self.assertEqual(p.returncode, 3)
        self.assertIn("ISOLATION CHECK FAILED", res["error"])
        self.assertIn("AGENTICSTUDIO_JOB_DB", res["error"])
        self.assertEqual(snapshot(self.proj), before)

    def test_a_missing_variable_is_refused(self):
        p, res, _ = self._run_runner({}, drop=["AGENTICSTUDIO_EVENTS_DB"])
        self.assertEqual(p.returncode, 3)
        self.assertIn("AGENTICSTUDIO_EVENTS_DB", res["error"])

    def test_a_relative_default_is_refused(self):
        p, res, _ = self._run_runner({"AGENTICSTUDIO_WORKDIR": "./workdir"})
        self.assertEqual(p.returncode, 3)
        self.assertIn("AGENTICSTUDIO_WORKDIR", res["error"])

    def test_fake_engine_is_off_unless_explicitly_enabled(self):
        p, res, _ = self._run_runner({"TESTDESK_ALLOW_FAKE_ENGINE": "0"})
        self.assertFalse(res["ok"])
        self.assertIn("fake engine is disabled", res["error"])


class SandboxEnvironment(Base):
    def test_every_guarded_variable_is_set_inside_the_sandbox(self):
        sb = sandbox.create_sandbox(self.proj, base_dir=self.sandboxes)
        root = sb.root.resolve()
        for var in sandbox.ISOLATED_PATH_VARS:
            self.assertIn(var, sb.env)
            p = Path(sb.env[var]).resolve()
            self.assertTrue(p == root or root in p.parents, var)

    def test_block_external_switches_outward_tools_off(self):
        sb = sandbox.create_sandbox(self.proj, base_dir=self.sandboxes)
        env = sandbox.build_process_env(sb, base={"AGENTICSTUDIO_DB_URI": "postgresql://prod/db", "AWS_ACCESS_KEY_ID": "real", "GMAIL_CREDENTIALS_PATH": "/x/creds.json", "ANTHROPIC_API_KEY": "keep-me"})
        self.assertEqual(env["AWS_ACCESS_KEY_ID"], "")          # present-but-empty, so load_dotenv() can't refill it from .env
        self.assertEqual(env["GMAIL_CREDENTIALS_PATH"], "")
        self.assertEqual(env["AGENTICSTUDIO_DB_URI"], "")        # the SQL tool must not reach a real database
        self.assertEqual(env["AGENTICSTUDIO_ALLOW_HTTP_REQUESTS"], "0")
        self.assertEqual(env["AGENTICSTUDIO_ALLOW_UNSANDBOXED_EXEC"], "0")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "keep-me")   # the LLM still works

    def test_block_external_can_be_turned_off(self):
        sb = sandbox.create_sandbox(self.proj, base_dir=self.sandboxes, block_external=False)
        self.assertNotIn("AGENTICSTUDIO_ALLOW_HTTP_REQUESTS", sb.env)

    def test_oversized_workdir_is_not_copied(self):
        big = self.proj / "workdir" / "big.bin"
        big.write_bytes(b"0" * 2048)
        sb = sandbox.create_sandbox(self.proj, base_dir=self.sandboxes)
        self.assertTrue((sb.root / "workdir" / "big.bin").exists())
        orig = sandbox._COPY_LIMIT_BYTES
        try:
            sandbox._COPY_LIMIT_BYTES = 100
            sb2 = sandbox.create_sandbox(self.proj, base_dir=self.sandboxes)
        finally:
            sandbox._COPY_LIMIT_BYTES = orig
        self.assertEqual(list((sb2.root / "workdir").iterdir()), [])


class EndToEndService(Base):
    def test_test_mode_saves_a_report_and_no_app_data(self):
        before = snapshot(self.proj)
        sc = {**SCENARIO, "expectations": ["fake answer"], "options": {}}
        rid = service.start_run(sc, "test", owner_id="u1", project_root=self.proj, base_dir=self.sandboxes, engine="fake", wait=True)
        row = store.get_result(rid)
        self.assertEqual((row["state"], row["verdict"], row["mode"]), ("finished", "pass", "test"))
        self.assertIsNone(row["run_id"])  # a sandbox run id means nothing outside the sandbox
        self.assertEqual(row["scenario"]["task"], "check the login page")
        self.assertEqual(snapshot(self.proj), before)
        self.assertEqual(service.active_runs(), 0)

    def test_failed_expectation_is_a_fail(self):
        sc = {**SCENARIO, "expectations": ["something the answer never says"]}
        rid = service.start_run(sc, "test", owner_id="u1", project_root=self.proj, base_dir=self.sandboxes, engine="fake", wait=True)
        self.assertEqual(store.get_result(rid)["verdict"], "fail")

    def test_prod_mode_goes_through_the_normal_run_function(self):
        calls = []

        def fake_run_job_sync(job_id, task, triggered_by, user_id):
            calls.append((job_id, task, triggered_by, user_id))
            return {"run_id": "real-run-1", "status": "done", "final_answer": "all good"}

        rid = service.start_run({**SCENARIO, "expectations": ["all good"]}, "prod", owner_id="u1", run_job_sync=fake_run_job_sync, wait=True)
        self.assertEqual(calls, [("demo", "check the login page", "testdesk", "u1")])
        row = store.get_result(rid)
        self.assertEqual((row["verdict"], row["run_id"], row["mode"]), ("pass", "real-run-1", "prod"))
        self.assertEqual(list(self.sandboxes.iterdir()), [])  # prod never builds a sandbox

    def test_prod_mode_refuses_agents_and_draft_jobs(self):
        for patch in ({"target_type": "agent"}, {"job_yaml": "id: demo\n"}):
            with self.assertRaises(service.ScenarioError):
                service.start_run({**SCENARIO, **patch}, "prod", owner_id="u1", run_job_sync=lambda *a: {})

    def test_prod_run_crash_becomes_an_error_report(self):
        def boom(*a):
            raise RuntimeError("db is locked")

        rid = service.start_run(SCENARIO, "prod", owner_id="u1", run_job_sync=boom, wait=True)
        row = store.get_result(rid)
        self.assertEqual(row["verdict"], "error")
        self.assertIn("db is locked", row["error"])

    def test_concurrency_limit(self):
        service._active = service.MAX_CONCURRENT_TEST_RUNS
        try:
            with self.assertRaises(service.ScenarioError):
                service.start_run(SCENARIO, "test", owner_id="u1", project_root=self.proj, engine="fake")
        finally:
            service._active = 0

    def test_a_crash_in_the_worker_never_leaves_a_running_row(self):
        orig = service.execute_test_mode
        service.execute_test_mode = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("kaboom"))
        try:
            rid = service.start_run(SCENARIO, "test", owner_id="u1", project_root=self.proj, wait=True)
        finally:
            service.execute_test_mode = orig
        row = store.get_result(rid)
        self.assertEqual((row["state"], row["verdict"]), ("finished", "error"))
        self.assertEqual(service.active_runs(), 0)


if __name__ == "__main__":
    unittest.main()
