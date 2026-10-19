"""HTTP-level tests for TestDesk/routes.py. Needs fastapi + httpx (skipped without them)."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    FastAPI = None

from TestDesk import service, store

from ._helpers import build_fake_project


@unittest.skipIf(FastAPI is None, "fastapi/httpx not installed")
class RouteTests(unittest.TestCase):
    def setUp(self):
        from TestDesk.routes import build_router

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        os.environ["TESTDESK_DB"] = str(self.tmp / "td.sqlite")
        self.proj = build_fake_project(self.tmp)
        self.user = SimpleNamespace(id="u1", email="a@x", role="end_user")
        self.prod_calls = []

        def run_job_sync(job_id, task, by, uid):
            self.prod_calls.append((job_id, task, by, uid))
            return {"run_id": "real-1", "status": "done", "final_answer": "ok"}

        async def current_user():
            return self.user

        # point the service at the fake project + fake engine so no LLM / LangGraph is needed
        self._orig_start = service.start_run
        service.start_run = lambda sc, mode, **kw: self._orig_start(
            sc, mode, **{**kw, "project_root": self.proj, "base_dir": self.tmp, "engine": "fake", "wait": True})

        app = FastAPI()
        app.include_router(build_router(current_user, run_job_sync, known_targets=lambda: ({"demo"}, {"helper"})))
        self.client = TestClient(app)

    def tearDown(self):
        service.start_run = self._orig_start
        os.environ.pop("TESTDESK_DB", None)
        self._tmp.cleanup()

    body = {"name": "n", "target_type": "job", "target_id": "demo", "task": "check login", "expectations": ["fake answer"]}

    def test_targets(self):
        self.assertEqual(self.client.get("/testdesk/targets").json(), {"jobs": ["demo"], "agents": ["helper"]})

    def test_scenario_crud(self):
        r = self.client.post("/testdesk/scenarios", json=self.body)
        self.assertEqual(r.status_code, 200, r.text)
        sid = r.json()["id"]
        self.assertEqual(self.client.get("/testdesk/scenarios").json()["scenarios"][0]["id"], sid)
        r = self.client.put(f"/testdesk/scenarios/{sid}", json={**self.body, "task": "changed"})
        self.assertEqual(r.json()["task"], "changed")
        self.assertEqual(self.client.delete(f"/testdesk/scenarios/{sid}").status_code, 200)
        self.assertEqual(self.client.get("/testdesk/scenarios").json()["scenarios"], [])

    def test_validation_errors_are_422_with_a_message(self):
        r = self.client.post("/testdesk/scenarios", json={**self.body, "target_id": "ghost"})
        self.assertEqual(r.status_code, 422)
        self.assertIn("ghost", r.json()["detail"])
        self.assertEqual(self.client.post("/testdesk/scenarios", json={**self.body, "task": ""}).status_code, 422)

    def test_run_test_mode_inline_then_read_report(self):
        r = self.client.post("/testdesk/run", json={"mode": "test", "scenario": self.body})
        self.assertEqual(r.status_code, 200, r.text)
        rep = self.client.get(f"/testdesk/results/{r.json()['result_id']}").json()
        self.assertEqual((rep["state"], rep["verdict"], rep["mode"]), ("finished", "pass", "test"))
        self.assertEqual(self.prod_calls, [])  # test mode never used the app's run path
        hist = self.client.get("/testdesk/results").json()["results"]
        self.assertEqual(len(hist), 1)

    def test_run_prod_uses_the_app_run_function(self):
        sid = self.client.post("/testdesk/scenarios", json=self.body).json()["id"]
        r = self.client.post("/testdesk/run", json={"mode": "prod", "scenario_id": sid})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.prod_calls, [("demo", "check login", "testdesk", "u1")])
        rep = self.client.get(f"/testdesk/results/{r.json()['result_id']}").json()
        self.assertEqual((rep["run_id"], rep["scenario_id"]), ("real-1", sid))

    def test_prod_refused_for_agents(self):
        r = self.client.post("/testdesk/run", json={"mode": "prod", "scenario": {**self.body, "target_type": "agent", "target_id": "helper"}})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.prod_calls, [])

    def test_run_needs_a_scenario(self):
        self.assertEqual(self.client.post("/testdesk/run", json={"mode": "test"}).status_code, 422)

    def test_users_cannot_see_each_others_scenarios_or_results_but_admins_can(self):
        sid = self.client.post("/testdesk/scenarios", json=self.body).json()["id"]
        rid = self.client.post("/testdesk/run", json={"mode": "test", "scenario_id": sid}).json()["result_id"]

        self.user = SimpleNamespace(id="u2", email="b@x", role="end_user")
        self.assertEqual(self.client.get("/testdesk/scenarios").json()["scenarios"], [])
        self.assertEqual(self.client.get(f"/testdesk/results/{rid}").status_code, 404)
        self.assertEqual(self.client.delete(f"/testdesk/scenarios/{sid}").status_code, 404)
        self.assertEqual(self.client.post("/testdesk/run", json={"mode": "test", "scenario_id": sid}).status_code, 404)

        self.user = SimpleNamespace(id="admin", email="root@x", role="admin")
        self.assertEqual(len(self.client.get("/testdesk/scenarios").json()["scenarios"]), 1)
        self.assertEqual(self.client.get(f"/testdesk/results/{rid}").status_code, 200)

    def test_concurrency_limit_is_409(self):
        service._active = service.MAX_CONCURRENT_TEST_RUNS
        try:
            service.start_run = self._orig_start  # use the real one: it raises before running anything
            r = self.client.post("/testdesk/run", json={"mode": "test", "scenario": self.body})
            self.assertEqual(r.status_code, 409)
        finally:
            service._active = 0


if __name__ == "__main__":
    unittest.main()
