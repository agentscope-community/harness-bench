"""Native Harbor adapters using the prebuilt extended runtime."""
import json
import inspect
import shlex
import yaml
from pydantic import BaseModel, Field
from typing import Literal
from harbor.agents.base import BaseAgent
from harbor.agents.agent_auth_resolution import ModelConnectionSpec
from harbor.agents.installed.claude_code import ClaudeCode, ClaudeCodeOptions

from harbor.agents.installed.hermes import Hermes, HermesOptions
from harbor.agents.installed.openclaw import OpenClaw, OpenClawOptions
from harbor.models.model_api_format import ModelApiFormat

from hbench.agents import _probe, record_request
from hbench.registry import harness_registry

EXTENSIONS = "/opt/hbench-runtime/extensions"


class InferenceBudget(BaseModel):
    max_tokens: int = Field(default=16384, ge=1)
    context_window_tokens: int = Field(default=131072, ge=1)


class FrozenClaudeCodeOptions(ClaudeCodeOptions, InferenceBudget):
    pass


class FrozenHermesOptions(HermesOptions, InferenceBudget):
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] = "none"


class FrozenOpenClawOptions(OpenClawOptions, InferenceBudget):
    pass


def _openclaw_export_to_session(events_text):
    """Convert per-event native exports, avoiding the bundle-wide size limit."""
    import json

    messages = []
    for line in events_text.splitlines():
        event = json.loads(line)
        if not isinstance(event, dict) or event.get("source") != "transcript":
            continue
        data = event.get("data")
        message = data.get("message") if isinstance(data, dict) else None
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant", "toolResult"}:
            continue
        messages.append({"type": "message", "id": event.get("entryId"),
                         "timestamp": event.get("ts"), "message": message})
    if not messages:
        raise ValueError("OpenClaw exported no transcript messages")
    return "".join(json.dumps(message) + "\n" for message in messages)


class FrozenNative:
    async def install(self, environment):
        expected = harness_registry()[self.name()]["kwargs"]["version"]
        if self.version() != expected:
            raise ValueError(f"Expected pinned {self.name()} version {expected}")
        result = await _probe(environment, f"cat {EXTENSIONS}/versions.json")
        if result.return_code:
            raise RuntimeError("Extended Harness runtime is missing; build Dockerfile.extended-runtime")
        versions = json.loads(result.stdout)
        if versions.get(self.name()) != expected:
            raise RuntimeError(f"Runtime version mismatch for {self.name()}")
        probe = await _probe(environment, self.get_version_command())
        if probe.return_code:
            raise RuntimeError(f"Cannot execute baked {self.name()}: {probe.stderr or probe.stdout}")
        (self.logs_dir / "version.json").write_text(json.dumps({
            "agent": self.name(), "expected_version": expected,
            "actual_version_output": probe.stdout, "runtime_version": versions[self.name()],
        }) + "\n")
        self._version = expected

    async def run(self, instruction, environment, context):
        record_request(self, self.render_instruction(instruction))
        (self.logs_dir / "inference-settings.json").write_text(json.dumps({
            **self.options.model_dump(include={"max_tokens", "context_window_tokens", "reasoning_effort", "max_thinking_tokens"}),
            "temperature_override": None,
        }) + "\n")
        await super().run(instruction, environment, context)


