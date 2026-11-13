# 操作指南

English: [operation-guide.md](operation-guide.md) · 返回 [README](../README_zh.md)

目标：在一台独立 ECS 上跑完整矩阵 `configs/matrices/bailian-18.yaml`（3 题库 × 2 模型 × 3 Harness = 18 组合）。
A 部分用单机 Docker；B 部分是以后切到 ACK 的追加步骤。标 **计费** 的步骤会调用百炼模型。

## A. 单机 Docker（ECS）

### A1. 准备主机

- 系统：CentOS Stream 9 x86_64（不支持 CentOS 7：没有 Python 3.12）。
- 地域：香港（可直连 Docker Hub / PyPI / npm / Maven / GitHub）；内地需配置镜像源。
- 规格：每个并发 trial 按 4 vCPU / 8 GiB 计。`HB_CONCURRENCY=4` → 16 vCPU / 64 GiB。
- 磁盘：数据盘约 1 TB（存放 SWE-bench / Terminal-Bench 镜像，估算值）。

```sh
sudo dnf install -y python3.12 python3.12-pip git rsync curl tar java-21-openjdk-devel skopeo

# Maven 3.9（dnf 自带的 maven 版本过旧，且会装上旧版 JDK）
curl -fsSL https://archive.apache.org/dist/maven/maven-3/3.9.9/binaries/apache-maven-3.9.9-bin.tar.gz \
  | sudo tar -xz -C /opt
cat >> ~/.bashrc <<'EOF'
export JAVA_HOME=/usr/lib/jvm/java-21-openjdk
export PATH=/opt/apache-maven-3.9.9/bin:$JAVA_HOME/bin:$PATH
EOF
. ~/.bashrc

# Docker（如已装 podman-docker，先卸载）
curl -fsSL https://get.docker.com -o get-docker.sh && sudo sh get-docker.sh   # 内地加 --mirror Aliyun
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER" && newgrp docker

# 必需：扩容 Docker 网络地址池。每个 trial 建一个 compose 网络，默认池只够约 15 个，
# 并发跑几分钟后就会报 "could not find an available, non-overlapping IPv4 address pool"
sudo mkdir -p /etc/docker
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{
  "default-address-pools": [
    { "base": "10.200.0.0/16", "size": 24 },
    { "base": "10.201.0.0/16", "size": 24 }
  ]
}
EOF
sudo systemctl restart docker
# base 要避开 VPC 网段（ip -4 route 查看）和 100.64.0.0/10；需要换 Docker 数据盘时把
# "data-root": "/data/docker" 合并进同一个 daemon.json。
docker network create netprobe >/dev/null && \
  docker network inspect netprobe --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}' && \
  docker network rm netprobe >/dev/null          # 应输出 10.200.x.0/24

docker info && docker compose version && java -version && mvn -v && python3.12 --version
docker login
```

### A2. 获取代码

```sh
git clone git@gitlab.alibaba-inc.com:ken.lj/harness-bench.git ~/harness-bench
cd ~/harness-bench
git switch datasets/bundled-prepared      # 可选：该分支已包含准备好的题库，A3 可跳过题库步骤
```

代码未推送时，在笔记本仓库根目录用 rsync：

```sh
rsync -az --exclude /.venv --exclude /artifacts --exclude target/ --exclude .env --exclude .DS_Store \
  ./ <user>@<ECS_IP>:~/harness-bench/
rsync -az artifacts/datasets <user>@<ECS_IP>:~/harness-bench/artifacts/   # 可选：带上题库
```

### A3. Python 环境与题库

```sh
cd ~/harness-bench
python3.12 -m venv .venv && . .venv/bin/activate
python -m pip install -r requirements-evals.txt
# 已有 artifacts/datasets 时跳过下面两行
python -m hbench datasets pull all
python -m hbench datasets prepare all
```

检查：`artifacts/datasets/` 下有 `terminal-bench`（66 题）、`swe-bench-verified`（500）、`tau3-bench`（375）。

### A4. 填写 `.env`

```sh
cp .env.example .env && chmod 600 .env
vi .env
```

