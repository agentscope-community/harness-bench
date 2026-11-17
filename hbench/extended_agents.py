"""Native Harbor adapters using the prebuilt extended runtime."""
import json
from harbor.agents.installed.claude_code import ClaudeCode

from harbor.agents.installed.hermes import Hermes
from harbor.agents.installed.openclaw import OpenClaw

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
            raise RuntimeError(f"Cannot execute baked {self.name()}: {probe.stderr}")
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
    pass


class FrozenOpenClaw(FrozenNative, OpenClaw):
    def get_version_command(self):
        return "openclaw --version"

    async def exec_as_agent(self, environment, command, **kwargs):
        # The frozen payload already has Node 24; upstream's installer uses nvm.
        command = command.replace(". ~/.nvm/nvm.sh && nvm use 24 && ", "")
        return await super().exec_as_agent(environment, command, **kwargs)
