"""Native Harbor adapters using the prebuilt extended runtime."""
import json
import shlex
from harbor.agents.base import BaseAgent
from harbor.agents.agent_auth_resolution import ModelConnectionSpec
from harbor.agents.installed.claude_code import ClaudeCode

from harbor.agents.installed.hermes import Hermes
from harbor.agents.installed.openclaw import OpenClaw
from harbor.models.model_api_format import ModelApiFormat

from hbench.agents import _probe, record_request
from hbench.registry import harness_registry

EXTENSIONS = "/opt/hbench-runtime/extensions"


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
        await super().run(instruction, environment, context)


class FrozenClaudeCode(FrozenNative, ClaudeCode):
    pass


class FrozenHermes(FrozenNative, Hermes):
    def get_version_command(self):
        return "hermes --version"


class FrozenOpenClaw(FrozenNative, OpenClaw):
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
            "models": [{"id": model_id, "name": model_id}],
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
        convert = (
            "import json; from pathlib import Path; "
            "p=Path('/tmp/hbench-export/.openclaw/trajectory-exports/trial/session-branch.json'); "
            "d=json.loads(p.read_text()); "
            "Path('/logs/agent/openclaw.session.jsonl').write_text("
            "''.join(json.dumps(e)+'\\n' for e in d['entries']))"
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