| 变量 | 填写 |
| --- | --- |
| `HB_BAILIAN_API_KEY` | 百炼 API Key |
| `HB_BAILIAN_BASE_URL` | 默认 `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `HB_MODEL_A_ID` / `HB_MODEL_B_ID` | 两个不同的模型 ID，都必须支持百炼 `/responses`（codex 需要） |
| `HB_TAU_API_KEY` / `HB_TAU_BASE_URL` | 默认复用百炼 |
| `HB_TAU_USER_MODEL` / `HB_TAU_ASSERTIONS_MODEL` | `openai/<model-id>`，可相同；对比期间不要改 |

tau3 的 task.toml 在生成 compose 环境和执行判分时会从控制机环境读取 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、
`TAU2_USER_MODEL`、`TAU2_NL_ASSERTIONS_MODEL`。`hbench run` 会用上面的 `HB_TAU_*` 自动派生并导出，不用另外配 `OPENAI_*`。
| `HB_PIP_INDEX_URL` | 可选，内地填 `https://mirrors.aliyun.com/pypi/simple/` |
| `HB_RUNTIME_IMAGE` | `hbench/harness-runtime:0.1.0` |
| `HB_CLUSTER` | `docker` |
| `HB_CONCURRENCY` | 总并发 trial 数 |
| `HB_N_TASKS` | 冒烟填 `1`；完整运行留空 |
| `HB_DRY_RUN_ONLY` | `1` 只做 dry-run；`0` 真跑 |

检查接口连通（不耗 token，200 正常，401 Key 错误）：

```sh
set -a; . ./.env; set +a
curl -sS -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $HB_BAILIAN_API_KEY" "$HB_BAILIAN_BASE_URL/models"
```

### A4a. OSS 归档（可选）

不配置就不归档，评测照常。凭证只来自 ECS 实例 RAM 角色，`.env` 里不放 AccessKey。

**1. Bucket**（控制台，一次）：与 ECS 同地域，读写权限选「私有」。建议加生命周期规则：7 天后删除碎片（未完成的分片上传）。

**2. RAM 角色**（一次；角色已绑定时只核对权限策略）：

1. RAM 控制台 → 身份管理 → 角色 → 创建角色，信任主体选「云服务 → 云服务器 ECS」。
2. 权限管理 → 权限策略 → 创建权限策略 → 脚本编辑，粘贴下面内容（替换 `<bucket>`；改了 `HB_OSS_PREFIX` 时同时替换 `harness-bench`），再把该策略授权给角色：

   ```json
   {
     "Version": "1",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": "oss:ListObjects",
         "Resource": "acs:oss:*:*:<bucket>",
         "Condition": {"StringLike": {"oss:Prefix": "harness-bench/*"}}
       },
       {
         "Effect": "Allow",
         "Action": ["oss:PutObject", "oss:GetObject", "oss:ListParts", "oss:AbortMultipartUpload"],
         "Resource": "acs:oss:*:*:<bucket>/harness-bench/*"
       }
     ]
   }
   ```

3. ECS 控制台 → 实例 → 更多（或「全部操作」）→ 实例设置 → 授予/收回 RAM 角色，选该角色。

在主机上确认（只输出角色名，不输出凭证）：

```sh
t=$(curl -s -X PUT -H 'X-aliyun-ecs-metadata-token-ttl-seconds: 60' http://100.100.100.200/latest/api/token)
curl -s -H "X-aliyun-ecs-metadata-token: $t" http://100.100.100.200/latest/meta-data/ram/security-credentials/
```

**3. 填写 `.env`**：

| 变量 | 值 |
| --- | --- |
| `HB_OSS_BUCKET` | Bucket 名；留空 = 不归档 |
| `HB_OSS_REGION` | Bucket 地域，如 `cn-hangzhou`（与 ECS 同地域） |
| `HB_OSS_ENDPOINT` | 留空 = 该地域内网 Endpoint；跨地域时填 `https://oss-<region>.aliyuncs.com`（走公网） |
| `HB_OSS_PREFIX` | 默认 `harness-bench`，须与策略中的前缀一致 |
| `HB_OSS_ROLE_NAME` | 留空 = 实例绑定的角色 |
| `HB_OSS_SYNC_INTERVAL` | 后台上传间隔（秒），默认 `300` |

**4. 禁止容器访问元数据服务**，防止题目代码读取角色凭证（每台主机一次）：

```sh
sudo mkdir -p /etc/systemd/system/docker.service.d
sudo tee /etc/systemd/system/docker.service.d/block-metadata.conf <<'EOT'
[Service]
ExecStartPost=-/bin/sh -c 'iptables -C DOCKER-USER -d 100.100.100.200/32 -j DROP 2>/dev/null || iptables -I DOCKER-USER -d 100.100.100.200/32 -j DROP'
EOT
sudo systemctl daemon-reload && sudo systemctl restart docker
docker run --rm busybox nc -w 3 100.100.100.200 80 </dev/null && echo "未拦截" || echo 已拦截
```

`run-matrix.sh` 每次用 `sudo -n iptables -C ...` 核对这条规则；运行用户没有免密 sudo 时会打印 `[oss] WARNING`，只要上面输出「已拦截」即可忽略。

**5. 检查**：

```sh
set -a; . ./.env; set +a
python -m hbench archive --check   # [oss] check OK: oss://<bucket>/harness-bench/ ...
```

