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

## Claude Code, Hermes and OpenClaw runtime

The original runtime remains usable for Codex, Pi and AgentScope Java. Claude
Code, Hermes and OpenClaw require the extended runtime:

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
inside the task container with memory embeddings disabled. Do not bind a personal agent home into trial
containers. The dataset verifier determines rewards independently of the
Harness process exit code.

Claude Code uses `anthropic-compatible.yaml`: set `HB_ANTHROPIC_BASE_URL` and
`HB_ANTHROPIC_MODEL_ID`, and provide the credential referenced by `api_key_name`.
An Anthropic Messages compatible endpoint can serve a non-Anthropic model;
validate tool use as well as simple text responses before comparing scores.

```sh
export HB_ANTHROPIC_BASE_URL=https://your-endpoint.example/apps/anthropic
export HB_ANTHROPIC_MODEL_ID=your-model-id
python -m hbench plan --benchmark terminal-bench --harness claude-code \
  --model anthropic-compatible --cluster docker --dataset-root artifacts/datasets \
  --tasks freight-dispatch-shift,coq-block-bound --concurrency 2 \
  --name claude-code-check --output artifacts/plans/claude-code-check.yaml
python -m hbench run --config artifacts/plans/claude-code-check.yaml
```

Run separate plans when the Harnesses require different model profiles. A
multi-Harness, multi-model plan creates every combination, so each model must
declare an API format supported by every selected Harness.

## Console matrices and comparison settings

The console lists every registered Harness and model, independently of the
default matrix's selections. Select `anthropic-compatible` for Claude Code;
Hermes and OpenClaw also support the existing Bailian profiles. Preview checks
every selected model/Harness pair; incompatible cross-products must be split
into separate matrices.

The three extended profiles explicitly set `context_window_tokens: 131072`
and `max_tokens: 16384`, matching the conservative AgentScope Java budget and
the existing Pi output ceiling. These are evaluation budgets, not claims about
the provider's actual maximum context size. Claude Code uses its documented
`CLAUDE_CODE_MAX_CONTEXT_TOKENS` and `CLAUDE_CODE_MAX_OUTPUT_TOKENS` variables;
Hermes uses its custom provider context and request body; OpenClaw uses model
metadata and the agent context budget. Overrides belong in the Harness profile's
`kwargs`, and each run records `agent/inference-settings.json`.

No temperature override is injected, as with the original profiles. This leaves
native client/provider behavior intact; it does not guarantee identical numeric
temperatures. In particular, Claude Code has no shared temperature control in
this adapter. Reasoning is requested off (`none` for Hermes, `off` for OpenClaw,
zero thinking budget and adaptive thinking disabled for Claude Code). A custom
gateway may still enforce its own reasoning policy. Native compaction strategies
and Codex's existing model-dependent limits remain different; these are not
identical inference implementations. No new turn-count cap is imposed.

Claude Code environment reference: https://code.claude.com/docs/en/env-vars

## Model selection in the console

The console shows resolved model names rather than transport profile names.
The legacy `anthropic-compatible` profile remains usable in CLI matrices but is
hidden in the console. `example` is a test fixture, not a selectable model.

A model profile may declare `routes` keyed by harness name. Each route specifies
`name`, `base_url`, `api_format`, and optionally `api_key_name`. A route is used
only when its resolved model name equals the selected model and its URL is set;
it never substitutes another model. The Qwen profile routes Claude Code through
`HB_ANTHROPIC_BASE_URL` when `HB_ANTHROPIC_MODEL_ID` matches `HB_MODEL_A_ID`.

Console matrices set `skip_incompatible: true`: unsupported pairs are recorded
in `skipped_combinations` and excluded from planned trials. A selected harness
with no usable selected model is an error. CLI matrices retain strict validation
unless this option is explicitly enabled. Preview and execution share the same
resolver. Kimi and GLM used as tau3 auxiliary models are not automatically added
as evaluation candidates; configure candidate model profiles separately.
