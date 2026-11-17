"""Offline tests for the evaluation console (no Docker, no model calls)."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from hbench_console import cleanup
from hbench_console.api import create_app
from hbench_console.config import REPO_ROOT, Settings
from hbench_console.demo import build
from hbench_console.db import Database, SCHEMA
from hbench_console.diagnosis import diagnose
from hbench_console.ingest import Ingestor, _evidence_links
from hbench_console.runner import Runner, RunnerError
from hbench.summary import aggregate

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
            sync_interval_seconds=0,
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

    def test_unknown_usage_does_not_imply_first_call_failure(self):
        for usage in ({}, {"input_tokens": None, "output_tokens": 0},
                      {"input_tokens": 0, "output_tokens": None}):
            diagnosis = diagnose({**usage, "reward": 0})
            self.assertNotIn("error_label", diagnosis)
            self.assertIn("usage_hint", diagnosis)

    def test_native_and_tool_failures_are_observations_not_outcomes(self):
        trial = {"reward": 1, "error": None, "input_tokens": 10, "output_tokens": 2,
                 "diagnostics": {"steps": [{"native_status": "failed"}],
                 "tool_failures": {"total": 1, "by_kind": {"command_failed": 1}}}}
        before = json.dumps(trial)
        diagnosis = diagnose(trial)
        self.assertNotIn("error_label", diagnosis)
        self.assertIn("native_label", diagnosis)
        self.assertIn("不等于题目失败", diagnosis["tool_failure_label"])
        self.assertIn("退出码", diagnosis["tool_failure_hint"])
        self.assertEqual(json.dumps(trial), before)

    def test_malformed_nested_diagnostics_do_not_break_console(self):
        for diagnostics in ([], "invalid", {"steps": [None, [], "bad"], "tool_failures": []},
                            {"steps": "bad", "tool_failures": {"total": "1"}},
                            {"tool_failures": {"total": 1, "by_kind": []}}):
            with self.subTest(diagnostics=diagnostics):
                result = diagnose({"diagnostics": diagnostics, "reward": 1})
                self.assertNotIn("error_label", result)

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
    def test_legacy_database_migration_is_additive_and_idempotent(self):
        database = Database(self.tmp / "legacy.sqlite")
        legacy = SCHEMA
        for line in ("    error_message TEXT,\n", "    evidence_links TEXT,\n", "    diagnostics TEXT,\n",
                     "    input_tokens_missing INTEGER,\n", "    output_tokens_missing INTEGER,\n"):
            legacy = legacy.replace(line, "")
        with database.connect() as connection:
            connection.executescript(legacy)
            connection.execute("INSERT INTO jobs (name, path, origin, updated_at) VALUES ('old', '/old', 'import', 'old')")
            connection.execute("INSERT INTO trials (job, trial, reward, input_tokens, output_tokens) VALUES ('old', 't', 1, 12, 0)")
            connection.execute("INSERT INTO combos (job, agent, model, dataset, recorded, errors, scored, passed, unscored, input_tokens, output_tokens) VALUES ('old', 'pi', 'm', 'd', 1, 0, 1, 1, 0, 12, 0)")
        database.create_run("saved-run", "saved-yaml", True)
        database.initialize()
        database.initialize()
        self.assertEqual(database.get_run("saved-run")["matrix_yaml"], "saved-yaml")
        total, trials = database.query_trials("old", outcome="passed")
        self.assertEqual(total, 1)
        self.assertEqual(trials[0]["reward"], 1)
        self.assertIsNone(trials[0]["input_tokens"])
        self.assertIsNone(trials[0]["output_tokens"])
        self.assertIsNone(database.get_job("old")["combos"][0]["input_tokens"])
        with database.connect() as connection:
            row = connection.execute("SELECT input_tokens, output_tokens FROM trials WHERE job = 'old'").fetchone()
            self.assertEqual(tuple(row), (12, 0))
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(trials)")}
            self.assertTrue({"diagnostics", "error_message", "evidence_links"}.issubset(columns))

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
        self.assertEqual(labels, {"result.json", "trial.log", "agent/native.jsonl", "agent/trajectory.json"})
        evidence = client.get(f"/api/evidence/{name}/{trial['evidence']}")
        self.assertEqual(evidence.status_code, 200)
        self.assertIn("fixture", evidence.text)
        self.assertEqual(client.get(f"/api/evidence/{name}/{trial['trial']}/trial.log").status_code, 200)
        self.assertEqual(client.get(f"/api/evidence/{name}/{trial['trial']}/agent/native.jsonl").status_code, 200)

    def test_public_evidence_redacts_copies_and_preserves_numeric_usage(self):
        settings = self.make_settings(allow_real_runs=False, real_hbench=True)
        name = self.demo_job(settings)
        folder = settings.jobs_root / name
        path = next(folder.glob("*/result.json"))
        raw = json.loads(path.read_text())
        escaped = 'private"quoted\ncredential'
        raw["config"]["agent"]["kwargs"] = {
            "extra_body": {"opaque": escaped},
            "extra_env": {"FLAG": "1", "MODEL_API_KEY": "private-api-key"},
        }
        raw["copied_text"] = escaped
        raw["agent_result"] = {"n_input_tokens": 123, "n_output_tokens": 0}
        path.write_text(json.dumps(raw))
        log = path.parent / "trial.log"
        log.write_text("Authorization: Basic YWJjZA==\nBearer private-bearer\nprivate-api-key\n")
        native = path.parent / "agent" / "native.jsonl"
        native.write_text("\n".join((json.dumps({"text": escaped, "inputTokens": 123}),
                                     json.dumps({"text": "late-private-secret"}),
                                     json.dumps({"extra_body": {"opaque": "late-private-secret"}}),
                                     '{"extra_body":{"opaque":"truncated-private-secret"')) + "\n")
        originals = {item: item.read_bytes() for item in (path, log, native)}
        database = Database(settings.database)
        database.initialize()
        Ingestor(settings, database).sync()
        client, _ = self.client(settings)
        result = client.get(f"/api/evidence/{name}/{path.relative_to(folder)}")
        self.assertEqual(result.status_code, 200)
        decoded = result.json()
        self.assertEqual(decoded["agent_result"], {"n_input_tokens": 123, "n_output_tokens": 0})
        self.assertEqual(decoded["copied_text"], "[REDACTED]")
        self.assertEqual(decoded["config"]["agent"]["kwargs"]["extra_body"], "[REDACTED]")
        for item in (log, native):
            response = client.get(f"/api/evidence/{name}/{item.relative_to(folder)}")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")
            for secret in (escaped, "YWJjZA==", "private-bearer", "private-api-key",
                           "late-private-secret", "truncated-private-secret"):
                self.assertNotIn(secret, response.text)
            if item == native:
                records = [json.loads(line) for line in response.text.splitlines()]
                self.assertEqual(records[0]["text"], "[REDACTED]")
                self.assertEqual(records[0]["inputTokens"], 123)
                self.assertEqual(records[1]["text"], "[REDACTED]")
                self.assertEqual(records[-1]["reason"], "malformed_jsonl")
        for item, original in originals.items():
            self.assertEqual(item.read_bytes(), original)

    def test_yaml_evidence_collects_secrets_before_redacting_earlier_copies(self):
        import yaml
        settings = self.make_settings(allow_real_runs=False, real_hbench=True)
        name = self.demo_job(settings)
        database = Database(settings.database)
        database.initialize()
        Ingestor(settings, database).sync()
        client, _ = self.client(settings)
        escaped = 'yaml"quoted\nprivate-credential'
        raw = {"copied_text": escaped, "input_tokens": 123, "extra_body": {"opaque": escaped}}
        path = settings.jobs_root / name / "evidence.yaml"
        path.write_text(yaml.safe_dump(raw, sort_keys=False))
        original = path.read_bytes()
        response = client.get(f"/api/evidence/{name}/evidence.yaml")
        self.assertEqual(response.status_code, 200)
        decoded = yaml.safe_load(response.text)
        self.assertEqual(decoded["copied_text"], "[REDACTED]")
        self.assertEqual(decoded["extra_body"], "[REDACTED]")
        self.assertEqual(decoded["input_tokens"], 123)
        self.assertEqual(path.read_bytes(), original)

    def test_unsafe_evidence_formats_are_not_returned_raw(self):
        settings = self.make_settings(allow_real_runs=False, real_hbench=True)
        name = self.demo_job(settings)
        database = Database(settings.database)
        database.initialize()
        Ingestor(settings, database).sync()
        client, _ = self.client(settings)
        folder = settings.jobs_root / name
        for filename, content in (("broken.json", '{"extra_body":"private'),
                                  ("unsafe.html", "<script>private</script>")):
            (folder / filename).write_text(content)
            response = client.get(f"/api/evidence/{name}/{filename}")
            self.assertEqual(response.status_code, 403)
            self.assertNotIn(content, response.text)

    def test_codex_and_pi_cli_outputs_are_linkable(self):
        settings = self.make_settings(allow_real_runs=False, real_hbench=True)
        name = self.demo_job(settings)
        database = Database(settings.database)
        database.initialize()
        ingestor = Ingestor(settings, database)
        ingestor.sync()
        client, _ = self.client(settings)
        for agent, directory in (("codex", "sessions"), ("pi", "pi/sessions")):
            with self.subTest(agent=agent):
                trial = client.get(f"/api/jobs/{name}/trials?agent={agent}&limit=1").json()["trials"][0]
                base = settings.jobs_root / name / trial["trial"] / "agent"
                (base / f"{agent}.txt").write_text(f"{agent} output\n", encoding="utf-8")
                sessions = base / directory
                sessions.mkdir(parents=True)
                (sessions / "native.jsonl").write_text("{}\n", encoding="utf-8")
                ingestor.sync()
                links = client.get(f"/api/jobs/{name}/trials?agent={agent}&limit=1").json()["trials"][0]["evidence_links"]
                self.assertIn({"label": f"agent/{agent}.txt", "path": f"{trial['trial']}/agent/{agent}.txt"}, links)
                self.assertIn({"label": f"agent/{directory}/native.jsonl",
                               "path": f"{trial['trial']}/agent/{directory}/native.jsonl"}, links)
                evidence = client.get(f"/api/evidence/{name}/{trial['trial']}/agent/{agent}.txt")
                self.assertEqual(evidence.text, f"{agent} output\n")
                self.assertEqual(client.get(f"/api/evidence/{name}/{trial['trial']}/agent/{directory}/native.jsonl").status_code, 200)

    def test_agentscope_diagnostics_reach_console_without_changing_outcome(self):
        settings = self.make_settings(allow_real_runs=False, real_hbench=True)
        name = self.demo_job(settings)
        folder = settings.jobs_root / name
        path = next(path for path in folder.glob("*/result.json")
                    if json.loads(path.read_text())["agent_info"]["name"] == "agentscope-java")
        raw = json.loads(path.read_text())
        raw["exception_info"] = None
        raw["verifier_result"] = {"rewards": {"reward": 1}}
        raw["agent_result"] = {"n_input_tokens": None, "n_output_tokens": 0, "metadata": {
            "native_status": "failed", "native_error_type": "Broken",
            "native_binding": {"max_tokens": 16384, "enable_thinking": False, "journal_enabled": True},
            "tool_failure_summary": {"total": 1, "by_kind": {"command_failed": 1}},
            "tool_failures": {"total": 1, "by_tool_kind": {"execute": {"command_failed": 1}}},
        }}
        raw["config"]["agent"]["kwargs"] = {"max_tokens": 16384, "extra_body": {"opaque": "private-body"}}
        path.write_text(json.dumps(raw))
        before = path.read_bytes()
        database = Database(settings.database)
        database.initialize()
        ingestor = Ingestor(settings, database)
        self.assertTrue(all(row["status"] == "indexed" for row in ingestor.sync()))
        client, _ = self.client(settings)
        response = client.get(f"/api/jobs/{name}/trials?outcome=passed&q={raw['trial_name']}")
        self.assertEqual(response.status_code, 200)
        trial = response.json()["trials"][0]
        self.assertEqual(trial["reward"], 1)
        self.assertIsNone(trial["error"])
        self.assertIsNone(trial["input_tokens"])
        self.assertEqual(trial["output_tokens"], 0)
        self.assertIn("usage_hint", trial)
        self.assertIn("native_label", trial)
        self.assertEqual(trial["diagnostics"]["tool_failures"]["total"], 1)
        self.assertEqual(trial["diagnostics"]["configured"], {"max_tokens": 16384})
        self.assertNotIn("private-body", response.text)
        self.assertEqual(client.get(f"/api/jobs/{name}/trials?outcome=error&q={raw['trial_name']}").json()["total"], 0)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(client.get(f"/api/evidence/{name}/{raw['trial_name']}/agent/trajectory.json").status_code, 200)

    def test_legacy_reports_refresh_once_even_without_new_result_mtimes(self):
        settings = self.make_settings(allow_real_runs=False, real_hbench=True)
        name = self.demo_job(settings)
        database = Database(settings.database)
        database.initialize()
        ingestor = Ingestor(settings, database)
        ingestor.sync()
        path = settings.reports_root / name / "summary.json"
        legacy = json.loads(path.read_text())
        legacy.pop("diagnostics_version")
        path.write_text(json.dumps(legacy))
        with mock.patch.object(ingestor, "_summarize", wraps=ingestor._summarize) as summarize:
            ingestor.sync()
            ingestor.sync()
        summarize.assert_called_once()
        self.assertIn("diagnostics_version", json.loads(path.read_text()))

    def test_native_session_links_are_bounded_and_confined(self):
        settings = self.make_settings(allow_real_runs=False, real_hbench=True)
        name = self.demo_job(settings)
        database = Database(settings.database)
        database.initialize()
        ingestor = Ingestor(settings, database)
        ingestor.sync()
        client, _ = self.client(settings)
        trial = client.get(f"/api/jobs/{name}/trials?agent=codex&limit=1").json()["trials"][0]
        sessions = settings.jobs_root / name / trial["trial"] / "agent" / "sessions"
        sessions.mkdir()
        for index in range(25):
            (sessions / f"rollout-{index}.jsonl").write_text("{}\n")
        outside = self.tmp / "outside.jsonl"
        outside.write_text("private-outside")
        (sessions / "escape.jsonl").symlink_to(outside)
        ingestor.sync()
        links = client.get(f"/api/jobs/{name}/trials?agent=codex&limit=1").json()["trials"][0]["evidence_links"]
        native = [item for item in links if "agent/sessions/" in item["path"]]
        self.assertLessEqual(len(native), 20)
        self.assertGreater(len(native), 0)
        self.assertFalse(any("escape" in item["path"] for item in native))
        self.assertEqual(client.get(f"/api/evidence/{name}/{native[0]['path']}").status_code, 200)
        self.assertEqual(client.get(f"/api/evidence/{name}/{trial['trial']}/agent/sessions/escape.jsonl").status_code, 404)

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

    def test_running_job_is_indexed_before_any_trial_finishes(self):
        settings = self.make_settings(allow_real_runs=False, real_hbench=True)
        name = "running-job"
        job_dir = settings.jobs_root / name
        job_dir.mkdir(parents=True)
        job_dir.joinpath("config.json").write_text(json.dumps({
            "job_name": name, "n_trials": 5646, "agents": [], "datasets": []
        }))
        database = Database(settings.database)
        database.initialize()
        ingestor = Ingestor(settings, database)
        result = next(r for r in ingestor.sync() if r["name"] == name)
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["recorded_trials"], 0)
        client, _ = self.client(settings)
        job = client.get(f"/api/jobs/{name}").json()
        self.assertEqual(job["status"], "running")
        self.assertEqual(job["recorded_trials"], 0)
        self.assertEqual(job["planned_trials"], 5646)

    def test_indexed_job_reports_status_indexed(self):
        settings = self.make_settings(allow_real_runs=False, real_hbench=True)
        name = self.demo_job(settings)
        database = Database(settings.database)
        database.initialize()
        Ingestor(settings, database).sync()
        client, _ = self.client(settings)
        self.assertEqual(client.get(f"/api/jobs/{name}").json()["status"], "indexed")

    def test_live_results_distinguish_scored_errors_running_and_queued(self):
        client, app = self.client()
        name = "live-results"
        job_dir = self.settings.jobs_root / name
        job_dir.mkdir(parents=True)
        agent = {"import_path": "hbench.agents:FrozenPi", "model_name": "openai/model"}
        (job_dir / "config.json").write_text(json.dumps({
            "agents": [agent], "datasets": [{"path": "/data/terminal-bench",
                "task_names": [f"task-{i}" for i in range(20)]}]}))
        (job_dir / "result.json").write_text(json.dumps({"finished_at": None,
            "stats": {"n_completed_trials": 19, "n_running_trials": 1}}))
        rows, trials = [], []
        for i in range(20):
            folder = job_dir / f"task-{i}__trial"
            folder.mkdir()
            (folder / "config.json").write_text(json.dumps({"agent": agent,
                "task": {"path": f"/data/terminal-bench/task-{i}", "source": "terminal-bench"}}))
            if i == 19:
                continue
            error = "ExampleError" if i >= 16 else None
            reward = None if error else int(i == 0)
            row = {"id": str(i), "agent_info": {"name": "pi", "model_info": {"name": "model"}},
                   "source": "terminal-bench", "verifier_result": {"rewards": {"reward": reward}},
                   "exception_info": {"exception_type": error} if error else None}
            rows.append(row)
            (folder / "result.json").write_text(json.dumps(row))
            trials.append({"trial": folder.name, "task": f"terminal-bench/task-{i}", "agent": "pi",
                           "model": "model", "dataset": "terminal-bench", "reward": reward, "error": error})
        summary = aggregate(rows, expected={("pi", "model", "terminal-bench"): 20})
        summary["trials"] = trials
        database = Database(self.settings.database)
        Ingestor(self.settings, database)._store(name, job_dir, summary)
        job = client.get(f"/api/jobs/{name}").json()
        self.assertEqual(job["status"], "running")
        self.assertEqual(job["counts"], {"scored": 16, "passed": 1, "failed": 15,
            "errors": 3, "unscored": 0, "running": 1, "queued": 0, "missing": 0})
        self.assertEqual(job["combos"][0]["mean_reward_scored"], 1 / 16)
        self.assertEqual(client.get(f"/api/jobs/{name}/missing").json()["total"], 0)
        self.assertEqual(client.get("/api/jobs").json()["jobs"][0]["status"], "running")
        compared = client.get(f"/api/compare?jobs={name}").json()["jobs"][0]["combos"][0]
        self.assertEqual((compared["running"], compared["missing"]), (1, 0))

        # Not yet started is queued, not running or missing.
        shutil.rmtree(job_dir / "task-19__trial")
        counts = client.get(f"/api/jobs/{name}").json()["counts"]
        self.assertEqual((counts["running"], counts["queued"], counts["missing"]), (0, 1, 0))

        # A terminal run overrides an unfinished Harbor snapshot.
        database.create_run(name, "name: live-results", False)
        database.update_run(name, status="cancelled")
        job = client.get(f"/api/jobs/{name}").json()
        self.assertEqual(job["status"], "indexed")
        self.assertEqual((job["counts"]["running"], job["counts"]["queued"], job["counts"]["missing"]), (0, 0, 1))
        missing = client.get(f"/api/jobs/{name}/missing").json()
        self.assertEqual(missing["total"], 1)
        self.assertEqual(missing["missing"][0]["task"], "task-19")

    def test_initial_plan_has_running_and_queued_counts_per_combination(self):
        client, _ = self.client()
        job_dir = self.settings.jobs_root / "initial"
        job_dir.mkdir(parents=True)
        agents = [{"name": "pi", "model_name": model} for model in ("a", "b")]
        (job_dir / "config.json").write_text(json.dumps({"agents": agents,
            "datasets": [{"path": "/data/bench", "task_names": ["task"],}], "n_attempts": 60}))
        for i in range(60):
            folder = job_dir / f"task__{i}"
            folder.mkdir()
            (folder / "config.json").write_text(json.dumps({"agent": agents[0],
                "task": {"path": "/data/bench/task", "source": "bench"}}))
        database = Database(self.settings.database)
        Ingestor(self.settings, database)._ingest(job_dir)
        job = client.get("/api/jobs/initial").json()
        self.assertEqual(job["planned_trials"], 120)
        self.assertEqual([(g["running"], g["queued"], g["missing"]) for g in job["combos"]],
                         [(60, 0, 0), (0, 60, 0)])
        self.assertEqual(len(job["progress"]["active"]), 50)
        # Stale trial directories must not remain running after completion.
        (job_dir / "result.json").write_text(json.dumps({"finished_at": "2026-11-17T08:00:00Z"}))
        job = client.get("/api/jobs/initial").json()
        self.assertEqual((job["counts"]["running"], job["counts"]["missing"]), (0, 120))


class MultiStepEvidenceTests(ConsoleFixture):
    def test_recorded_steps_and_sessions_are_indexed_and_served(self):
        settings = self.make_settings(allow_real_runs=False, real_hbench=True)
        name = self.demo_job(settings)
        database = Database(settings.database)
        database.initialize()
        ingestor = Ingestor(settings, database)
        self.assertTrue(all(item["status"] == "indexed" for item in ingestor.sync()))
        client, _ = self.client(settings)
        trial = client.get(f"/api/jobs/{name}/trials?agent=codex&limit=1").json()["trials"][0]
        folder = settings.jobs_root / name
        trial_dir = folder / trial["trial"]
        top_links = _evidence_links(folder, trial["trial"])
        result_path = trial_dir / "result.json"
        raw = json.loads(result_path.read_text())
        raw["step_results"] = [{"step_name": "prepare"}, {"step_name": "verify"},
                               {"step_name": "prepare"}]
        result_path.write_text(json.dumps(raw))
        before = result_path.read_bytes()
        for step in ("prepare", "verify", "unrecorded"):
            agent = trial_dir / "steps" / step / "agent"
            agent.mkdir(parents=True)
            for filename in ("native.jsonl", "codex.txt", "pi.txt", "trajectory.json"):
                (agent / filename).write_text("{}\n", encoding="utf-8")
            for directory in ("sessions", "pi/sessions"):
                sessions = agent / directory / "nested"
                sessions.mkdir(parents=True)
                for index in range(25):
                    (sessions / f"session-{index}.jsonl").write_text("{}\n", encoding="utf-8")
                (sessions / "ignore.txt").write_text("not a session")
        summary = json.loads((settings.reports_root / name / "summary.json").read_text())
        row = next(item for item in summary["trials"] if item["trial"] == trial["trial"])
        # Raw names take precedence over stale summary diagnostics.
        row["diagnostics"] = {"steps": [{"step_name": "unrecorded"}, {"step": 1}]}
        ingestor._store(name, folder, summary)
        links = client.get(f"/api/jobs/{name}/trials?agent=codex&limit=1").json()["trials"][0]["evidence_links"]
        self.assertTrue(all(link in links for link in top_links))
        self.assertFalse(any("unrecorded" in link["path"] for link in links))
        self.assertEqual(len(links), len({link["path"] for link in links}))
        for step in ("prepare", "verify"):
            prefix = f"steps/{step}/agent"
            for filename in ("native.jsonl", "codex.txt", "pi.txt", "trajectory.json"):
                self.assertIn({"label": f"{prefix}/{filename}",
                               "path": f"{trial['trial']}/{prefix}/{filename}"}, links)
            for directory in ("sessions", "pi/sessions"):
                sessions = [link for link in links if link["label"].startswith(f"{prefix}/{directory}/")]
                self.assertEqual(len(sessions), 20)
        for link in links:
            self.assertEqual(client.get(f"/api/evidence/{name}/{link['path']}").status_code, 200)
        self.assertEqual(result_path.read_bytes(), before)

    def test_embedded_result_only_uses_current_trial_steps(self):
        name = "embedded-job"
        folder = self.settings.jobs_root / name
        trial_dir = folder / "embedded"
        for step in ("recorded", "other-trial", "summary-only"):
            agent = trial_dir / "steps" / step / "agent"
            agent.mkdir(parents=True)
            (agent / "native.jsonl").write_text("{}\n")
        raw = {"trial_results": [
            {"trial_name": "other", "step_results": [{"step_name": "other-trial"}]},
            {"trial_name": "embedded", "step_results": [{"step_name": "recorded"}]},
        ]}
        result_path = folder / "result.json"
        result_path.write_text(json.dumps(raw))
        before = result_path.read_bytes()
        database = Database(self.settings.database)
        database.initialize()
        summary = {"trials": [{"trial": "embedded", "diagnostics": {
            "steps": [{"step_name": "summary-only"}]}}]}
        Ingestor(self.settings, database)._store(name, folder, summary)
        client, _ = self.client()
        links = client.get(f"/api/jobs/{name}/trials").json()["trials"][0]["evidence_links"]
        self.assertEqual(links, [
            {"label": "result.json (job)", "path": "result.json"},
            {"label": "steps/recorded/agent/native.jsonl",
             "path": "embedded/steps/recorded/agent/native.jsonl"},
        ])
        for link in links:
            self.assertEqual(client.get(f"/api/evidence/{name}/{link['path']}").status_code, 200)
        self.assertEqual(result_path.read_bytes(), before)

    def test_summary_names_are_fallback_not_inferred_step_indices(self):
        folder = self.settings.jobs_root / "fallback-job"
        trial_dir = folder / "trial"
        for step in ("named", "1", "unrecorded"):
            agent = trial_dir / "steps" / step / "agent"
            agent.mkdir(parents=True)
            (agent / "native.jsonl").write_text("{}\n")
        database = Database(self.settings.database)
        database.initialize()
        ingestor = Ingestor(self.settings, database)
        result_path = trial_dir / "result.json"
        cases = [
            (b"invalid json", [{"step_name": "named"}, {"step": 1}], True),
            (b"\xff", [{"step_name": "named"}], True),
            (b"{}", [{"step_name": "named"}], True),
            (b'{"step_results": []}', [{"step_name": "named"}], False),
            (b'{"step_results": [{"step": 1}]}', [{"step_name": "named"}], False),
            (b"{}", [{"step": 1}, None, [], {"step_name": "../unrecorded"}], False),
            (b"{}", "malformed", False),
        ]
        for payload, steps, expected in cases:
            with self.subTest(payload=payload, steps=steps):
                result_path.write_bytes(payload)
                summary = {"trials": [{"trial": "trial", "diagnostics": {"steps": steps}}]}
                ingestor._store(folder.name, folder, summary)
                _, trials = database.query_trials(folder.name)
                links = trials[0]["evidence_links"]
                native = [link for link in links if "/steps/" in link["path"]]
                self.assertEqual(native, [{"label": "steps/named/agent/native.jsonl",
                                          "path": "trial/steps/named/agent/native.jsonl"}] if expected else [])
                self.assertEqual(result_path.read_bytes(), payload)

    def test_malicious_names_and_symlink_escapes_are_not_linked(self):
        folder = self.settings.jobs_root / "confined-job"
        trial_dir = folder / "trial"
        other = folder / "other" / "agent"
        outside = self.tmp / "outside" / "agent"
        agent = trial_dir / "steps" / "safe" / "agent"
        for base in (agent, other, outside, trial_dir / "agent"):
            (base / "sessions").mkdir(parents=True)
            (base / "native.jsonl").write_text("{}\n")
            (base / "sessions" / "native.jsonl").write_text("{}\n")
        (trial_dir / "result.json").write_text("{}")
        for step, target in (("cross-trial", other.parent), ("outside", outside.parent)):
            (trial_dir / "steps" / step).symlink_to(target, target_is_directory=True)
        loop = trial_dir / "steps" / "loop"
        loop.symlink_to(loop, target_is_directory=True)
        (agent / "codex.txt").symlink_to(other / "native.jsonl")
        (agent / "trajectory.json").symlink_to(outside / "native.jsonl")
        (agent / "pi").mkdir()
        (agent / "pi" / "sessions").symlink_to(other / "sessions", target_is_directory=True)
        for label, target in (("cross-trial", other), ("outside", outside)):
            (agent / "sessions" / f"{label}.jsonl").symlink_to(target / "native.jsonl")
        session_loop = agent / "sessions" / "loop.jsonl"
        session_loop.symlink_to(session_loop)
        for step in ("unrecorded", "bad..name"):
            hidden = trial_dir / "steps" / step / "agent"
            hidden.mkdir(parents=True)
            (hidden / "native.jsonl").write_text("must not be linked")
        names = ["safe", "cross-trial", "outside", "loop", "", " ", ".", "..", "../other",
                 "/outside", "bad/name", "bad\\name", "bad..name", "nul\x00name", None, 7, []]
        links = _evidence_links(folder, "trial", names)
        self.assertEqual({link["label"] for link in links}, {
            "result.json", "agent/native.jsonl", "agent/sessions/native.jsonl",
            "steps/safe/agent/native.jsonl", "steps/safe/agent/sessions/native.jsonl",
        })
        alias = folder / "alias"
        alias.symlink_to(other.parent, target_is_directory=True)
        for trial in ("alias", "../other", "", ".", "/outside"):
            self.assertEqual(_evidence_links(folder, trial, ["safe"]), [])
        unsafe = folder / "unsafe"
        unsafe.mkdir()
        (unsafe / "agent").symlink_to(other, target_is_directory=True)
        (unsafe / "result.json").symlink_to(outside / "native.jsonl")
        (folder / "result.json").symlink_to(outside / "native.jsonl")
        self.assertEqual(_evidence_links(folder, "unsafe"), [])

    def test_session_scan_errors_do_not_block_indexing_or_later_steps(self):
        folder = self.settings.jobs_root / "scan-error-job"
        trial_dir = folder / "trial"
        for step in ("first", "second"):
            agent = trial_dir / "steps" / step / "agent"
            (agent / "sessions").mkdir(parents=True)
            (agent / "native.jsonl").write_text("{}\n")
            (agent / "sessions" / "native.jsonl").write_text("{}\n")
        (trial_dir / "result.json").write_text(json.dumps({"step_results": [
            {"step_name": "first"}, {"step_name": "second"}]}))
        database = Database(self.settings.database)
        database.initialize()
        rglob = Path.rglob
        broken_sessions = (trial_dir / "steps" / "first" / "agent" / "sessions").resolve()

        def fail_first(base, pattern):
            if base == broken_sessions:
                raise PermissionError("offline simulated scan failure")
            return rglob(base, pattern)

        with mock.patch.object(Path, "rglob", autospec=True, side_effect=fail_first):
            Ingestor(self.settings, database)._store(folder.name, folder, {"trials": [{"trial": "trial"}]})
        _, trials = database.query_trials(folder.name)
        labels = {link["label"] for link in trials[0]["evidence_links"]}
        self.assertIn("steps/first/agent/native.jsonl", labels)
        self.assertNotIn("steps/first/agent/sessions/native.jsonl", labels)
        self.assertIn("steps/second/agent/sessions/native.jsonl", labels)


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

    def test_running_cancel_cleans_job_resources(self):
        database = Database(self.settings.database)
        database.initialize()
        runner = Runner(self.settings, database, Ingestor(self.settings, database))
        summary = {"projects": 1, "containers": 2, "networks": 1, "volumes": 0, "errors": []}
        with mock.patch.dict(os.environ, {"FAKE_RUN_DELAY": "30"}), \
                mock.patch("hbench_console.runner.cleanup_job", return_value=summary) as cleanup_job:
            runner.start()
            try:
                runner.submit("run-cleanup", "name: x\n", dry_run=True)
                wait_for(lambda: database.get_run("run-cleanup")["status"] == "running", timeout=15)
                runner.cancel("run-cleanup")
                run = wait_for(lambda: (database.get_run("run-cleanup")
                                        if database.get_run("run-cleanup")["status"] == "cancelled" else None))
            finally:
                runner.stop()
        self.assertEqual(run["status"], "cancelled")
        cleanup_job.assert_called()
        self.assertEqual(cleanup_job.call_args[0][0], self.settings.jobs_root / "run-cleanup")
        self.assertIn("cleanup: 1 trials, 2 containers, 1 networks, 0 volumes",
                      Path(run["log_path"]).read_text())

    def test_missing_images_are_built_before_the_run(self):
        database = Database(self.settings.database)
        database.initialize()
        runner = Runner(self.settings, database, None)
        plan = self.settings.plans_root / "run-images.yaml"
        log = self.settings.data_dir / "run-images.log"
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_text("benchmarks: [terminal-bench]\n", encoding="utf-8")

        def inspect(command, **kwargs):
            return mock.Mock(returncode=1 if "harness-runtime" in command[-1] else 0)

        with log.open("a") as stream, \
                mock.patch("hbench_console.runner.subprocess.run", side_effect=inspect), \
                mock.patch.object(runner, "_spawn", return_value=0) as spawn:
            self.assertTrue(runner._ensure_images(plan, stream))
        command = spawn.call_args[0][0]
        self.assertIn("images", command)
        self.assertIn("build", command)
        self.assertIn("--execute", command)

        with log.open("a") as stream, \
                mock.patch("hbench_console.runner.subprocess.run",
                           return_value=mock.Mock(returncode=0)), \
                mock.patch.object(runner, "_spawn", return_value=0) as spawn:
            self.assertTrue(runner._ensure_images(plan, stream))
        spawn.assert_not_called()

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


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cleanup-test-"))
        self.job = self.tmp / "job"
        (self.job / "task-a__abc").mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_project_names_ignore_non_trial_entries(self):
        (self.job / "Task B__x y").mkdir()
        (self.job / ".sources").mkdir()
        (self.job / "config.json").write_text("{}")
        self.assertEqual(cleanup.project_names(self.job), ["task-a__abc", "task-b__x-y"])

    def test_cleanup_removes_containers_networks_and_volumes(self):
        calls = []

        def fake(args):
            calls.append(args)
            if args[0] == "ps":
                return subprocess.CompletedProcess(args, 0, "c1\nc2\n", "")
            if args[:2] == ["network", "ls"]:
                return subprocess.CompletedProcess(args, 0, "n1\n", "")
            if args[:2] == ["volume", "ls"]:
                return subprocess.CompletedProcess(args, 0, "v1\n", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(cleanup, "_engine", side_effect=fake):
            summary = cleanup.cleanup_job(self.job)
        self.assertEqual((summary["projects"], summary["containers"],
                          summary["networks"], summary["volumes"]), (1, 2, 1, 1))
        self.assertIn(["container", "rm", "-f", "c1", "c2"], calls)
        self.assertIn(["network", "rm", "n1"], calls)
        self.assertIn(["volume", "rm", "-f", "v1"], calls)
        self.assertIn("label=com.docker.compose.project=task-a__abc", calls[0])

    def test_failed_removal_is_reported_and_cleanup_continues(self):
        def fake(args):
            if args[0] == "ps":
                return subprocess.CompletedProcess(args, 0, "c1\n", "")
            if args[:2] == ["network", "ls"]:
                return subprocess.CompletedProcess(args, 0, "n1\n", "")
            if args[:2] == ["volume", "ls"]:
                return subprocess.CompletedProcess(args, 0, "v1\n", "")
            if args[:2] == ["container", "rm"]:
                return subprocess.CompletedProcess(args, 1, "", "container in use")
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(cleanup, "_engine", side_effect=fake):
            summary = cleanup.cleanup_job(self.job)
        self.assertEqual(summary["containers"], 0)
        self.assertEqual(summary["networks"], 1)
        self.assertEqual(summary["volumes"], 1)
        self.assertTrue(any("container in use" in error for error in summary["errors"]))

    def test_cleanup_covers_verifier_projects_and_init_containers(self):
        calls = []

        def fake(args):
            calls.append(args)
            if args[0] == "ps" and "name=^task-a__abc[_-]" in args:
                return subprocess.CompletedProcess(args, 0, "init1\nmain1\n", "")
            if args[:2] == ["volume", "ls"]:
                return subprocess.CompletedProcess(args, 0, "v1\n", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(cleanup, "_engine", side_effect=fake):
            summary = cleanup.cleanup_job(self.job)
        self.assertEqual(summary["containers"], 2)
        self.assertEqual(summary["volumes"], 1)
        self.assertIn(["container", "rm", "-f", "init1", "main1"], calls)
        for scope in ("task-a__abc__env", "task-a__abc__verifier__trial"):
            self.assertIn(["volume", "ls", "-q", "--filter",
                           f"label=com.docker.compose.project={scope}"], calls)
            self.assertIn(["ps", "-aq", "--filter",
                           f"label=com.docker.compose.project={scope}"], calls)

    def test_missing_job_directory_skips_the_engine(self):
        with mock.patch.object(cleanup, "_engine", side_effect=AssertionError("no call")):
            summary = cleanup.cleanup_job(self.tmp / "absent")
        self.assertEqual(summary["projects"], 0)
        self.assertEqual(summary["errors"], [])


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

    def test_real_run_gate_can_be_toggled_from_the_console(self):
        client, _ = self.client()
        self.assertFalse(client.get("/api/settings").json()["allow_real_runs"])
        self.assertFalse(client.get("/api/catalog").json()["allow_real_runs"])
        response = client.put("/api/settings/real-runs", json={"allow": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["allow_real_runs"])
        self.assertTrue(client.get("/api/catalog").json()["allow_real_runs"])
        self.assertTrue(client.get("/api/health").json()["allow_real_runs"])
        created = client.post("/api/runs", json={
            "matrix": "name: m\n", "name": "run-real-toggle", "dry_run": False,
            "overrides": {"cluster": "docker"}})
        self.assertEqual(created.status_code, 200)
        self.assertFalse(created.json()["dry_run"])
        client.put("/api/settings/real-runs", json={"allow": False})
        refused = client.post("/api/runs", json={
            "matrix": "name: m\n", "name": "run-real-off", "dry_run": False})
        self.assertEqual(refused.status_code, 403)
        self.assertFalse(client.get("/api/settings").json()["allow_real_runs"])

    def test_real_run_setting_survives_a_restart(self):
        client, _ = self.client()
        client.put("/api/settings/real-runs", json={"allow": True})
        restarted, _ = self.client()
        self.assertTrue(restarted.get("/api/settings").json()["allow_real_runs"])

    def test_preview_overrides_dataset_harness_and_model_lists(self):
        import yaml
        client, _ = self.client()
        response = client.post("/api/runs/preview", json={
            "matrix": "name: m\nbenchmarks: [terminal-bench]\nmodels: [bailian-model-a]\n"
                      "harnesses: [codex]\ncluster: docker\n",
            "overrides": {"benchmarks": ["terminal-bench", "swe-bench-verified"],
                          "models": ["bailian-model-b"], "harnesses": ["pi", "agentscope-java"]}})
        self.assertEqual(response.status_code, 200)
        value = yaml.safe_load(response.json()["matrix"])
        self.assertEqual(value["benchmarks"], ["terminal-bench", "swe-bench-verified"])
        self.assertEqual(value["models"], ["bailian-model-b"])
        self.assertEqual(value["harnesses"], ["pi", "agentscope-java"])
        for bad in ([], [""], [1]):
            with self.subTest(bad=bad):
                response = client.post("/api/runs/preview", json={
                    "matrix": "name: m\nbenchmarks: [terminal-bench]\n"
                              "models: [bailian-model-a]\nharnesses: [codex]\n",
                    "overrides": {"benchmarks": bad}})
                self.assertEqual(response.status_code, 422)

    def test_matrix_exposes_structured_fields_for_the_form(self):
        client, _ = self.client()
        value = client.get("/api/matrices/bailian-18").json()
        self.assertEqual(value["fields"]["cluster"], "docker")
        self.assertIn("tau3-bench", value["fields"]["benchmarks"])
        self.assertIn("agentscope-java", value["fields"]["harnesses"])
        self.assertIn("selection", value["fields"])
        self.assertEqual(client.get("/api/matrices/../../etc").status_code, 404)

    def test_run_progress_counts_completed_and_active_trials(self):
        client, app = self.client()
        database = app.state.console.database
        database.create_run("run-progress", "name: x\n", False)
        job_dir = self.settings.jobs_root / "run-progress"
        job_dir.mkdir(parents=True)
        for index in range(5):
            trial = job_dir / f"task-{index}__abc"
            trial.mkdir()
            if index < 3:
                (trial / "result.json").write_text("{}")
        database.update_run("run-progress", status="running", planned_trials=10,
                            job_dir=str(job_dir), started_at="2026-11-17T00:00:00+00:00")
        progress = client.get("/api/runs/run-progress/progress").json()["progress"]
        self.assertEqual(progress["planned"], 10)
        self.assertEqual(progress["recorded"], 3)
        self.assertEqual(progress["running"], 2)
        self.assertEqual(progress["queued"], 5)
        self.assertEqual(progress["percent"], 30.0)
        self.assertEqual(len(progress["active"]), 2)
        run = client.get("/api/runs/run-progress").json()
        self.assertEqual(run["progress"]["recorded"], 3)
        listed = client.get("/api/runs").json()["runs"][0]
        self.assertEqual(listed["progress"]["running"], 2)

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
