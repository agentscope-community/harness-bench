# Copyright 2024-2026 the original author or authors.
# SPDX-License-Identifier: Apache-2.0
"""Frozen installed agents for Harbor 0.25.0; no trial-time package installs."""

import json
import re
import shlex
from pathlib import Path
from uuid import uuid4

from packaging.specifiers import SpecifierSet
from packaging.version import Version
from pydantic import Field

from harbor.agents.agent_auth_resolution import ModelConnectionSpec, ProviderAccess
from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.agents.installed.codex import Codex, CodexOptions
from harbor.agents.installed.pi import Pi, PiOptions
from harbor.agents.options import InstalledAgentOptions
from harbor.models.model_api_format import ModelApiFormat

_VERSION_PATTERN = r"^\d+\.\d+\.\d+$"
CODEX_VERSION = "0.167.2"
PI_VERSION = "1.5.2"
PI_MCP_ADAPTER_VERSION = "7.0.1"
PI_MCP_VERSIONS = SpecifierSet(">=1.0.0,<2.0.0")
PI_MCP_EXTENSION = "/tmp/harbor-pi-mcp-adapter/node_modules/pi-mcp-adapter/index.ts"
PI_MCP_CONFIG = "/tmp/harbor-pi-agent/mcp.json"
AGENTSCOPE_CLI_VERSION = "1.3.0"
JAVA_CLI = (
    "java -cp '/opt/harness/classes:/opt/harness/dependency/*' "
    "io.agentscope.examples.eval.HarnessEvalCli"
)


