# Harness 评测与 Benchmark

这套 benchmark 评估 Agent 能否通过真实 Harness 完成任务，覆盖工具调用、文件操作、记忆、
上下文压缩、技能、子代理和会话生命周期。它用于 AgentScope Java 的发布回归，也支持与原生
Codex、Pi Agent 对比。适配器保留各框架自己的执行循环，通过任务结果与行为记录进行评测。

English: [English guide](README.md)。

本独立仓库将 `agentscope-java-evals` commit `3d4148ea4` 中跟踪的 Harness benchmark 内容
平铺到根目录。所有 benchmark 命令均从 `harness-bench` 仓库根目录执行；AgentScope Java
框架源码另置于独立 checkout。原 `artifacts/` 中历史第一方 `.py`/`.md`/`.sh` 工具另归档到
Git 忽略的 `tools/campaigns/`：尚未适配，仅供参考，不能当作直接可运行的服务，不含历史结果。
结果、虚拟环境、`target/` 构建产物和密钥不迁移；疑似凭据文件不迁移或脱敏，并排除提交。

## Harbor 新流程——主要入口：`python -m hbench`

新 [CLI](hbench/__main__.py) 基于 **Harbor 0.25.0** 生成并执行原生 **Codex、Pi、AgentScope Java**
的评测矩阵，可选 **单机 Docker（包括阿里云 ECS）** 或 **Kubernetes / ACK**，**ACS 可选**。
入口覆盖题库拉取与准备、对比计划、执行、镜像工具和离线报告。当前是已有本地 fixture/mock
检查的实现，**不是已完成真实评测验收**：这里尚未验证真实容器/模型/ECS/集群评测或 Docker
镜像构建。执行前须提供真实模型与执行主机/集群配置；不声明、补造或记录虚假成绩。

