# Legacy runner — `eval.py` / `configs/suites/`

Back to the [README](../README.md). This is the original `eval.py` guide, kept for its owned tasks, JSON suites, release checks and archive formats. New evaluations use `python -m hbench`; see the [operation guide](operation-guide.md).

## Scope and current readiness

Use this benchmark to investigate capability gaps, check a Harness change before release, or compare
agents under a declared task/model/budget contract. Results describe selected tasks and adapted
environments; they are not official upstream leaderboard scores or a measure of the model alone.

| Capability | Available entrypoint / requirement |
| --- | --- |
| Offline protocol and suite checks | `eval.py protocol-smoke`, `protocol-check`, `release validate` |
| Prepare and export tasks | `eval.py prepare-tasks`, `export-tasks`; no model calls |
| Run AgentScope locally against a model | Native Java CLI; Java 17+, Maven and model credentials |
| Run production framework contracts | `eval.py release contracts`; local Java tests with fixtures/mocks |
| Execute suites across agents | Harbor adapters and runtime primitives are included; a separate runner and execution environments are required |
| Execute business tasks | Also requires isolated official tau-bench/AppWorld packages, data and evaluators |
| Compare archived results | `eval.py release compare`; no model calls |

**Legacy `eval.py` has no `run` command or complete local suite/matrix orchestrator.** Preparing or
exporting these legacy tasks does not execute them. This limitation applies to `eval.py` and
`configs/suites/`, not the new `hbench` Harbor runner above: legacy suite JSON is not its input.
The legacy migration tools do not submit cluster jobs or operate cloud storage/deployments.

## Task collections

There are **968 unique task IDs: 153 owned tasks and 815 external tasks**. Suites reuse these IDs;
they do not maintain duplicate task folders. `full` is a 63-task selection, not all 968.

| Collection | Count | Coverage and maintenance |
| --- | ---: | --- |
| [Harness](../datasets/harness/) | 26 | Directly maintained framework regression scenarios |
| [Capabilities](../datasets/capabilities/README.md) | 127 | Directly maintained capability tasks, fixtures and assertions |
| [Terminal Bench](../external/terminal-bench/) | 8 | Selected terminal tasks, fetched at a pinned commit with local overlays |
| [Business](../external/business/) | 807 | 165 tau-bench and 642 AppWorld IDs; wrappers generated from pinned declarations and a shared template |

The 26 Harness tasks cover:

| Area | Count | Examples |
| --- | ---: | --- |
| Basic completion | 2 | Aggregate a ledger, repair handling of empty input |
| Files | 4 | Empty files, localized edits, nested paths, file tails |
| Tools | 4 | Arguments, dependency ordering, distractions, failure recovery |
| Memory | 4 | Persistence, ephemeral state, updates, overrides |
| Compaction | 4 | Preserve constraints, continue work, flush memory, pending tools |
| Skills and subagents | 4 | Discovery, permissions, delegation, failure handling |
| Session lifecycle | 4 | Cancellation, duplicate requests, multiple turns, restart |

The 127 capability tasks comprise tool use (51), memory (23), file operations (21), retrieval (8),
component behavior (`unit_test`, 8), conversation (7), planning (7) and summarization (2). These are
executable task scenarios; `unit_test` is separate from the repository's Python/JUnit test suite.
Instructions and assertions live beside each task's `scenario.json`.

The eight terminal tasks are `tb-cancel-async-tasks`, `tb-fix-git`, `tb-large-scale-text-editing`,
`tb-log-summary-date-ranges`, `tb-multi-source-data-merger`, `tb-polyglot-c-py`, `tb-regex-log` and
`tb-write-compressor`. Business tasks exercise official tool-driven worlds: tau-bench interaction and
AppWorld application workflows, including selected dev, test-normal and test-challenge IDs.

**Maintain owned tasks directly; fetch external tasks at fixed versions and retain only necessary
adaptation code, patches and resources.** External downloads and materialized tasks live under ignored
`artifacts/datasets/`, not in committed upstream copies. See [dataset maintenance](../datasets/README.md)
and [external preparation](../external/README.md).

## Choose a suite

Profiles live in [`configs/suites/`](../configs/suites/). Counts below are task selections, before
multiplying by agents and rollouts. Three-agent profiles select AgentScope, Codex and Pi; release
profiles select AgentScope only. Capability limitations must remain explicit in results.

| Profile | Tasks | Agents × rollouts | Intended use |
| --- | ---: | ---: | --- |
| `smoke` | 2 | 3 × 1 | Basic cross-agent integration |
| `release-smoke` | 3 | 1 × 3 | Small release-path check |
| `release` | 18 | 1 × 3 | 54 planned release regression trials |
| `comparison` | 17 | 3 × 3 | 153 planned comparison trials |
| `full` | 63 | 3 × 1 | 26 Harness + 8 terminal + 29 business tasks |
| `capabilities-preflight` | 7 | 3 × 1 | Capability adapter preflight |
| `capabilities` | 127 | 3 × 1 | All owned capability tasks |
| `business-preflight` | 4 | 3 × 1 | Business backend preflight |
| `business-tau` | 165 | 3 × 1 | Selected tau-bench tasks |
| `business-appworld` | 642 | 3 × 1 | Selected AppWorld tasks |
| `business` | 807 | 3 × 1 | Both business collections |

