# 评测控制台执行文档

把 hbench 包装成可独立部署的"控制面 + 看板"：配置任务组合、触发运行、展示结果。
不重写 Harbor 的调度、验证和证据链，逐题轨迹复用 `harbor view`。

## 1. 范围与结论

要做的是一个薄层，边界如下：

| 层 | 归属 | 说明 |
| --- | --- | --- |
| 调度、并发、重试、验证、证据 | Harbor | 原样使用，不做二次实现 |
| 任务编制、执行、汇总、归档 | hbench CLI | 控制台只调用 `python -m hbench ...` |
| 配置、排队、状态、索引、展示 | hbench-console（新增） | 本次开发内容 |

明确不做：官方榜单口径、按 trial 重新评分、多集群同时调度、付费运行自动触发。

## 2. 已定决策

1. **同机部署**：控制台与评测 ECS 同机，通过环境变量指向仓库和 `artifacts/`。需要分离时，
   把 `CONSOLE_JOBS_ROOT` 指向同步下来的 OSS 镜像目录即可，代码不变。
2. **真实运行默认关闭**：只有设置 `CONSOLE_ALLOW_REAL_RUNS=1` 才允许执行付费作业；
   否则只接受演练（`--dry-run`）。这是授权闸门，不靠前端提示。
3. **凭证不落库**：只保存 profile 里的 `api_key_name` 引用。控制台不读取、不展示、不写入密钥值，
   只报告"环境变量已设置/未设置"。
4. **不做前端构建链**：静态 HTML + ES Module + 手写 SVG/CSS 图表，无 CDN、无 npm。
   后端只新增 FastAPI（Harbor 自带依赖），存储用 SQLite。
5. **结果口径沿用 hbench summary**：主指标 `pass_rate_planned`，错误、未评分、缺失一律不算通过。
   页面固定标注"适配评测，非官方榜单"。

## 3. 架构

```
浏览器 ──► 反向代理（内网/鉴权）
              ├── /           静态前端
              ├── /api        控制面 API（FastAPI）
              ├── /evidence   只读证据文件
              └── /trace      harbor view（只读，屏蔽 run/upload/summarize）

控制面
  ├── SQLite 索引（runs / jobs / combos / trials）
  ├── 单 worker 线程：FIFO，一次一个 Harbor job
  └── 子进程调用：hbench matrix → run → summary → archive
                          │
                          ▼
                 artifacts/{plans,jobs,reports}（一手证据）
```

为什么单 worker：单台 ECS 的 Docker 地址池和百炼限流都要求全局并发上限；串行作业最稳。
作业内部的并发仍由矩阵里的 `concurrency` 控制。

## 4. 代码结构

```
hbench_console/
  config.py    环境变量与路径
  db.py        SQLite schema 与查询
  ingest.py    扫描 jobs 目录 → hbench summary → 入库
  runner.py    队列、执行、取消、日志
  api.py       FastAPI 路由
  main.py      python -m hbench_console 入口
  demo.py      生成带 fixture 标记的演示证据
  static/      index.html / app.js / style.css
tests/console/ 单测与端到端测试
```

## 5. 数据模型

- `runs`：名称、状态、矩阵 YAML、计划路径、Harbor 作业目录、planned_trials、dry_run、
  时间戳、退出码、错误、日志路径、取消标记。
- `jobs`：作业名、证据目录、来源（run/import）、fixture 标记、汇总计数。
- `combos`：`(job, agent, model, dataset)` 主键，planned/recorded/missing/errors/scored/passed、
  两种通过率、平均 reward、token。
- `trials`：`(job, trial)` 主键，task、agent、model、dataset、reward、error、耗时、token、证据相对路径。

`jobs/combos/trials` 每次同步按作业整体替换，保证不会残留旧 trial。

