import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from hbench.registry import harness_registry, harness_name, harness_evidence_files
from hbench.jobs import load_profile
from hbench_console.ingest import _agent_identity


class RegistryTests(unittest.TestCase):
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
                    self.assertFalse(cfg["agents"]["defaults"]["memorySearch"]["enabled"])
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
