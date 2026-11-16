# Copyright 2024-2026 the original author or authors.
# SPDX-License-Identifier: Apache-2.0
"""Offline adapter checks against the installed Harbor API; no model or cluster calls."""

import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from harbor.agents.installed.base import (
    AgentAuthenticationError,
    ApiRateLimitError,
    NonZeroAgentExitCodeError,
)
from harbor.agents.installed.codex import Codex
from harbor.agents.installed.pi import Pi
from harbor.agents.options import InstalledAgentOptions
from harbor.environments.base import ExecResult
from harbor.models.agent.context import AgentContext
from harbor.models.model_api_format import ModelApiFormat
from harbor.models.task.config import MCPServerConfig

from hbench.agents import (
    AGENTSCOPE_CLI_VERSION,
    CODEX_VERSION,
    JAVA_CLI,
    PI_MCP_ADAPTER_VERSION,
    PI_MCP_CONFIG,
    PI_MCP_EXTENSION,
    PI_MCP_VERSIONS,
    PI_VERSION,
    AgentScopeJava,
    AgentScopeJavaOptions,
    FrozenCodex,
    FrozenPi,
    record_request,
)
from runtime.protocol import validate_request


def result(stdout="", return_code=0, stderr=""):
    return ExecResult(stdout=stdout, stderr=stderr, return_code=return_code)


def fake_environment():
    def execute(command, **kwargs):
        if command == "pwd" or command == "set -o pipefail; pwd":
            return result("/workspace with spaces\n")
        if "CODEX_RUNTIME_ID" in command:
            return result("CODEX_RUNTIME_ID 1000 1000\n")
        return result()

    return SimpleNamespace(
        exec=AsyncMock(side_effect=execute),
        upload_file=AsyncMock(),
        default_user=None,
    )


class AgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.logs = Path(self.temporary.name)
        self.environment = fake_environment()

    def agent(self, cls, **kwargs):
        config = {
            "logs_dir": self.logs,
            "model_name": "openai/mock-model",
            "base_url": "https://model.invalid/v1",
            "extra_env": {"OPENAI_API_KEY": "test-key"},
        }
        if issubclass(cls, Pi):
            config["model_api_format"] = ModelApiFormat.OPENAI_CHAT_COMPLETIONS
        config.update(kwargs)
        return cls(**config)

    def commands(self):
        return [call.kwargs["command"] for call in self.environment.exec.await_args_list]

    async def test_defaults_are_frozen_and_install_only_checks(self):
        for cls, version, stdout in (
            (FrozenCodex, CODEX_VERSION, "codex-cli " + CODEX_VERSION),
            (FrozenPi, PI_VERSION, PI_VERSION),
            (AgentScopeJava, AGENTSCOPE_CLI_VERSION, f"agentscope-harness-eval {AGENTSCOPE_CLI_VERSION}"),
        ):
            with self.subTest(agent=cls.__name__):
                agent = self.agent(cls)
                self.assertEqual(agent.version(), version)
                self.environment.exec.reset_mock(side_effect=True)
                self.environment.exec.return_value = result(stdout)
                await agent.install(self.environment)
                self.environment.exec.assert_awaited_once_with(
                    command=agent.get_version_command(), timeout_sec=30
                )
                evidence = json.loads((self.logs / "version.json").read_text())
                self.assertEqual(evidence["actual_version"], version)
                self.assertEqual(evidence["expected_version"], version)
                self.assertEqual(evidence["requested_version"], version)
                self.assertFalse(any("npm" in command for command in self.commands()))
                self.assertEqual(agent._version, version)

    async def test_explicit_pinned_versions_are_checked(self):
        for cls, version, stdout in (
            (FrozenCodex, CODEX_VERSION, "codex-cli " + CODEX_VERSION),
            (FrozenPi, PI_VERSION, PI_VERSION),
            (AgentScopeJava, AGENTSCOPE_CLI_VERSION, f"agentscope-harness-eval {AGENTSCOPE_CLI_VERSION}"),
        ):
            with self.subTest(agent=cls.__name__):
                self.environment.exec.side_effect = None
                self.environment.exec.return_value = result(stdout)
                await self.agent(cls, version=version).install(self.environment)

    async def test_latest_ranges_and_empty_versions_are_rejected_without_exec(self):
        for cls in (FrozenCodex, FrozenPi, AgentScopeJava):
            for version in ("latest", "^0.85.1", "", ">=0.84.1", "v0.85.1"):
                with self.subTest(agent=cls.__name__, version=version):
                    self.environment.exec.reset_mock()
                    with self.assertRaises(ValueError):
                        await self.agent(cls, version=version).install(self.environment)
                    self.environment.exec.assert_not_awaited()
                    with self.assertRaises(ValueError):
                        cls.preflight({"version": version})

    async def test_requested_version_mismatch_fails_and_records_actual(self):
        self.environment.exec.side_effect = None
        self.environment.exec.return_value = result("codex-cli " + CODEX_VERSION)
        agent = self.agent(FrozenCodex, version="0.166.0")
        with self.assertRaisesRegex(RuntimeError, "requested 0.166.0"):
            await agent.install(self.environment)
        evidence = json.loads((self.logs / "version.json").read_text())
        self.assertEqual(evidence["requested_version"], "0.166.0")
        self.assertEqual(evidence["actual_version"], CODEX_VERSION)

    async def test_wrong_baked_version_missing_binary_and_empty_output_fail_closed(self):
        for cls in (FrozenCodex, FrozenPi, AgentScopeJava):
            for probe in (result("0.1.0"), result("", 127), result("\n")):
                with self.subTest(agent=cls.__name__, probe=probe):
                    self.environment.exec.side_effect = None
                    self.environment.exec.return_value = probe
                    with self.assertRaisesRegex(RuntimeError, "version mismatch"):
                        await self.agent(cls).install(self.environment)
                    self.assertTrue((self.logs / "version.json").is_file())

    async def test_setup_does_not_trigger_native_installer_or_duplicate_version_probe(self):
        agent = self.agent(FrozenCodex)
        self.environment.exec.side_effect = lambda command, **kwargs: result(
            "codex-cli " + CODEX_VERSION if "--version" in command else ""
        )
        with patch.object(Codex, "install", new_callable=AsyncMock) as installer:
            await agent.setup(self.environment)
            installer.assert_not_awaited()
        self.assertEqual(sum("--version" in command for command in self.commands()), 1)

    def mcp(self, **kwargs):
        return MCPServerConfig(name="tools", url="https://mcp.invalid", **kwargs)

    async def test_pi_mcp_checks_baked_adapter_and_harbor_extension_path(self):
        agent = self.agent(FrozenPi, mcp_servers=[self.mcp()])
        self.environment.exec.side_effect = [result(PI_VERSION), result(PI_MCP_ADAPTER_VERSION)]
        await agent.install(self.environment)
        commands = self.commands()
        self.assertEqual(len(commands), 2)
        self.assertIn("test -f " + PI_MCP_EXTENSION, commands[1])
        self.assertIn("package.json", commands[1])
        self.assertFalse(any("npm" in command for command in commands))
        evidence = json.loads((self.logs / "version.json").read_text())
        self.assertEqual(evidence["mcp_adapter_version"], PI_MCP_ADAPTER_VERSION)
        self.assertEqual(evidence["mcp_extension"], PI_MCP_EXTENSION)

    async def test_pi_mcp_missing_extension_or_wrong_adapter_fails(self):
        for adapter in (result("", 1), result("2.34.0"), result("7.0.0")):
            with self.subTest(adapter=adapter):
                self.environment.exec.side_effect = [result(PI_VERSION), adapter]
                with self.assertRaisesRegex(RuntimeError, "MCP extension missing or version mismatch"):
                    await self.agent(FrozenPi, mcp_servers=[self.mcp()]).install(self.environment)

    async def test_pi_mcp_version_range_is_enforced_before_exec(self):
        for version in ("0.84.0", "0.85.1", "0.86.0", "2.0.0"):
            with self.subTest(version=version):
                self.assertNotIn(version, PI_MCP_VERSIONS)
                self.environment.exec.reset_mock()
                with self.assertRaisesRegex(ValueError, "Pi MCP adapter requires"):
                    await self.agent(FrozenPi, version=version, mcp_servers=[self.mcp()]).install(
                        self.environment
                    )
                self.environment.exec.assert_not_awaited()
        for version in ("1.0.0", "1.5.2", "1.99.99"):
            with self.subTest(version=version):
                self.assertIn(version, PI_MCP_VERSIONS)

    async def test_frozen_run_records_request_and_delegates_unchanged(self):
        for cls, native in ((FrozenCodex, Codex), (FrozenPi, Pi)):
            with self.subTest(agent=cls.__name__):
                agent = self.agent(cls)
                context = AgentContext(metadata={"original": True})
                instruction = "Read 'quoted' paths; 中文\nthen stop"
                with patch.object(native, "run", new_callable=AsyncMock) as run:
                    await agent.run(instruction, self.environment, context)
                    run.assert_awaited_once_with(instruction, self.environment, context)
                request = json.loads((self.logs / "request.json").read_text())
                validate_request(request)
                self.assertEqual(request["events"][0]["content"][0]["text"], instruction)
                self.assertEqual(context.metadata, {"original": True})

    async def test_real_harbor_native_command_builders_keep_loops_and_logs(self):
        for cls, launch, filename in (
            (FrozenCodex, "codex exec ", "codex.txt"),
            (FrozenPi, "pi --print ", "pi.txt"),
        ):
            with self.subTest(agent=cls.__name__):
                self.environment = fake_environment()
                await self.agent(cls).run("finish the task", self.environment, AgentContext())
                launches = [command for command in self.commands() if launch in command]
                self.assertEqual(len(launches), 1)
                self.assertTrue(launches[0].endswith(f"tee /logs/agent/{filename}"))
                self.assertIn("2>&1", launches[0])
                self.assertFalse(any("npm install" in command for command in self.commands()))
                if cls is FrozenPi:
                    self.assertNotIn("--mcp-config", launches[0])
                    self.assertTrue(all(
                        "PI_MCP_CONFIG_MODE" not in (call.kwargs["env"] or {})
                        for call in self.environment.exec.await_args_list
                    ))

    async def test_pi_mcp_native_run_keeps_extension_configuration_and_turn_budget(self):
        agent = self.agent(FrozenPi, version=PI_VERSION, mcp_servers=[self.mcp()], max_turns=4)
        uploads = {}
        async def upload(source, target):
            uploads[target] = Path(source).read_text()
        self.environment.upload_file.side_effect = upload
        await agent.run("use tools", self.environment, AgentContext())
        execution = next(
            call.kwargs for call in self.environment.exec.await_args_list
            if "pi --print " in call.kwargs["command"]
        )
        launch = execution["command"]
        self.assertIn("--extension " + PI_MCP_EXTENSION, launch)
        self.assertIn("--extension /tmp/harbor-pi-agent/max-turns.ts", launch)
        self.assertIn("PI_CODING_AGENT_DIR=/tmp/harbor-pi-agent", launch)
        self.assertEqual(PI_MCP_CONFIG, "/tmp/harbor-pi-agent/mcp.json")
        tokens = shlex.split(launch)
        self.assertEqual(tokens.count("--mcp-config"), 1)
        self.assertEqual(tokens[tokens.index("--mcp-config") + 1], PI_MCP_CONFIG)
        self.assertEqual(execution["env"]["PI_MCP_CONFIG_MODE"], "exclusive")
        self.assertTrue(all(
            call.kwargs["env"]["PI_MCP_CONFIG_MODE"] == "exclusive"
            for call in self.environment.exec.await_args_list
        ))
        config = json.loads(uploads[PI_MCP_CONFIG])
        self.assertEqual(config["mcpServers"]["tools"]["url"], "https://mcp.invalid")
        self.assertIn("const maxTurns = 4", uploads["/tmp/harbor-pi-agent/max-turns.ts"])
        self.assertIn("ctx.abort();", uploads["/tmp/harbor-pi-agent/max-turns.ts"])
        self.assertIn("/tmp/harbor-pi-agent/models.json", uploads)
        self.assertFalse(any("npm install" in command for command in self.commands()))

        frozen_uploads = uploads.copy()
        frozen_env = execution["env"].copy()
        uploads.clear()
        self.environment = fake_environment()
        self.environment.upload_file.side_effect = upload
        native = self.agent(Pi, version=PI_VERSION, mcp_servers=[self.mcp()], max_turns=4)
        await native.run("use tools", self.environment, AgentContext())
        native_execution = next(
            call.kwargs for call in self.environment.exec.await_args_list
            if "pi --print " in call.kwargs["command"]
        )
        self.assertEqual(
            launch.replace(shlex.join(["--mcp-config", PI_MCP_CONFIG]) + " ", "", 1),
            native_execution["command"],
        )
        self.assertEqual(frozen_env.pop("PI_MCP_CONFIG_MODE"), "exclusive")
        self.assertEqual(frozen_env, native_execution["env"])
        models_path = "/tmp/harbor-pi-agent/models.json"
        frozen_models = json.loads(frozen_uploads.pop(models_path))
        native_models = json.loads(uploads.pop(models_path))
        self.assertEqual(frozen_uploads, uploads)
        # Bailian rejects the developer role; only the frozen endpoint disables it.
        model = frozen_models["providers"]["harbor-endpoint"]["models"][0]
        self.assertEqual(model.pop("compat"), {"supportsDeveloperRole": False})
        self.assertEqual(frozen_models, native_models)

    def test_pi_cli_flags_preserve_parent_flags_and_quote_mcp_config(self):
        for servers in ([], [self.mcp()]):
            for parent_flags in ("", "--thinking high"):
                with self.subTest(mcp=bool(servers), flags=parent_flags):
                    agent = self.agent(FrozenPi, mcp_servers=servers)
                    path = "/tmp/config with 'quotes'/mcp.json"
                    with patch.object(Pi, "build_cli_flags", return_value=parent_flags) as build:
                        with patch("hbench.agents.PI_MCP_CONFIG", path):
                            flags = agent.build_cli_flags()
                    build.assert_called_once_with()
                    if servers:
                        self.assertEqual(
                            flags,
                            (parent_flags + " " if parent_flags else "")
                            + shlex.join(["--mcp-config", path]),
                        )
                        self.assertEqual(shlex.split(flags)[-2:], ["--mcp-config", path])
                    else:
                        self.assertIs(flags, parent_flags)
                        self.assertNotIn("--mcp-config", flags)

    async def test_pi_mcp_exec_copies_env_and_overrides_caller_mode(self):
        agent = self.agent(FrozenPi, mcp_servers=[self.mcp()])
        for env in (None, {}, {"A": "b"}, {"A": "b", "PI_MCP_CONFIG_MODE": "merge"}):
            with self.subTest(env=env):
                original = env.copy() if env is not None else None
                expected = result("stdout", stderr="stderr")
                with patch.object(Pi, "exec_as_agent", new_callable=AsyncMock) as execute:
                    execute.return_value = expected
                    actual = await agent.exec_as_agent(
                        self.environment, "mkdir -p /tmp/harbor-pi-agent",
                        env, "/work", 42,
                    )
                    execute.assert_awaited_once_with(
                        self.environment, "mkdir -p /tmp/harbor-pi-agent",
                        env={**(env or {}), "PI_MCP_CONFIG_MODE": "exclusive"},
                        cwd="/work", timeout_sec=42,
                    )
                    self.assertIsNot(execute.await_args.kwargs["env"], env)
                    self.assertIs(actual, expected)
                self.assertEqual(env, original)

    async def test_pi_mcp_exec_propagates_parent_error_unchanged(self):
        agent = self.agent(FrozenPi, mcp_servers=[self.mcp()])
        env = {"A": "b", "PI_MCP_CONFIG_MODE": "merge"}
        error = TimeoutError("native execution timed out")
        with patch.object(Pi, "exec_as_agent", new_callable=AsyncMock) as execute:
            execute.side_effect = error
            with self.assertRaises(TimeoutError) as raised:
                await agent.exec_as_agent(
                    self.environment, "pi --print task", env=env, cwd="/work", timeout_sec=42
                )
            self.assertIs(raised.exception, error)
            execute.assert_awaited_once_with(
                self.environment, "pi --print task",
                env={"A": "b", "PI_MCP_CONFIG_MODE": "exclusive"},
                cwd="/work", timeout_sec=42,
            )
        self.assertEqual(env, {"A": "b", "PI_MCP_CONFIG_MODE": "merge"})

    async def test_pi_non_mcp_exec_passes_env_unchanged_including_none(self):
        agent = self.agent(FrozenPi)
        for env in (None, {}, {"A": "b", "PI_MCP_CONFIG_MODE": "merge"}):
            with self.subTest(env=env):
                with patch.object(Pi, "exec_as_agent", new_callable=AsyncMock) as execute:
                    actual = await agent.exec_as_agent(self.environment, "pi --print task", env=env)
                    execute.assert_awaited_once_with(
                        self.environment, "pi --print task", env=env, cwd=None, timeout_sec=None
                    )
                    self.assertIs(execute.await_args.kwargs["env"], env)
                    self.assertIs(actual, execute.return_value)

    async def test_native_exec_preserves_command_options_and_stdout_stderr(self):
        for cls, native, launch, filename, servers in (
            (FrozenCodex, Codex, "codex exec --json task", "codex.txt", []),
            (FrozenPi, Pi, "pi --print --mode json task", "pi.txt", []),
            (FrozenPi, Pi, "pi --print --mode json task", "pi.txt", [self.mcp()]),
        ):
            with self.subTest(agent=cls.__name__, mcp=bool(servers)):
                if cls is FrozenCodex:
                    self.assertIs(cls.exec_as_agent, native.exec_as_agent)
                else:
                    self.assertIsNot(cls.exec_as_agent, native.exec_as_agent)
                agent = self.agent(
                    cls, environment_logs_dir=PurePosixPath("/logs/user-agent"),
                    mcp_servers=servers,
                )
                command = launch + f" | tee /logs/user-agent/{filename}"
                expected = result("native stdout", stderr="native stderr")
                self.environment.exec.reset_mock(side_effect=True)
                self.environment.exec.return_value = expected
                actual = await agent.exec_as_agent(
                    self.environment, command, env={"A": "b"}, cwd="/work", timeout_sec=42
                )
                self.environment.exec.assert_awaited_once_with(
                    command="set -o pipefail; " + command, user=None,
                    env={"A": "b", **({"PI_MCP_CONFIG_MODE": "exclusive"} if servers else {})},
                    cwd="/work", timeout_sec=42,
                )
                self.assertIs(actual, expected)
                self.assertEqual((actual.stdout, actual.stderr), ("native stdout", "native stderr"))

    async def test_native_auth_and_rate_limit_failures_keep_harbor_classification(self):
        for cls, launch, servers in (
            (FrozenCodex, "codex exec ", []),
            (FrozenPi, "pi --print ", []),
            (FrozenPi, "pi --print ", [self.mcp()]),
        ):
            for message, error in (
                ("Invalid API key", AgentAuthenticationError),
                ("Too many requests: rate limit exceeded", ApiRateLimitError),
                ("CLI failed", NonZeroAgentExitCodeError),
            ):
                for stream in ("stdout", "stderr"):
                    with self.subTest(
                        agent=cls.__name__, error=error.__name__, stream=stream, mcp=bool(servers)
                    ):
                        self.environment = fake_environment()
                        original = self.environment.exec.side_effect
                        output = json.dumps({"type": "turn.failed", "error": {"message": message}}) + "\n"

                        def execute(command, **kwargs):
                            if launch in command:
                                # The native CLI merges stderr into tee's stdout; model
                                # the old suppression so this catches the classification bug.
                                if stream == "stdout":
                                    return result("" if command.endswith(" >/dev/null") else output, 1)
                                return result("", 1, output)
                            return original(command, **kwargs)

                        self.environment.exec.side_effect = execute
                        with self.assertRaises(error) as raised:
                            await self.agent(cls, mcp_servers=servers).run(
                                "finish", self.environment, AgentContext()
                            )
                        self.assertIn(message, str(raised.exception))
                        native_command = next(command for command in self.commands() if launch in command)
                        self.assertFalse(native_command.endswith(" >/dev/null"))

    def test_native_post_run_usage_implementations_are_not_replaced(self):
        self.assertIs(FrozenCodex.populate_context_post_run, Codex.populate_context_post_run)
        self.assertIs(FrozenPi.populate_context_post_run, Pi.populate_context_post_run)
        self.assertFalse(FrozenCodex.capabilities.bridges)

    def test_pi_native_usage_is_backfilled_without_adapter_changes(self):
        agent = self.agent(FrozenPi)
        (self.logs / "pi.txt").write_text(json.dumps({
            "type": "message_end", "message": {"role": "assistant", "usage": {
                "input": 100, "output": 10, "cacheRead": 20, "cacheWrite": 5,
                "cost": {"total": 0.25},
            }},
        }) + "\n")
        context = AgentContext()
        agent.populate_context_post_run(context)
        self.assertEqual((context.n_input_tokens, context.n_output_tokens, context.n_cache_tokens), (125, 10, 20))
        self.assertEqual(context.cost_usd, 0.25)

    def test_codex_native_sessions_backfill_usage_and_keep_atif_conversion(self):
        session_dir = self.logs / "sessions" / "2026" / "11" / "12"
        session_dir.mkdir(parents=True)
        usage = {"input_tokens": 100, "output_tokens": 10, "cached_input_tokens": 30, "total_tokens": 110}
        events = [
            {"type": "session_meta", "payload": {"id": "fixture-session", "cli_version": CODEX_VERSION}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "task"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": usage, "total_token_usage": usage}}},
        ]
        (session_dir / "rollout-fixture.jsonl").write_text("\n".join(json.dumps(event) for event in events) + "\n")
        agent = self.agent(FrozenCodex)
        context = AgentContext()
        with patch.object(agent, "_compute_cost_from_pricing", return_value=0.5):
            agent.populate_context_post_run(context)
        self.assertEqual((context.n_input_tokens, context.n_output_tokens, context.n_cache_tokens), (100, 10, 30))
        self.assertEqual(context.cost_usd, 0.5)
        self.assertTrue((self.logs / "trajectory.json").is_file())

    async def test_pi_keeps_native_requirement_for_custom_endpoint_api_format(self):
        with self.assertRaisesRegex(ValueError, "base-url overrides"):
            await self.agent(FrozenPi, model_api_format=None).run(
                "test", self.environment, AgentContext()
            )
        self.environment.exec.assert_not_awaited()

    async def test_java_writes_request_as_agent_and_passes_model_and_harness_options(self):
        agent = self.agent(
            AgentScopeJava, extra_env={"MODEL_API_KEY": "java-key"}, max_turns=7,
            temperature=0.4, max_tokens=2048, extra_body={"enable_thinking": True, "seed": 8},
            turn_timeout_seconds=123, system_prompt="system text", mcp_servers=[self.mcp()],
            context_window_tokens=131072,
        )
        agent.session_id = "harbor-session"
        context = AgentContext(metadata={"existing": True})
        await agent.run("request with 'quotes'", self.environment, context)
        self.environment.upload_file.assert_not_awaited()
        request_write = next(
            call.kwargs for call in self.environment.exec.await_args_list
            if "printf '%s'" in call.kwargs["command"]
        )
        self.assertEqual(request_write["command"], (
            "set -o pipefail; umask 077; "
            "printf '%s' \"$HB_PRIVATE_CONFIG_SECRET\" > /logs/agent/request.json"
        ))
        self.assertIsNone(request_write["user"])
        self.assertEqual(
            request_write["env"],
            {"HB_PRIVATE_CONFIG_SECRET": (self.logs / "request.json").read_text()},
        )
        validate_request(json.loads((self.logs / "request.json").read_text()))
        execution = next(call for call in self.environment.exec.await_args_list if JAVA_CLI in call.kwargs["command"])
        command = execution.kwargs["command"]
        self.assertIn(JAVA_CLI, command)
        self.assertEqual(shlex.split(command)[-3:], ["/logs/agent/request.json", "/logs/agent/native.jsonl", "/workspace with spaces"])
        env = execution.kwargs["env"]
        self.assertEqual(env["MODEL_API_KEY"], "java-key")
        self.assertNotIn("DASHSCOPE_API_KEY", env)
        self.assertEqual(env["MODEL_ID"], "mock-model")
        self.assertEqual(env["MODEL_BASE_URL"], "https://model.invalid/v1")
        self.assertEqual(env["MAX_TURNS"], "7")
        self.assertEqual(env["EVAL_MODEL_TEMPERATURE"], "0.4")
        self.assertEqual(env["EVAL_MODEL_MAX_TOKENS"], "2048")
        self.assertEqual(json.loads(env["EVAL_MODEL_EXTRA_BODY"]), {"enable_thinking": True, "seed": 8})
        self.assertEqual(env["EVAL_SYSTEM_PROMPT"], "system text")
        self.assertEqual(env["EVAL_SESSION_ID"], "harbor-session")
        self.assertEqual(env["EVAL_USER_ID"], "harbor-session")
        self.assertEqual(env["EVAL_STATE_DIR"], "/logs/agent/workspace-state")
        self.assertEqual(env["EVAL_SKILLS_DIR"], "")
        self.assertEqual(env["TURN_TIMEOUT_SECONDS"], "123")
        self.assertEqual(env["EVAL_MODEL_CONTEXT_WINDOW"], "131072")
        self.assertEqual(json.loads(env["EVAL_MCP_SERVERS"])[0]["name"], "tools")
        self.assertIsNone(execution.kwargs["timeout_sec"])
        self.assertIsNone(execution.kwargs["user"])
        self.assertEqual(context.metadata, {"existing": True, "native_log": "native.jsonl", "atif_supported": False})

    async def test_java_defaults_disable_thinking_and_dashscope_key_is_supported(self):
        agent = self.agent(AgentScopeJava, extra_env={"DASHSCOPE_API_KEY": "dashscope-key"})
        await agent.run("test", self.environment, AgentContext())
        env = next(call.kwargs["env"] for call in self.environment.exec.await_args_list if JAVA_CLI in call.kwargs["command"])
        self.assertEqual(env["MODEL_API_KEY"], "dashscope-key")
        self.assertEqual(json.loads(env["EVAL_MODEL_EXTRA_BODY"]), {"enable_thinking": False})
        self.assertEqual(env["EVAL_MODEL_MAX_TOKENS"], "16384")
        self.assertEqual(env["EVAL_MODEL_TEMPERATURE"], "")
        self.assertNotIn("MAX_TURNS", env)
        self.assertEqual(env["TURN_TIMEOUT_SECONDS"], "0")
        self.assertEqual(env["EVAL_DISABLE_MEMORY_HOOKS"], "true")
        self.assertEqual(env["EVAL_MCP_SERVERS"], "[]")

    async def test_java_temperature_none_and_empty_extra_body_are_explicit_opt_outs(self):
        await self.agent(AgentScopeJava, temperature=None, extra_body={}).run(
            "test", self.environment, AgentContext()
        )
        env = next(call.kwargs["env"] for call in self.environment.exec.await_args_list if JAVA_CLI in call.kwargs["command"])
        self.assertEqual(env["EVAL_MODEL_TEMPERATURE"], "")
        self.assertEqual(env["EVAL_MODEL_EXTRA_BODY"], "{}")

    async def test_java_resolves_mcp_headers_without_logging_or_archiving_credentials(self):
        secret = "private-mcp-secret"
        literal = "literal-header-secret"
        agent = self.agent(
            AgentScopeJava,
            mcp_servers=[self.mcp(transport="streamable-http", headers={
                "Authorization": "Bearer ${MCP_TOKEN}", "X-Private": literal,
            })],
            extra_env={"OPENAI_API_KEY": "test-key", "MCP_TOKEN": secret},
        )
        with self.assertLogs(agent.logger, level="DEBUG") as captured:
            await agent.run("mcp", self.environment, AgentContext())
        self.environment.upload_file.assert_not_awaited()
        writes = [call.kwargs for call in self.environment.exec.await_args_list if "printf '%s'" in call.kwargs["command"]]
        self.assertEqual(len(writes), 2)
        private_write = writes[1]
        target = shlex.split(private_write["command"])[-1]
        self.assertTrue(target.startswith("/tmp/harbor-agentscope-java-mcp-"))
        self.assertTrue(target.endswith("/mcp.json"))
        self.assertIn("umask 077; set -C;", private_write["command"])
        config = json.loads(private_write["env"]["HB_PRIVATE_CONFIG_SECRET"])
        self.assertEqual(config[0]["headers"], {"Authorization": "Bearer " + secret, "X-Private": literal})
        launch = next(call.kwargs for call in self.environment.exec.await_args_list if JAVA_CLI in call.kwargs["command"])
        self.assertIn(f'EVAL_MCP_SERVERS="$(cat {target})"', launch["command"])
        self.assertNotIn("EVAL_MCP_SERVERS", launch["env"])
        self.assertNotIn("HB_PRIVATE_CONFIG_SECRET", launch["env"])
        self.assertIn(
            f"set -o pipefail; rm -f -- {target} && rmdir -- {str(Path(target).parent)}",
            self.commands(),
        )
        self.assertTrue(all(call.kwargs["user"] is None for call in self.environment.exec.await_args_list))
        for value in (secret, literal):
            self.assertNotIn(value, repr(self.commands()))
            self.assertNotIn(value, repr([record.__dict__ for record in captured.records]))
            self.assertNotIn(value, "\n".join(captured.output))
            self.assertFalse(any(value in path.read_text() for path in self.logs.iterdir() if path.is_file()))
        write_log = next(record for record in captured.records if "printf '%s'" in record.getMessage() and "/mcp.json" in record.getMessage())
        self.assertNotIn(secret, write_log.env["HB_PRIVATE_CONFIG_SECRET"])

    async def test_java_missing_mcp_header_variable_fails_before_exec(self):
        agent = self.agent(AgentScopeJava, mcp_servers=[self.mcp(headers={"Authorization": "Bearer ${UNSET_HBENCH_MCP_TOKEN}"})])
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ValueError):
                await agent.run("test", self.environment, AgentContext())
        self.environment.exec.assert_not_awaited()

    async def test_java_http_mcp_private_file_cleanup_on_process_failure(self):
        agent = self.agent(
            AgentScopeJava, mcp_servers=[self.mcp(headers={"Authorization": "Bearer ${TOKEN}"})],
            extra_env={"OPENAI_API_KEY": "test-key", "TOKEN": "private-token"},
        )
        original = self.environment.exec.side_effect
        def execute(command, **kwargs):
            if JAVA_CLI in command:
                return result("", 1, "CLI failed")
            return original(command, **kwargs)
        self.environment.exec.side_effect = execute
        with self.assertRaises(NonZeroAgentExitCodeError):
            await agent.run("test", self.environment, AgentContext())
        self.assertTrue(self.commands()[-1].startswith("set -o pipefail; rm -f -- /tmp/harbor-agentscope-java-mcp-"))
        self.assertIn(" && rmdir -- ", self.commands()[-1])

    @unittest.skipIf(os.getuid() == 0, "requires a local implicit non-root agent UID")
    async def test_java_implicit_nonroot_owns_private_files_and_cleans_up_even_on_write_failure(self):
        secret = "nonroot-header-secret-'quoted'\\n$not_expanded"
        instruction = "request 'quoted' 中文\n$not_expanded; `not-executed`"
        for failure in (None, "launch", "config"):
            with self.subTest(failure=failure):
                remote_logs = self.logs / f"remote logs {failure}"
                observed = {}
                agent = self.agent(
                    AgentScopeJava, environment_logs_dir=PurePosixPath(str(remote_logs)),
                    mcp_servers=[self.mcp(headers={"Authorization": "Bearer ${TOKEN}"})],
                    extra_env={"OPENAI_API_KEY": "test-key", "TOKEN": secret},
                )

                def execute(command, user=None, env=None, cwd=None, timeout_sec=None):
                    self.assertIsNone(user)
                    if JAVA_CLI in command:
                        self.assertIsNone(timeout_sec)
                        request = json.loads((remote_logs / "request.json").read_text())
                        validate_request(request)
                        self.assertEqual(request["events"][0]["content"][0]["text"], instruction)
                        config = json.loads(observed["private_path"].read_text())
                        self.assertEqual(config[0]["headers"]["Authorization"], "Bearer " + secret)
                        observed["launched"] = True
                        return result("", 1, "CLI failed") if failure == "launch" else result()
                    # Run only local file operations as the current non-root UID;
                    # the Java/model launch above is always a fake.
                    completed = subprocess.run(
                        ["bash", "-c", command], cwd=cwd or self.logs,
                        env={**os.environ, **(env or {})}, text=True, capture_output=True,
                    )
                    if "printf '%s'" in command and completed.returncode == 0:
                        path = Path(shlex.split(command)[-1])
                        self.assertEqual(path.stat().st_uid, os.getuid())
                        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                        self.assertEqual((completed.stdout, completed.stderr), ("", ""))
                        if path.name == "mcp.json":
                            observed["private_path"] = path
                            self.assertEqual(path.parent.stat().st_uid, os.getuid())
                            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
                            if failure == "config":
                                # Simulate failure after a partial credential write.
                                return result("", 1, "config write failed")
                    return result(completed.stdout, completed.returncode, completed.stderr)

                environment = SimpleNamespace(
                    exec=AsyncMock(side_effect=execute), upload_file=AsyncMock(), default_user=None,
                )
                with self.assertLogs(agent.logger, level="DEBUG") as captured:
                    if failure:
                        with self.assertRaises(NonZeroAgentExitCodeError):
                            await agent.run(instruction, environment, AgentContext())
                    else:
                        await agent.run(instruction, environment, AgentContext())
                environment.upload_file.assert_not_awaited()
                self.assertEqual(observed.get("launched", False), failure != "config")
                self.assertFalse(observed["private_path"].exists())
                self.assertFalse(observed["private_path"].parent.exists())
                self.assertEqual((remote_logs / "request.json").stat().st_uid, os.getuid())
                # Check the stable marker too: JSON/repr escaping must not hide a leak.
                marker = "nonroot-header-secret"
                self.assertNotIn(marker, repr([record.__dict__ for record in captured.records]))
                self.assertNotIn(marker, "\n".join(captured.output))
                self.assertTrue(all(
                    marker not in call.kwargs["command"] for call in environment.exec.await_args_list
                ))
                self.assertFalse(any(
                    marker in path.read_text() for path in self.logs.rglob("*") if path.is_file()
                ))

    async def test_java_config_writer_does_not_clobber_existing_files_or_symlinks(self):
        existing = self.logs / "existing.json"
        existing.write_text("original")
        link = self.logs / "config link.json"
        link.symlink_to(existing)

        def execute(command, **kwargs):
            completed = subprocess.run(
                ["bash", "-c", command], env={**os.environ, **kwargs["env"]},
                text=True, capture_output=True,
            )
            return result(completed.stdout, completed.returncode, completed.stderr)

        self.environment.exec.side_effect = execute
        agent = self.agent(AgentScopeJava)
        for target in (existing, link):
            with self.subTest(target=target), self.assertRaises(NonZeroAgentExitCodeError):
                await agent._write_agent_config_text(
                    self.environment, content="replacement-secret", remote_path=str(target)
                )
            self.assertEqual(existing.read_text(), "original")
        self.assertTrue(link.is_symlink())
        self.environment.upload_file.assert_not_awaited()

    async def test_java_rejects_unsupported_formats_before_model_exec(self):
        self.assertEqual(AgentScopeJava.MODEL_CONNECTION.supported_api_formats, [ModelApiFormat.OPENAI_CHAT_COMPLETIONS])
        for api_format in (ModelApiFormat.OPENAI_RESPONSES, ModelApiFormat.ANTHROPIC_MESSAGES):
            with self.assertRaisesRegex(ValueError, "Unsupported API format"):
                await self.agent(AgentScopeJava, model_api_format=api_format).run(
                    "test", self.environment, AgentContext()
                )
        self.environment.exec.assert_not_awaited()

    async def test_java_requires_model_key_and_does_not_claim_skills(self):
        for kwargs, message in (
            ({"model_name": None}, "model name"),
            ({"extra_env": {}}, "model API key"),
            ({"skills_dir": "/remote/skills"}, "skills uploads"),
        ):
            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaisesRegex(ValueError, message):
                    await self.agent(AgentScopeJava, **kwargs).run("test", self.environment, AgentContext())
        self.environment.exec.assert_not_awaited()
        self.assertTrue(AgentScopeJava.capabilities.base_url)
        self.assertTrue(AgentScopeJava.capabilities.mcp_servers)
        self.assertFalse(AgentScopeJava.capabilities.skills)
        self.assertFalse(AgentScopeJava.capabilities.atif)
        self.assertTrue(issubclass(AgentScopeJavaOptions, InstalledAgentOptions))

    def test_java_options_validate_limits_and_unknown_options(self):
        for kwargs in (
            {"max_turns": 0}, {"max_tokens": 0}, {"temperature": -0.1},
            {"temperature": float("nan")}, {"turn_timeout_seconds": -1},
            {"extra_body": []}, {"model_api": "openai-responses"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.agent(AgentScopeJava, **kwargs)

    async def test_java_non_json_extra_body_is_rejected_before_upload(self):
        agent = self.agent(AgentScopeJava, extra_body={"temperature": float("nan")})
        with self.assertRaises(ValueError):
            await agent.run("test", self.environment, AgentContext())
        self.environment.exec.assert_not_awaited()
        self.environment.upload_file.assert_not_awaited()

    async def test_java_uses_rendered_prompt_in_request(self):
        template = self.logs / "prompt.j2"
        template.write_text("prefix {{ instruction }} suffix")
        agent = self.agent(AgentScopeJava, prompt_template_path=str(template))
        await agent.run("task", self.environment, AgentContext())
        request = json.loads((self.logs / "request.json").read_text())
        self.assertEqual(request["events"][0]["content"][0]["text"], "prefix task suffix")

    def write_native(self, events):
        (self.logs / "native.jsonl").write_text("\n".join(json.dumps(event) for event in events) + "\n")

    def test_java_native_usage_metadata_and_raw_output_are_preserved(self):
        self.write_native([
            {"type": "eval.binding", "session_id": "native-session", "profile": "local-project-writable", "tools": ["read"],
             "adapter_version": "1.4.0", "identity_prompt_version": "coding-agent-v1", "identity_prompt_sha256": "abc"},
            {"type": "eval.model_end", "id": "a", "usage": {"inputTokens": 100, "outputTokens": 20, "cachedTokens": 30, "cacheCreationTokens": 4, "reasoningTokens": 5}},
            {"type": "MODEL_RESPONSE", "usage": {"inputTokens": 999}},
            {"type": "eval.model_end", "id": "b", "usage": {"inputTokens": 40, "outputTokens": 10, "cachedTokens": 8}},
            {"type": "eval.completed"},
        ])
        original = (self.logs / "native.jsonl").read_bytes()
        context = AgentContext(metadata={"existing": True})
        self.agent(AgentScopeJava).populate_context_post_run(context)
        self.assertEqual((context.n_input_tokens, context.n_output_tokens, context.n_cache_tokens), (140, 30, 38))
        self.assertIsNone(context.cost_usd)
        self.assertEqual(context.metadata["native_status"], "completed")
        self.assertEqual(context.metadata["native_binding"]["session_id"], "native-session")
        self.assertEqual(context.metadata["native_binding"]["adapter_version"], "1.4.0")
        self.assertEqual(context.metadata["native_binding"]["identity_prompt_sha256"], "abc")
        self.assertEqual(context.metadata["cache_creation_tokens"], 4)
        self.assertEqual(context.metadata["reasoning_tokens"], 5)
        self.assertTrue(context.metadata["existing"])
        self.assertTrue(context.metadata["native_log_preserved"])
        self.assertFalse(context.metadata["atif_supported"])
        self.assertEqual((self.logs / "native.jsonl").read_bytes(), original)
        self.assertFalse((self.logs / "trajectory.json").exists())

    def test_java_duplicate_usage_is_not_counted_twice_and_reparse_is_idempotent(self):
        event = {"type": "eval.model_end", "id": "call", "usage": {"inputTokens": 10, "outputTokens": 2, "cachedTokens": 3}}
        self.write_native([event, event, {"type": "eval.completed"}])
        context = AgentContext()
        agent = self.agent(AgentScopeJava)
        agent.populate_context_post_run(context)
        agent.populate_context_post_run(context)
        self.assertEqual((context.n_input_tokens, context.n_output_tokens, context.n_cache_tokens), (10, 2, 3))

    def test_java_iteration_limit_is_a_completed_stop_not_a_failure(self):
        self.write_native([
            {"type": "eval.iteration_limit", "max_iters": 60},
            {"type": "eval.completed", "stopReason": "iteration_limit"},
        ])
        context = AgentContext()
        self.agent(AgentScopeJava).populate_context_post_run(context)
        self.assertEqual(context.metadata["native_status"], "completed")
        self.assertTrue(context.metadata["iteration_limit"])
        self.assertNotIn("native_error_type", context.metadata)

    def test_java_partial_failed_native_log_keeps_usage_and_failure_metadata(self):
        self.write_native([
            {"type": "eval.model_end", "id": "call", "usage": {"inputTokens": 12, "outputTokens": 0, "cachedTokens": 0}},
            {"type": "eval.failed", "error_type": "IllegalStateException", "iteration_limit": True},
        ])
        with (self.logs / "native.jsonl").open("a") as stream:
            stream.write("[]\n{partial")
        context = AgentContext()
        self.agent(AgentScopeJava).populate_context_post_run(context)
        self.assertEqual(context.n_input_tokens, 12)
        self.assertEqual(context.n_output_tokens, 0)
        self.assertEqual(context.metadata["native_status"], "failed")
        self.assertEqual(context.metadata["native_error_type"], "IllegalStateException")
        self.assertTrue(context.metadata["iteration_limit"])
        self.assertEqual(context.metadata["native_parse_errors"], 2)

    def test_java_missing_or_unknown_usage_is_not_fabricated(self):
        context = AgentContext()
        agent = self.agent(AgentScopeJava)
        agent.populate_context_post_run(context)
        self.assertFalse(context.metadata["native_log_preserved"])
        self.assertIsNone(context.n_input_tokens)
        self.write_native([
            {"type": "eval.model_end", "usage": {}},
            {"type": "eval.model_end", "usage": {"inputTokens": True, "outputTokens": -1, "cachedTokens": "2"}},
            {"type": "eval.completed"},
        ])
        agent.populate_context_post_run(context)
        self.assertIsNone(context.n_input_tokens)
        self.assertIsNone(context.n_output_tokens)
        self.assertIsNone(context.cost_usd)

    def test_agent_context_uses_installed_harbor_fields(self):
        self.assertTrue({"n_input_tokens", "n_output_tokens", "n_cache_tokens", "cost_usd", "metadata"}.issubset(AgentContext.model_fields))
        self.assertNotIn("usage", AgentContext.model_fields)

    def test_record_request_rejects_non_text(self):
        with self.assertRaises(TypeError):
            record_request(self.agent(FrozenPi), {"content": "bad"})


class RuntimeDockerfileTests(unittest.TestCase):
    def test_config_pins_and_readme_tables_match_frozen_versions(self):
        self.assertEqual((CODEX_VERSION, PI_VERSION, PI_MCP_ADAPTER_VERSION), ("0.167.2", "1.5.2", "7.0.1"))
        for name, version in (("codex", CODEX_VERSION), ("pi", PI_VERSION)):
            with self.subTest(config=name):
                config = yaml.safe_load((ROOT / f"configs/harnesses/{name}.yaml").read_text())
                self.assertEqual(config["kwargs"]["version"], version)
        for filename in ("README.md", "README_zh.md"):
            readme = (ROOT / filename).read_text()
            for harness, version in (
                ("Codex", CODEX_VERSION), ("Pi", PI_VERSION),
                ("AgentScope Java", AGENTSCOPE_CLI_VERSION),
            ):
                with self.subTest(readme=filename, harness=harness):
                    self.assertEqual(
                        re.findall(rf"^\| {re.escape(harness)} \| `([^`]+)` \|", readme, re.MULTILINE),
                        [version],
                    )
            self.assertIn(f"`pi-mcp-adapter` `{PI_MCP_ADAPTER_VERSION}`", readme)
            self.assertIn("Node >=22.19.0", readme)
            self.assertIn("`>=1.0.0,<2.0.0`", readme)

    def test_harness_profiles_share_no_step_cap_and_no_temperature(self):
        profiles = {
            name: yaml.safe_load((ROOT / f"configs/harnesses/{name}.yaml").read_text())["kwargs"]
            for name in ("agentscope-java", "pi", "codex")
        }
        for name, kwargs in profiles.items():
            with self.subTest(harness=name):
                self.assertNotIn("max_turns", kwargs)
                self.assertNotIn("temperature", kwargs)
        self.assertEqual(profiles["pi"]["reasoning_effort"], "off")
        self.assertEqual(profiles["codex"]["reasoning_effort"], "none")
        self.assertFalse(profiles["agentscope-java"]["extra_body"]["enable_thinking"])
        self.assertEqual(profiles["agentscope-java"]["turn_timeout_seconds"], 0)
        self.assertTrue(profiles["agentscope-java"]["disable_memory_hooks"])

    def test_runtime_installs_version_checks_and_labels_match_frozen_versions(self):
        dockerfile = (ROOT / "environments/hbench/Dockerfile.runtime").read_text()
        logical = dockerfile.replace("\\\n", " ")
        installs = [shlex.split(args) for args in re.findall(r"\bnpm install ([^&\n]+)", logical)]
        self.assertEqual(installs, [
            ["-g", "@openai/codex@" + CODEX_VERSION, "@earendil-works/pi-coding-agent@" + PI_VERSION],
            ["--prefix", "/opt/pi-mcp-adapter", "--ignore-scripts",
             "pi-mcp-adapter@" + PI_MCP_ADAPTER_VERSION, "@earendil-works/pi-coding-agent@" + PI_VERSION],
        ])
        checks = [
            (" ".join(command.split()), version)
            for command, version in re.findall(r"test \"\$\((.*?)\)\" = '([^']+)'", logical)
        ]
        self.assertEqual(checks, [
            ("codex --version", "codex-cli " + CODEX_VERSION),
            ("pi --version", PI_VERSION),
            ("node -p \"require('/opt/pi-mcp-adapter/node_modules/pi-mcp-adapter/package.json').version\"",
             PI_MCP_ADAPTER_VERSION),
            (JAVA_CLI + " --version", "agentscope-harness-eval " + AGENTSCOPE_CLI_VERSION),
        ])
        self.assertEqual(re.findall(r'org\.harness-bench\.([\w-]+)\.version="([^"]+)"', dockerfile), [
            ("codex", CODEX_VERSION), ("pi", PI_VERSION),
            ("pi-mcp-adapter", PI_MCP_ADAPTER_VERSION), ("agentscope-cli", AGENTSCOPE_CLI_VERSION),
        ])
        self.assertIn("test -f " + PI_MCP_EXTENSION, dockerfile)

    def test_runtime_checks_node_minimum_before_package_installs(self):
        dockerfile = (ROOT / "environments/hbench/Dockerfile.runtime").read_text()
        assertion = (
            'node -e \'const [major, minor] = process.versions.node.split(".").map(Number); '
            'if (major < 22 || (major === 22 && minor < 19)) '
            'throw new Error("Pi requires Node >=22.19.0")\''
        )
        self.assertIn(assertion, dockerfile)
        self.assertLess(dockerfile.index(assertion), dockerfile.index("npm install"))

    def test_runtime_contains_only_prebuilt_java_artifacts_and_pinned_tools(self):
        dockerfile = (ROOT / "environments/hbench/Dockerfile.runtime").read_text()
        for text in (
            "FROM node:22-bookworm-slim", "FROM eclipse-temurin:21-jdk-jammy",
            "FROM ghcr.io/astral-sh/uv:0.7.13", "FROM python:3.12-slim-bookworm",
            "@openai/codex@" + CODEX_VERSION, "@earendil-works/pi-coding-agent@" + PI_VERSION,
            "pi-mcp-adapter@" + PI_MCP_ADAPTER_VERSION, "--prefix /opt/pi-mcp-adapter",
            "COPY adapters/agentscope-java/target/classes/ /opt/harness/classes/",
            "COPY adapters/agentscope-java/target/dependency/ /opt/harness/dependency/",
            "COPY --from=jdk /opt/java/openjdk /opt/java/openjdk",
            "COPY --from=uv /uv /uvx /usr/local/bin/", "ripgrep procps",
            "ln -s /opt/pi-mcp-adapter /tmp/harbor-pi-mcp-adapter",
        ):
            self.assertIn(text, dockerfile)
        for forbidden in ("git clone", "@latest", "COPY . ", "COPY .\n", "RUN mvn", "ENTRYPOINT"):
            # Comments may mention the environment's ENTRYPOINT but no instruction is set here.
            executable = "\n".join(line for line in dockerfile.splitlines() if not line.startswith("#"))
            self.assertNotIn(forbidden, executable)

    def test_runtime_run_commands_have_valid_bash_syntax(self):
        dockerfile = (ROOT / "environments/hbench/Dockerfile.runtime").read_text()
        logical_lines = dockerfile.replace("\\\n", " ").splitlines()
        scripts = [line.removeprefix("RUN ") for line in logical_lines if line.startswith("RUN ")]
        self.assertGreaterEqual(len(scripts), 2)
        for script in scripts:
            check = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
            self.assertEqual(check.returncode, 0, check.stderr)


if __name__ == "__main__":
    unittest.main()
