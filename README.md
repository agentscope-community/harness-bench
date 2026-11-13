# Harness evaluations and benchmarks

Evaluate whether an agent can complete tasks through its real Harness: tool calls, files, memory,
context compaction, skills, delegation and session lifecycle. This package supports AgentScope Java
regression checks and comparisons with native Codex and Pi agents. Adapters preserve each agent's
execution loop; the benchmark evaluates task outcomes and recorded behavior.

中文：[中文说明](README_zh.md)。

This standalone repository flattens the tracked Harness benchmark tree from `agentscope-java-evals`
commit `3d4148ea4`. Run all benchmark commands from the `harness-bench` repository root; AgentScope
Java framework source stays in a separate checkout. Historical first-party `.py`/`.md`/`.sh` tools
from `artifacts/` are archived separately under Git-ignored `tools/campaigns/`: they are unadapted
reference material, not a directly runnable service, and contain no historical results. Results,
virtual environments, `target/` build outputs and secrets are not migrated; suspected credential
files are omitted or redacted and excluded from commits.

## Harbor workflow — main entrypoint: `python -m hbench`

The new [CLI](hbench/__main__.py) composes Harbor **0.25.0** jobs for native **Codex, Pi and
AgentScope Java** on **single-host Docker (including Alibaba Cloud ECS)** or **Kubernetes / ACK**,
with **ACS optional**. It provides dataset pull/preparation, comparison planning, an execution
entrypoint, image tooling and offline reports. This is an implementation with local fixture/mock
checks, **not completed real evaluation acceptance**: no real container/model/ECS/cluster evaluation
or Docker image build has been verified here. Actual model and execution-host/cluster settings must
be supplied before execution. No benchmark scores are claimed or fabricated.