Profiles also declare agent versions, model/API, timeouts, turn limits, concurrency and retry policy.
Read the chosen JSON before execution. Defaults use `qwen3.8-max` at the configured compatible
endpoint; this provider alias is **not an immutable model snapshot**. Changes to tasks, adapters,
scorers, model configuration or environment contracts require a new comparison baseline.

## Quick start: offline checks and task preparation

Use Python 3.12+ in a virtual environment. From the `harness-bench` repository root:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-protocol.txt
python eval.py protocol-smoke
python eval.py release validate --definitions-only
```

`protocol-smoke` replays a fixture, not a real agent/model. `--definitions-only` checks selections
without external downloads. Start with owned tasks:

```sh
python eval.py release validate --config configs/suites/capabilities-preflight.json
python eval.py export-tasks configs/suites/capabilities-preflight.json --output artifacts/export-capabilities-preflight
```

For profiles containing external tasks, prepare them first:

```sh
python eval.py prepare-tasks configs/suites/full.json
python eval.py release validate --config configs/suites/full.json
python eval.py export-tasks configs/suites/full.json --output artifacts/export-full
# Later, reuse verified cached downloads without network access:
python eval.py prepare-tasks configs/suites/full.json --offline
```

Preparation fetches selected files at locked commits and verifies SHA256. Export writes task
directories and a provenance `manifest.json`; it rejects an existing output directory. Validation
and export never download. Missing or corrupt inputs fail explicitly.

Business preparation only materializes wrappers and pinned metadata. It does **not** install the
official worlds, databases or full AppWorld data. The backend must match
[`sources.lock.json`](../external/business/sources.lock.json), including AppWorld data version `0.2.0`.
Record actual package/data identities and image digests used for execution.

## Run a native AgentScope smoke request

This checks the real Java Harness against a model. It is not a scored benchmark trial and does not
install task fixtures or trusted verifiers. Use a dedicated workspace: the agent can modify its files.

Build from the `harness-bench` repository root with Java 17+ and Maven. Point to the separate candidate
framework checkout, read its POM's actual version and install its modules before building the adapter:

```sh
export AGENTSCOPE_JAVA_REPO=/path/to/agentscope-java
CANDIDATE_VERSION=$(mvn -q -T1 -f "$AGENTSCOPE_JAVA_REPO/pom.xml" help:evaluate -Dexpression=project.version -DforceStdout)
mvn -B -T1 -f "$AGENTSCOPE_JAVA_REPO/pom.xml" -pl agentscope-harness,agentscope-extensions/agentscope-extensions-model/agentscope-extensions-model-openai -am install -DskipTests -Dmaven.javadoc.skip=true -Dgpg.skip=true
mvn -B -T1 -f adapters/agentscope-java/pom.xml package dependency:copy-dependencies spotless:check -DincludeScope=runtime -Dcandidate.version="$CANDIDATE_VERSION"
export HARNESS_EVAL_CLASSPATH="$PWD/adapters/agentscope-java/target/classes:$PWD/adapters/agentscope-java/target/dependency/*"
java -cp "$HARNESS_EVAL_CLASSPATH" io.agentscope.examples.eval.HarnessEvalCli --version
```

Supply `DASHSCOPE_API_KEY` through your environment. Still from the `harness-bench` repository root:

```sh
export MODEL_ID=qwen3.8-max
export MODEL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
mkdir -p artifacts
EVAL_RUN_DIR=$(mktemp -d "$PWD/artifacts/local-smoke.XXXXXX")
cat > "$EVAL_RUN_DIR/request.json" <<'JSON'
{"events":[{"type":"user.message","content":[{"type":"text","text":"Create hello.txt in the workspace containing exactly hello followed by a newline."}]}]}
JSON
java -cp "$HARNESS_EVAL_CLASSPATH" io.agentscope.examples.eval.HarnessEvalCli \
  "$EVAL_RUN_DIR/request.json" "$EVAL_RUN_DIR/native.jsonl" "$EVAL_RUN_DIR/workspace"