def record_request(agent, instruction: str) -> Path:
    """Write the text request accepted by runtime.protocol.validate_request."""
    if not isinstance(instruction, str):
        raise TypeError("instruction must be a string")
    request = {
        "events": [{"type": "user.message", "content": [{"type": "text", "text": instruction}]}]
    }
    path = agent.logs_dir / "request.json"
    path.write_text(json.dumps(request, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _frozen_version(configured: str | None, baked: str) -> str:
    if configured is not None and not re.fullmatch(r"\d+\.\d+\.\d+", configured):
        raise ValueError("Frozen agent version must be an exact version, not latest or a range")
    return configured or baked


async def _check_version(agent, environment, baked: str) -> dict:
    requested = agent.version()
    result = await environment.exec(command=agent.get_version_command(), timeout_sec=30)
    actual = None
    if result.return_code == 0 and (result.stdout or "").strip():
        actual = agent.parse_version(result.stdout)
    evidence = {
        "agent": agent.name(),
        "requested_version": requested,
        "expected_version": baked,
        "actual_version": actual,
    }
    (agent.logs_dir / "version.json").write_text(json.dumps(evidence) + "\n", encoding="utf-8")
    if actual != baked or requested != actual:
        raise RuntimeError(
            f"Baked {agent.name()} version mismatch: baked {baked}, "
            f"requested {requested}, actual {actual}"
        )
    # Prevent BaseInstalledAgent.setup from doing a second version probe.
    agent._version = actual
    return evidence


class FrozenCodexOptions(CodexOptions):
    version: str | None = Field(
        default=CODEX_VERSION, pattern=_VERSION_PATTERN,
        description="Exact CLI version to check against the baked Codex; never installs packages.",
    )


class FrozenPiOptions(PiOptions):
    version: str | None = Field(
        default=PI_VERSION, pattern=_VERSION_PATTERN,
        description="Exact CLI version to check against the baked Pi; never installs packages.",
    )


class FrozenCodex(Codex):
    # The runtime bakes the native CLI, not the separately installed codex-acp bridge.
    capabilities = Codex.capabilities.model_copy(update={"bridges": frozenset()})
    options_model = FrozenCodexOptions

    def version(self):
        return _frozen_version(super().version(), CODEX_VERSION)

    async def install(self, environment):
        await _check_version(self, environment, CODEX_VERSION)

    async def run(self, instruction, environment, context):
        record_request(self, self.render_instruction(instruction))
        await super().run(instruction, environment, context)


class FrozenPi(Pi):
    options_model = FrozenPiOptions

    def version(self):
        return _frozen_version(super().version(), PI_VERSION)

    def _build_custom_models_json(self, access, model_id):
        models_json = super()._build_custom_models_json(access, model_id)
        if models_json is not None:
            for provider in models_json["providers"].values():
                for model in provider["models"]:
                    # Pi sends the OpenAI ``developer`` role whenever a custom
                    # endpoint's model is marked reasoning-capable. Bailian only
                    # accepts system/assistant/user/tool/function and rejects
                    # those requests with HTTP 400.
                    model["compat"] = {"supportsDeveloperRole": False}
        return models_json

    def get_version_command(self):
        return "if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; pi --version"

    def build_cli_flags(self):
        flags = super().build_cli_flags()
        if self.mcp_servers:
            return (flags + " " if flags else "") + shlex.join(["--mcp-config", PI_MCP_CONFIG])
        return flags

    async def exec_as_agent(self, environment, command, env=None, cwd=None, timeout_sec=None):
        if self.mcp_servers:
            # The adapter otherwise merges global, shared, and project MCP configs.
            env = dict(env or {})
            env["PI_MCP_CONFIG_MODE"] = "exclusive"
        return await super().exec_as_agent(
            environment, command, env=env, cwd=cwd, timeout_sec=timeout_sec
        )

    async def install(self, environment):
        if self.mcp_servers and Version(self.version()) not in PI_MCP_VERSIONS:
            raise ValueError(f"Pi MCP adapter requires Pi {PI_MCP_VERSIONS}; got {self.version()}")
        evidence = await _check_version(self, environment, PI_VERSION)
        if self.mcp_servers:
            package_json = str(Path(PI_MCP_EXTENSION).with_name("package.json"))
            result = await environment.exec(
                command=(
                    f"test -f {shlex.quote(PI_MCP_EXTENSION)} && "
                    f"node -p {shlex.quote('require(' + json.dumps(package_json) + ').version')}"
                ),
                timeout_sec=30,
            )
            actual = (result.stdout or "").strip() if result.return_code == 0 else None
            evidence["mcp_adapter_version"] = actual
            evidence["mcp_extension"] = PI_MCP_EXTENSION
            (self.logs_dir / "version.json").write_text(
                json.dumps(evidence) + "\n", encoding="utf-8"
            )
            if actual != PI_MCP_ADAPTER_VERSION:
                raise RuntimeError(
                    f"Baked Pi MCP extension missing or version mismatch: "
                    f"expected {PI_MCP_ADAPTER_VERSION} at {PI_MCP_EXTENSION}, got {actual}"
                )

    async def run(self, instruction, environment, context):
        record_request(self, self.render_instruction(instruction))
        await super().run(instruction, environment, context)


class AgentScopeJavaOptions(InstalledAgentOptions):
    version: str | None = Field(
        default=AGENTSCOPE_CLI_VERSION, pattern=_VERSION_PATTERN,
        description="Exact CLI version to check against the baked Java adapter; never installs packages.",
    )
    max_turns: int = Field(default=60, ge=1)
    temperature: float | None = Field(default=0.0, ge=0, le=2, allow_inf_nan=False)
    max_tokens: int = Field(default=8192, ge=1)
    context_window_tokens: int = Field(
        default=0, ge=0,
        description="Reported model context window in tokens; 0 infers it from the model name.",
    )
    extra_body: dict = Field(default_factory=lambda: {"enable_thinking": False})
    turn_timeout_seconds: int = Field(default=600, ge=1)
    system_prompt: str = ""


class AgentScopeJava(BaseInstalledAgent):
    # Skills are deliberately unsupported: Harbor's skills_dir is remote, not an upload source.
    capabilities = AgentCapabilities(base_url=True, mcp_servers=True, mcp_http_headers=True)
    MODEL_CONNECTION = ModelConnectionSpec(
        default_provider="openai",
        agent_provider_access=ProviderAccess(
            api_key_candidate_names=["MODEL_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_API_KEY"],
            base_url_candidate_names=["MODEL_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_BASE"],
        ),
        api_key_destination_names={"default": "MODEL_API_KEY"},
        base_url_destination_names={"default": "MODEL_BASE_URL"},
        supported_api_formats=[ModelApiFormat.OPENAI_CHAT_COMPLETIONS],
        default_api_format_by_provider={"default": ModelApiFormat.OPENAI_CHAT_COMPLETIONS},
        inject_default_base_url=True,
    )
    options_model = AgentScopeJavaOptions

    @staticmethod
    def name():
        return "agentscope-java"

    def version(self):
        return _frozen_version(super().version(), AGENTSCOPE_CLI_VERSION)

    def get_version_command(self):
        return JAVA_CLI + " --version"

    def parse_version(self, stdout):
        lines = stdout.strip().splitlines()
        return lines[-1].split()[-1] if lines else ""

    async def install(self, environment):
        await _check_version(self, environment, AGENTSCOPE_CLI_VERSION)

    def _mcp_configs(self):
        # Harbor 0.25.0 exposes mcp_servers + mcp_header_env, not resolved_mcp_servers.
        headers = self.mcp_header_env
        configs = []
        for server in self.mcp_servers:
            config = server.model_dump(mode="json", exclude_none=True)
            if server.name in headers.refs:
                config["headers"] = {
                    key: headers.env[variable] for key, variable in headers.refs[server.name].items()
                }
            configs.append(config)
        return configs

    async def _write_agent_config_text(
        self, environment, *, content: str, remote_path: str, overwrite: bool = False
    ):
        # Uploads are root-owned even when the image's implicit USER is non-root.
        # SECRET makes Harbor redact this per-exec value before DEBUG logging.
        # Noclobber by default so a planted file or symlink is never followed.
        # The request path is the exception: record_request() already wrote the same
        # file host-side and it is mounted at /logs/agent, so it must be overwritten.
        guard = "" if overwrite else "set -C; "
        await self.exec_as_agent(
            environment,
            command=(
                "umask 077; " + guard
                + f"printf '%s' \"$HB_PRIVATE_CONFIG_SECRET\" > {shlex.quote(remote_path)}"
            ),
            env={"HB_PRIVATE_CONFIG_SECRET": content},
        )

    @with_prompt_template
    async def run(self, instruction, environment, context):
        if self.skills_dir:
            raise ValueError("AgentScopeJava does not support Harbor skills uploads yet")
        if not self.model_name:
            raise ValueError("AgentScopeJava requires a model name")
        connection = self.model_connection
        if connection.model_api_format != ModelApiFormat.OPENAI_CHAT_COMPLETIONS:
            raise ValueError("AgentScopeJava only supports openai_chat_completions")
        if not connection.api_key:
            raise ValueError("AgentScopeJava requires a model API key")
        if not connection.execution_base_url:
            raise ValueError("AgentScopeJava requires an OpenAI chat-completions base URL")
        # Validate JSON options before writing requests or starting the CLI.
        extra_body = json.dumps(self.options.extra_body, allow_nan=False)
        mcp_configs = self._mcp_configs()
        request = record_request(self, instruction)
        remote_request = str(self.environment_logs_dir / "request.json")
        native = str(self.environment_logs_dir / "native.jsonl")
        await self.exec_as_agent(
            environment, command=f"mkdir -p {shlex.quote(str(self.environment_logs_dir))}"
        )
        await self._write_agent_config_text(
            environment, content=request.read_text(encoding="utf-8"), remote_path=remote_request,
            overwrite=True,
        )
        workdir_result = await self.exec_as_agent(environment, command="pwd")
        workdir = (workdir_result.stdout or "").strip()
        if not workdir.startswith("/") or "\n" in workdir:
            raise RuntimeError("Could not resolve the environment working directory")
        session_id = self.session_id or str(self.context_id or uuid4())
        env = {
            "MODEL_API_KEY": connection.api_key,
            "MODEL_ID": self._parsed_model_name,
            "MODEL_BASE_URL": connection.execution_base_url,
            "MAX_TURNS": str(self.options.max_turns),
            "EVAL_SESSION_ID": session_id,
            "EVAL_USER_ID": session_id,
            "EVAL_STATE_DIR": str(self.environment_logs_dir / "workspace-state"),
            "EVAL_SYSTEM_PROMPT": self.options.system_prompt,
            "EVAL_SKILLS_DIR": "",
            "EVAL_MODEL_EXTRA_BODY": extra_body,
            "EVAL_MODEL_MAX_TOKENS": str(self.options.max_tokens),
            "EVAL_MODEL_TEMPERATURE": (
                "" if self.options.temperature is None else str(self.options.temperature)
            ),
            "TURN_TIMEOUT_SECONDS": str(self.options.turn_timeout_seconds),
        }
        if self.options.context_window_tokens:
            env["EVAL_MODEL_CONTEXT_WINDOW"] = str(self.options.context_window_tokens)
        command = JAVA_CLI + " " + shlex.join([remote_request, native, workdir])
        private_mcp_dir = None
        context.metadata = {
            **(context.metadata or {}),
            "native_log": "native.jsonl",
            "atif_supported": False,
        }
        try:
            if any(server.headers for server in self.mcp_servers):
                # A freshly created private directory prevents path/symlink collisions.
                # Only clean it up after mkdir succeeds, including on a partial write.
                directory = f"/tmp/harbor-agentscope-java-mcp-{uuid4().hex}"
                await self.exec_as_agent(
                    environment, command=f"umask 077; mkdir -- {shlex.quote(directory)}"
                )
                private_mcp_dir = directory
                private_mcp_path = f"{directory}/mcp.json"
                await self._write_agent_config_text(
                    environment, content=json.dumps(mcp_configs), remote_path=private_mcp_path
                )
                command = f'EVAL_MCP_SERVERS="$(cat {shlex.quote(private_mcp_path)})" ' + command
            else:
                env["EVAL_MCP_SERVERS"] = json.dumps(mcp_configs)
            # The CLI enforces a per-turn timeout internally. Harbor owns the
            # whole trial budget; do not cap a multi-turn run to one turn here.
            await self.exec_as_agent(environment, command=command, env=env)
        finally:
            if private_mcp_dir:
                try:
                    await self.exec_as_agent(
                        environment,
                        command=(
                            f"rm -f -- {shlex.quote(private_mcp_dir + '/mcp.json')} && "
                            f"rmdir -- {shlex.quote(private_mcp_dir)}"
                        ),
                    )
                except Exception:
                    self.logger.warning("Could not remove the private Java MCP configuration")

    def populate_context_post_run(self, context):
        """Read native evidence after Harbor downloads logs; no fabricated ATIF or cost."""
        path = self.logs_dir / "native.jsonl"
        metadata = {**(context.metadata or {}), "native_log": "native.jsonl", "atif_supported": False}
        context.metadata = metadata
        metadata["native_log_preserved"] = path.is_file()
        metadata["native_status"] = "incomplete"
        if not path.is_file():
            return
        totals = {key: 0 for key in (
            "inputTokens", "outputTokens", "cachedTokens", "cacheCreationTokens", "reasoningTokens"
        )}
        usage_seen = False
        seen_ids = set()
        parse_errors = 0
        try:
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        parse_errors += 1
                        continue
                    if not isinstance(event, dict):
                        parse_errors += 1
                        continue
                    kind = event.get("type")
                    if kind == "eval.binding":
                        metadata["native_binding"] = {
                            key: event[key] for key in (
                                "session_id", "profile", "candidate_sha", "candidate_source_sha256", "tools"
                            ) if key in event
                        }
                    elif kind in ("eval.completed", "eval.failed"):
                        metadata["native_status"] = kind.removeprefix("eval.")
                        if kind == "eval.failed":
                            metadata["native_error_type"] = event.get("error_type")
                            metadata["iteration_limit"] = event.get("iteration_limit", False)
                    elif kind == "eval.model_end":
                        usage = event.get("usage")
                        if not isinstance(usage, dict):
                            continue
                        call_id = event.get("id")
                        if isinstance(call_id, str):
                            if call_id in seen_ids:
                                continue
                            seen_ids.add(call_id)
                        for key in totals:
                            value = usage.get(key)
                            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                                totals[key] += value
                                usage_seen = True
        except (OSError, UnicodeError):
            self.logger.warning("Could not fully read Java native logs")
            parse_errors += 1
        metadata["native_parse_errors"] = parse_errors
        if usage_seen:
            # ChatUsage.inputTokens already includes cache; never add cachedTokens again.
            context.n_input_tokens = totals["inputTokens"]
            context.n_output_tokens = totals["outputTokens"]
            context.n_cache_tokens = totals["cachedTokens"]
            metadata["cache_creation_tokens"] = totals["cacheCreationTokens"]
            metadata["reasoning_tokens"] = totals["reasoningTokens"]