To run the whole 3 × 2 × 3 matrix on a dedicated ECS host, follow the
[step-by-step runbook](#step-by-step-runbook-the-full-18-combination-suite-on-a-dedicated-ecs-host).

### Initial Hub datasets

These are the new [benchmark profiles](configs/benchmarks/), separate from the legacy selections below.
All three use immutable Hub `sha256` version pins; counts are expected prepared tasks, not results.

| Profile | Hub package | Tasks | Pinned version |
| --- | --- | ---: | --- |
| [tau3-bench](configs/benchmarks/tau3-bench.yaml) | `sierra-research/tau3-bench` | 375 | `sha256:a57304f682894ac061090769af771a3617664f3ff6e5417d4eadf8e30433e4d9` |
| [swe-bench-verified](configs/benchmarks/swe-bench-verified.yaml) | `swe-bench/swe-bench-verified` | 500 | `sha256:b934b0cc3dc800fe945eaf9f1623329db97ee3133c706d20644524c7759fb341` |
| [terminal-bench](configs/benchmarks/terminal-bench.yaml) | `terminal-bench/terminal-bench` | 66 | `sha256:5881c1b1c74a96f07baef00a7c869e09802704bc0dc912c870e7acccc11440da` |

The requested tau2 family is represented by **Hub tau3, based on tau2**; it is not the original tau2
package or the legacy 165-task tau-bench selection. The TB count describes this pinned Hub package,
not every upstream Terminal-Bench release.

### Install, pull and prepare

Use Python 3.12+ and run from the `harness-bench` repository root:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-evals.txt
python -m hbench list
python -m hbench datasets pull all
python -m hbench datasets prepare all
```

Single-benchmark alternatives (do not repeat them against the same existing outputs):

```sh
python -m hbench datasets pull terminal-bench
python -m hbench datasets prepare terminal-bench
# Alternatively, prepare a package root already downloaded elsewhere:
python -m hbench datasets prepare tau3-bench --source /path/to/downloaded_dir
```

`pull BENCH` resolves the pinned package through Harbor and downloads/exports it to
`artifacts/hub/BENCH/`; it calls no model and creates no cluster resources. `prepare BENCH` copies the
cached package into `artifacts/datasets/BENCH/`, checks the expected task count, records full original
file SHA256 hashes and adaptations in `manifest.json`, and leaves downloaded sources untouched.
It preserves task instructions, fixtures and scoring logic; only execution configuration is adapted.
The profile digest is a Hub metadata pin, **not an archive digest recomputed by `prepare`**; an explicit
`--source` is the caller's provenance responsibility. Both actions support `all`; `--source` requires
one benchmark. `--root` selects the Hub cache and `prepare --output-root` selects prepared outputs.
Existing download/prepared directories are refused rather than overwritten.

### Model and execution profile configuration

Use [configs/models/example.yaml](configs/models/example.yaml), or pass your own YAML path to
`--model`. Configuration uses exported environment variables; `hbench` does **not** auto-load `.env`.
If maintaining a local shell env file, inspect its contents and confirm shell compatibility before
sourcing it. Keep keys out of profiles, plans and commits.

| Candidate setting | Environment variable |
| --- | --- |
| Actual model ID | `HB_MODEL_ID` |
| Model endpoint base URL | `HB_MODEL_BASE_URL` |
| Candidate API key, resolved at execution | `HB_MODEL_API_KEY` |

[Harness profiles](configs/harnesses/) declare explicit protocol support:

| Harness | Frozen CLI version | Supported `api_formats` |
| --- | --- | --- |
| Codex | `0.167.2` | `openai_responses` only |
| Pi | `1.5.2` | `openai_chat_completions`, `openai_responses`, `anthropic_messages` |
| AgentScope Java | `1.2.0` | `openai_chat_completions` only |

Pi requires Node >=22.19.0; the frozen baseline uses external `pi-mcp-adapter` `7.0.1`
with a Pi compatibility gate of `>=1.0.0,<2.0.0`.

The example selects Responses for Codex and Chat Completions for Pi/Java. A three-harness comparison
therefore requires the **same declared model endpoint to implement both Responses and Chat
Completions**. Do not silently substitute protocols or models; an incompatible selection is rejected.
Pi's other formats require an explicitly compatible model profile. Harness turn/token/reasoning
settings are in the harness YAMLs and must be recorded as part of the comparison budget.

For tau3, configure its simulated user and natural-language assertions separately from the candidate:

| Auxiliary setting | Environment variable | Forwarded to tau3 |
| --- | --- | --- |
| Auxiliary API key | `HB_TAU_API_KEY` | `OPENAI_API_KEY` |
| Auxiliary endpoint | `HB_TAU_BASE_URL` | `OPENAI_BASE_URL` |
| Simulated-user model | `HB_TAU_USER_MODEL` | `TAU2_USER_MODEL` |
| Assertions model | `HB_TAU_ASSERTIONS_MODEL` | `TAU2_NL_ASSERTIONS_MODEL` |

Do not reuse the candidate budget implicitly: declare and account for **candidate, user and
assertions budgets independently**, including task/runtime limits and auxiliary usage. These
variables select models/credentials; they do not implement a unified auxiliary-budget controller.

Choose [docker](configs/clusters/docker.yaml) for local execution, [kubernetes](configs/clusters/kubernetes.yaml),
[ack](configs/clusters/ack.yaml) or optional [acs](configs/clusters/acs.yaml), or supply a complete custom
execution-profile YAML path with `--cluster`. The existing flag name is retained: `--cluster docker`
selects a **local execution profile**, not a Kubernetes cluster. ACK/ACS continue to use the Kubernetes
backend; ACS additionally requires an ACS-enabled namespace, compatible scheduling/networking and quotas.

| Execution setting | Environment variable | Scope |
| --- | --- | --- |
| Built runtime image | `HB_RUNTIME_IMAGE` | Docker: locally available, default `hbench/harness-runtime:0.1.0`; Kubernetes/ACK/ACS: published and reachable |
| Isolated namespace | `HB_NAMESPACE` (default `harness-bench`) | Kubernetes/ACK/ACS only |
| Kubeconfig path / context | `HB_KUBECONFIG`, `HB_KUBE_CONTEXT` | Kubernetes/ACK/ACS only |
| Registry repository prefix | `HB_REGISTRY` | Kubernetes/ACK/ACS publishing/mirroring |
| Existing image-pull secret name | `HB_IMAGE_PULL_SECRET` | ACK/ACS; add `kwargs.image_pull_secret` to a custom Kubernetes profile if needed |

Kubernetes/ACK/ACS profiles use ordinary Pods, not privileged Docker-in-Docker, host networking or
SandboxClaim. Registry credentials are managed externally; the runner does not provision registries,
clusters or ECS hosts. For SWE-bench, verifier `uv`/`pip` needs internet access or an accessible package
mirror; `HB_PIP_INDEX_URL` configures both `PIP_INDEX_URL` and `UV_DEFAULT_INDEX` in the example profile.

### Single-host Docker / ECS

Use a **dedicated evaluation host**, not a production host sharing unrelated workloads. Linux x86_64
is recommended; Python 3.12+, Docker Engine and a modern Compose plugin supporting `up --wait` and
`depends_on: condition: service_completed_successfully` are required. Building the runtime also needs
Maven and JDK 17+. The `hbench` controller and Docker daemon must be on **the same machine**. The user must
install and confirm Docker beforehand: this workflow does not request ECS or install Docker, and
requires neither single-node Kubernetes nor kubeconfig/RBAC.

User-operated preflight checks (not run by this documentation update):

```sh
docker info
docker compose version
```

Images need only be available to the local daemon; **no registry push is required**. Generate a build
plan for the frozen harness runtime plus both shared tau3 images:

```sh
python -m hbench images build --runtime-image hbench/harness-runtime:0.1.0 \
  --tau3-dataset artifacts/datasets/tau3-bench
```

This only plans/stages inputs, without Maven or Docker execution. **Only after explicit user opt-in**,
append `--execute` to actually build locally; it never pushes. The default `hbench/tau3-main:0.1.0`
and `hbench/tau3-runtime:0.1.0` tags match the prepared tau3 dataset: tau3 requires **both**, not just
its main image. TB/SWE can use a runtime-only build plan by omitting `--tau3-dataset`; their task images
must also be available locally. Actual builds need registry/outbound access to fetch base images and
dependencies (or a verified local cache). If changing tags, review the build plan's `image_map` and
pass it to a fresh `plan --image-map BUILD_PLAN_JSON`; mappings cover main, runtime and sidecar images.

The thin `hbench.docker_environment:FrozenDockerEnvironment` subclass uses Harbor 0.25's native
Docker backend for containers/Compose, networks, resources, logs, exec, transfers and cleanup. A
one-shot init service populates project-scoped named volumes from the same frozen harness runtime;
main mounts them read-only and waits for `service_completed_successfully`. Setup runs as root, while
Harbor retains the task's agent-user switching. This does **not** use the Kubernetes Compose
translator or force services onto localhost: native sidecar DNS/networks, volumes, healthchecks and
`depends_on` are preserved. The supplied `name: docker`, `backend: docker` profile uses
`auto_mounting: true`, `keep_containers: false`.
Do not mount the Docker socket, host secrets or cloud credentials into tasks.
The runtime overlay reserves its service/volume names and mount paths; conflicting task mounts and
Compose `include`, `extends` or `volumes_from` are rejected rather than partially translated.

Task/harness main images still require Linux/glibc, Python 3, Bash, GNU `stdbuf`, `/bin/sh` and `tar`;
runtime mounts do not guarantee equivalence for every upstream image. GPU tasks require the actual
requested GPU model **and NVIDIA Container Toolkit**; type checks use read-only host `nvidia-smi`,
not just a GPU count or an ignored H100 requirement. Dynamic network policy uses Harbor's native
egress sidecar and host kernel capabilities, not CNI/Kubernetes permissions. Both GPU and network
enforcement need real-host validation; this is **not a claim that all tasks can run unconditionally**.

### Plan, dry-run, execute later, summarize

After preparing datasets, the placeholder model profile is sufficient to generate a local Docker
plan. Docker dry-run also runs Harbor's native read-only `docker info` preflight, so the local Docker
daemon must be running; this is not a container/image/GPU/network acceptance test.
These commands **only plan and validate**, not execute trials:

```sh
python -m hbench plan \
  --benchmark tau3-bench,swe-bench-verified,terminal-bench \
  --harness codex,pi,agentscope-java --model example --cluster docker \
  --dataset-root artifacts/datasets --n-tasks 1 --exclude-gpu --concurrency 1 \
  --name smoke-docker --output artifacts/plans/smoke-docker.yaml
python -m hbench run --config artifacts/plans/smoke-docker.yaml --dry-run
```

Use `--cluster kubernetes`, `--cluster ack` or `--cluster acs` for those backends, with a fresh plan
name/output. `--n-tasks 1` selects one task **per benchmark**, so this example plans 9 trials
(3 benchmarks × 3 harnesses × 1 attempt), not scores. `--exclude-gpu` is a reduced selection: the
current prepared Terminal-Bench package has **3 tasks declaring H100**, so this is not a full-dataset
result. `plan` writes the job YAML and `smoke-docker.plan.json` contract without execution;
`--concurrency`, `--attempts`, `--tasks` and `--exclude-gpu` control selections. Dry-run asks Harbor to
validate the job with credential checks skipped; it does not start containers, submit Pods, call
models, build images or prove readiness. Outputs must be fresh; regenerate a plan before rerunning
or after changing candidate-model/execution-profile settings. Candidate-model and execution-profile
non-secret settings are frozen during planning; credential references and the model profile's
preserved `environment_env` / `verifier_env` templates resolve at run time, including auxiliary model
IDs, endpoints and package indexes. For reproducible comparisons, the caller should keep these
resolved auxiliary non-secret values consistent and record them separately, without credentials.

**Separate user opt-in for a real run:** first supply real model credentials, confirm local images
and Docker/GPU/network capabilities (or published reachable images and context/RBAC/CNI for
Kubernetes/ACK/ACS), and regenerate the plan with the confirmed settings. Then, only if explicitly
choosing to execute the potentially paid model calls:

```sh
# User-operated real execution only; not part of the plan/dry-run above.
python -m hbench run --config artifacts/plans/smoke-docker.yaml
python -m hbench summary artifacts/jobs/smoke-docker --output artifacts/reports/smoke-docker
```

Default `example-model` and `example.invalid` endpoint/runtime-image settings are rejected for real
`run` but accepted for dry-run; Docker's local runtime tag is not evidence that it exists. Real `run`
can call models and create/delete containers, or trial Pods and configured NetworkPolicies on
Kubernetes. **This update built no images and ran no real containers, models or ECS/cluster operations.**
`summary artifacts/jobs/NAME --output artifacts/reports/NAME` renders existing Harbor evidence as
`summary.json` and `report.html`, without model calls. Errors/unscored trials are not passes; missing
trials are reported when the Harbor job's planned count is available. It neither invents results nor
produces official leaderboard scores.

### Matrix runs: datasets × models × harnesses

`plan --model` accepts comma-separated profiles. For repeatable comparisons, describe the whole
matrix in one file and render it with `matrix`. `configs/matrices/bailian-18.yaml` covers
3 datasets × 2 models × 3 harnesses = 18 combinations in **one** Harbor job:

```sh
python -m hbench matrix --config configs/matrices/bailian-18.yaml \
  --output artifacts/plans/bailian-18.yaml
python -m hbench run --config artifacts/plans/bailian-18.yaml --dry-run
```

Matrix keys: `name`, `cluster` (`docker`, `kubernetes`, `ack`, `acs` or a YAML path), `dataset_root`,
`benchmarks`, `models`, `harnesses`, `concurrency`, `attempts`, `selection` (`n_tasks`, `tasks`,
`exclude_gpu`) and `image_map`. Entries are profile names under `configs/`, or YAML paths. Relative
paths are repository-relative. The job name defaults to `<name>-<UTC timestamp>`, so each render can
be run once. Switch between Docker and ACK only by changing `cluster`, then render a new plan.
Harbor schedules every task × model × harness trial from a single global queue. `concurrency` is
the total number of trials running at the same time across all combinations, not a per-combination
limit. The `.plan.json` contract lists each combination and its planned trials. `summary`
reports one row per (harness, model, dataset). Models in one matrix must have distinct resolved
names and identical `environment_env`/`verifier_env`, because those settings are job-wide.

`configs/models/bailian-model-{a,b}.yaml` target Bailian's OpenAI-compatible endpoint. Credentials
stay out of files. Set `HB_BAILIAN_API_KEY` in the controller shell. Use `HB_MODEL_A_ID` and
`HB_MODEL_B_ID` for model IDs, and optionally `HB_BAILIAN_BASE_URL` (default
`https://dashscope.aliyuncs.com/compatible-mode/v1`). codex uses the Responses API, so choose
models that Bailian serves on `/responses`. Untrimmed, the prepared datasets currently give
(66 + 500 + 375) tasks × 2 models × 3 harnesses = 5,646 trials, including 3 Terminal-Bench tasks
that declare H100 GPUs. For a smoke run, set `selection.n_tasks: 1` first (18 trials).

One-click: `cp .env.example .env`, fill it (`.env` is git-ignored), then run `bash deploy/run-matrix.sh`.
It loads the file, checks required settings, renders a fresh plan (`HB_CLUSTER`, `HB_CONCURRENCY`, `HB_N_TASKS`
override the matrix), runs a dry-run, then the **real, billable** run and `summary`.
`HB_DRY_RUN_ONLY=1` stops before any model call.

### Step-by-step runbook: the full 18-combination suite on a dedicated ECS host

This is the operator checklist for running `configs/matrices/bailian-18.yaml` end to end. Part A uses
native Docker on one ECS host and needs no registry or cluster. Part B lists the extra steps for moving
the same matrix to ACK later. Every command is run by you. **Nothing below has been executed from this
repository yet.** No image has been built, nothing pushed, and no real trial run. Steps marked
**billable** call the Bailian models.

#### A1. Prepare the ECS host

Suggested host:

- **OS / CPU:** Ubuntu 24.04 x86_64, so Python 3.12 is the system Python.
- **Region:** Hong Kong (or another region with direct Docker Hub, PyPI, npm, Maven Central and GitHub access).
  Mainland regions need registry/package mirrors.
- **Size:** at least 4 vCPU and 8 GiB memory per concurrent trial, since a tau3 trial requests 4 CPU / 8 GiB.
  For example, 16 vCPU / 64 GiB for `HB_CONCURRENCY=4`, or 32 vCPU / 128 GiB for 8.
- **Disk:** a large data disk for Docker images. The 500 SWE-bench instance images and 160
  Terminal-Bench images are pulled and kept. Plan on roughly 1 TB; this is an unmeasured estimate.
- **GPU:** no GPU is needed, except for 3 Terminal-Bench tasks that declare H100. See A7.

Install the tools:

```sh
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv git rsync curl openjdk-21-jdk-headless maven skopeo
curl -fsSL https://get.docker.com -o get-docker.sh && sudo sh get-docker.sh   # mainland: add --mirror Aliyun
sudo usermod -aG docker "$USER" && newgrp docker
# Optional: put Docker's data on the big disk, e.g. mounted at /data
#   echo '{"data-root": "/data/docker"}' | sudo tee /etc/docker/daemon.json && sudo systemctl restart docker
docker info && docker compose version && java -version && mvn -v && python3.12 --version
docker login            # a Docker Hub account avoids anonymous pull-rate limits
```

`skopeo` is only needed for Part B. `docker compose version` must report the Compose v2 plugin.

#### A2. Copy the repository to the host

Most of this repository is not committed to Git yet, so copy the working tree rather than cloning.
Run this **on your laptop, from the repository root**:

```sh
rsync -az --exclude /.venv --exclude /artifacts --exclude target/ --exclude .env --exclude .DS_Store \
  ./ <user>@<ECS_IP>:~/harness-bench/
# Optional: reuse datasets you have already prepared (~0.6 GB) and skip pull/prepare in A3
rsync -az artifacts/datasets <user>@<ECS_IP>:~/harness-bench/artifacts/
```

#### A3. Python environment and datasets (on ECS)

```sh
cd ~/harness-bench
python3.12 -m venv .venv && . .venv/bin/activate
python -m pip install -r requirements-evals.txt
python -m hbench list
# Skip the next two lines if artifacts/datasets was copied in A2
python -m hbench datasets pull all
python -m hbench datasets prepare all
python -m unittest discover -s tests/hbench -p 'test_*.py'   # optional offline self-check
```

After this, `artifacts/datasets/{terminal-bench,swe-bench-verified,tau3-bench}` should each contain a
`manifest.json`, with 66, 500 and 375 tasks respectively.

#### A4. Fill in the environment file

```sh
cp .env.example .env && chmod 600 .env
vi .env
```

| Variable | What to put there |
| --- | --- |
| `HB_BAILIAN_API_KEY` | Your Bailian API key (secret; keep it only in `.env`). |
| `HB_BAILIAN_BASE_URL` | Default `https://dashscope.aliyuncs.com/compatible-mode/v1`, or `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`. |
| `HB_MODEL_A_ID`, `HB_MODEL_B_ID` | The two candidate model IDs. They must be different, and both must be served on Bailian's `/responses` endpoint, because codex uses the Responses API. |
| `HB_TAU_API_KEY`, `HB_TAU_BASE_URL` | The tau3 simulated-user / assertion endpoint. The defaults reuse the Bailian values. |
| `HB_TAU_USER_MODEL`, `HB_TAU_ASSERTIONS_MODEL` | The tau3 auxiliary models, usually `openai/<model-id>` for LiteLLM. They may be the same model. Keep them fixed across runs you want to compare. |
| `HB_PIP_INDEX_URL` | Optional package index for SWE-bench verifiers, e.g. `https://mirrors.aliyun.com/pypi/simple/` on mainland hosts. |
| `HB_RUNTIME_IMAGE` | Keep `hbench/harness-runtime:0.1.0` for Docker. It must match the tag built in A5. |
| `HB_CLUSTER` | `docker` for Part A. |
| `HB_CONCURRENCY` | The total number of trials running at once across all 18 combinations. Size it to the host (see A1). |
| `HB_N_TASKS` | `1` for the smoke run (18 trials). Leave it empty for the full datasets (5,646 trials). |
| `HB_DRY_RUN_ONLY` | `1` stops after the dry-run; `0` continues to the real, billable run. |

Optional connectivity check from the host. It needs no tokens: 200 means OK, and 401 means a wrong key.
Any HTTP status at all shows the endpoint is reachable.

```sh
set -a; . ./.env; set +a
curl -sS -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $HB_BAILIAN_API_KEY" "$HB_BAILIAN_BASE_URL/models"
```

#### A5. Build the images locally (Maven + Docker, no push)

The harness runtime image packages codex, pi and the AgentScope Java CLI. The two tau3 images serve
the tau3 tasks. Preview the plan first, then build:

```sh
python -m hbench images build --runtime-image hbench/harness-runtime:0.1.0 \
  --tau3-dataset artifacts/datasets/tau3-bench              # plan only
python -m hbench images build --runtime-image hbench/harness-runtime:0.1.0 \
  --tau3-dataset artifacts/datasets/tau3-bench --execute    # runs mvn clean package + 3 docker builds
docker image ls | grep -E 'hbench/(harness-runtime|tau3-main|tau3-runtime)'
```

You should see three local images: `hbench/harness-runtime:0.1.0`, `hbench/tau3-main:0.1.0` and
`hbench/tau3-runtime:0.1.0`. The printed `plan_path` records what was built. Rebuild after changing
`adapters/agentscope-java` or `environments/hbench/`.

#### A6. Optional: pre-pull task images

Harbor pulls Terminal-Bench and SWE-bench images on demand. Pre-pulling separates download problems
and rate limits from model runs:

```sh
for d in terminal-bench swe-bench-verified; do
  python -m hbench images list "artifacts/datasets/$d" |
    python -c 'import json,sys; print("\n".join(i for i in json.load(sys.stdin) if not i.startswith("hbench/")))'
done | sed -E 's/:[^/@]+@/@/' | sort -u > artifacts/task-images.txt
wc -l artifacts/task-images.txt
xargs -n1 docker pull < artifacts/task-images.txt
```

The `sed` drops the tag where a digest pins the image. Run `docker system df` to watch disk usage.

#### A7. Smoke run: 1 task per dataset, 18 trials

Set `HB_N_TASKS=1` in `.env`. Run the dry-run first; it makes no model calls:

```sh
HB_DRY_RUN_ONLY=1 bash deploy/run-matrix.sh     # renders a fresh plan + Harbor dry-run, no model calls
bash deploy/run-matrix.sh                       # billable: 18 real trials, then summary
```

Open the report path printed at the end (`artifacts/reports/<name>/report.html`). Check that every
combination has a recorded trial and that errors are task failures, not setup failures. Setup failures
include a missing image, a 401 or 404 from the model endpoint, or codex being unable to use
`/responses`.

**GPU tasks:** 3 Terminal-Bench tasks declare an H100. On a CPU-only host they are refused by the GPU
preflight and recorded as errors, not passes. To leave them out, set `selection.exclude_gpu: true` in
the matrix file. This is a reduced selection, so state it when reporting results.

#### A8. Full run: all tasks, 5,646 trials

Clear `HB_N_TASKS` in `.env` and choose `HB_CONCURRENCY`. Then start the run in the background so it
survives logout:

```sh
nohup bash deploy/run-matrix.sh > artifacts/run-matrix.log 2>&1 &
tail -f artifacts/run-matrix.log
ls artifacts/jobs/                      # the job directory is <HB_RUN_NAME>-<UTC timestamp>
docker ps                               # running trial containers
```

This is billable and long-running. Each invocation renders a new, uniquely named plan and job. For an
interim report while the run is still going:

```sh
python -m hbench summary artifacts/jobs/<name> --output artifacts/reports/<name>-interim
```

If the controller process dies, you have two options:

- Start a fresh job by re-running the script. This repeats every trial.
- Try Harbor's native resume on the same job directory. This has not been exercised with hbench here.

  ```sh
  set -a; . ./.env; set +a
  PYTHONPATH="$PWD" .venv/bin/harbor jobs resume -p artifacts/jobs/<name>
  ```

#### A9. Collect results

The script writes `artifacts/reports/<name>/summary.json` and `report.html`, with one row per
(harness, model, dataset), giving 18 rows. Copy them back **from your laptop**:

```sh
rsync -az <user>@<ECS_IP>:~/harness-bench/artifacts/reports/ ./artifacts/reports/
```

`artifacts/` and `.env` are Git-ignored. Do not commit them, or paste keys into tickets or chat.

#### B. Later: run the same matrix on ACK

Additional prerequisites:

- An ACR namespace, for example `registry.cn-hongkong.aliyuncs.com/<ns>`.
- An ACK cluster that can pull from that registry, ideally in the same region.
- A kubeconfig for that cluster on the ECS host.
- Network access from Pods to the Bailian endpoint.

Run all of these steps on the ECS host.

**B1. Build with registry tags, then push.** Build never pushes, so push the three images yourself:

```sh
export REG=registry.cn-hongkong.aliyuncs.com/<ns>        # becomes HB_REGISTRY
python -m hbench images build --runtime-image "$REG/harness-runtime:0.1.0" \
  --tau3-dataset artifacts/datasets/tau3-bench --tau3-image-prefix "$REG/tau3" --execute
BUILD_PLAN_JSON=<the plan_path printed above>
docker login "${REG%%/*}"
docker push "$REG/harness-runtime:0.1.0"
docker push "$REG/tau3-main:0.1.0"
docker push "$REG/tau3-runtime:0.1.0"
```

**B2. Mirror every image a dataset needs into ACR.** This uses skopeo and pushes copies of the Docker
Hub task images plus the three images from B1. Mirroring SWE-bench moves a lot of data. Review a
plan-only pass first:

```sh
skopeo login docker.io && skopeo login "${REG%%/*}"
for d in terminal-bench swe-bench-verified tau3-bench; do
  python -m hbench images mirror "artifacts/datasets/$d" --registry "$REG" \
    --built-image-map "$BUILD_PLAN_JSON" --output "artifacts/mirror-plan-$d.json"            # plan only
done
for d in terminal-bench swe-bench-verified tau3-bench; do
  python -m hbench images mirror "artifacts/datasets/$d" --registry "$REG" \
    --built-image-map "$BUILD_PLAN_JSON" --output "artifacts/image-map-$d.json" --execute    # copies/pushes
done
python - <<'EOF'
import json
merged = {}
for d in ("terminal-bench", "swe-bench-verified", "tau3-bench"):
    merged.update(json.load(open(f"artifacts/image-map-{d}.json"))["image_map"])
with open("artifacts/image-map.json", "x") as f:
    json.dump({"image_map": merged}, f, indent=2)
print(len(merged), "mappings")
EOF
```

**B3. Create an ACK matrix file** pointing at the merged map:

```sh
cp configs/matrices/bailian-18.yaml configs/matrices/bailian-18-ack.yaml
# edit it: cluster: ack   and   image_map: artifacts/image-map.json
```

**B4. Bootstrap the namespace.** Run this once, after reviewing the manifests and confirming the
context. Bind the Role to the identity your kubeconfig actually uses (`kubectl auth whoami` shows it);
no binding is guessed:

```sh
export KUBECONFIG=<path> CTX=<context>
kubectl --context "$CTX" apply -f deploy/k8s/namespace.yaml -f deploy/k8s/rbac.yaml
kubectl --context "$CTX" -n harness-bench create rolebinding harness-bench-runner \
  --role=harness-bench-runner --user=<kubeconfig-user>
kubectl --context "$CTX" -n harness-bench auth can-i create pods/exec
# Image pull access: use ACK's ACR credential helper, or create a pull secret
read -rs ACR_PASSWORD
kubectl --context "$CTX" -n harness-bench create secret docker-registry hb-acr \
  --docker-server="${REG%%/*}" --docker-username=<acr-user> --docker-password="$ACR_PASSWORD"
unset ACR_PASSWORD
```

**B5. Switch `.env` and run.** Set these values, then repeat A7 (smoke) and A8 (full) unchanged:

```sh
HB_MATRIX=configs/matrices/bailian-18-ack.yaml
HB_CLUSTER=ack
HB_REGISTRY=registry.cn-hongkong.aliyuncs.com/<ns>
HB_RUNTIME_IMAGE=registry.cn-hongkong.aliyuncs.com/<ns>/harness-runtime:0.1.0
HB_NAMESPACE=harness-bench
HB_KUBECONFIG=<path>
HB_KUBE_CONTEXT=<context>
HB_IMAGE_PULL_SECRET=hb-acr          # empty if using the credential helper
```

ACK limits differ from Docker:

- **Network policy:** the supplied ACK profile uses `network_enforcement: none`, so restricted-network
  tasks are rejected. Running them needs a custom profile with `networkpolicy` and an egress-enforcing
  CNI.
- **GPU tasks:** they need matching H100 nodes.

See the Kubernetes section below for details.

### Image inventory, mirror and build plans

`DATASET` is a downloaded/prepared dataset **directory**, not a profile name. The available interface
is defined by [hbench/__main__.py](hbench/__main__.py) and [hbench/images.py](hbench/images.py):

```sh
python -m hbench images list artifacts/datasets/terminal-bench
# Runtime-only plan; no tau3 data required:
python -m hbench images build --runtime-image reg/org/harness-runtime:0.1.0
# Alternatively, explicitly include shared tau3 images:
python -m hbench images build --runtime-image reg/org/harness-runtime:0.1.0 \
  --tau3-dataset artifacts/datasets/tau3-bench --tau3-image-prefix reg/org/tau3
```

By default these commands **only inventory/plan**: build writes local plans and, when tau3 is requested,
stages shared contexts under `artifacts/build/`, without running Maven or Docker. Omitting
`--tau3-dataset` passes `dataset=None` and plans **only the harness runtime**, with no cached tau3
fallback. Explicit `--tau3-dataset DATASET` adds the shared tau3 main/runtime images. The optional
`--tau3-image-prefix` defaults to `hbench/tau3` and must contain no tag or digest; `reg/org/tau3`
produces `reg/org/tau3-main:0.1.0` and `reg/org/tau3-runtime:0.1.0`.

The CLI requires `--runtime-image`; the logical runtime reference (and Python builder default) is
`hbench/harness-runtime:0.1.0`. Build returns an `image_map` from logical references to build targets
and `plan_path`, the saved build-plan JSON path. Runtime-only maps the logical runtime; with tau3,
it also maps `hbench/tau3-main:0.1.0` and `hbench/tau3-runtime:0.1.0`. Planned tags are **not evidence
of built or published images**.

- **Only explicitly user-authorized `build --execute`** runs local Maven
  `clean package dependency:copy-dependencies -DincludeScope=runtime` first, then `docker build`.
  `clean` prevents stale mixed SNAPSHOT/BETA JARs on the wildcard classpath. Build **never pushes**.
  Docker execution only needs locally available targets; Kubernetes/ACK/ACS execution requires
  separately published targets reachable by the cluster and externally managed registry authentication.
  The Java POM defaults to `candidate.version=2.1.0-BETA1`.
- **For registry mirroring only**, after building and separately publishing the targets, pass the saved
  plan with `mirror --built-image-map BUILD_PLAN_JSON`. Its `image_map` replaces logical `hbench/*`
  inventory references with published sources **before** mirror planning/copying. If publishing uses
  different references, update the mapping first. Mirror neither builds nor publishes missing source
  images, and **must not be executed against local-only logical tags**; skip it for local Docker use.

For remote registry sources, set `BUILD_PLAN_JSON` to the relevant build's returned `plan_path`
(runtime-only for TB/SWE, or runtime + tau3 for tau3), then generate a mirror plan:

```sh
python -m hbench images mirror artifacts/datasets/terminal-bench \
  --registry reg/org --built-image-map "$BUILD_PLAN_JSON" --output artifacts/image-map.json
```

- Without `--execute`, mirror only writes mappings/commands; it does not run skopeo. `--registry`
  selects the destination registry/org prefix; `--output` must be a fresh JSON path.
- **Only explicitly user-authorized `mirror --execute`** uses
  `skopeo copy --all --preserve-digests` and digest inspection, and **does copy/push** to the destination
  registry. Configure source/destination auth externally first. Every substituted source must be
  remotely resolvable; execution rejects remaining logical `hbench/*` references.
- Mirror's output `image_map` retains mappings for the original logical references. Review it, set
  `HB_RUNTIME_IMAGE` to the actual published runtime image, and render a **fresh** job plan with
  `--image-map artifacts/image-map.json`. Inventory/mapping alone does not prove image availability.

No `--execute` image operation was performed here. Docker build and actual registry/cluster behavior
remain unverified; direct tau2 dependencies are pinned, but the build is not a pre-resolved full
transitive dependency lock.

### Kubernetes / ACK / ACS bootstrap and execution limits

**This section applies only to Kubernetes/ACK/ACS**, not the native Docker backend. In particular,
its `network_enforcement: none`, Pod identity/startup, Compose translation and CNI/RBAC limits do not
apply to Docker. See the single-host section for Docker's native networking and runtime contract.

Only [deploy/k8s/namespace.yaml](deploy/k8s/namespace.yaml) and
[deploy/k8s/rbac.yaml](deploy/k8s/rbac.yaml) are provided for bootstrap. Review them, align their
namespace with `HB_NAMESPACE`, confirm the intended kubeconfig/context, and **apply them yourself**:

```sh
# User-operated only; HB_* must identify the intended cluster, not placeholders.
kubectl --kubeconfig "$HB_KUBECONFIG" --context "$HB_KUBE_CONTEXT" apply -f deploy/k8s/namespace.yaml
kubectl --kubeconfig "$HB_KUBECONFIG" --context "$HB_KUBE_CONTEXT" apply -f deploy/k8s/rbac.yaml
```

There is no automatic bootstrap or cluster-wide permission grant. The namespaced Role needs an
explicit RoleBinding to the actual kubeconfig User/Group/ServiceAccount identity; no binding is
guessed. Keep `automountServiceAccountToken: false` on trial Pods. Use an isolated namespace with
no workload-accessible secrets or unrelated workloads; an optional registry pull secret is only
for kubelet image pulls, not a mounted task credential.

- **Network policy:** supplied Kubernetes/ACK/ACS profiles default to `kwargs.network_enforcement: none`.
  Restricted/no-network tasks are explicitly rejected under that setting (including TB2 restricted
  verifier tasks); do not describe the default as supporting full-dataset execution. For those tasks,
  use a complete custom cluster profile with `network_enforcement: networkpolicy`, an egress-enforcing
  CNI and Role permission for NetworkPolicies. Verify enforcement, not merely API acceptance or an
  ACS annotation, and exclude additive allow policies selecting the trial Pods.
- The adapter implements dynamic phase policies before/through execution. Exact allowlists support
  **literal IPv4 addresses/CIDRs only**, not IPv6 or exact FQDN enforcement. If the model endpoint is
  a hostname during a restricted phase, use an approved fixed-IP gateway and declare its IP; do not
  silently open internet access or weaken the task contract. The optional cluster-DNS exception is
  TCP/UDP 53 to kube-system/kube-dns, not an FQDN firewall. Live phase transitions still need CNI verification.
- **Main-container identity:** runtime injection and Harbor's root setup require the main Pod
  container to run as **UID 0**. Non-root main Pod/container `runAsUser` or `runAsNonRoot: true`
  overrides must be explicitly rejected **before Pod creation**, not silently replaced. Supplied
  profiles already use `runAsUser: 0` with `privileged: false`; UID 0 does not mean privileged mode.
  A task's default agent user may still be non-root: execute via `su` from the root container,
  initializing that user's own `HOME` and `~/.nvm` rather than reusing root's home.
- **Startup/keepalive contract:** idle main containers and separate verifier containers use
  `sleep infinity` as arguments while preserving the image ENTRYPOINT. A known workload ENTRYPOINT
  recorded in prepared `hbench.json` is retained with the image CMD instead of an idle keepalive;
  explicit Compose `command`/`entrypoint` overrides are preserved. Main healthchecks remain in use,
  but `postStart`, readiness and `depends_on` do **not** guarantee strict startup order: setup hooks
  can race entrypoints.
- **Compose is a supported subset, not native Docker Compose parity.** Services share one Pod's
  network namespace with `hostAliases` pointing to `127.0.0.1` and shared log volumes, not isolated
  Compose networks. Cross-service overlaps in declared `ports`/`expose` must be rejected by
  container-port range and protocol (not merely published host port). **Undeclared listeners and
  actual runtime bind conflicts cannot be proven conflict-free** from declarations; conflicting
  tasks need rejection or a different environment. Non-log data mounts/build-only sidecars and
  unsupported Compose keys require explicit adaptation or rejection. See [compose](hbench/compose.py),
  [environment](hbench/environment.py) and [runtime setup](hbench/runtime_mount.py).
- **Final resources:** merged container requests must not exceed limits; reject `request > limit`
  before Pod creation. In particular, Compose CPU/memory limits must respect task requests rather
  than producing an invalid final Pod specification.
- **Task/harness main images** must be Linux/glibc-compatible and include Python 3, Bash, GNU
  `stdbuf`, `/bin/sh` and `tar`; musl-based images are unsupported. Runtime injection does not ensure
  every upstream image has the required ABI/tools. Preserve upstream task results/verifiers rather
  than rewriting them to pass; adapted runs are **not official upstream leaderboard submissions**.
  The identity rejection/user-home setup, keepalive and declared-port/resource checks above are
  covered only by offline unit tests that build Pod specs and simulate exec calls. Actual Docker,
  cluster and CNI behavior remains unverified; these contracts do not establish full environment
  equivalence.
- GPU quantity alone does not satisfy a requested GPU type (for example H100). Supply an explicit
  matching product `nodeSelector` or `gpu_type_node_selectors` mapping plus actual device-plugin
  resources/capacity. Unsupported types must fail; `--exclude-gpu` is an explicit reduced selection,
  not a full-dataset result.

### Local development checks (new runner)

```sh
python -m unittest discover -s tests/hbench -p 'test_*.py'
```

These tests use local fixtures/mocks, not real model or cluster services. A prior local offline Java
build succeeded with cached dependencies. When switching between legacy SNAPSHOT and new BETA
workflows, clear `adapters/agentscope-java/target/dependency/` or run `mvn clean` on the adapter before
rebuilding, to prevent mixed JAR versions on the wildcard classpath. Use `clean` for fresh packaging:

```sh
mvn -o -f adapters/agentscope-java/pom.xml clean package dependency:copy-dependencies -DincludeScope=runtime
```

The default framework dependency is `2.1.0-BETA1`; offline success requires those artifacts/plugins
already in the local Maven cache. This documentation update did not rerun packaging. The prior build
is Java packaging evidence only, **not a Docker build, a model-connected trial or real end-to-end
acceptance**. Legacy tests remain documented separately below.

| New path | Responsibility |
| --- | --- |
| [hbench/](hbench/) | CLI, jobs, dataset adaptations, native agents, Docker/Kubernetes environments, image tools and summaries |
| [configs/benchmarks/](configs/benchmarks/), [configs/harnesses/](configs/harnesses/) | Initial Hub pins and native harness profiles |
| [configs/models/](configs/models/), [configs/clusters/](configs/clusters/) | Model protocols/env references and Docker/Kubernetes/ACK/ACS execution profiles |
| [environments/hbench/](environments/hbench/), [deploy/k8s/](deploy/k8s/) | Runtime/tau3 image recipes; bootstrap manifests for Kubernetes/ACK/ACS only |
| [tests/hbench/](tests/hbench/) | Local fixture/mock verification for the new runner |

## Legacy — `eval.py` / `configs/suites/` (not the new entrypoint)

The following original migration guide is retained for its owned tasks, JSON suites, release checks,
native Java smoke requests and archive formats. Its task counts, defaults and runner requirements
apply **only to this legacy scope**; use the Harbor workflow above for the new initial three datasets.

### Scope and current readiness

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

### Task collections

There are **968 unique task IDs: 153 owned tasks and 815 external tasks**. Suites reuse these IDs;
they do not maintain duplicate task folders. `full` is a 63-task selection, not all 968.

| Collection | Count | Coverage and maintenance |
| --- | ---: | --- |
| [Harness](datasets/harness/) | 26 | Directly maintained framework regression scenarios |
| [Capabilities](datasets/capabilities/README.md) | 127 | Directly maintained capability tasks, fixtures and assertions |
| [Terminal Bench](external/terminal-bench/) | 8 | Selected terminal tasks, fetched at a pinned commit with local overlays |
| [Business](external/business/) | 807 | 165 tau-bench and 642 AppWorld IDs; wrappers generated from pinned declarations and a shared template |

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
`artifacts/datasets/`, not in committed upstream copies. See [dataset maintenance](datasets/README.md)
and [external preparation](external/README.md).

### Choose a suite

Profiles live in [`configs/suites/`](configs/suites/). Counts below are task selections, before
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

### Quick start: offline checks and task preparation

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
[`sources.lock.json`](external/business/sources.lock.json), including AppWorld data version `0.2.0`.
Record actual package/data identities and image digests used for execution.

### Run a native AgentScope smoke request

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

### Execute scored suites

A runner integrating the included components must:

1. Select and validate a suite, prepare/export tasks, and freeze the evaluation contract.
2. Supply isolated environments using the applicable [`environments/`](environments/) recipes,
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

### Scores, release checks and reports

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
as the candidate framework. See [environment recipes](environments/README.md).

Contracts run 27 declared production test classes and verify Surefire XML. These fixture/mock tests
complement real-model trials. `--verify-only` inspects existing XML, not a fresh run. Comparison needs
`manifest.json`, `summary.json` and the per-trial `result.json` files named in the summary. It verifies
evidence and writes `release-gate.json`, `release-gate.html` and `report.html`, without calling models
or repairing absent results. Exit codes: 0 (`pass`), 3 (`review`), 2 (`fail`/`invalid`). Unlocked model
aliases require review. See [release policies and evidence requirements](docs/release-regression.md).

The separate business renderer accepts the business batch format (`manifest.cases`, case results,
traces and ledger), not the unified release archive format:

```sh
mkdir -p artifacts
python report.py --local /path/to/business-archive --output artifacts/business-report.html
```

[`reports/templates/business.html`](reports/templates/business.html) is its maintained source template.
Generated HTML embeds data for offline viewing and belongs under `artifacts/`. `combine_reports.py`
and `compare_reports.py` handle business archives; consult their `--help`. They are separate from the
release gate.

### Layout and development checks

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
| `docs/` | [Protocol](docs/agent-protocol.md) and [release regression](docs/release-regression.md) guides |
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