| 输出 | 处理 |
| --- | --- |
| `alibabacloud-oss-v2 is not installed` | `python -m pip install -r requirements-evals.txt` |
| `HB_OSS_REGION is required` | 在 `.env` 填 `HB_OSS_REGION` |
| `No RAM role is attached to this ECS instance` | 做第 2 步第 3 项（绑定角色） |
| `ECS metadata returned no credentials for role ...` | `HB_OSS_ROLE_NAME` 与绑定的角色不一致，留空即可 |
| `status=403 code=AccessDenied` | 策略里的 Bucket 名、前缀，或策略未授权给该角色 |
| `status=404 code=NoSuchBucket` | Bucket 名或 `HB_OSS_REGION` 填错 |
| 连接超时 / 无法连接 | 内网 Endpoint 只在同地域可达：核对 `HB_OSS_REGION`，或设置 `HB_OSS_ENDPOINT` |

**运行时**：`run-matrix.sh` 开跑前自动 `--check`，失败只提示 `continuing WITHOUT OSS archive`，评测照常。之后每隔
`HB_OSS_SYNC_INTERVAL` 秒在后台上传已结束的 trial，summary 后再上传全部文件和报告（最多 30 分钟，没传完的留在本地）。
OSS 出错只打印 `[oss] ...`，不中断评测、不改变退出码，本地文件保留。`job.log`（Harbor 调试日志，未脱敏）不上传。
对象路径：`<prefix>/jobs/<name>/...`、`<prefix>/reports/<name>/...`。

手动补传（只传新增或变化的文件）；加 `--dry-run` 只列出将要上传的对象：

```sh
python -m hbench archive artifacts/jobs/<name> --report artifacts/reports/<name>
```

### A5. 构建镜像

```sh
python -m hbench images build --runtime-image hbench/harness-runtime:0.1.0 \
  --tau3-dataset artifacts/datasets/tau3-bench --execute
docker image ls | grep -E 'hbench/(harness-runtime|tau3-main|tau3-runtime)'
```

检查：出现 `hbench/harness-runtime:0.1.0`、`hbench/tau3-main:0.1.0`、`hbench/tau3-runtime:0.1.0` 三个镜像。
修改 `adapters/agentscope-java` 或 `environments/hbench/` 后重新执行本步。

### A6. 预拉取题目镜像（可选）

```sh
for d in terminal-bench swe-bench-verified; do
  python -m hbench images list "artifacts/datasets/$d" |
    python -c 'import json,sys; print("\n".join(i for i in json.load(sys.stdin) if not i.startswith("hbench/")))'
done | sed -E 's/:[^/@]+@/@/' | sort -u > artifacts/task-images.txt
xargs -n1 docker pull < artifacts/task-images.txt
docker system df
```

### A7. 冒烟（18 个 trial）

`.env` 中设 `HB_N_TASKS=1`：

```sh
HB_DRY_RUN_ONLY=1 bash deploy/run-matrix.sh   # 只做 dry-run
bash deploy/run-matrix.sh                     # 计费：真实运行 + 汇总
```

检查：打开输出的 `artifacts/reports/<name>/report.html`，18 个组合都有记录，没有镜像缺失、401/404 或 `/responses` 报错。

3 道 Terminal-Bench 题需要 H100，纯 CPU 主机上会记为错误；要排除就在矩阵文件里设 `selection.exclude_gpu: true`。

### A8. 完整运行（5646 个 trial）

`.env` 中清空 `HB_N_TASKS`：

```sh
nohup bash deploy/run-matrix.sh > artifacts/run-matrix.log 2>&1 &   # 计费
tail -f artifacts/run-matrix.log
docker ps
```

中途看报告：

```sh
python -m hbench summary artifacts/jobs/<name> --output artifacts/reports/<name>-interim
```

进程中断后续跑（未经 hbench 实测；不行就重新执行 `bash deploy/run-matrix.sh`）：

```sh
set -a; . ./.env; set +a
PYTHONPATH="$PWD" .venv/bin/harbor jobs resume -p artifacts/jobs/<name>
```

### A9. 取回结果

在笔记本上：

```sh
rsync -az <user>@<ECS_IP>:~/harness-bench/artifacts/reports/ ./artifacts/reports/
rsync -az <user>@<ECS_IP>:~/harness-bench/artifacts/jobs/<name> ./artifacts/jobs/   # 可选：报告中逐题链接指向的证据
```

或从 OSS：把 `<prefix>/reports/<name>/` 和 `<prefix>/jobs/<name>/` 一起下载（控制台或 `ossutil cp -r`）。