cat "$EVAL_RUN_DIR/workspace/hello.txt"
```

The CLI accepts `REQUEST_JSON NATIVE_JSONL WORKSPACE`. Use a fresh run directory to isolate state
and appended logs. It emits **native** events; `eval.py protocol-check` expects canonical events
converted through `runtime/native_events.py`, not this raw log.

## Execute scored suites

A runner integrating the included components must:

1. Select and validate a suite, prepare/export tasks, and freeze the evaluation contract.
2. Supply isolated environments using the applicable [`environments/`](../environments/) recipes,
   preinstall selected agent versions, and record resulting image digests.
3. Pass each scenario to `runtime/harbor_agents.py`. Install fixtures and skills, preserve multi-turn
   and session semantics, and supply official business backends where needed.
4. Run model/tool preflight before the matrix. Preserve per-trial state isolation, budgets and
   rollouts, without automatic whole-trial retries. `runtime/schedule.py` provides scheduling
   primitives, not environment provisioning.
5. Collect and convert native events, then execute outcome, behavior or official verifiers. Keep
   trusted tool receipts and scoring outside the candidate workspace; candidate-written files cannot
   substitute for trusted evidence. Archive every planned trial, including failures.
6. Produce immutable run evidence and use the offline reports and release gate below.

Components include `runtime/scenarios.py` (scenario execution), `runtime/fixture_tools.py` (trusted
fixtures), `runtime/task_score.py` and `runtime/behavior_checks.py` (scoring), `runtime/ledger.py`
(evidence), and `worker.py` / `runtime/business_server.py` (business backends). Installing
`requirements-evals.txt` supplies pinned Harbor dependencies; it does not turn these legacy JSON
suite profiles into jobs for the new `hbench` runner.

## Scores, release checks and reports

Task outcome checks, behavior assertions and official business evaluators have different contracts.
Capabilities use `capabilities-task-outcomes-v2`; Harness release profiles use `task-outcome-v1`.
Unsupported, missing and unscored trials must remain visible; they never count as passes.

From the `harness-bench` repository root, run framework contracts against the separate framework
checkout or compare existing run archives:

```sh
export AGENTSCOPE_JAVA_REPO=/path/to/agentscope-java
python eval.py release contracts --repo "$AGENTSCOPE_JAVA_REPO" --output artifacts/contracts-unique
python eval.py release compare --candidate /path/to/candidate-evidence \
  --baseline /path/to/baseline-evidence --output artifacts/review-unique
```

Contracts and source snapshots prefer explicit `--repo`; when omitted, they use
`AGENTSCOPE_JAVA_REPO`. If neither is set, they immediately report the missing framework repository;
they never default to the benchmark root. Capture the external framework into a fresh output directory:

```sh
python snapshot.py --repo "$AGENTSCOPE_JAVA_REPO" --output artifacts/source-snapshot-unique
```

For sandbox Docker builds, the build context must include both the benchmark files and the external
framework's `candidate-source.tgz`; Docker copies them separately. Never use `harness-bench` itself
as the candidate framework. See [environment recipes](../environments/README.md).

Contracts run 27 declared production test classes and verify Surefire XML. These fixture/mock tests
complement real-model trials. `--verify-only` inspects existing XML, not a fresh run. Comparison needs
`manifest.json`, `summary.json` and the per-trial `result.json` files named in the summary. It verifies
evidence and writes `release-gate.json`, `release-gate.html` and `report.html`, without calling models
or repairing absent results. Exit codes: 0 (`pass`), 3 (`review`), 2 (`fail`/`invalid`). Unlocked model
aliases require review. See [release policies and evidence requirements](release-regression.md).

The separate business renderer accepts the business batch format (`manifest.cases`, case results,
traces and ledger), not the unified release archive format:

```sh
mkdir -p artifacts
python report.py --local /path/to/business-archive --output artifacts/business-report.html
```

[`reports/templates/business.html`](../reports/templates/business.html) is its maintained source template.
Generated HTML embeds data for offline viewing and belongs under `artifacts/`. `combine_reports.py`
and `compare_reports.py` handle business archives; consult their `--help`. They are separate from the
release gate.

## Layout and development checks

| Path | Responsibility |
| --- | --- |
| `eval.py`, `release_cli.py` | Offline checks, preparation/export and release entrypoints |
| `adapters/` | Native adapters, including the Java CLI |
| `runtime/`, `schemas/` | Protocol, scenarios, fixtures, scoring and scheduling primitives |
| `configs/suites/`, `contracts/` | Suite profiles and production framework test selection |
| `datasets/`, `external/` | Owned tasks and pinned external adaptations |
| `environments/` | Portable environment/image recipes |
| `reports/`, `report.py`, `*_reports.py` | Maintained report implementations and templates |
| `worker.py`, `prepare_appworld.py`, `snapshot.py` | Business workers, verified data preparation and source snapshots |
| `tests/`, `test_*.py` | Test source files, not run results |
| `docs/` | [Protocol](agent-protocol.md) and [release regression](release-regression.md) guides |
| `tools/campaigns/` | Git-ignored historical first-party tools; unadapted reference archive, no historical results or runnable service |
| `artifacts/`, `.venv/`, `__pycache__/` | Ignored outputs, environments and caches |

With the virtual environment active, run from the `harness-bench` repository root:

```sh
python -m pip install -r requirements-evals.txt
PYTHONPATH=. python -m unittest discover -s tests -p 'test_*.py'
PYTHONPATH=. python -m unittest test_worker test_snapshot test_runner_integration test_prepare_appworld test_report test_compare_reports test_combine_reports
```

Set `HARNESS_EVAL_CLASSPATH` as in the Java build section to enable local Java loopback tests.
Set `HARNESS_EVAL_MAVEN_TEST=1` to enable snapshot Maven packaging checks. Without these settings,
those checks are skipped. Tests use local fixtures/mocks, not real model services.

Keep downloads, traces, generated reports, investigation notes and campaign progress under ignored
`artifacts/`. Do not commit credentials, virtual environments, build outputs or intermediate results.