## 6. 接口

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/health` | 存活、模式（是否允许真实运行） |
| GET | `/api/catalog` | profile 列表、凭证是否就绪（布尔）、矩阵文件 |
| GET | `/api/matrices/{name}` | 读取 `configs/matrices/*.yaml` |
| POST | `/api/runs/preview` | 渲染矩阵，返回计划合同，不执行 |
| POST | `/api/runs` | 校验并入队 |
| GET | `/api/runs` | 运行列表 |
| GET | `/api/runs/{name}` | 运行详情 |
| GET | `/api/runs/{name}/log` | 日志尾部（支持 offset） |
| POST | `/api/runs/{name}/cancel` | 取消 |
| POST | `/api/jobs/sync` | 重新扫描并摄入 |
| GET | `/api/jobs` | 作业列表 |
| GET | `/api/jobs/{name}` | 作业详情 + 组合汇总 |
| GET | `/api/jobs/{name}/trials` | trial 列表，按 agent/model/dataset/结果筛选 |
| GET | `/api/jobs/{name}/tasks` | 逐题 × 组合透视 |
| GET | `/api/compare?jobs=a,b` | 多作业组合对比 |
| GET | `/api/evidence/{job}/{path}` | 只读证据文件，限定在作业目录内并拒绝敏感文件名 |

## 7. 运行生命周期

```
queued → planning → running → summarizing → archiving → succeeded
                                  ↘ failed / cancelled
```

- planning：`hbench matrix` 渲染计划，记录 planned_trials。
- running：`hbench run --config ...`；dry-run 到此直接成功。
- summarizing：`hbench summary` 生成 summary.json/report.html。
- archiving：仅当配置了 `HB_OSS_*` 才执行；失败只写日志，不改变状态。
- 取消：向进程组发 SIGTERM，让 Harbor 清理容器，状态置 cancelled。
- 控制台重启：未完成的运行标记 failed 并提示用 `harbor jobs resume` 续跑；不自动续跑。
- 日志：`artifacts/console/logs/<run>.log`，子进程 stdout/stderr 实时写入。

## 8. 展示

- 作业详情：每个数据集一张 harness × model 热力图；格子显示通过率与 errors/unscored/missing。
- 逐题：同一道题在各组合下的通过情况，可筛出"仅某 harness 失败"。
- Trial：reward、错误类型、耗时、token，链接到证据文件或 `harbor view`。
- 对比：同一组合跨运行的通过率与 token 变化。
- 演示数据：带 `fixture.json` 标记的作业在页面上显著标注"演示数据"。

## 9. 安全

- 证据接口做目录包含校验，拒绝 `.env*`、`*.pem`、`*.key`、`id_rsa*`、`auth.json`。
- API 错误信息不回显矩阵明文之外的凭证引用值；矩阵合同不包含密钥。
- 真实运行需要 `CONSOLE_ALLOW_REAL_RUNS=1`；UI 默认演练模式。
- 默认监听 `0.0.0.0:8090`；Console 自身没有鉴权，必须用安全组/防火墙限制来源，
  公网访问须经反向代理加鉴权。
- 演示/测试数据只写入被忽略的 `artifacts/`。

## 10. 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `CONSOLE_HOST` / `CONSOLE_PORT` | `0.0.0.0` / `8090` | 监听地址；默认对所有网卡开放，可用 `CONSOLE_HOST=127.0.0.1` 收回本机 |
| `CONSOLE_DATA_DIR` | `artifacts/console` | SQLite 与日志 |
| `CONSOLE_JOBS_ROOT` | `artifacts/jobs` | 证据扫描目录（可指向 OSS 镜像） |
| `CONSOLE_REPORTS_ROOT` | `artifacts/reports` | summary 缓存 |
| `CONSOLE_PLANS_ROOT` | `artifacts/plans` | 渲染出的计划 |
| `CONSOLE_ALLOW_REAL_RUNS` | `0` | 付费运行开关 |
| `CONSOLE_HBENCH_CMD` | `python -m hbench` | hbench 调用命令，测试时替换 |
| `CONSOLE_VIEWER_URL` | 空 | `harbor view` 只读地址，用于轨迹下钻 |

## 11. 阶段与验收

**P0 只读（本次）**
- 扫描证据目录，`summary` 入库；热力图、逐题、trial、对比可用。
- 证据路径穿越与敏感文件被拒绝。

**P1 控制面（本次）**
- 矩阵预览返回 planned_trials；演练运行完整走完状态机；取消生效；日志可见。
- 未开启 `CONSOLE_ALLOW_REAL_RUNS` 时真实运行被拒绝。

**P2 后续（不在本次）**
- 多用户与 RBAC、ACK/Kubernetes worker、OSS 直读与镜像同步、钉钉通知。

验收命令：

```
python -m unittest discover -s tests/hbench -p 'test_*.py'
python -m unittest discover -s tests/console -p 'test_*.py'
python -m hbench_console --port 8090
python -m hbench_console.demo          # 生成演示证据（仅本地 artifacts）
```

## 12. 执行记录

P0 与 P1 已实现并离线验证：

- `hbench_console` 完成：配置、SQLite 索引、`hbench summary` 摄入、单 worker 队列、
  状态流转、取消、日志、演示数据生成、静态看板。
- `tests/console/` 11 项测试通过；`tests/hbench/` 248 项测试通过。
- 用 18 组合演示数据验证：热力图、逐题对比、trial 筛选、证据访问、路径穿越与 `.env` 拒绝。
- 用假 hbench 命令验证演练运行：queued → planning → running → succeeded，日志与计划入库；
  取消运行中进程、重启后中断标记、非授权真实运行拒绝均有测试覆盖。
- 真实 `hbench run` 与 ECS 部署尚未执行，需要单独授权；本机未启动 Docker，预览在题库
  未 prepare 时会返回明确错误。

后续需要人工决定的只有部署位置与访问方式；代码侧 P2 项（RBAC、ACK worker、OSS 直读、
通知）未开始。

## 13. 部署

同机 ECS 上用 systemd 运行 `python -m hbench_console`，默认监听 `0.0.0.0:8090`：安全组只放行
内网/办公网来源，公网入口由 ALB 或反向代理加鉴权；Aone 负责跑测试、
构建 runtime 镜像和触发发布，运行体保持"独占 ECS + 宿主 Docker"。具体脚本、目录约定和
回滚步骤见 [deploy/aone/README.md](../deploy/aone/README.md)。

`harbor view artifacts/jobs --jobs --host 127.0.0.1 --port 8091` 作为只读轨迹服务，
反向代理把 `/trace` 指过去并屏蔽 `/api/run*`、`/api/jobs/*/upload`、`/api/jobs/*/summarize`。
