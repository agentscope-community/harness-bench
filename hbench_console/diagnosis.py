"""Read-only diagnosis: never change rewards, execution or outcome classification."""
import re

ZERO_TOKEN_HINT = "日志记录为 0 token，不等于未调用模型；检查 trial.log 与原生 agent/ 证据确认首次请求情况"

RULES = (
    ("cancelled", "被连带取消", "同作业内其他 trial 抛出硬错误或运行被中断；先定位首个错误与取消来源"),
    ("noclobber", "私有配置写入冲突", "容器内目标文件已存在；检查镜像内配置文件与启动脚本是否重复写入"),
    ("cannot overwrite", "私有配置写入冲突", "容器内目标文件已存在；检查镜像内配置文件与启动脚本是否重复写入"),
    ("ratelimit", "模型限流（429）", "核对并发与模型配额；统一调整运行配置，不在单个 harness 中增加重试"),
    ("throttl", "模型限流（429）", "核对并发与模型配额；统一调整运行配置，不在单个 harness 中增加重试"),
    ("400", "请求被端点拒绝（400）", "检查请求字段与端点兼容性（如 developer role）；细节见 trial.log 与原生 agent/ 证据"),
    ("context window", "上下文超限", "核对模型真实容量及窗口配置；保留原生压缩行为，不改题面或注入额外压缩"),
    ("context length", "上下文超限", "核对模型真实容量及窗口配置；保留原生压缩行为，不改题面或注入额外压缩"),
    ("timeout", "运行超时", "检查统一 trial 时限与模型延迟；用日志确认卡在工具、验证还是模型调用"),
)

TOOL_HINTS = {
    "command_failed": "命令返回失败：查看原生命令、退出码与输出",
    "not_found": "目标不存在：查看模型请求的路径或资源",
    "permission_denied": "权限被拒绝：核对统一沙箱与工具权限",
    "invalid_argument": "工具参数无效：查看原生调用参数与工具 schema",
    "rate_limited": "工具上游限流：核对服务配额与统一并发",
    "timeout": "工具超时：查看工具耗时及统一时限",
    "configuration_error": "工具配置错误：核对接入配置",
    "io_error": "工具 I/O 错误：查看文件系统或网络证据",
    "upstream_error": "工具上游错误：查看服务响应",
}


def diagnose(trial: dict) -> dict:
    """Keep Harbor errors, native status, tool observations and usage separate."""
    result = {}
    diagnostics = trial.get("diagnostics")
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    steps = diagnostics.get("steps")
    steps = [step for step in steps if isinstance(step, dict)] if isinstance(steps, list) else []
    if any(step.get("native_status") == "failed" for step in steps):
        result.update(native_label="原生执行报告失败", native_hint="查看原生失败事件；trial 结果仍以 Harbor 异常与验证器 reward 为准")
    elif any(step.get("iteration_limit") for step in steps):
        result.update(native_label="原生轮数上限停止", native_hint="这是停止原因，不直接判失败；仍由验证器评分")
    elif any(step.get("native_status") == "incomplete" or step.get("native_parse_errors") for step in steps):
        result.update(native_label="原生证据不完整", native_hint="结束事件缺失或日志解析不完整；不能推定执行成功或失败")
    failures = diagnostics.get("tool_failures")
    failures = failures if isinstance(failures, dict) else {}
    total = failures.get("total")
    if isinstance(total, int) and not isinstance(total, bool) and total > 0:
        result["tool_failure_label"] = f"工具调用失败 {total} 次（不等于题目失败）"
        by_kind = failures.get("by_kind")
        by_kind = by_kind if isinstance(by_kind, dict) else {}
        hints = [TOOL_HINTS.get(kind, f"{kind}：查看原生工具结果") for kind, count in by_kind.items()
                 if isinstance(count, int) and not isinstance(count, bool) and count > 0]
        result["tool_failure_hint"] = "；".join(hints) or "查看原生工具结果"
    if trial.get("input_tokens") is None or trial.get("output_tokens") is None:
        result["usage_hint"] = "token 未记录或多步证据不完整，不代表 0，不能据此判定调用失败"
    error = (trial.get("error") or "").strip()
    message = (trial.get("error_message") or "").strip()
    text = f"{error} {message}".lower()
    if error:
        for needle, label, hint in RULES:
            if needle in text:
                if needle == "400" and not re.search(r"\b400\b", text):
                    continue
                return {**result, "error_label": label, "error_hint": hint}
        return {**result, "error_label": error, "error_hint": "查看 trial.log 与 agent/ 日志定位调用栈"}
    if trial.get("input_tokens") == 0 and trial.get("output_tokens") == 0 and trial.get("reward") != 1:
        result.update(error_label="疑似首次调用失败", error_hint=ZERO_TOKEN_HINT)
    return result
