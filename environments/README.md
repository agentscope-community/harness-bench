# Evaluation environment recipes / 评测环境配方

`Dockerfile.sandbox` installs the native Java, Codex and Pi adapters. Run benchmark commands from the
`harness-bench` repository root. The sandbox build context must contain both the benchmark files and
`candidate-source.tgz`, an archive of the separate AgentScope Java framework checkout; Docker copies
the candidate archive and benchmark files separately. Do not use `harness-bench` itself as the
candidate framework source. Supply the matching Maven version and explicit Python/Node/Maven base
images. Pin the resulting image digest in the runner's execution contract. It is an environment
recipe, not a cluster deployment entrypoint. `Dockerfile.tb-*` extends that native image for the
selected upstream Terminal Bench tasks; use `NATIVE_IMAGE` to supply the built sandbox image.

`Dockerfile.sandbox` 安装 Java、Codex、Pi 原生适配器。benchmark 命令从 `harness-bench` 仓库
根目录执行。沙箱构建上下文必须同时包含 benchmark 文件及独立 AgentScope Java 框架 checkout
的归档 `candidate-source.tgz`；Docker 分别 COPY 候选归档和 benchmark 文件。
不能把 `harness-bench` 本身用作候选框架源码。提供对应 Maven 版本及明确的 Python/Node/Maven
基础镜像，runner 的执行契约应固定构建结果的镜像 digest。这里是环境配方，不是集群部署入口。
`Dockerfile.tb-*` 为选定的上游 Terminal Bench 题目扩展原生环境，通过 `NATIVE_IMAGE`
指定已构建的沙箱镜像。

Official business environments and data are supplied separately; private prebuilt image addresses are
not part of these recipes. / 官方业务环境和数据另行提供，这些配方不包含私有预构建镜像地址。

Terminal Bench recipes use an exported suite directory as their build context, not the benchmark
repository root. Run `prepare-tasks` and `export-tasks` from the root first; see [external inputs](../external/README.md).
Terminal Bench 配方以导出的套件目录作为构建上下文，不使用 benchmark 仓库根目录。
先在根目录执行 `prepare-tasks` 和 `export-tasks`，详见[外部输入](../external/README.md)。
