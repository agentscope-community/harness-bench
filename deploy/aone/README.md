# Aone 构建 + 主机部署

控制台与评测运行在同一台独占 ECS 上：systemd 管进程，宿主 Docker 跑 trial 容器。
Aone 只负责跑测试、构建 runtime 镜像和触发发布，不托管运行体。

目录约定（可用 `HB_CODE_DIR` / `HB_DATA_DIR` 覆盖）：

| 路径 | 内容 |
| --- | --- |
| `/opt/harness-bench` | 代码 checkout + `.venv` |
| `/data/harness-bench/artifacts` | bind mount 到 `<代码目录>/artifacts`，含 datasets、jobs、reports、SQLite、日志 |
| `/etc/harness-bench/console.env` | 运行配置与密钥，0640 root:hbench |
| `/etc/harness-bench/runtime-image.env` | 当前 runtime 镜像 tag，由发布脚本改写 |

`artifacts` 必须挂数据盘：`hbench matrix` 把 job 目录固定写在 `<代码目录>/artifacts/jobs`，
没有单独的输出目录参数，所以数据盘要 bind mount 到这个位置，而不是改控制台的环境变量。

## 一、一次性初始化（主机）

前置条件按 [操作指南](../../docs/operation-guide_zh.md) 的 A1 完成：Python 3.12、Docker
（含 `default-address-pools` 扩容）、Maven、Java 21、数据盘挂载。

```sh
# 代码放到 /opt（不要放 /root，服务用户进不去）
sudo mkdir -p /opt && sudo git clone <Code 仓库地址> /opt/harness-bench
cd /opt/harness-bench && sudo git checkout <要部署的 ref>
sudo python3.12 -m venv .venv
sudo .venv/bin/python -m pip install -r requirements-evals.txt

# 建服务用户、bind mount、systemd unit、env 模板
sudo HB_CODE_DIR=/opt/harness-bench HB_DATA_DIR=/data/harness-bench \
  bash deploy/aone/install.sh

# 填密钥与模型 ID；CONSOLE_ALLOW_REAL_RUNS 先保持 0
sudo vi /etc/harness-bench/console.env
sudo deploy/aone/preflight.sh
sudo systemctl start hbench-console
curl -fsS http://127.0.0.1:8090/api/health
```

`install.sh` 会把 `.env.example` 的 `HB_*` 配置合并进 `console.env`；systemd 用 `bash -c`
source 这个文件，所以 `HB_TAU_API_KEY="${HB_BAILIAN_API_KEY}"` 这类写法可用。
`HB_RUNTIME_IMAGE` 不放这里，而是单独写在 `runtime-image.env`（`install.sh` 用
`.env.example` 的默认值初始化），由 `release.sh` 随版本改写。

主机需要能拉取镜像仓库，首次执行一次 `sudo -u hbench docker login <CR>`。

题库只需准备一次，之后发布不会覆盖：

```sh
cd /opt/harness-bench
sudo -u hbench .venv/bin/python -m hbench datasets pull all
sudo -u hbench .venv/bin/python -m hbench datasets prepare all
```

## 二、CI 流水线

线上已建好单测流水线 **harness-bench-ut**（pipeline ID 344158，模板
`python-unittest 10004544`）：Python 3.12、`requirements-evals.txt`、pip 缓存开启，
命令为 `mkdir -p artifacts` + `pytest tests/hbench tests/console --import-mode=importlib -q`，
push main 与 MR→main（opened/reopened）触发。

镜像不在 AoneCI 里构建，在发布主机上构建：

```sh
# 1. 单测（CI 负责，见上）
# 2. 适配器 + runtime 镜像，tag 用目标 commit，在主机构建并 push 到 CR
sudo -u hbench deploy/aone/build-runtime.sh <CR>/<namespace>/harness-runtime:<commit>
# 3. 计划渲染冒烟，不调用模型（可选，发布前手工执行）
python -m hbench matrix --config configs/matrices/bailian-18.yaml \
  --cluster docker --name smoke-<commit> --output artifacts/plans/smoke.yaml
```

原因：`Dockerfile.runtime` 依赖 `adapters/agentscope-java/target/{classes,dependency}`，
`**/target/` 不入库；适配器从内部 SNAPSHOT 仓库取 `agentscope-harness:2.1.0-SNAPSHOT`，
需要本机 `~/.m2/settings.xml` 的内网镜像。AoneCI 的 alios 沙箱有 docker CLI 但没有
daemon、没有 Compose 插件、也没有 Maven 层，`a1 build image create` 的远程构建同样拿不到
这两个目录。只有适配器/runtime 本身变更才需要重建镜像，纯代码发布复用
`runtime-image.env` 里的 tag。

## 三、发布

Aone 应用配置为**主机部署、单台、单分组**，发布脚本调用：

```sh
sudo /opt/harness-bench/deploy/aone/release.sh "$HB_RELEASE_REF" \
  "<CR>/<namespace>/harness-runtime:$HB_RELEASE_REF"
```

`release.sh` 的顺序：

1. 跑 `preflight.sh`（可用 `HB_SKIP_PREFLIGHT=1` 跳过）；
2. 读取 `/api/health`，有正在运行或排队的任务就拒绝发布（`HB_FORCE=1` 强制，任务会被标记中断）；
3. 记录当前 commit 和镜像 tag；
4. 停服务 → `git fetch` → checkout 目标 commit → `pip install` → 写镜像 tag → 启动；
5. 轮询 `/api/health`，60 秒不通过则回滚到上一个 commit 和镜像并重启。

只更新代码、不换镜像时可以省略第二个参数，已有的 `runtime-image.env` 保持不变。

手工发布与流水线发布是同一条路径，便于排障：

```sh
sudo deploy/aone/release.sh main
sudo journalctl -u hbench-console -n 100 --no-pager
```

## 四、回滚

`release.sh` 失败会自动回滚。需要手动回到某个版本时，把那个 commit 当作目标再发一次：

```sh
sudo deploy/aone/release.sh <上一个 commit> <上一版 runtime 镜像>
```

不需要 `git reset`：checkout 是 detached 的，`artifacts/` 不受影响，历史运行结果保留。

## 五、验收

```sh
systemctl status hbench-console
curl -fsS http://127.0.0.1:8090/api/health
curl -fsS http://127.0.0.1:8090/api/jobs | head -c 300
```

健康检查返回里 `runner.active` 为空、`sync.error` 为 null 即正常。`CONSOLE_HOST` 默认
`0.0.0.0`（端口 8090），浏览器可直接访问 ECS 地址；Console 自身没有鉴权，安全组只放行
内网/办公网来源，公网入口走 ALB 或反向代理加鉴权。只想本机访问时改为 `127.0.0.1`。

## 六、注意事项

- **只能单实例**：SQLite 是单写者，两个进程会同时消费队列、互相覆盖索引。
- 发布最好避开活动任务；`hbench run` 被中断后可用 `harbor jobs resume` 续跑。
- unit 里不要加 `PrivateTmp`、`ProtectSystem`、`ProtectHome`：hbench 把宿主路径交给
  Docker daemon，私有挂载命名空间会让 daemon 看不到文件。
- `CONSOLE_ALLOW_REAL_RUNS=1` 只在生产主机的 `console.env` 里打开。
- 容器访问元数据服务 `100.100.100.200` 的 DROP 规则是主机一次性配置（见操作指南第 4 节），
  `preflight.sh` 只做检查，不会修改 iptables。