报告：`artifacts/reports/<name>/summary.json` 和 `report.html`：每个 harness × 模型 × 题库一行；另有 Trials 表，
每个 trial 一行，相对路径链接到它的 `result.json`，同目录的 `agent/`、`verifier/` 是轨迹和测试输出。

## B. ACK（以后）

前提：ACR 命名空间（如 `registry.cn-hongkong.aliyuncs.com/<ns>`）、同地域 ACK 集群、ECS 上有 kubeconfig、Pod 能访问百炼。以下在 ECS 上执行。

### B1. 构建并推送镜像

```sh
export REG=registry.cn-hongkong.aliyuncs.com/<ns>
python -m hbench images build --runtime-image "$REG/harness-runtime:0.1.0" \
  --tau3-dataset artifacts/datasets/tau3-bench --tau3-image-prefix "$REG/tau3" --execute
BUILD_PLAN_JSON=<上一条输出的 plan_path>
docker login "${REG%%/*}"
docker push "$REG/harness-runtime:0.1.0"
docker push "$REG/tau3-main:0.1.0"
docker push "$REG/tau3-runtime:0.1.0"
```

### B2. 同步题目镜像到 ACR

```sh
skopeo login docker.io && skopeo login "${REG%%/*}"
for d in terminal-bench swe-bench-verified tau3-bench; do
  python -m hbench images mirror "artifacts/datasets/$d" --registry "$REG" \
    --built-image-map "$BUILD_PLAN_JSON" --output "artifacts/image-map-$d.json" --execute
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

去掉 `--execute` 并换一个 `--output` 文件名，可以先只生成计划检查。

### B3. ACK 矩阵文件

```sh
cp configs/matrices/bailian-18.yaml configs/matrices/bailian-18-ack.yaml
# 修改：cluster: ack    image_map: artifacts/image-map.json
```

### B4. 初始化命名空间（一次）

```sh
export KUBECONFIG=<path> CTX=<context>
kubectl --context "$CTX" auth whoami
kubectl --context "$CTX" apply -f deploy/k8s/namespace.yaml -f deploy/k8s/rbac.yaml
kubectl --context "$CTX" -n harness-bench create rolebinding harness-bench-runner \
  --role=harness-bench-runner --user=<上面查到的用户>
kubectl --context "$CTX" -n harness-bench auth can-i create pods/exec
read -rs ACR_PASSWORD
kubectl --context "$CTX" -n harness-bench create secret docker-registry hb-acr \
  --docker-server="${REG%%/*}" --docker-username=<acr-user> --docker-password="$ACR_PASSWORD"
unset ACR_PASSWORD
```

### B5. 修改 `.env` 并运行

```sh
HB_MATRIX=configs/matrices/bailian-18-ack.yaml
HB_CLUSTER=ack
HB_REGISTRY=registry.cn-hongkong.aliyuncs.com/<ns>
HB_RUNTIME_IMAGE=registry.cn-hongkong.aliyuncs.com/<ns>/harness-runtime:0.1.0
HB_NAMESPACE=harness-bench
HB_KUBECONFIG=<path>
HB_KUBE_CONTEXT=<context>
HB_IMAGE_PULL_SECRET=hb-acr
```

然后执行 A7、A8。

注意：ACK 默认 `network_enforcement: none`，限制网络的题会被拒绝；GPU 题需要 H100 节点。

## C. 常用命令

```sh
python -m hbench list                                                     # 列出题库/harness/模型/集群/矩阵 profile
python -m hbench matrix --config configs/matrices/bailian-18.yaml \
  --output artifacts/plans/<name>.yaml [--cluster ack] [--concurrency 8] [--n-tasks 1]
python -m hbench run --config artifacts/plans/<name>.yaml --dry-run      # 不调用模型
python -m hbench run --config artifacts/plans/<name>.yaml                # 计费
python -m hbench summary artifacts/jobs/<name> --output artifacts/reports/<name>
python -m hbench images list artifacts/datasets/<dataset>
python -m unittest discover -s tests/hbench -p 'test_*.py'               # 离线单测
```

- 每个计划只能执行一次，重跑要重新生成（`run-matrix.sh` 自动加时间戳）。
- `concurrency` 是全部组合合计的并发数。
- `.env`、`artifacts/` 不要提交。

### C1. 清理残留（评测未在跑时）

每个 trial 结束（成功或失败）都会自动执行 `docker compose down --rmi local --volumes --remove-orphans`，容器、卷和网络都会删掉；只有进程被 `kill -9` / OOM / SSH 断开（SIGHUP）打断时才会残留，残留网络会耗尽地址池。

```sh
docker compose ls -a                                     # 残留的 compose 项目
docker ps -aq | xargs -r docker rm -f                    # 残留容器
docker network prune -f                                  # 未使用的网络
```
