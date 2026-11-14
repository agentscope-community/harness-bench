"""Offline summary fixtures using Harbor 0.25 schemas; no execution services."""
from copy import deepcopy
import html
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch
from uuid import UUID

from harbor.models.job.lock import JobLock
from harbor.models.job.result import JobResult
from harbor.models.trial.result import TrialResult

from hbench.summary import aggregate, summarize_job


class SummaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.job = self.root / "job"
        self.job.mkdir()
        self.output = self.root / "report"
        network = patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden"))
        network.start()
        self.addCleanup(network.stop)

    def write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def agent(self, name="pi", model="mock/model"):
        paths = {"pi": "hbench.agents:FrozenPi", "codex": "hbench.agents:FrozenCodex",
                 "agentscope-java": "hbench.agents:AgentScopeJava"}
        return {"import_path": paths[name], "model_name": model, "skip_model_parsing": True}

    def trial(self, number=1, *, agent="pi", source="benchmark", reward=1):
        name = f"task__trial-{number}"
        task = {"path": f"/offline/{source}/task", "source": source}
        row = {"id": str(UUID(int=number)), "task_name": "task", "trial_name": name,
               "trial_uri": f"file:///offline/{name}", "task_id": {"path": task["path"]},
               "source": source, "task_checksum": "checksum",
               "config": {"task": task, "agent": self.agent(agent), "trial_name": name},
               "agent_info": {"name": agent, "version": "offline",
                              "model_info": {"name": "mock/model"}},
               "agent_result": {"n_input_tokens": 7, "n_output_tokens": 3}}
        if reward is not None:
            row["verifier_result"] = {"rewards": {"reward": reward}}
        TrialResult.model_validate(row)
        return row

    def job_result(self, rows, planned):
        raw = {"id": str(UUID(int=1000)), "started_at": "2026-11-12T00:00:00Z",
               "n_total_trials": planned, "stats": {}, "trial_results": rows}
        JobResult.model_validate(raw)
        self.write_json(self.job / "result.json", raw)

    def lock(self, entries):
        # JobLock.trials is the expanded list: attempts are already represented.
        raw = {"n_concurrent_trials": 1, "retry": {"max_retries": 0}, "trials": [
            {"task": {"name": task, "source": source, "type": "local",
                      "path": f"/offline/{source}/{task}", "digest": "sha256:" + "0" * 64},
             "agent": self.agent(agent), "environment": {}, "verifier": {}}
            for agent, source, task in entries]}
        JobLock.model_validate(raw)
        self.write_json(self.job / "lock.json", raw)

    def config(self, agents=("pi", "codex"), sources=("benchmark",), attempts=2):
        raw = {"n_attempts": attempts, "agents": [self.agent(agent) for agent in agents],
               "datasets": [{"path": f"/offline/{source}", "task_names": ["a", "b"]}
                            for source in sources]}
        self.write_json(self.job / "config.json", raw)
        return raw

    def summarize(self):
        summary = summarize_job(self.job, self.output)
        self.assertEqual(json.loads((self.output / "summary.json").read_text()), summary)
        return summary

    def assert_rejected(self, message):
        with self.assertRaisesRegex(ValueError, message):
            summarize_job(self.job, self.output)
        self.assertFalse(self.output.exists())

    def test_embedded_only_and_root_evidence(self):
        self.job_result([self.trial()], 2)
        summary = self.summarize()
        self.assertEqual(summary["recorded_trials"], 1)
        self.assertEqual(summary["missing_trials"], 1)
        self.assertEqual(summary["pass_rate_planned"], 0.5)
        self.assertEqual(summary["groups"][0]["passed"], 1)
        page = (self.output / "report.html").read_text()
        self.assertIn('href="../job/result.json"', page)
        self.assertNotIn("file://", page)
        self.assertIn("task__trial-1", page)
        trial = summary["trials"][0]
        self.assertEqual((trial["trial"], trial["task"], trial["agent"], trial["reward"], trial["error"]),
                         ("task__trial-1", "task", "pi", 1, None))
        self.assertEqual((trial["input_tokens"], trial["output_tokens"], trial["evidence"]), (7, 3, "../job/result.json"))
        self.assertIn('name="viewport" content="width=device-width, initial-scale=1"', page)
        self.assertIn('.table-scroll{overflow-x:auto}', page)
        self.assertIn('<div class="table-scroll"><table>', page)

    def test_trial_rows_report_error_and_duration(self):
        raw = self.trial(reward=None)
        raw.update(started_at="2026-11-12T00:00:00Z", finished_at="2026-11-12T00:01:30.5Z",
                   exception_info={"exception_type": "AgentTimeoutError", "exception_message": "x",
                                   "exception_traceback": "", "occurred_at": "2026-11-12T00:01:30Z"})
        self.job_result([raw], 1)
        trial = self.summarize()["trials"][0]
        self.assertEqual((trial["error"], trial["duration_sec"], trial["reward"]), ("AgentTimeoutError", 90.5, None))
        self.assertIn("<td>AgentTimeoutError</td>", (self.output / "report.html").read_text())

    def test_normalized_duplicate_keeps_both_evidence_links(self):
        raw = self.trial(171)
        self.job_result([raw], 1)
        normalized = TrialResult.model_validate(raw).model_dump(mode="json")
        normalized["id"] = normalized["id"].upper()
        path = self.job / "trial" / "result.json"
        self.write_json(path, normalized)
        summary = self.summarize()
        group = summary["groups"][0]
        self.assertEqual(summary["recorded_trials"], 1)
        self.assertEqual(group["input_tokens"], 7)
        self.assertEqual(group["output_tokens"], 3)
        page = (self.output / "report.html").read_text()
        self.assertIn('href="../job/result.json"', page)
        self.assertIn('href="../job/trial/result.json"', page)
        self.assertEqual([t["evidence"] for t in summary["trials"]], ["../job/trial/result.json"])

    def test_conflicting_duplicate_fails_before_output(self):
        self.job_result([self.trial()], 1)
        self.write_json(self.job / "trial" / "result.json", self.trial(reward=0))
        self.assert_rejected("Conflicting evidence")

    def test_conflicting_non_score_evidence_fails(self):
        raw = self.trial()
        self.job_result([raw], 1)
        raw["agent_result"]["n_input_tokens"] = 8
        self.write_json(self.job / "trial" / "result.json", raw)
        self.assert_rejected("Conflicting evidence")

    def test_conflicting_sensitive_evidence_is_not_redacted_for_comparison(self):
        raw = self.trial()
        raw["config"]["agent"]["env"] = {"OPENAI_API_KEY": "offline-first"}
        self.job_result([raw], 1)
        raw["config"]["agent"]["env"]["OPENAI_API_KEY"] = "offline-second"
        self.write_json(self.job / "trial" / "result.json", raw)
        self.assert_rejected("Conflicting evidence")

    def test_embedded_and_file_unique_trials_merge(self):
        self.job_result([self.trial()], 2)
        self.write_json(self.job / "trial" / "result.json", self.trial(2, reward=0))
        summary = self.summarize()
        self.assertEqual(summary["recorded_trials"], 2)
        self.assertEqual(summary["groups"][0]["passed"], 1)

    def test_lock_prebuilds_wholly_missing_harness_dataset_groups(self):
        self.lock([(agent, source, "task") for agent in ("pi", "codex")
                   for source in ("benchmark", "other") for _ in range(2)])
        # An unresolved config must not override an available expanded lock.
        self.write_json(self.job / "config.json", {"n_attempts": 99, "datasets": [{"name": "remote"}]})
        self.job_result([self.trial()], 8)
        summary = self.summarize()
        self.assertEqual(len(summary["groups"]), 4)
        self.assertEqual(summary["missing_trials"], 7)
        for group in summary["groups"]:
            self.assertEqual(group["planned"], 2)
            self.assertEqual(group["missing"], 2 - group["recorded"])
            if not group["recorded"]:
                self.assertIsNone(group["pass_rate_recorded"])
                self.assertIsNone(group["mean_reward_scored"])
                self.assertEqual(group["pass_rate_planned"], 0)

    def test_exact_local_config_fallback_and_attempts(self):
        self.config(agents=("pi", "codex", "agentscope-java"), sources=("benchmark", "other"))
        self.write_json(self.job / "trial" / "result.json", self.trial())
        summary = self.summarize()
        self.assertEqual(summary["planned_trials"], 24)
        self.assertEqual(summary["missing_trials"], 23)
        self.assertEqual(len(summary["groups"]), 6)
        self.assertEqual({group["agent"] for group in summary["groups"]},
                         {"pi", "codex", "agentscope-java"})
        self.assertTrue(all(group["planned"] == 4 for group in summary["groups"]))

    def test_agent_class_name_without_constructing_or_running(self):
        raw = self.config(agents=("pi",), attempts=1)
        raw["agents"][0] = {"import_path": "harbor.agents.nop:NopAgent"}
        self.write_json(self.job / "config.json", raw)
        from harbor.agents.nop import NopAgent
        with patch.object(NopAgent, "__init__", side_effect=AssertionError("No construction")), \
                patch.object(NopAgent, "run", side_effect=AssertionError("No execution")):
            summary = self.summarize()
        group = summary["groups"][0]
        self.assertEqual(group["agent"], NopAgent.name())
        self.assertEqual(group["model"], "unknown")
        self.assertEqual(group["missing"], 2)

    def test_model_identity_matches_harbor_provider_parsing(self):
        raw = self.config(agents=("pi",), attempts=1)
        raw["agents"][0].update(model_name="openai/gpt-4o", skip_model_parsing=False)
        self.write_json(self.job / "config.json", raw)
        row = self.trial()
        row["config"]["agent"] = raw["agents"][0]
        row["agent_info"]["model_info"] = {"name": "gpt-4o", "provider": "openai"}
        self.job_result([row], 2)
        group = self.summarize()["groups"][0]
        self.assertEqual(group["model"], "gpt-4o")
        self.assertEqual(group["planned"], 2)

    def test_model_parsing_is_offline_in_fresh_process_and_restores_environment(self):
        raw = self.config(agents=("pi",), attempts=1)
        models = {"openai/gpt-4o": "gpt-4o",
                  "openrouter/anthropic/claude-3.5-sonnet": "anthropic/claude-3.5-sonnet",
                  "not-a-provider/offline-model": "not-a-provider/offline-model",
                  "github_copilot/copilot-model": "copilot-model",
                  "chatgpt/chatgpt-model": "chatgpt-model"}
        raw["agents"] = [{**self.agent(), "model_name": model, "skip_model_parsing": False}
                         for model in models]
        self.write_json(self.job / "config.json", raw)
        script = textwrap.dedent("""\
            import os
            from pathlib import Path
            import socket
            import sys
            from unittest.mock import patch

            assert not any(name == "litellm" or name.startswith("litellm.") for name in sys.modules)
            original = dict(os.environ)
            with patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")) as connect, \\
                    patch.object(socket.socket, "connect_ex", side_effect=AssertionError("Network forbidden")) as connect_ex, \\
                    patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden")) as dns:
                from hbench.summary import summarize_job
                assert "litellm" not in sys.modules
                summary = summarize_job(Path(sys.argv[1]), Path(sys.argv[2]))
                assert "litellm" in sys.modules, "Provider parsing must run, not be skipped"
                assert {g["model"] for g in summary["groups"]} == {
                    "gpt-4o", "anthropic/claude-3.5-sonnet", "not-a-provider/offline-model",
                    "copilot-model", "chatgpt-model"}
                assert all(g["planned"] == 2 and g["missing"] == 2 for g in summary["groups"])
                assert summary["planned_trials"] == 10
                connect.assert_not_called()
                connect_ex.assert_not_called()
                dns.assert_not_called()
                assert dict(os.environ) == original, "Caller environment changed"
                # Repeated calls must also stay offline after restoring a false/unset flag.
                summarize_job(Path(sys.argv[1]), Path(sys.argv[2]).with_name("repeat"))
                connect.assert_not_called()
                connect_ex.assert_not_called()
                dns.assert_not_called()
                assert dict(os.environ) == original, "Caller environment changed on repeat"
            """)
        for value in (None, "False"):
            with self.subTest(local_cost_map=value):
                env = dict(os.environ)
                for key in ("LITELLM_LOCAL_MODEL_COST_MAP", "LITELLM_OFFLINE_MODE"):
                    if value is None:
                        env.pop(key, None)
                    else:
                        env[key] = value
                output = self.root / str(value) / "report"
                completed = subprocess.run(
                    [sys.executable, "-c", script, str(self.job), str(output)],
                    cwd=Path(__file__).resolve().parents[2], env=env,
                    capture_output=True, text=True, timeout=60,
                )
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_model_parsing_restores_environment_on_error(self):
        self.config(agents=("pi",), attempts=1)

        def fail_parsing(*args, **kwargs):
            self.assertEqual(os.environ["LITELLM_LOCAL_MODEL_COST_MAP"], "True")
            self.assertEqual(os.environ["LITELLM_OFFLINE_MODE"], "True")
            raise RuntimeError("Parsing failed")

        for value in (None, "False", "True", ""):
            with self.subTest(local_cost_map=value), patch.dict(os.environ):
                for key in ("LITELLM_LOCAL_MODEL_COST_MAP", "LITELLM_OFFLINE_MODE"):
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                original = dict(os.environ)
                with patch("harbor.agents.agent_auth_resolution.parse_model_name", side_effect=fail_parsing):
                    with self.assertRaisesRegex(RuntimeError, "Parsing failed"):
                        summarize_job(self.job, self.output)
                self.assertEqual(dict(os.environ), original)
                self.assertFalse(self.output.exists())

    def test_no_records_with_only_lock_or_config(self):
        for source in ("lock", "config"):
            with self.subTest(source=source):
                if source == "lock":
                    self.lock([("pi", "benchmark", "task")])
                else:
                    (self.job / "lock.json").unlink()
                    self.config(agents=("pi",), attempts=1)
                summary = summarize_job(self.job, self.root / source)
                self.assertEqual(summary["recorded_trials"], 0)
                self.assertEqual(summary["missing_trials"], summary["planned_trials"])
                self.assertIsNone(summary["groups"][0]["pass_rate_recorded"])
                self.assertEqual(summary["pass_rate_planned"], 0)

    def test_root_result_with_no_records(self):
        self.job_result([], 3)
        summary = self.summarize()
        self.assertEqual(summary["groups"], [])
        self.assertEqual(summary["missing_trials"], 3)
        self.assertEqual(summary["pass_rate_planned"], 0)

    def test_empty_directory_rejected(self):
        self.assert_rejected("No Harbor")

    def test_unresolved_datasets_do_not_invent_counts_or_call_apis(self):
        raw = self.config(agents=("pi",))
        raw["datasets"] = [{"name": "remote", "task_names": ["a"]},
                           {"path": "/offline/glob", "task_names": ["task-*"]}]
        self.write_json(self.job / "config.json", raw)
        from harbor.models.job.config import DatasetConfig
        with patch.object(DatasetConfig, "get_task_configs", side_effect=AssertionError("No dataset API")):
            summary = self.summarize()
        self.assertIsNone(summary["planned_trials"])
        self.assertEqual(len(summary["groups"]), 2)
        self.assertTrue(all(group["planned"] is None for group in summary["groups"]))

    def test_job_count_mismatch_rejected(self):
        self.lock([("pi", "benchmark", "task")])
        self.job_result([], 2)
        self.assert_rejected("disagrees")

    def test_config_count_mismatch_rejected(self):
        self.config(agents=("pi",), attempts=1)
        self.job_result([], 1)
        self.assert_rejected("disagrees")

    def test_group_overflow_rejected_even_with_room_in_total(self):
        self.lock([("pi", "benchmark", "task"), ("codex", "benchmark", "task")])
        self.job_result([self.trial(), self.trial(2)], 2)
        self.assert_rejected("planned group count")

    def test_unexpected_group_rejected(self):
        self.lock([("codex", "benchmark", "task")])
        self.job_result([self.trial()], 1)
        self.assert_rejected("unexpected group")

    def test_recorded_overflow_without_group_plan_rejected(self):
        self.job_result([self.trial()], 0)
        self.assert_rejected("planned trial count")

    def test_invalid_counts_rejected(self):
        for planned in (-1, True, 1.5, "2"):
            with self.subTest(planned=planned):
                self.write_json(self.job / "result.json", {"n_total_trials": planned})
                self.assert_rejected("nonnegative integer")

    def test_missing_id_and_invalid_trial_validation(self):
        raw = self.trial()
        del raw["id"]
        self.write_json(self.job / "result.json", {"trial_results": [raw]})
        self.assert_rejected("Missing trial identity")
        raw = self.trial()
        del raw["agent_info"]["version"]
        self.write_json(self.job / "result.json", {"trial_results": [raw]})
        self.assert_rejected("version")

    def test_errors_unscored_and_missing_are_not_passes(self):
        rows = [self.trial(), self.trial(2), self.trial(3, reward=None), self.trial(4)]
        error = {"exception_type": "Broken", "exception_message": "offline",
                 "exception_traceback": "offline", "occurred_at": "2026-11-12T00:00:00Z"}
        rows[1]["exception_info"] = error
        rows[3]["step_results"] = [{"step_name": "step", "exception_info": error}]
        self.lock([("pi", "benchmark", "task")] * 5)
        self.job_result(rows, 5)
        group = self.summarize()["groups"][0]
        self.assertEqual((group["passed"], group["scored"], group["errors"], group["unscored"]), (1, 1, 2, 3))
        self.assertEqual(group["missing"], 1)
        self.assertEqual(group["pass_rate_recorded"], 0.25)
        self.assertEqual(group["pass_rate_planned"], 0.2)

    def test_aggregate_compatibility_and_nonfinite_rewards(self):
        rows = []
        for index, reward in enumerate((1, True, float("nan"), float("inf"), None)):
            rows.append({"id": str(index), "agent_info": {"name": "pi"},
                         "verifier_result": {"rewards": {"reward": reward}}})
        summary = aggregate(rows, 6)
        self.assertEqual(summary["missing_trials"], 1)
        self.assertEqual(summary["groups"][0]["scored"], 1)
        self.assertEqual(summary["groups"][0]["pass_rate_recorded"], 0.2)
        self.assertIsNone(summary["groups"][0]["pass_rate_planned"])
        with self.assertRaises(ValueError):
            aggregate([rows[0], rows[0]])
        self.assertIsNone(aggregate([], 0)["pass_rate_planned"])

    def test_html_escapes_group_and_evidence_text(self):
        raw = self.trial()
        values = ['<script>"harness" & bad</script>', '<model a="b">', '<dataset>&']
        raw["agent_info"]["name"], raw["agent_info"]["model_info"]["name"], raw["source"] = values
        raw["trial_name"] = '<img src=x onerror="bad"> & evidence'
        self.job_result([raw], 1)
        path = self.job / 'trial " & <' / "result.json"
        self.write_json(path, deepcopy(raw))
        self.summarize()
        page = (self.output / "report.html").read_text()
        for value in values + [raw["trial_name"]]:
            self.assertIn(html.escape(value), page)
            self.assertNotIn(value, page)
        self.assertIn('href="../job/trial%20%22%20%26%20%3C/result.json"', page)
        self.assertNotIn("<script>", page)


if __name__ == "__main__":
    unittest.main()