在独立 ECS 上跑完整 3 × 2 × 3 矩阵，请按[分步操作手册](#分步操作手册在独立-ecs-上跑完整-18-组合)执行。

### 首批 Hub 题库

新 [benchmark profiles](configs/benchmarks/) 与下方 legacy 选题分开。三套题库均固定到 Hub 的
不可变 `sha256` 版本；数量是准备阶段要求的题数，不是已经执行的结果。

| Profile | Hub 包 | 题数 | 固定版本 |
| --- | --- | ---: | --- |
| [tau3-bench](configs/benchmarks/tau3-bench.yaml) | `sierra-research/tau3-bench` | 375 | `sha256:a57304f682894ac061090769af771a3617664f3ff6e5417d4eadf8e30433e4d9` |
| [swe-bench-verified](configs/benchmarks/swe-bench-verified.yaml) | `swe-bench/swe-bench-verified` | 500 | `sha256:b934b0cc3dc800fe945eaf9f1623329db97ee3133c706d20644524c7759fb341` |
| [terminal-bench](configs/benchmarks/terminal-bench.yaml) | `terminal-bench/terminal-bench` | 66 | `sha256:5881c1b1c74a96f07baef00a7c869e09802704bc0dc912c870e7acccc11440da` |

用户所需的 tau2 系列在 Hub 中对应 **基于 tau2 的 tau3**，不是原始 tau2 包，也不是 legacy
中 165 道 tau-bench 选题。TB 的 66 道指此固定 Hub 包，不代表所有上游 Terminal-Bench 版本。

### 安装、拉取、准备

使用 Python 3.12+，所有命令从 `harness-bench` 仓库根目录执行：

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-evals.txt
python -m hbench list
python -m hbench datasets pull all
python -m hbench datasets prepare all
```

单题库的替代用法（不要对同一已有输出重复执行）：

```sh
python -m hbench datasets pull terminal-bench
python -m hbench datasets prepare terminal-bench
# 或者使用已在其它位置下载的包根目录：
python -m hbench datasets prepare tau3-bench --source /path/to/downloaded_dir
```

`pull BENCH` 通过 Harbor 按固定版本下载、导出到 `artifacts/hub/BENCH/`，不调用模型、不创建
集群资源。`prepare BENCH` 将缓存复制到 `artifacts/datasets/BENCH/`，检查题数，在 `manifest.json`
记录原始文件完整 SHA256 与适配信息，不改下载源。保留题目指令、fixture 和评分逻辑，仅适配
执行配置。Profile 中的 digest 是 Hub 元数据 pin，**不是 `prepare` 重新计算的压缩包 digest**；
显式 `--source` 的来源可信性由调用方负责。两者均支持 `all`，但 `--source` 只能指定单题库。
`--root` 选择 Hub 缓存，`prepare --output-root` 选择准备输出；已有下载/准备目录会拒绝覆盖。

### 模型与执行 profile 配置

使用 [configs/models/example.yaml](configs/models/example.yaml)，或通过 `--model` 传入自己的
YAML 路径。配置读取已导出的环境变量，`hbench` **不会自动加载 `.env`**。如自行维护 shell
环境文件，source 前先检查内容并确认 shell 兼容；密钥不写入 profile、plan 或提交。

| 候选模型配置 | 环境变量 |
| --- | --- |
| 实际模型 ID | `HB_MODEL_ID` |
| 模型 endpoint base URL | `HB_MODEL_BASE_URL` |
| 候选模型 API key，执行时解析 | `HB_MODEL_API_KEY` |

[Harness profiles](configs/harnesses/) 明确声明协议，不隐式转换：

| Harness | 固定 CLI 版本 | 支持的 `api_formats` |
| --- | --- | --- |
| Codex | `0.167.2` | 仅 `openai_responses` |
| Pi | `1.5.2` | `openai_chat_completions`、`openai_responses`、`anthropic_messages` |
| AgentScope Java | `1.2.0` | 仅 `openai_chat_completions` |

Pi 要求 Node >=22.19.0；固定基线使用外部 `pi-mcp-adapter` `7.0.1`，
Pi 兼容性门禁范围为 `>=1.0.0,<2.0.0`。

示例为 Codex 选择 Responses，为 Pi/Java 选择 Chat Completions。跨三个 Harness 对比需要
**同一个声明的模型 endpoint 同时支持 Responses 与 Chat Completions**。不能偷换协议或模型，
不兼容的选择会被拒绝；Pi 使用其它协议时需显式提供兼容的模型 profile。Harness 的轮数、
token、reasoning 配置位于各 YAML，必须作为比较预算的一部分记录。

tau3 的模拟用户与自然语言断言模型独立于候选模型配置：

| 辅助配置 | 环境变量 | 传给 tau3 |
| --- | --- | --- |
| 辅助 API key | `HB_TAU_API_KEY` | `OPENAI_API_KEY` |
| 辅助 endpoint | `HB_TAU_BASE_URL` | `OPENAI_BASE_URL` |
| 模拟用户模型 | `HB_TAU_USER_MODEL` | `TAU2_USER_MODEL` |
| 断言模型 | `HB_TAU_ASSERTIONS_MODEL` | `TAU2_NL_ASSERTIONS_MODEL` |

**候选、模拟用户、断言三类预算必须独立声明、核算**，包括题目/runtime 限制和辅助调用用量，
不能默认与候选共用预算。上述变量只选择模型/凭据，并不实现统一的辅助模型预算控制器。

本机执行选择 [docker](configs/clusters/docker.yaml)；另可选择
[kubernetes](configs/clusters/kubernetes.yaml)、[ack](configs/clusters/ack.yaml) 或可选
[acs](configs/clusters/acs.yaml)。`--cluster` 也接受完整的自定义执行 profile YAML 路径。
保留现有参数名：`--cluster docker` 选择的是**本机执行 profile**，不是 Kubernetes 集群。
ACK/ACS 仍用 Kubernetes 后端；ACS 另需启用 ACS 的 namespace、兼容的调度/网络和配额。

| 执行配置 | 环境变量 | 适用范围 |
| --- | --- | --- |
| 已构建的 runtime 镜像 | `HB_RUNTIME_IMAGE` | Docker：本机可用，默认 `hbench/harness-runtime:0.1.0`；Kubernetes/ACK/ACS：已发布且可达 |
| 隔离 namespace | `HB_NAMESPACE`，默认 `harness-bench` | 仅 Kubernetes/ACK/ACS |
| Kubeconfig 路径 / context | `HB_KUBECONFIG`、`HB_KUBE_CONTEXT` | 仅 Kubernetes/ACK/ACS |
| Registry 仓库前缀 | `HB_REGISTRY` | Kubernetes/ACK/ACS 的发布/mirror |
| 已有 image-pull secret 名称 | `HB_IMAGE_PULL_SECRET` | ACK/ACS；普通 Kubernetes 按需在自定义 profile 增加 `kwargs.image_pull_secret` |

Kubernetes/ACK/ACS profile 使用普通 Pod，不使用 privileged Docker-in-Docker、host networking 或
SandboxClaim。Registry 认证由外部工具维护；runner 不创建 registry、集群或 ECS。SWE-bench
verifier 的 `uv`/`pip` 需要互联网或可访问的包镜像；示例中 `HB_PIP_INDEX_URL` 同时配置
`PIP_INDEX_URL` 与 `UV_DEFAULT_INDEX`。

### 单机 Docker / ECS（Single-host Docker / ECS）

使用**专用评测主机**，不要与生产或无关 workload 混跑。推荐 Linux x86_64，需 Python 3.12+、
Docker Engine 和现代 Compose 插件（支持 `up --wait` 及
`depends_on: condition: service_completed_successfully`）；构建 runtime 另需 Maven 与 JDK 17+。
`hbench` controller 与 Docker daemon 必须在**同一台机器**。Docker 由用户预先安装、确认；
本流程不申请 ECS、不安装 Docker，不需要单节点 Kubernetes 或 kubeconfig/RBAC。

以下仅供用户自行做 preflight 检查，本次文档更新没有执行：

```sh
docker info
docker compose version
```

镜像只需对本机 daemon 可用，**无需 push 到 registry**。先生成固定 harness runtime 与两个
共享 tau3 镜像的 build 计划：

```sh
python -m hbench images build --runtime-image hbench/harness-runtime:0.1.0 \
  --tau3-dataset artifacts/datasets/tau3-bench
```

此命令只计划/暂存输入，不执行 Maven 或 Docker。**用户明确选择后**，才追加 `--execute` 做
本机构建，且从不 push。默认 `hbench/tau3-main:0.1.0`、`hbench/tau3-runtime:0.1.0` 与已准备
的 tau3 题库匹配；tau3 必须**同时构建两者**，不能只构建 main。TB/SWE 可省略 `--tau3-dataset`
生成 runtime-only 计划，其任务镜像也须在本机可用。实际 build 需要能访问 registry/外网来拉取
基础镜像与依赖，或已有验证过的本机缓存。若更改 tag，检查 build plan 的 `image_map`，以
`plan --image-map BUILD_PLAN_JSON` 生成新计划；映射覆盖 main、runtime 和 sidecar 镜像。

薄子类 `hbench.docker_environment:FrozenDockerEnvironment` 使用 Harbor 0.25 原生 Docker 后端
管理容器/Compose、网络、资源、日志、exec、传输和清理。一次性 init 服务从同一个固定
harness runtime 填充项目作用域的 named volumes；main 只读挂载这些卷，并等待
`service_completed_successfully`。Setup 以 root 执行，题目的 agent 用户仍由 Harbor 原生切换。
不复用 Kubernetes Compose 翻译，也不把服务全部强制到 localhost：保留原生 sidecar DNS/网络、
volumes、healthchecks 与 `depends_on`。默认 profile 为 `name: docker`、`backend: docker`，使用
`auto_mounting: true`、`keep_containers: false`。不要把 Docker socket、宿主 secrets 或云凭据挂载到任务中。
Runtime overlay 保留自身的 service/volume 名称与挂载路径；冲突的任务挂载，以及 Compose
`include`、`extends`、`volumes_from` 会明确拒绝，不做不完整的翻译。

Task/harness main 镜像仍需兼容 Linux/glibc，具备 Python 3、Bash、GNU `stdbuf`、`/bin/sh` 与 `tar`；
挂载 runtime 不保证所有上游镜像等价。GPU 任务需要**匹配的实体 GPU 型号与 NVIDIA Container Toolkit**；
型号校验使用宿主只读 `nvidia-smi`，不能只按数量判断或忽略 H100 要求。动态网络策略沿用 Harbor
原生 egress sidecar 与宿主 kernel 能力，不依赖 CNI/Kubernetes 权限。GPU 与网络强制能力仍需
实机验证，**不能宣称所有任务都可无条件运行**。

### 生成计划、dry-run、稍后执行、汇总

题库准备完成后，placeholder 模型 profile 即可生成本机 Docker 计划。Docker dry-run 还会执行
Harbor 原生的只读 `docker info` preflight，因此本机 Docker daemon 必须已启动；这不是容器、
镜像、GPU 或网络能力验收。以下命令**只生成、校验计划，不执行 trial**：

```sh
python -m hbench plan \
  --benchmark tau3-bench,swe-bench-verified,terminal-bench \
  --harness codex,pi,agentscope-java --model example --cluster docker \
  --dataset-root artifacts/datasets --n-tasks 1 --exclude-gpu --concurrency 1 \
  --name smoke-docker --output artifacts/plans/smoke-docker.yaml
python -m hbench run --config artifacts/plans/smoke-docker.yaml --dry-run
```

其它后端可选 `--cluster kubernetes`、`--cluster ack` 或 `--cluster acs`，使用新的 plan 名称/输出。
`--n-tasks 1` 是**每个 benchmark 选一道**，本例计划 9 个 trial（3 题库 × 3 Harness × 1 次），
不是成绩。`--exclude-gpu` 是缩减选题：当前 prepared Terminal-Bench 包恰有 **3 项声明 H100**，
因此不代表全量成绩。`plan` 只写 job YAML 与 `smoke-docker.plan.json` 契约，不执行；
`--concurrency`、`--attempts`、`--tasks`、`--exclude-gpu` 控制选题和矩阵。Dry-run 让 Harbor
校验 job、跳过凭据检查，不启动容器、不提交 Pod、不调用模型、不构建镜像，也不证明真实环境
就绪。输出路径必须全新；重复执行或修改候选模型/执行 profile 后应重新生成 plan。
候选模型与执行 profile 的非密钥配置在 plan 时固定；凭据引用和模型 profile 中保留的
`environment_env` / `verifier_env` 模板在 run 时解析，包括辅助模型 ID、endpoint 与包索引。
为保证对比可复现，调用方应保持这些解析后的辅助非密钥值一致，并单独记录，不包含凭据。

**真实 run 需用户单独明确选择：** 先提供真实模型凭据，确认本机镜像与 Docker/GPU/网络能力
（Kubernetes/ACK/ACS 则确认已发布镜像可达及 context/RBAC/CNI），按确认后的配置重新生成 plan。
只有明确选择执行可能付费的模型调用后，才自行运行：

```sh
# 仅用户操作的真实执行，不属于上面的 plan/dry-run。
python -m hbench run --config artifacts/plans/smoke-docker.yaml
python -m hbench summary artifacts/jobs/smoke-docker --output artifacts/reports/smoke-docker
```

默认 `example-model`、`example.invalid` endpoint/runtime 镜像会拒绝真实 `run`，但可用于 dry-run；
Docker 默认本机 runtime tag 不证明镜像存在。真实 `run` 会调用模型并创建/删除容器，或在
Kubernetes 上创建/删除 trial Pod 及配置启用的 NetworkPolicy。**本次未构建任何镜像，未运行真实
容器/模型/ECS，也未操作集群。** `summary artifacts/jobs/NAME --output artifacts/reports/NAME`
从已有 Harbor 证据生成 `summary.json`、`report.html`，不调用模型。错误/未评分项不算通过；
有 Harbor job 计划总数时报告缺失项，不补造结果，也不生成官方排行榜成绩。

### 矩阵评测：题库 × 模型 × Harness

`plan --model` 支持逗号分隔多个模型 profile。可复现的对比建议把整个矩阵写进一个配置文件，用
`matrix` 生成计划。`configs/matrices/bailian-18.yaml` 在**一个** Harbor job 中覆盖
3 个题库 × 2 个模型 × 3 个 Harness = 18 个组合：

```sh
python -m hbench matrix --config configs/matrices/bailian-18.yaml \
  --output artifacts/plans/bailian-18.yaml
python -m hbench run --config artifacts/plans/bailian-18.yaml --dry-run
```

矩阵字段包括 `name`、`cluster`（`docker`、`kubernetes`、`ack`、`acs` 或 YAML 路径）、`dataset_root`、
`benchmarks`、`models`、`harnesses`、`concurrency`、`attempts`、`selection`（`n_tasks`、`tasks`、
`exclude_gpu`）与 `image_map`。条目可以是 `configs/` 下的 profile 名，也可以是 YAML 路径；相对路径
都相对仓库根目录。job 名默认为 `<name>-<UTC 时间戳>`，每次生成的计划只能执行一次。切换 Docker
和 ACK 只需修改 `cluster`，然后重新生成计划。Harbor 用一个全局队列调度全部题目 × 模型 × Harness
trial。`concurrency` 是所有组合同时运行 trial 的**总数**，不是每个组合各自的并发。`.plan.json`
契约列出每个组合及其计划 trial 数；`summary` 按（harness, model, dataset）每组输出一行。
由于 `environment_env`/`verifier_env` 作用于整个 job，同一矩阵中各模型解析后的名称必须不同，
这两项配置必须一致。

`configs/models/bailian-model-{a,b}.yaml` 对接百炼 OpenAI 兼容接口，文件中不包含凭据。请在
controller shell 中设置 `HB_BAILIAN_API_KEY`，用 `HB_MODEL_A_ID`、`HB_MODEL_B_ID` 指定模型 ID；
`HB_BAILIAN_BASE_URL` 可选，默认为 `https://dashscope.aliyuncs.com/compatible-mode/v1`。codex 使用
Responses API，所选模型需在百炼 `/responses` 上可用。不裁剪时，当前已准备题库共
（66 + 500 + 375）题 × 2 模型 × 3 Harness = 5646 个 trial，其中 3 道 Terminal-Bench 题声明需要
H100 GPU。冒烟验证可先设 `selection.n_tasks: 1`（18 个 trial）。

一键运行：`cp .env.example .env` 后填写（`.env` 已被 git 忽略），执行 `bash deploy/run-matrix.sh`。脚本会
加载该文件、检查必填项，生成新计划（`HB_CLUSTER`、`HB_CONCURRENCY`、`HB_N_TASKS` 可覆盖矩阵配置），
先 dry-run，再执行**真实、会计费**的运行并生成 `summary`。设 `HB_DRY_RUN_ONLY=1` 时在任何模型调用前停止。

### 分步操作手册：在独立 ECS 上跑完整 18 组合

这是端到端运行 `configs/matrices/bailian-18.yaml` 的操作清单。A 部分在一台 ECS 上使用原生 Docker，
不需要镜像仓库或集群；B 部分列出以后把同一矩阵迁到 ACK 时需要追加的步骤。所有命令都由你执行。
**本仓库尚未执行过下面任何一步**：没有构建镜像、没有推送，也没有跑过真实 trial。标注**计费**的
步骤会调用百炼模型。

#### A1. 准备 ECS 主机

建议配置：

- **系统 / CPU：** Ubuntu 24.04 x86_64，系统自带 Python 3.12。
- **地域：** 香港，或其他可直连 Docker Hub、PyPI、npm、Maven Central 和 GitHub 的地域。内地地域需要
  配置镜像源 / 包源。
- **规格：** 每个并发 trial 至少 4 vCPU、8 GiB 内存（单个 tau3 trial 申请 4 CPU / 8 GiB）。例如
  `HB_CONCURRENCY=4` 用 16 vCPU / 64 GiB，8 并发用 32 vCPU / 128 GiB。
- **磁盘：** Docker 镜像需要一块大数据盘。500 个 SWE-bench 实例镜像和 160 个 Terminal-Bench 镜像会被拉取
  并保留在本地，建议约 1 TB（粗略估计，未实测）。
- **GPU：** 不需要，只有 3 道声明 H100 的 Terminal-Bench 题例外，见 A7。

安装工具：

```sh
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv git rsync curl openjdk-21-jdk-headless maven skopeo
curl -fsSL https://get.docker.com -o get-docker.sh && sudo sh get-docker.sh   # 内地地域加 --mirror Aliyun
sudo usermod -aG docker "$USER" && newgrp docker
# 可选：把 Docker 数据放到大盘上，例如挂载在 /data
#   echo '{"data-root": "/data/docker"}' | sudo tee /etc/docker/daemon.json && sudo systemctl restart docker
docker info && docker compose version && java -version && mvn -v && python3.12 --version
docker login            # 使用 Docker Hub 账号，避免匿名拉取限流
```

`skopeo` 只有 B 部分需要。`docker compose version` 必须显示 Compose v2 插件。

#### A2. 把代码拷到主机

仓库大部分文件还没有提交到 Git，所以要拷贝工作区，不能 clone。**在你的笔记本上、仓库根目录**执行：

```sh
rsync -az --exclude /.venv --exclude /artifacts --exclude target/ --exclude .env --exclude .DS_Store \
  ./ <user>@<ECS_IP>:~/harness-bench/
# 可选：复用本地已准备好的题库（约 0.6 GB），这样 A3 可以跳过 pull/prepare
rsync -az artifacts/datasets <user>@<ECS_IP>:~/harness-bench/artifacts/
```

#### A3. Python 环境与题库（在 ECS 上）

```sh
cd ~/harness-bench
python3.12 -m venv .venv && . .venv/bin/activate
python -m pip install -r requirements-evals.txt
python -m hbench list
# 如果 A2 已拷贝 artifacts/datasets，跳过下面两行
python -m hbench datasets pull all
python -m hbench datasets prepare all
python -m unittest discover -s tests/hbench -p 'test_*.py'   # 可选的离线自检
```

完成后，`artifacts/datasets/{terminal-bench,swe-bench-verified,tau3-bench}` 下应各有一个
`manifest.json`，题数分别为 66、500、375。

`datasets/bundled-prepared` 分支已提交这三套准备好的题库（约 0.6 GB），检出该分支即可跳过
`datasets pull`/`prepare`。其中 `manifest.json` 记录的是准备机器上的绝对路径，仅作来源信息，运行时不使用。

#### A4. 填写环境变量文件

```sh
cp .env.example .env && chmod 600 .env
vi .env
```

| 变量 | 填什么 |
| --- | --- |
| `HB_BAILIAN_API_KEY` | 百炼 API Key（密钥，只写在 `.env` 里）。 |
| `HB_BAILIAN_BASE_URL` | 默认 `https://dashscope.aliyuncs.com/compatible-mode/v1`，也可以用 `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`。 |
| `HB_MODEL_A_ID`、`HB_MODEL_B_ID` | 两个候选模型 ID。两者必须不同，且都要在百炼 `/responses` 接口上可用，因为 codex 使用 Responses API。 |
| `HB_TAU_API_KEY`、`HB_TAU_BASE_URL` | tau3 模拟用户 / 断言模型的接口，默认复用百炼的值。 |
| `HB_TAU_USER_MODEL`、`HB_TAU_ASSERTIONS_MODEL` | tau3 辅助模型，LiteLLM 一般写成 `openai/<model-id>`。两者可以是同一个模型。需要对比的多次运行之间保持不变。 |
| `HB_PIP_INDEX_URL` | 可选，SWE-bench verifier 使用的包源；内地主机可用 `https://mirrors.aliyun.com/pypi/simple/`。 |
| `HB_RUNTIME_IMAGE` | Docker 模式保持 `hbench/harness-runtime:0.1.0`，必须与 A5 构建的 tag 一致。 |
| `HB_CLUSTER` | A 部分填 `docker`。 |
| `HB_CONCURRENCY` | 18 个组合合计同时运行的 trial 数，按主机规格设置（见 A1）。 |
| `HB_N_TASKS` | 冒烟时填 `1`（18 个 trial）；留空表示完整题库（5646 个 trial）。 |
| `HB_DRY_RUN_ONLY` | `1` 表示 dry-run 后停止；`0` 表示继续真实、计费的运行。 |

可选：在主机上检查连通性。这一步不消耗 token：返回 200 表示正常，401 表示 Key 错误；只要拿到任意
HTTP 状态码，就说明接口可达。

```sh
set -a; . ./.env; set +a
curl -sS -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $HB_BAILIAN_API_KEY" "$HB_BAILIAN_BASE_URL/models"
```

#### A5. 本地构建镜像（Maven + Docker，不推送）

Harness 运行时镜像打包了 codex、pi 和 AgentScope Java CLI，两个 tau3 镜像用于 tau3 题目。先预览
计划，再构建：

```sh
python -m hbench images build --runtime-image hbench/harness-runtime:0.1.0 \
  --tau3-dataset artifacts/datasets/tau3-bench              # 仅生成计划
python -m hbench images build --runtime-image hbench/harness-runtime:0.1.0 \
  --tau3-dataset artifacts/datasets/tau3-bench --execute    # 执行 mvn clean package 和 3 次 docker build
docker image ls | grep -E 'hbench/(harness-runtime|tau3-main|tau3-runtime)'
```

本地应出现三个镜像：`hbench/harness-runtime:0.1.0`、`hbench/tau3-main:0.1.0` 和
`hbench/tau3-runtime:0.1.0`。输出中的 `plan_path` 记录了本次构建的内容。修改
`adapters/agentscope-java` 或 `environments/hbench/` 后需要重新构建。

#### A6. 可选：预拉取题目镜像

Harbor 会按需拉取 Terminal-Bench 和 SWE-bench 镜像。预先拉取可以把下载问题和限流与模型运行分开：

```sh
for d in terminal-bench swe-bench-verified; do
  python -m hbench images list "artifacts/datasets/$d" |
    python -c 'import json,sys; print("\n".join(i for i in json.load(sys.stdin) if not i.startswith("hbench/")))'
done | sed -E 's/:[^/@]+@/@/' | sort -u > artifacts/task-images.txt
wc -l artifacts/task-images.txt
xargs -n1 docker pull < artifacts/task-images.txt
```

`sed` 会在镜像已由 digest 固定时去掉 tag。用 `docker system df` 观察磁盘占用。

#### A7. 冒烟运行：每个题库 1 题，共 18 个 trial

在 `.env` 中设 `HB_N_TASKS=1`。先跑 dry-run，它不会调用模型：

```sh
HB_DRY_RUN_ONLY=1 bash deploy/run-matrix.sh     # 生成新计划并执行 Harbor dry-run，不调用模型
bash deploy/run-matrix.sh                       # 计费：18 个真实 trial，之后生成 summary
```

打开最后打印的报告路径（`artifacts/reports/<name>/report.html`）。检查每个组合都有 trial 记录，并且
出现的错误是题目本身失败，而不是环境问题。环境问题包括缺少镜像、模型接口返回 401 或 404，以及
codex 无法使用 `/responses`。

**GPU 题：** 3 道 Terminal-Bench 题声明需要 H100。在纯 CPU 主机上，它们会被 GPU 预检拒绝并记为错误，
不算通过。如果要排除它们，在矩阵文件中设 `selection.exclude_gpu: true`。这属于缩减题集，报告结果时
需要注明。

#### A8. 完整运行：全部题目，共 5646 个 trial

清空 `.env` 中的 `HB_N_TASKS`，设置好 `HB_CONCURRENCY`，然后在后台启动，退出登录后也能继续运行：

```sh
nohup bash deploy/run-matrix.sh > artifacts/run-matrix.log 2>&1 &
tail -f artifacts/run-matrix.log
ls artifacts/jobs/                      # job 目录名为 <HB_RUN_NAME>-<UTC 时间戳>
docker ps                               # 正在运行的 trial 容器
```

这一步会计费，而且耗时较长。每次执行都会生成新的、名称唯一的计划和 job。运行过程中如果想看阶段
报告：

```sh
python -m hbench summary artifacts/jobs/<name> --output artifacts/reports/<name>-interim
```

如果 controller 进程中断，有两种做法：

- 重新执行脚本，开一个新 job。所有 trial 会重跑。
- 对同一个 job 目录尝试 Harbor 原生的 resume。这里尚未用 hbench 实际验证过。

  ```sh
  set -a; . ./.env; set +a
  PYTHONPATH="$PWD" .venv/bin/harbor jobs resume -p artifacts/jobs/<name>
  ```

#### A9. 收集结果

脚本会生成 `artifacts/reports/<name>/summary.json` 和 `report.html`，按（harness, model, dataset）每组
一行，共 18 行。**在笔记本上**执行以下命令拷回：

```sh
rsync -az <user>@<ECS_IP>:~/harness-bench/artifacts/reports/ ./artifacts/reports/
```

`artifacts/` 和 `.env` 已被 Git 忽略。不要提交它们，也不要把 Key 贴到工单或聊天里。

#### B. 以后：在 ACK 上跑同一矩阵

额外前提：

- 一个 ACR 命名空间，例如 `registry.cn-hongkong.aliyuncs.com/<ns>`。
- 能从该仓库拉取镜像的 ACK 集群，最好在同一地域。
- ECS 上有该集群的 kubeconfig。
- Pod 能访问百炼接口。

以下步骤都在 ECS 上执行。

**B1. 用仓库前缀构建镜像，然后推送。** build 不会推送，需要你手动推送这三个镜像：

```sh
export REG=registry.cn-hongkong.aliyuncs.com/<ns>        # 即 HB_REGISTRY
python -m hbench images build --runtime-image "$REG/harness-runtime:0.1.0" \
  --tau3-dataset artifacts/datasets/tau3-bench --tau3-image-prefix "$REG/tau3" --execute
BUILD_PLAN_JSON=<上面输出的 plan_path>
docker login "${REG%%/*}"
docker push "$REG/harness-runtime:0.1.0"
docker push "$REG/tau3-main:0.1.0"
docker push "$REG/tau3-runtime:0.1.0"
```

**B2. 把每个题库需要的镜像 mirror 到 ACR。** 这一步使用 skopeo，会把 Docker Hub 上的题目镜像和 B1 的
三个镜像复制推送到 ACR。SWE-bench 的数据量很大。建议先跑一遍仅生成计划的命令并检查：

```sh
skopeo login docker.io && skopeo login "${REG%%/*}"
for d in terminal-bench swe-bench-verified tau3-bench; do
  python -m hbench images mirror "artifacts/datasets/$d" --registry "$REG" \
    --built-image-map "$BUILD_PLAN_JSON" --output "artifacts/mirror-plan-$d.json"            # 仅计划
done
for d in terminal-bench swe-bench-verified tau3-bench; do
  python -m hbench images mirror "artifacts/datasets/$d" --registry "$REG" \
    --built-image-map "$BUILD_PLAN_JSON" --output "artifacts/image-map-$d.json" --execute    # 实际复制/推送
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

**B3. 新建 ACK 矩阵文件**，指向合并后的映射：

```sh
cp configs/matrices/bailian-18.yaml configs/matrices/bailian-18-ack.yaml
# 编辑：cluster: ack   以及   image_map: artifacts/image-map.json
```

**B4. 初始化命名空间。** 只需执行一次：先审阅清单、确认 context。把 Role 绑定到你的 kubeconfig 实际
使用的身份（用 `kubectl auth whoami` 查看），不会自动猜测绑定对象：

```sh
export KUBECONFIG=<path> CTX=<context>
kubectl --context "$CTX" apply -f deploy/k8s/namespace.yaml -f deploy/k8s/rbac.yaml
kubectl --context "$CTX" -n harness-bench create rolebinding harness-bench-runner \
  --role=harness-bench-runner --user=<kubeconfig-user>
kubectl --context "$CTX" -n harness-bench auth can-i create pods/exec
# 镜像拉取权限：使用 ACK 的 ACR 免密组件，或者创建拉取 Secret
read -rs ACR_PASSWORD
kubectl --context "$CTX" -n harness-bench create secret docker-registry hb-acr \
  --docker-server="${REG%%/*}" --docker-username=<acr-user> --docker-password="$ACR_PASSWORD"
unset ACR_PASSWORD
```

**B5. 修改 `.env` 后运行。** 设置以下值，然后照常执行 A7（冒烟）和 A8（完整）：

```sh
HB_MATRIX=configs/matrices/bailian-18-ack.yaml
HB_CLUSTER=ack
HB_REGISTRY=registry.cn-hongkong.aliyuncs.com/<ns>
HB_RUNTIME_IMAGE=registry.cn-hongkong.aliyuncs.com/<ns>/harness-runtime:0.1.0
HB_NAMESPACE=harness-bench
HB_KUBECONFIG=<path>
HB_KUBE_CONTEXT=<context>
HB_IMAGE_PULL_SECRET=hb-acr          # 使用免密组件时留空
```

ACK 与 Docker 的限制不同：

- **网络策略：** 提供的 ACK profile 使用 `network_enforcement: none`，限制网络的题目会被拒绝。要运行
  这些题，需要使用 `networkpolicy` 的自定义 profile，以及能强制出网策略的 CNI。
- **GPU 题：** 需要匹配的 H100 节点。

详见下文 Kubernetes 一节。

### 镜像清单、mirror 与 build 计划

`DATASET` 是已下载/准备的题库**目录**，不是 profile 名。接口以
[hbench/__main__.py](hbench/__main__.py) 和 [hbench/images.py](hbench/images.py) 为准：

```sh
python -m hbench images list artifacts/datasets/terminal-bench
# 仅 runtime 计划，不需要 tau3 数据：
python -m hbench images build --runtime-image reg/org/harness-runtime:0.1.0
# 或者显式加入共享 tau3 镜像：
python -m hbench images build --runtime-image reg/org/harness-runtime:0.1.0 \
  --tau3-dataset artifacts/datasets/tau3-bench --tau3-image-prefix reg/org/tau3
```

默认**只列清单/生成计划**：build 在 `artifacts/build/` 写本地计划，仅显式请求 tau3 时暂存共享
上下文，不执行 Maven 或 Docker。省略 `--tau3-dataset` 时传入 `dataset=None`，**只计划 harness
runtime**，不会回退到缓存的 tau3 数据。显式 `--tau3-dataset DATASET` 才加入共享 tau3 main/runtime
镜像。可选 `--tau3-image-prefix` 默认 `hbench/tau3`，不能带 tag 或 digest；`reg/org/tau3`
产生 `reg/org/tau3-main:0.1.0`、`reg/org/tau3-runtime:0.1.0`。

CLI 必须传入 `--runtime-image`；runtime 的逻辑引用（也是 Python builder 默认值）为
`hbench/harness-runtime:0.1.0`。Build 返回逻辑引用到构建目标的 `image_map`，以及已保存的
build-plan JSON 路径 `plan_path`。Runtime-only 只映射逻辑 runtime；加入 tau3 时另映射
`hbench/tau3-main:0.1.0`、`hbench/tau3-runtime:0.1.0`。计划中的 tag **不证明镜像已构建或发布**。

- **仅在用户明确授权后使用 `build --execute`**，先在本机执行 Maven
  `clean package dependency:copy-dependencies -DincludeScope=runtime`，再执行 `docker build`。
  `clean` 避免残留的 SNAPSHOT/BETA JAR 混用 wildcard classpath。Build **从不 push**。
  Docker 执行只需构建目标在本机可用；Kubernetes/ACK/ACS 需单独发布到集群可达的 registry，
  并通过外部工具维护认证。Java POM 默认 `candidate.version=2.1.0-BETA1`。
- **仅在需要 registry mirror 时**，构建并单独发布目标后，通过
  `mirror --built-image-map BUILD_PLAN_JSON` 传入已保存的 build plan。其 `image_map` 在
  mirror 计划/复制前，将逻辑 `hbench/*` 引用替换为已发布的源镜像引用；发布时若改用其它引用，
  须先更新映射。Mirror 不会构建或发布缺失的源镜像，**不能对仅在本机存在的逻辑 tag 执行**；
  本机 Docker 路径跳过 mirror。

针对远端 registry 源，将 `BUILD_PLAN_JSON` 设置为相应 build 返回的 `plan_path`（TB/SWE 使用
runtime-only 计划，tau3 使用 runtime + tau3 计划），再生成 mirror 计划：

```sh
python -m hbench images mirror artifacts/datasets/terminal-bench \
  --registry reg/org --built-image-map "$BUILD_PLAN_JSON" --output artifacts/image-map.json
```

- 不传 `--execute` 时，mirror 只写映射与命令，不执行 skopeo。`--registry` 指定目标 registry/org
  前缀，`--output` 必须是全新的 JSON 路径。
- **仅在用户明确授权后使用 `mirror --execute`**，执行
  `skopeo copy --all --preserve-digests` 并检查 digest，**会向目标 registry copy/push**。
  先通过外部工具维护源/目标认证；替换后的每个源镜像必须在远端可解析。仍有逻辑 `hbench/*`
  引用时会拒绝执行。
- Mirror 输出的 `image_map` 保留原始逻辑引用的映射。检查映射，设置真实已发布的
  `HB_RUNTIME_IMAGE`，再以 `--image-map artifacts/image-map.json` 生成**新** job plan；
  仅有清单/映射不证明镜像可用。

这里未执行任何镜像 `--execute`。Docker 构建、实际 registry/集群行为仍待验证；tau2 直接依赖
已固定，但不能宣称已提供预解析的完整传递依赖锁。

### Kubernetes / ACK / ACS 初始化与执行限制

**本节仅适用于 Kubernetes/ACK/ACS**，不适用于原生 Docker 后端。尤其不能将这里的
`network_enforcement: none`、Pod 身份/启动、Compose 翻译及 CNI/RBAC 限制套给 Docker；
Docker 原生网络与 runtime 契约见上方单机章节。

初始化只提供 [deploy/k8s/namespace.yaml](deploy/k8s/namespace.yaml) 和
[deploy/k8s/rbac.yaml](deploy/k8s/rbac.yaml)。先检查清单，将 namespace 与 `HB_NAMESPACE` 对齐，
确认实际 kubeconfig/context，再由**用户自己 apply**：

```sh
# 仅用户操作；HB_* 必须指向已确认的实际集群，不能是 placeholder。
kubectl --kubeconfig "$HB_KUBECONFIG" --context "$HB_KUBE_CONTEXT" apply -f deploy/k8s/namespace.yaml
kubectl --kubeconfig "$HB_KUBECONFIG" --context "$HB_KUBE_CONTEXT" apply -f deploy/k8s/rbac.yaml
```

没有自动 bootstrap 或默认的全局授权。Namespaced Role 必须显式用 RoleBinding 绑定当前
kubeconfig 的 User/Group/ServiceAccount 身份，不猜测绑定对象。Trial Pod 保持
`automountServiceAccountToken: false`。使用无题目可访问 secrets、无无关 workload 的隔离 namespace；
可选的 registry pull secret 仅供 kubelet 拉镜像，不能挂载成题目凭据。

- **网络策略：** 示例 Kubernetes/ACK/ACS profile 默认 `kwargs.network_enforcement: none`，明确拒绝 restricted /
  no-network 任务，包括 TB2 的 restricted verifier 题；不能宣称默认支持全量执行。这些题需要
  在完整自定义集群 profile 中开启 `network_enforcement: networkpolicy`、有实际强制 egress 的 CNI，
  并授予 namespaced Role 操作 NetworkPolicy 的权限。应验证实际拦截，不仅是 API 接受或 ACS 注解；
  排除其它选择这些 trial Pod 的叠加放行策略。
- 适配器实现执行前及执行阶段的动态 phase 网络策略。精确 allowlist **仅支持字面量 IPv4 地址/CIDR**，
  不支持 IPv6 或精确 FQDN。Restricted 阶段如模型 endpoint 是 hostname，应使用经批准的固定 IP
  网关并声明其 IP，不能悄悄开放互联网或削弱题目契约。可选集群 DNS 例外仅为访问
  kube-system/kube-dns 的 TCP/UDP 53，不是 FQDN 防火墙；真实 phase 切换仍需 CNI 验证。
- **Main 容器身份：** runtime 注入与 Harbor 的 root setup 要求 main Pod 容器以 **UID 0** 运行。
  main 的 Pod/container 级非 root `runAsUser` 或 `runAsNonRoot: true` override 必须在
  **创建 Pod 前显式拒绝**，不能悄悄覆盖。示例 profile 已使用 `runAsUser: 0` 和
  `privileged: false`；UID 0 不等于 privileged 模式。题目的默认 agent 用户仍可为非 root：
  从 root 容器通过 `su` 执行，并初始化该用户自己的 `HOME` 与 `~/.nvm`，而非复用 root 的 home。
- **启动/保活契约：** 空闲 main 与独立 verifier 容器以 `sleep infinity` 作为参数保活，保留镜像
  ENTRYPOINT。已准备 `hbench.json` 记录的已知 workload ENTRYPOINT 配合镜像 CMD 保留，
  不替换为空闲保活命令；显式 Compose `command` / `entrypoint` override 保留。Main healthcheck
  仍保留，但 `postStart`、readiness / `depends_on` **不保证严格启动顺序**，setup hook 可能
  与 entrypoint 并发。
- **Compose 是支持子集，不是原生 Docker Compose 的完全等价实现。** 服务在同一个 Pod 内共享
  网络，`hostAliases` 指向 `127.0.0.1`，共享日志卷，不具有独立 Compose 网络。跨服务声明的
  `ports` / `expose` 重叠必须按容器端口范围及协议拒绝，不能只比较发布的 host 端口。
  **未声明的监听器与实际运行时绑定冲突无法仅凭声明证明不存在**；冲突题目须拒绝或另实现环境。
  非日志数据挂载、仅 build 的 sidecar、不支持的 Compose 字段需显式适配或拒绝。限制见
  [compose](hbench/compose.py)、[environment](hbench/environment.py)、
  [runtime setup](hbench/runtime_mount.py)。
- **最终资源：** 合并后的容器 request 不得超过 limit；`request > limit` 必须在创建 Pod 前拒绝。
  尤其 Compose CPU/内存 limit 必须满足题目 request，不能生成无效的最终 Pod 规格。
- **Task/harness main 镜像**须兼容 Linux/glibc，具备 Python 3、Bash、GNU `stdbuf`、`/bin/sh` 与 `tar`；
  musl 镜像不支持。注入 runtime 不保证所有上游镜像都具有必需 ABI/工具。不改上游题目结果/验证器
  来制造通过；适配执行**不是官方上游排行榜提交**。
  上述身份拒绝/用户 home 初始化、保活、声明端口与资源检查仅由离线单元测试覆盖（构造 Pod 规格并模拟
  exec）。实际 Docker、集群与 CNI 行为仍待验证；这些契约不证明执行环境完全等价。
- GPU 数量不能自动满足指定型号（如 H100）。必须显式配置匹配的 product `nodeSelector` 或
  `gpu_type_node_selectors` 映射，并具有实际 device-plugin 资源/容量。不支持的型号必须失败；
  `--exclude-gpu` 是显式缩减选题，不代表全量结果。

### 本地开发检查（新 runner）

```sh
python -m unittest discover -s tests/hbench -p 'test_*.py'
```

这些测试使用本地 fixture/mock，不调用真实模型或集群服务。此前已有缓存依赖下，本地 Java
离线打包成功。在 legacy SNAPSHOT 与新 BETA 流程之间切换时，重新构建前须清空
`adapters/agentscope-java/target/dependency/`，或对适配器执行 `mvn clean`，避免 wildcard classpath
混用不同版本的 JAR。重新打包时使用 `clean`：

```sh
mvn -o -f adapters/agentscope-java/pom.xml clean package dependency:copy-dependencies -DincludeScope=runtime
```

框架依赖默认 `2.1.0-BETA1`；离线成功要求相关依赖/plugin 已在本机 Maven 缓存。
本次文档更新没有重新执行打包。此前的成功只是 Java 打包证据，**不是 Docker 构建、真实模型 trial
或端到端真实验收**。Legacy 测试在下方单独保留。

| 新路径 | 职责 |
| --- | --- |
| [hbench/](hbench/) | CLI、job、题库适配、原生 Agent、Docker/Kubernetes 环境、镜像工具与汇总 |
| [configs/benchmarks/](configs/benchmarks/)、[configs/harnesses/](configs/harnesses/) | 首批 Hub pins 与原生 Harness profiles |
| [configs/models/](configs/models/)、[configs/clusters/](configs/clusters/) | 模型协议/env 引用与 Docker/Kubernetes/ACK/ACS 执行 profiles |
| [environments/hbench/](environments/hbench/)、[deploy/k8s/](deploy/k8s/) | Runtime/tau3 镜像配方；初始化清单仅供 Kubernetes/ACK/ACS |
| [tests/hbench/](tests/hbench/) | 新 runner 的本地 fixture/mock 验证 |

## Legacy——`eval.py` / `configs/suites/`（不是新入口）

以下保留原迁移说明，包括自有题目、JSON 套件、发布检查、原生 Java 冒烟请求和归档格式。
题数、默认值和 runner 要求**仅适用于 legacy 范围**；首批三题库评测请使用上面的 Harbor 新流程。

### 定位与当前可用范围

适合用它排查能力短板、验证 Harness 改动是否引入回归，以及在明确的题目、模型和预算约束下
比较不同 Agent。成绩只代表选定题目及适配环境中的表现，不等同于官方上游排行榜成绩，
也不能单独归因于模型能力。

| 能力 | 可用入口与要求 |
| --- | --- |
| 离线协议及套件检查 | `eval.py protocol-smoke`、`protocol-check`、`release validate` |
| 准备、导出题目 | `eval.py prepare-tasks`、`export-tasks`，不调用模型 |
| 本地调用真实 AgentScope | 原生 Java CLI；需要 Java 17+、Maven 和模型凭据 |
| 生产框架契约测试 | `eval.py release contracts`，使用本地 fixture/mock 的 Java 测试 |
| 跨 Agent 执行整套题目 | 已有 Harbor 适配器及运行基础组件；需另外提供 runner 和执行环境 |
| 业务题执行 | 还需隔离的官方 tau-bench/AppWorld 包、数据及评分器 |
| 已有结果离线对比 | `eval.py release compare`，不调用模型 |

**Legacy `eval.py` 没有 `run` 命令，也没有完整的本地套件/矩阵编排入口。** 准备、导出这些
legacy 题目不代表已经执行评测。此限制仅指 `eval.py` 和 `configs/suites/`，不适用于上面的
新 `hbench` Harbor runner；legacy 套件 JSON 不是新入口的输入。
Legacy 迁移工具不提交集群任务，也不操作云存储或部署。

### 有哪些评测题目

当前共 **968 个唯一题目 ID：153 道自有题，815 道外部题**。各套件复用这些 ID，不维护重复目录。
`full` 是选定的 63 道综合题，并不表示全部 968 道。

| 题库 | 数量 | 覆盖内容与维护方式 |
| --- | ---: | --- |
| [Harness](datasets/harness/) | 26 | 直接维护的框架行为回归题 |
| [Capabilities](datasets/capabilities/README.md) | 127 | 直接维护的能力题、fixture 和断言 |
| [Terminal Bench](external/terminal-bench/) | 8 | 终端任务选题，固定 commit 获取并应用本地适配 |
| [Business](external/business/) | 807 | 165 道 tau-bench、642 道 AppWorld；由固定题目声明和公共模板生成包装 |

26 道 Harness 题覆盖：

| 方向 | 数量 | 示例 |
| --- | ---: | --- |
| 基础任务完成 | 2 | 汇总账本、修复空输入处理 |
| 文件 | 4 | 空文件、局部修改、嵌套路径、文件尾部 |
| 工具 | 4 | 参数、调用依赖、干扰工具、失败恢复 |
| 记忆 | 4 | 持久化、临时状态、更新、覆盖 |
| 上下文压缩 | 4 | 保留约束、继续任务、写入记忆、待执行工具 |
| 技能与子代理 | 4 | 发现、权限、委派、失败处理 |
| 会话生命周期 | 4 | 取消、重复请求、多轮、重启 |

127 道能力题分为：工具使用 51、记忆 23、文件操作 21、检索 8、组件行为（`unit_test`）8、
对话 7、规划 7、摘要 2。这里的 `unit_test` 是可执行题目的分类，不是仓库的 Python/JUnit
测试套件。每道题的指令和断言与其 `scenario.json` 放在一起。

8 道终端题为 `tb-cancel-async-tasks`、`tb-fix-git`、`tb-large-scale-text-editing`、
`tb-log-summary-date-ranges`、`tb-multi-source-data-merger`、`tb-polyglot-c-py`、`tb-regex-log`
和 `tb-write-compressor`。业务题覆盖官方工具驱动环境中的 tau-bench 交互及 AppWorld
应用工作流，包含选定的 dev、test-normal、test-challenge 题目 ID。

**自有题库直接维护，外部题库固定版本获取，仓库只保留必要适配代码、补丁和资源。**
下载及展开后的外部题位于已忽略的 `artifacts/datasets/`，仓库不保存完整上游题库副本。
详见[题库维护说明](datasets/README_zh.md)和[外部题目准备说明](external/README.md)。

### 选择评测套件

配置位于 [`configs/suites/`](configs/suites/)。表中的题数尚未乘以 Agent 数与重复次数。
三 Agent 套件选择 AgentScope、Codex、Pi；发布套件只选择 AgentScope。各 Agent 的能力限制
必须在结果中明确保留。

| 配置名称 | 题数 | Agent 数 × 重复次数 | 用途 |
| --- | ---: | ---: | --- |
| `smoke` | 2 | 3 × 1 | 跨 Agent 基础集成检查 |
| `release-smoke` | 3 | 1 × 3 | 小规模发布流程检查 |
| `release` | 18 | 1 × 3 | 计划 54 次发布回归试验 |
| `comparison` | 17 | 3 × 3 | 计划 153 次跨 Agent 对比试验 |
| `full` | 63 | 3 × 1 | 26 道 Harness + 8 道终端 + 29 道业务题 |
| `capabilities-preflight` | 7 | 3 × 1 | 能力适配预检 |
| `capabilities` | 127 | 3 × 1 | 全部自有能力题 |
| `business-preflight` | 4 | 3 × 1 | 业务后端预检 |
| `business-tau` | 165 | 3 × 1 | 选定 tau-bench 题目 |
| `business-appworld` | 642 | 3 × 1 | 选定 AppWorld 题目 |
| `business` | 807 | 3 × 1 | 全部业务选题 |

配置还声明 Agent 版本、模型/API、超时、轮数上限、并发度和重试策略，执行前应阅读对应 JSON。
默认使用配置中的兼容接口与 `qwen3.8-max`；这个提供商别名**不是不可变的模型快照**。
题目、适配器、评分器、模型配置或环境契约发生变化后，需要建立新的对比基线。

### 快速开始：离线检查与题目准备

使用 Python 3.12+ 和虚拟环境。从 `harness-bench` 仓库根目录开始：

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-protocol.txt
python eval.py protocol-smoke
python eval.py release validate --definitions-only
```

`protocol-smoke` 只回放本地 fixture，不代表真实模型或 Agent 已经运行。
`--definitions-only` 不要求下载外部题目，只检查选题定义。可以先从不需要下载的自有题开始：

```sh
python eval.py release validate --config configs/suites/capabilities-preflight.json
python eval.py export-tasks configs/suites/capabilities-preflight.json --output artifacts/export-capabilities-preflight
```

如果套件包含外部题，先准备再校验和导出：

```sh
python eval.py prepare-tasks configs/suites/full.json
python eval.py release validate --config configs/suites/full.json
python eval.py export-tasks configs/suites/full.json --output artifacts/export-full
# 后续可以离线复用已校验的下载缓存：
python eval.py prepare-tasks configs/suites/full.json --offline
```

准备过程只获取固定 commit 上的选中文件，并核对 SHA256。导出目录包含题目和来源
`manifest.json`；输出目录已存在时拒绝覆盖。校验和导出本身不下载，输入缺失或损坏会明确报错。

业务题准备只生成包装并获取固定元数据，**不会安装官方环境、数据库或完整 AppWorld 数据**。
后端必须符合 [`sources.lock.json`](external/business/sources.lock.json)，包括 AppWorld
数据版本 `0.2.0`。执行时应记录实际使用的包、数据身份和镜像 digest。

### 本地运行原生 AgentScope 冒烟请求

这一步验证真实 Java Harness 能否调用模型，不是带评分的 benchmark trial，也不会自动安装
题目 fixture 或可信验证器。使用专门的工作目录，Agent 可以修改其中的文件。

在 `harness-bench` 仓库根目录使用 Java 17+ 和 Maven 构建。先指定独立的候选框架 checkout，
从其 POM 读取实际版本并安装框架模块，再构建本仓库的适配器：

```sh
export AGENTSCOPE_JAVA_REPO=/path/to/agentscope-java
CANDIDATE_VERSION=$(mvn -q -T1 -f "$AGENTSCOPE_JAVA_REPO/pom.xml" help:evaluate -Dexpression=project.version -DforceStdout)
mvn -B -T1 -f "$AGENTSCOPE_JAVA_REPO/pom.xml" -pl agentscope-harness,agentscope-extensions/agentscope-extensions-model/agentscope-extensions-model-openai -am install -DskipTests -Dmaven.javadoc.skip=true -Dgpg.skip=true
mvn -B -T1 -f adapters/agentscope-java/pom.xml package dependency:copy-dependencies spotless:check -DincludeScope=runtime -Dcandidate.version="$CANDIDATE_VERSION"
export HARNESS_EVAL_CLASSPATH="$PWD/adapters/agentscope-java/target/classes:$PWD/adapters/agentscope-java/target/dependency/*"
java -cp "$HARNESS_EVAL_CLASSPATH" io.agentscope.examples.eval.HarnessEvalCli --version
```

通过环境变量提供 `DASHSCOPE_API_KEY`。继续在 `harness-bench` 仓库根目录执行：

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

CLI 的三个参数为 `REQUEST_JSON NATIVE_JSONL WORKSPACE`。每次使用新的运行目录，以隔离
状态和追加写入的日志。CLI 输出的是**原生事件**；`eval.py protocol-check` 接收规范协议事件，
不能直接拿这份原生日志校验，需要先通过 `runtime/native_events.py` 转换。

### 如何执行带评分的套件

集成现有组件的 runner 需要完成以下步骤：

1. 选定、校验套件，准备并导出题目，冻结评测契约。
2. 按适用的 [`environments/`](environments/) 配方提供隔离环境，预装选定 Agent 版本，
   记录最终镜像 digest。
3. 把每道题的 scenario 传给 `runtime/harbor_agents.py` 中的适配器。安装 fixture 和技能，
   保持多轮与会话语义；业务题另外接入官方后端。
4. 先完成模型与工具预检，再执行矩阵。隔离每次试验的状态，遵循预算和重复次数，不自动重跑
   整个 trial。`runtime/schedule.py` 提供调度基础能力，但不创建执行环境。
5. 收集并转换原生事件，执行对应的结果、行为或官方评分器。可信工具回执和评分过程应位于
   候选 Agent 工作区之外，不能用候选自己写出的文件代替可信证据。所有计划试验都应归档，包括失败。
6. 生成不可变的运行证据，再使用下面的离线报告和发布判定入口。

对应组件包括 `runtime/scenarios.py`（场景执行）、`runtime/fixture_tools.py`（可信 fixture）、
`runtime/task_score.py` 与 `runtime/behavior_checks.py`（评分）、`runtime/ledger.py`（证据），
以及 `worker.py` / `runtime/business_server.py`（业务后端）。安装 `requirements-evals.txt`
提供固定版本的 Harbor 等依赖，但不会将这些 legacy JSON 套件自动变成新 `hbench` runner 的任务。

### 评分、发布检查与报告

任务结果检查、行为断言和官方业务评分器使用不同契约。能力题使用
`capabilities-task-outcomes-v2`，Harness 发布配置使用 `task-outcome-v1`。
不支持、缺失和未评分的试验必须显式呈现，不能计为通过。

在 `harness-bench` 仓库根目录对独立的框架 checkout 运行生产契约测试，或比较已有评测归档：

```sh
export AGENTSCOPE_JAVA_REPO=/path/to/agentscope-java
python eval.py release contracts --repo "$AGENTSCOPE_JAVA_REPO" --output artifacts/contracts-unique
python eval.py release compare --candidate /path/to/candidate-evidence \
  --baseline /path/to/baseline-evidence --output artifacts/review-unique
```

contracts 和源码快照优先使用显式 `--repo`；未传时使用 `AGENTSCOPE_JAVA_REPO`。
两者均未设置时，立即提示缺少框架仓库，不会默认使用 benchmark 根目录。
将外置框架源码快照写入新的输出目录：

```sh
python snapshot.py --repo "$AGENTSCOPE_JAVA_REPO" --output artifacts/source-snapshot-unique
```

沙箱 Docker 构建上下文必须同时包含 benchmark 文件及外置框架的 `candidate-source.tgz`，
Docker 分别 COPY 两者。不能把 `harness-bench` 本身用作候选框架，详见[环境配方](environments/README.md)。

契约检查执行声明的 27 个生产测试类并核对 Surefire XML，使用 fixture/mock，是对真实模型
试验的补充。`--verify-only` 只检查已有 XML，不代表新执行了一次测试。
离线对比需要 `manifest.json`、`summary.json` 及 summary 指向的逐 trial `result.json`，会核对
归档一致性并生成 `release-gate.json`、`release-gate.html` 和 `report.html`。它不调用模型，
也不补造缺失结果。退出码分别为 0（`pass`）、3（`review`）、2（`fail` / `invalid`）。
未锁定的模型别名需要人工复核。详见[发布策略和证据要求](docs/release-regression_zh.md)。

另一个业务报告入口接受业务批次格式（`manifest.cases`、case 结果、轨迹和 ledger），
与统一发布归档的格式不同：

```sh
mkdir -p artifacts
python report.py --local /path/to/business-archive --output artifacts/business-report.html
```

[`reports/templates/business.html`](reports/templates/business.html) 是它的源码模板。
生成的 HTML 内嵌结果，可以离线查看，应保存到 `artifacts/`。`combine_reports.py` 和
`compare_reports.py` 用于业务归档合并与对比，参数见各自的 `--help`；它们与发布判定入口分开。

### 目录与开发验证

| 路径 | 职责 |
| --- | --- |
| `eval.py`、`release_cli.py` | 离线检查、准备/导出、发布判定入口 |
| `adapters/` | 原生 Agent 适配器，包括 Java CLI |
| `runtime/`、`schemas/` | 协议、场景、可信 fixture、评分与调度基础组件 |
| `configs/suites/`、`contracts/` | 套件配置与生产框架测试选择 |
| `datasets/`、`external/` | 自有题目与固定版本的外部适配 |
| `environments/` | 通用执行环境及镜像配方 |
| `reports/`、`report.py`、`*_reports.py` | 维护中的报告实现及模板 |
| `worker.py`、`prepare_appworld.py`、`snapshot.py` | 业务 worker、带校验的数据准备、源码快照 |
| `tests/`、`test_*.py` | 自动化测试源码，不是评测结果 |
| `docs/` | [协议](docs/agent-protocol_zh.md)与[发布回归](docs/release-regression_zh.md)指南 |
| `tools/campaigns/` | Git 忽略的历史第一方工具；未适配的参考归档，无历史结果，不是可运行服务 |
| `artifacts/`、`.venv/`、`__pycache__/` | 已忽略的本地产物、环境与缓存 |

在虚拟环境中，从 `harness-bench` 仓库根目录执行：

```sh
python -m pip install -r requirements-evals.txt
PYTHONPATH=. python -m unittest discover -s tests -p 'test_*.py'
PYTHONPATH=. python -m unittest test_worker test_snapshot test_runner_integration test_prepare_appworld test_report test_compare_reports test_combine_reports
```

按 Java 构建一节设置 `HARNESS_EVAL_CLASSPATH`，可启用 Java 本地回环测试。
设置 `HARNESS_EVAL_MAVEN_TEST=1` 可启用源码快照 Maven 打包检查；未设置这些变量时，
相应检查会跳过。测试使用本地 fixture/mock，不调用真实模型服务。

下载、轨迹、生成报告、调研文档及批次进展统一存放到已忽略的 `artifacts/`。
不提交凭据、虚拟环境、构建产物或评测中间结果。