class FrozenClaudeCode(FrozenNative, ClaudeCode):
    options_model = FrozenClaudeCodeOptions

    async def exec_as_agent(self, environment, command, env=None, **kwargs):
        env = {**(env or {}),
               "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(self.options.max_tokens),
               "CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(self.options.context_window_tokens)}
        if self.options.max_thinking_tokens == 0:
            env["CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING"] = "1"
        return await super().exec_as_agent(environment, command, env=env, **kwargs)


class FrozenHermes(FrozenNative, Hermes):
    capabilities = Hermes.capabilities.model_copy(update={"reasoning_effort": True})
    options_model = FrozenHermesOptions

    def _build_config_yaml(self, model, max_turns=None, *, connection=None):
        config = yaml.safe_load(super()._build_config_yaml(model, max_turns, connection=connection))
        config["agent"] = {"max_turns": max_turns, "reasoning_effort": self.options.reasoning_effort}
        config["model"] = {"default": model, "provider": config["provider"],
                           "context_length": self.options.context_window_tokens}
        for provider in config.get("custom_providers", []):
            provider["context_length"] = self.options.context_window_tokens
            token_field = "max_output_tokens" if provider["api_mode"] == "codex_responses" else "max_tokens"
            provider["extra_body"] = {token_field: self.options.max_tokens}
        return yaml.safe_dump(config, sort_keys=False)
    def get_version_command(self):
        return "hermes --version"

    def _convert_hermes_session_to_atif(self, jsonl_text, session_id):
        trajectory = super()._convert_hermes_session_to_atif(jsonl_text, session_id)
        if trajectory is None:
            return None
        records = []
        for line in jsonl_text.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and "messages" in record and "input_tokens" in record:
                records.append(record)
        if records:
            # Hermes exports usage on the session, not on individual messages.
            # Its input_tokens excludes cache reads/writes; ATIF includes them.
            metrics = trajectory.final_metrics
            metrics.total_prompt_tokens = sum(
                (r.get("input_tokens") or 0) + (r.get("cache_read_tokens") or 0)
                + (r.get("cache_write_tokens") or 0) for r in records
            )
            metrics.total_completion_tokens = sum(r.get("output_tokens") or 0 for r in records)
            metrics.total_cached_tokens = sum(r.get("cache_read_tokens") or 0 for r in records)
            trajectory.session_id = records[0].get("id") or session_id
        return trajectory

    def populate_context_post_run(self, context):
        super().populate_context_post_run(context)
        path = self.logs_dir / "trajectory.json"
        if path.is_file():
            metrics = json.loads(path.read_text()).get("final_metrics") or {}
            context.n_cache_tokens = metrics.get("total_cached_tokens")


class FrozenOpenClaw(FrozenNative, OpenClaw):
    options_model = FrozenOpenClawOptions
    # Benchmark containers have no personal memory or embedding service.
    _DEFAULT_CONFIG = {"memory": {"search": {"enabled": False}}}
    API_MODES = {
        ModelApiFormat.OPENAI_CHAT_COMPLETIONS: "openai-completions",
        ModelApiFormat.OPENAI_RESPONSES: "openai-responses",
        ModelApiFormat.ANTHROPIC_MESSAGES: "anthropic-messages",
    }
    MODEL_CONNECTION = ModelConnectionSpec(
        supported_api_formats=list(API_MODES),
        api_key_destination_names={"default": "HB_OPENCLAW_API_KEY"},
        base_url_destination_names={"default": None},
    )

    @classmethod
    def resolve_model_connection_config(cls, model_name, resolve_env, **kwargs):
        # Harbor's native OpenClaw adapter only accepts provider-specific config.
        # Translate the shared benchmark connection into an isolated provider.
        return BaseAgent.resolve_model_connection_config.__func__(
            cls, model_name, resolve_env, **kwargs
        )

    def _merge_model_connection(self, cfg):
        access = self.model_connection
        model_id = self._parse_model_name(self.model_name)[1]
        cfg.setdefault("models", {}).setdefault("providers", {})["hbench"] = {
            "baseUrl": access.execution_base_url,
            "apiKey": "${HB_OPENCLAW_API_KEY}",
            "api": self.API_MODES[access.model_api_format],
            "models": [{"id": model_id, "name": model_id,
                        "contextWindow": self.options.context_window_tokens,
                        "contextTokens": self.options.context_window_tokens,
                        "maxTokens": self.options.max_tokens}],
        }

    def get_version_command(self):
        return "openclaw --version"

    def _parse_stdout(self):
        path = self.logs_dir / "openclaw.txt"
        if not path.is_file():
            return None
        text = path.read_text()
        decoder = json.JSONDecoder()
        envelope = None
        offset = 0
        for line in text.splitlines(keepends=True):
            if line.startswith("{"):
                try:
                    value, _ = decoder.raw_decode(text[offset:])
                    if isinstance(value, dict) and isinstance(value.get("meta"), dict):
                        envelope = value
                except ValueError:
                    pass
            offset += len(line)
        return envelope

    async def run(self, instruction, environment, context):
        try:
            await super().run(instruction, environment, context)
        finally:
            try:
                await self._copy_openclaw_session_file_to_agent_logs(
                    environment, dict(self.model_connection.env)
                )
            except Exception:
                self.logger.exception("Could not export the OpenClaw session")

    async def _copy_openclaw_session_file_to_agent_logs(self, environment, env):
        await super()._copy_openclaw_session_file_to_agent_logs(environment, env)
        transcript = self.logs_dir / "openclaw.session.jsonl"
        if transcript.is_file() and transcript.stat().st_size:
            return
        # Current OpenClaw stores transcripts in SQLite. Its native export
        # handles compression, active branches and redaction for us.
        agent_id = self.options.openclaw_agent_id
        export = (
            "mkdir -p /tmp/hbench-export && openclaw sessions export-trajectory "
            f"--agent {shlex.quote(agent_id)} "
            f"--session-key {shlex.quote('agent:' + agent_id + ':main')} "
            "--workspace /tmp/hbench-export --output trial --json"
        )
        convert = inspect.getsource(_openclaw_export_to_session) + "\n" + (
            "from pathlib import Path\n"
            "p=Path('/tmp/hbench-export/.openclaw/trajectory-exports/trial/events.jsonl')\n"
            "events=p.read_text()\n"
            "Path('/logs/agent/openclaw.events.jsonl').write_text(events)\n"
            "Path('/logs/agent/openclaw.session.jsonl').write_text(_openclaw_export_to_session(events))\n"
        )
        await self.exec_as_agent(
            environment, command=export + " && python3 -c " + shlex.quote(convert),
            env=env, timeout_sec=60,
        )

    async def exec_as_agent(self, environment, command, **kwargs):
        # The frozen payload already has Node 24; upstream's installer uses nvm.
        command = command.replace(". ~/.nvm/nvm.sh && nvm use 24 && ", "")
        if "openclaw agent --local --json " in command:
            provider, model_id = self._parse_model_name(self.model_name)
            selected = model_id if self.skip_model_parsing or not provider else f"{provider}/{model_id}"
            command = command.replace(
                f"--model {shlex.quote(selected)} ",
                f"--model {shlex.quote('hbench/' + model_id)} ", 1,
            )
        return await super().exec_as_agent(environment, command, **kwargs)
