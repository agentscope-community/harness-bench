"""Offline tests for the evaluation console (no Docker, no model calls)."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from hbench_console.api import create_app
from hbench_console.config import REPO_ROOT, Settings
from hbench_console.demo import build
from hbench_console.db import Database
from hbench_console.diagnosis import diagnose
from hbench_console.ingest import Ingestor
from hbench_console.runner import Runner, RunnerError

FAKE = r'''
import json, os, sys, time
from pathlib import Path

args = sys.argv[1:]
command = args[0]
if command == "matrix":
    out = Path(args[args.index("--output") + 1])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("job_name: fake\n")
    out.with_suffix(".plan.json").write_text(json.dumps({
        "planned_trials": 4,
        "combinations": [{"agent": "codex", "model": "fake", "dataset": "fake"}],
        "backend": "docker"}))
    print(json.dumps({"planned_trials": 4, "executed": False}))
elif command == "run":
    time.sleep(float(os.environ.get("FAKE_RUN_DELAY", "0")))
    print("fake run completed")
elif command == "summary":
    out = Path(args[args.index("--output") + 1])
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps({
        "recorded_trials": 0, "planned_trials": 4, "missing_trials": 4,
        "pass_rate_planned": 0.0, "groups": [], "trials": [], "note": "fake"}))
    print(json.dumps({"recorded_trials": 0}))
elif command == "archive":
    print("fake archive")
else:
    raise SystemExit(f"unexpected command: {command}")
'''


def wait_for(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError("condition not reached before timeout")


class ConsoleFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="console-test-"))
        self.fake = self.tmp / "fake_hbench.py"
        self.fake.write_text(FAKE, encoding="utf-8")
        self.settings = self.make_settings(allow_real_runs=False)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_settings(self, *, allow_real_runs: bool, real_hbench: bool = False) -> Settings:
        return Settings(
            repo_root=REPO_ROOT,
            data_dir=self.tmp / "data",
            jobs_root=self.tmp / "jobs",
            reports_root=self.tmp / "reports",
            plans_root=self.tmp / "plans",
            allow_real_runs=allow_real_runs,
            hbench_cmd=((sys.executable, "-m", "hbench") if real_hbench
                        else (sys.executable, str(self.fake))),
            viewer_url=None, host="127.0.0.1", port=8090, sync_on_start=False,
        )

    def client(self, settings=None, start_worker=False):
        app = create_app(settings or self.settings, start_worker=start_worker)
        return TestClient(app), app

    def demo_job(self, settings=None, tasks=2) -> str:
        settings = settings or self.make_settings(allow_real_runs=False, real_hbench=True)
        name = "demo-fixture-test-" + str(time.time_ns())
        build(settings.jobs_root / name, tasks, seed=7)
        return name


class DiagnosisTests(unittest.TestCase):
    def test_zero_tokens_without_exception_is_flagged(self):
        self.assertEqual(diagnose({"input_tokens": 0, "output_tokens": 0, "reward": 0.0})["error_label"],
                         "疑似首次调用失败")
        self.assertFalse(diagnose({"input_tokens": 0, "output_tokens": 0, "reward": 1.0}))

    def test_known_failures_map_to_actionable_labels(self):
        cases = [
            ({"error": "ApiRateLimitError", "error_message": "429 Too Many Requests"}, "模型限流（429）"),
            ({"error": "NonZeroAgentExitCodeError", "error_message": "sed: cannot overwrite existing file"},
             "私有配置写入冲突"),
            ({"error": "NonZeroAgentExitCodeError", "error_message": "HTTP 400 developer role unsupported"},
             "请求被端点拒绝（400）"),
            ({"error": "CancelledError", "error_message": "task group cancelled"}, "被连带取消"),
            ({"error": "AgentTimeoutError", "error_message": "timed out"}, "运行超时"),
        ]
        for trial, label in cases:
            with self.subTest(label=label):
                diagnosis = diagnose(trial)
                self.assertEqual(diagnosis["error_label"], label)
                self.assertTrue(diagnosis["error_hint"])


class IngestTests(ConsoleFixture):
    def test_fixture_job_is_indexed_and_served(self):
        settings = self.make_settings(allow_real_runs=False, real_hbench=True)
        name = self.demo_job(settings)
        ingestor = Ingestor(settings, Database(settings.database))
        Database(settings.database).initialize()
        results = ingestor.sync()
        self.assertEqual(results[0]["status"], "indexed")
        client, _ = self.client(settings)
        job = client.get(f"/api/jobs/{name}").json()
        self.assertTrue(job["fixture"])
        self.assertEqual(len(job["combos"]), 18)
        self.assertGreater(job["recorded_trials"], 0)
        self.assertGreater(job["combos"][0]["missing"], 0)
        listing = client.get("/api/jobs").json()["jobs"][0]
        self.assertEqual(listing["combos"], 18)
        trials = client.get(f"/api/jobs/{name}/trials?outcome=error&limit=5").json()
        self.assertTrue(all(t["error"] for t in trials["trials"]))
        self.assertTrue(all(t["error_label"] == "运行超时" for t in trials["trials"]))
        missing = client.get(f"/api/jobs/{name}/missing").json()
        self.assertGreater(missing["total"], 0)
        self.assertEqual(sum(row["missing"] for row in missing["missing"]), missing["total"])
        self.assertTrue(all(row["missing"] >= 1 for row in missing["missing"]))
        tasks = client.get(f"/api/jobs/{name}/tasks").json()
        self.assertEqual(len(tasks["combos"]), 6)
        self.assertEqual(len(tasks["tasks"]), 6)
        first = tasks["tasks"][0]
        self.assertEqual(len(first["cells"]), 6)
        # tasks_per_dataset=2 skips model A for the first task, so only 3 records remain.
        self.assertEqual(sum(cell["recorded"] for cell in first["cells"]), 3)
        self.assertIn(name, client.get(f"/api/compare?jobs={name}").json()["jobs"][0]["job"])

    def test_evidence_is_confined_and_filters_secrets(self):
        settings = self.make_settings(allow_real_runs=False, real_hbench=True)
        name = self.demo_job(settings)
        ingestor = Ingestor(settings, Database(settings.database))
        Database(settings.database).initialize()
        ingestor.sync()
        client, _ = self.client(settings)
        (settings.jobs_root / name / ".env").write_text("SECRET=1\n")
        self.assertEqual(client.get(f"/api/evidence/{name}/.env").status_code, 403)
        self.assertEqual(client.get(f"/api/evidence/{name}/../outside").status_code, 404)
        self.assertEqual(client.get(f"/api/evidence/{name}/config.json").status_code, 200)
        trial = client.get(f"/api/jobs/{name}/trials?limit=1").json()["trials"][0]
        labels = {link["label"] for link in trial["evidence_links"]}
        self.assertEqual(labels, {"result.json", "trial.log", "agent/native.jsonl"})
        evidence = client.get(f"/api/evidence/{name}/{trial['evidence']}")
        self.assertEqual(evidence.status_code, 200)
        self.assertIn("fixture", evidence.text)
        self.assertEqual(client.get(f"/api/evidence/{name}/{trial['trial']}/trial.log").status_code, 200)
        self.assertEqual(client.get(f"/api/evidence/{name}/{trial['trial']}/agent/native.jsonl").status_code, 200)

    def test_stale_index_is_pruned_when_job_disappears(self):
        settings = self.make_settings(allow_real_runs=False, real_hbench=True)
        name = self.demo_job(settings)
        database = Database(settings.database)
        database.initialize()
        ingestor = Ingestor(settings, database)
        ingestor.sync()
        import shutil
        shutil.rmtree(settings.jobs_root / name)
        ingestor.sync()
        self.assertEqual(database.list_jobs(), [])


class RunTests(ConsoleFixture):
    def test_real_run_is_refused_without_flag(self):
        runner = Runner(self.settings, Database(self.settings.database), None)
        Database(self.settings.database).initialize()
        with self.assertRaises(RunnerError):
            runner.submit("run-real", "name: x\n", dry_run=False)
        client, _ = self.client()
        response = client.post("/api/runs", json={
            "matrix": "name: x\nbenchmarks: [a]\nmodels: [b]\nharnesses: [c]\n",
            "name": "run-real", "dry_run": False})
        self.assertEqual(response.status_code, 403)

    def test_dry_run_reaches_succeeded(self):
        database = Database(self.settings.database)
        database.initialize()
        runner = Runner(self.settings, database, Ingestor(self.settings, database))
        runner.start()
        try:
            runner.submit("run-ok", "name: x\n", dry_run=True)
            run = wait_for(lambda: (database.get_run("run-ok")
                                    if database.get_run("run-ok")["status"] == "succeeded" else None))
        finally:
            runner.stop()
        self.assertEqual(run["planned_trials"], 4)
        self.assertEqual(run["exit_code"], 0)
        log = Path(run["log_path"]).read_text()
        self.assertIn("dry-run succeeded", log)
        self.assertIn("matrix", log)

    def test_queued_cancel(self):
        database = Database(self.settings.database)
        database.initialize()
        runner = Runner(self.settings, database, None)
        runner.submit("run-queued", "name: x\n", dry_run=True)
        runner.cancel("run-queued")
        self.assertEqual(database.get_run("run-queued")["status"], "cancelled")

    def test_running_cancel_terminates_process(self):
        database = Database(self.settings.database)
        database.initialize()
        runner = Runner(self.settings, database, Ingestor(self.settings, database))
        with mock.patch.dict(os.environ, {"FAKE_RUN_DELAY": "30"}):
            runner.start()
            try:
                runner.submit("run-cancel", "name: x\n", dry_run=True)
                wait_for(lambda: database.get_run("run-cancel")["status"] == "running", timeout=15)
                runner.cancel("run-cancel")
                run = wait_for(lambda: (database.get_run("run-cancel")
                                        if database.get_run("run-cancel")["status"] == "cancelled" else None))
            finally:
                runner.stop()
        self.assertEqual(run["status"], "cancelled")

    def test_interrupted_runs_are_marked_on_start(self):
        database = Database(self.settings.database)
        database.initialize()
        database.create_run("run-old", "name: x\n", True)
        database.update_run("run-old", status="running")
        runner = Runner(self.settings, database, None)
        runner.start()
        runner.stop()
        run = database.get_run("run-old")
        self.assertEqual(run["status"], "failed")
        self.assertIn("harbor jobs resume", run["error"])


class ApiTests(ConsoleFixture):
    def test_preview_and_overrides(self):
        client, _ = self.client()
        response = client.post("/api/runs/preview", json={
            "matrix": "name: m\nbenchmarks: [terminal-bench]\nmodels: [bailian-model-a]\n"
                      "harnesses: [codex]\n",
            "overrides": {"n_tasks": 2, "tasks": "a,b", "cluster": "docker"}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["planned_trials"], 4)
        self.assertIn("cluster: docker", response.json()["matrix"])
        bad = client.post("/api/runs/preview", json={"matrix": "name: m\n", "overrides": {"bogus": 1}})
        self.assertEqual(bad.status_code, 422)

    def test_duplicate_run_name_rejected(self):
        client, app = self.client()
        runner = app.state.console.runner
        runner.submit("run-dup", "name: x\n", dry_run=True)
        response = client.post("/api/runs", json={"matrix": "name: x\n", "name": "run-dup", "dry_run": True})
        self.assertEqual(response.status_code, 400)

    def test_invalid_job_and_evidence(self):
        client, _ = self.client()
        self.assertEqual(client.get("/api/jobs/missing").status_code, 404)
        self.assertEqual(client.get("/api/evidence/missing/x").status_code, 404)


if __name__ == "__main__":
    unittest.main()
