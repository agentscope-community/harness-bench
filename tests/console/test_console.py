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
from hbench_console.db import Database, SCHEMA
from hbench_console.diagnosis import diagnose
from hbench_console.ingest import Ingestor, _evidence_links
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
