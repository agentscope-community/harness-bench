# Registering a Harness

`configs/harnesses/*.yaml` is the shared registry used by planning, console
discovery, result identity and native evidence links. The profile filename must
match `name`; each `import_path` must be unique.

```yaml
name: hermes
import_path: hbench.extended_agents:FrozenHermes
supported_api_formats: [openai_chat_completions, openai_responses, anthropic_messages]
evidence_files: [hermes-session.jsonl]
kwargs:
  version: v2026.9.24
```

The adapter's `name()` must match the registered name. Evidence filenames are
relative to each trial's `agent/` directory; nested sessions and the normalized
`trajectory.json` are also indexed. Declare only public logs, never configuration
files containing credentials.

## Hermes and OpenClaw runtime

The original runtime remains usable for Codex, Pi and AgentScope Java. Hermes
and OpenClaw require the extended runtime:

```sh
docker build -f environments/hbench/Dockerfile.extended-runtime \
  --build-arg BASE_RUNTIME=hbench/harness-runtime:0.1.0 \
  -t hbench/harness-runtime:extended .
export HB_RUNTIME_IMAGE=hbench/harness-runtime:extended
```

This image installs pinned Harness releases at build time and carries its own
Node 24 and managed Python environment. Trial setup validates the version
manifest and executable without installing the Harness again. Task containers
must remain compatible with the runtime's native libraries.

Add each Harness to the selected model profile's `api_formats` mapping. The
bundled Bailian profiles use OpenAI Chat Completions for Hermes and OpenClaw.
Keys stay in environment variables referenced by `api_key_name`.

```sh
python -m hbench plan --benchmark terminal-bench --harness hermes,openclaw \
  --model bailian-model-b --cluster docker --dataset-root artifacts/datasets \
  --tasks freight-dispatch-shift,coq-block-bound --concurrency 2 \
  --name native-harness-check --output artifacts/plans/native-harness-check.yaml
python -m hbench run --config artifacts/plans/native-harness-check.yaml
```

Each trial gets its own container and state. Hermes uses `/tmp/hermes` with
memory disabled by the Harbor adapter; OpenClaw creates a local agent session
inside the task container. Do not bind a personal agent home into trial
containers. The dataset verifier determines rewards independently of the
Harness process exit code.

Claude Code uses `anthropic-compatible.yaml`: set `HB_ANTHROPIC_BASE_URL` and
`HB_ANTHROPIC_MODEL_ID`, and provide the credential referenced by `api_key_name`.
An Anthropic Messages compatible endpoint can serve a non-Anthropic model;
validate tool use as well as simple text responses before comparing scores.
