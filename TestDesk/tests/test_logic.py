"""Pure-logic tests: scenario validation, judging, and the results store."""

import os
import tempfile
import unittest

from TestDesk import service, store

JOBS, AGENTS = {"qa_job", "other"}, {"planner", "qa_lead"}


def norm(**kw):
    raw = {"task": "check login", "target_type": "job", "target_id": "qa_job", **kw}
    return service.normalize_scenario(raw, known_jobs=JOBS, known_agents=AGENTS)


class NormalizeTests(unittest.TestCase):
    def test_defaults_and_auto_name(self):
        s = norm()
        self.assertEqual(s["name"], "check login")
        self.assertEqual(s["expect_status"], "done")
        self.assertTrue(s["options"]["block_external"])
        self.assertTrue(s["options"]["auto_approve"])
        self.assertFalse(s["options"]["keep_sandbox"])

    def test_empty_task_rejected(self):
        with self.assertRaises(service.ScenarioError):
            norm(task="   ")

    def test_unknown_targets_rejected(self):
        with self.assertRaises(service.ScenarioError):
            norm(target_id="nope")
        with self.assertRaises(service.ScenarioError):
            norm(target_type="agent", target_id="qa_job")  # a job name is not an agent
        with self.assertRaises(service.ScenarioError):
            norm(target_type="spaceship")

    def test_agent_target_ok(self):
        self.assertEqual(norm(target_type="agent", target_id="planner")["target_type"], "agent")

    def test_expectations_from_text_are_split_and_cleaned(self):
        self.assertEqual(norm(expectations="login\n\n  refund  \n")["expectations"], ["login", "refund"])

    def test_timeout_clamped_and_validated(self):
        self.assertEqual(norm(options={"timeout_seconds": 1})["options"]["timeout_seconds"], 30)
        self.assertEqual(norm(options={"timeout_seconds": 99999})["options"]["timeout_seconds"], 3600)
        with self.assertRaises(service.ScenarioError):
            norm(options={"timeout_seconds": "soon"})

    def test_unknown_options_ignored(self):
        self.assertNotIn("rm_rf", norm(options={"rm_rf": True})["options"])

    def test_bad_expect_status(self):
        with self.assertRaises(service.ScenarioError):
            norm(expect_status="maybe")

    def test_draft_yaml_sets_the_target(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("pyyaml not installed")
        s = norm(target_id="", job_yaml="id: brand_new\nname: X\nagents: [planner]\n")
        self.assertEqual((s["target_type"], s["target_id"]), ("job", "brand_new"))

    def test_draft_yaml_without_id_rejected(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("pyyaml not installed")
        with self.assertRaises(service.ScenarioError):
            norm(job_yaml="name: no id here\n")


class EvaluateTests(unittest.TestCase):
    sc = {"expect_status": "done", "expectations": ["Login", "refund"]}

    def test_pass(self):
        v, checks = service.evaluate(self.sc, {"ok": True, "run_status": "done", "final_answer": "login flow ok; REFUND covered"})
        self.assertEqual(v, "pass")
        self.assertTrue(all(c["ok"] for c in checks))

    def test_missing_phrase_fails(self):
        v, checks = service.evaluate(self.sc, {"ok": True, "run_status": "done", "final_answer": "login only"})
        self.assertEqual(v, "fail")
        self.assertEqual([c["ok"] for c in checks], [True, True, False])

    def test_job_error_is_a_fail_not_an_error(self):
        v, _ = service.evaluate(self.sc, {"ok": True, "run_status": "error", "error": "boom", "final_answer": None})
        self.assertEqual(v, "fail")

    def test_expecting_an_error_can_pass(self):
        v, _ = service.evaluate({"expect_status": "error", "expectations": []}, {"ok": True, "run_status": "error"})
        self.assertEqual(v, "pass")

    def test_awaiting_human(self):
        v, checks = service.evaluate(self.sc, {"ok": True, "run_status": "awaiting_human", "paused_agent": "qa_lead"})
        self.assertEqual(v, "awaiting_human")
        self.assertIn("qa_lead", checks[0]["detail"])

    def test_harness_failure_is_error(self):
        v, checks = service.evaluate(self.sc, {"ok": False, "error": "timed out"})
        self.assertEqual(v, "error")
        self.assertIn("timed out", checks[0]["detail"])

    def test_no_answer_reported(self):
        v, checks = service.evaluate({"expect_status": "done", "expectations": ["x"]}, {"ok": True, "run_status": "done", "final_answer": None})
        self.assertEqual(v, "fail")
        self.assertIn("no final answer", checks[-1]["detail"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["TESTDESK_DB"] = os.path.join(self._tmp.name, "td.sqlite")

    def tearDown(self):
        os.environ.pop("TESTDESK_DB", None)
        self._tmp.cleanup()

    def test_scenario_round_trip_and_update(self):
        sid = store.create_scenario(name="n", target_type="job", target_id="qa_job", task="t", expectations=["a", "b"],
                                    expect_status="done", job_yaml="", options={"keep_sandbox": True}, owner_id="u1")
        row = store.get_scenario(sid)
        self.assertEqual(row["expectations"], ["a", "b"])
        self.assertEqual(row["options"], {"keep_sandbox": True})
        store.update_scenario(sid, task="t2", expectations=["z"])
        self.assertEqual(store.get_scenario(sid)["task"], "t2")
        self.assertEqual(store.get_scenario(sid)["expectations"], ["z"])
        self.assertEqual(len(store.list_scenarios("u1")), 1)
        self.assertEqual(store.list_scenarios("someone-else"), [])
        store.delete_scenario(sid)
        self.assertIsNone(store.get_scenario(sid))

    def test_result_lifecycle_and_orphan_cleanup(self):
        rid = store.create_result(scenario={"id": None, "name": "adhoc"}, mode="test", owner_id="u1")
        self.assertEqual(store.get_result(rid)["state"], "running")
        self.assertEqual(store.mark_orphaned_results_as_error(), 1)
        row = store.get_result(rid)
        self.assertEqual((row["state"], row["verdict"]), ("finished", "error"))

        rid2 = store.create_result(scenario={"id": "s", "name": "x"}, mode="prod", owner_id="u1")
        store.finish_result(rid2, verdict="pass", checks=[{"name": "c", "ok": True, "detail": ""}], events=[{"a": 1}])
        got = store.get_result(rid2)
        self.assertEqual(got["checks"][0]["ok"], True)
        self.assertEqual(got["events"], [{"a": 1}])
        summaries = store.list_results("u1")
        self.assertEqual(len(summaries), 2)
        self.assertNotIn("events", summaries[0])  # history rows are summaries only


if __name__ == "__main__":
    unittest.main()
