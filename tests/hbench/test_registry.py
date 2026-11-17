import tempfile
import json
import unittest
from unittest.mock import patch
from pathlib import Path

from hbench.registry import harness_registry, harness_name, harness_evidence_files
from hbench.jobs import load_profile
from hbench_console.ingest import _agent_identity


class RegistryTests(unittest.TestCase):
    def test_openclaw_native_events_keep_messages_after_large_system_events(self):
        from hbench.extended_agents import _openclaw_export_to_session
        from harbor.agents.installed.openclaw import openclaw_session_jsonl_to_atif_steps

        messages = [
            {"role": "user", "content": "read file"},
            {"role": "assistant", "content": [{"type": "toolCall", "id": "call-1", "name": "read", "arguments": {"path": "/app/input"}}]},
            {"role": "toolResult", "toolCallId": "call-1", "content": [{"type": "text", "text": "42"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        ]
        events = [{"source": "transcript", "data": {"customType": "system", "data": "[Truncated]"}}]
        events += [{"source": "transcript", "data": {"message": m}} for m in messages]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            path.write_text(_openclaw_export_to_session("\n".join(json.dumps(e) for e in events)))
            steps = openclaw_session_jsonl_to_atif_steps(path, instruction="read file", model_name="test")
            self.assertEqual(len(steps), 3)
            self.assertEqual(steps[1].tool_calls[0].function_name, "read")
            self.assertEqual(steps[-1].message, "done")

    def test_hermes_session_usage_includes_cached_input(self):
        from hbench.extended_agents import FrozenHermes
        from harbor.models.agent.context import AgentContext

        with tempfile.TemporaryDirectory() as directory:
            logs = Path(directory)
            agent = FrozenHermes(logs_dir=logs)
            (logs / "hermes-session.jsonl").write_text(json.dumps({
                "id": "native-session", "input_tokens": 20, "cache_read_tokens": 80,
                "cache_write_tokens": 10, "output_tokens": 7,
                "messages": [{"role": "user", "content": "hello"},
                             {"role": "assistant", "content": "done"}],
            }) + "\n")
            context = AgentContext()
            agent.populate_context_post_run(context)
            self.assertEqual((context.n_input_tokens, context.n_output_tokens, context.n_cache_tokens), (110, 7, 80))
            self.assertEqual(json.loads((logs / "trajectory.json").read_text())["session_id"], "native-session")

    def test_openclaw_result_survives_trailing_native_shutdown_logs(self):
        from hbench.extended_agents import FrozenOpenClaw

        with tempfile.TemporaryDirectory() as directory:
            agent = FrozenOpenClaw(logs_dir=Path(directory))
            result = {"payloads": [{"text": "done"}], "meta": {"agentMeta": {"usage": {"input": 12}}}}
            (Path(directory) / "openclaw.txt").write_text(
                "[memory] startup log\n" + json.dumps(result, indent=2)
                + "\n[state/agent-db] agent database clean-close receipt: written\n"
            )
            self.assertEqual(agent._parse_stdout(), result)

    def test_openclaw_translates_model_connection_without_serializing_credentials(self):
        from hbench.extended_agents import FrozenOpenClaw

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"TEST_HARNESS_KEY": "test-secret"}
        ):
            for api_format, native in FrozenOpenClaw.API_MODES.items():
                with self.subTest(api_format=api_format):
                    agent = FrozenOpenClaw(
                        logs_dir=Path(directory), model_name="model/opaque-id",
                        base_url="https://example.test/api",
                        model_api_key_name="TEST_HARNESS_KEY",
                        model_api_format=api_format, skip_model_parsing=True,
                    )
                    cfg = agent._build_full_openclaw_config()
                    self.assertFalse(cfg["memory"]["search"]["enabled"])
                    provider = cfg["models"]["providers"]["hbench"]
                    self.assertEqual(provider["api"], native)
                    self.assertEqual(provider["baseUrl"], "https://example.test/api")
                    self.assertEqual(provider["models"][0]["id"], "model/opaque-id")
                    self.assertEqual(provider["apiKey"], "${HB_OPENCLAW_API_KEY}")
                    self.assertNotIn("test-secret", str(cfg))
                    self.assertEqual(agent.model_connection.env["HB_OPENCLAW_API_KEY"], "test-secret")

    def test_profiles_share_identity_and_execution_configuration(self):
        for name, profile in harness_registry().items():
            with self.subTest(name=name):
                self.assertEqual(load_profile("harnesses", name), profile)
                self.assertEqual(harness_name(profile["import_path"]), name)
                self.assertEqual(_agent_identity({"import_path": profile["import_path"],
                                                 "model_name": "model"}), (name, "model"))
                from harbor.agents.factory import AgentFactory
                from harbor.models.trial.config import AgentConfig
                cls = AgentFactory.get_agent_class_from_config(AgentConfig(import_path=profile["import_path"]))
                self.assertEqual(cls.name(), name)

    def test_new_native_logs_are_discoverable(self):
        for file in ("claude-code.txt", "hermes-session.jsonl", "openclaw.session.jsonl"):
            self.assertIn(file, harness_evidence_files())

    def test_duplicate_identity_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("a", "b"):
                (root / (name + ".yaml")).write_text(
                    f"name: {name}\nimport_path: example:Agent\nsupported_api_formats: [openai_chat_completions]\n")
            with self.assertRaisesRegex(ValueError, "duplicate"):
                harness_registry(root)
