"""Turn recorded trial failures into short Chinese labels and next steps."""
import re

ZERO_TOKEN_HINT = "0 token 且无异常：请求可能在首次调用即被端点拒绝；先看 trial.log 与 agent/native.jsonl"

RULES = (
    ("cancelled", "被连带取消", "同作业内其他 trial 抛出硬错误或运行被中断；先修首个错误，再考虑单独重跑"),
    ("noclobber", "私有配置写入冲突", "容器内目标文件已存在；检查镜像内配置文件与启动脚本是否重复写入"),
    ("cannot overwrite", "私有配置写入冲突", "容器内目标文件已存在；检查镜像内配置文件与启动脚本是否重复写入"),
    ("ratelimit", "模型限流（429）", "降低并发或申请更高配额；当前配置不会自动重试"),
    ("throttl", "模型限流（429）", "降低并发或申请更高配额；当前配置不会自动重试"),
    ("400", "请求被端点拒绝（400）", "检查请求字段与端点兼容性（如 developer role）；细节见 trial.log / agent/native.jsonl"),
    ("context window", "上下文超限", "检查输入长度与上下文窗口配置；必要时减少题面或压缩工具输出"),
    ("context length", "上下文超限", "检查输入长度与上下文窗口配置；必要时减少题面或压缩工具输出"),
    ("timeout", "运行超时", "检查 trial 时限与模型延迟；可用日志确认卡在工具、验证还是模型调用"),
)


def diagnose(trial: dict) -> dict:
    """Return ``error_label``/``error_hint`` without hiding the raw error."""
    error = (trial.get("error") or "").strip()
    message = (trial.get("error_message") or "").strip()
    text = f"{error} {message}".lower()
    if error:
        for needle, label, hint in RULES:
            if needle in text:
                # A bare 400 in an unrelated message is too weak a signal.
                if needle == "400" and not re.search(r"\b400\b", text):
                    continue
                return {"error_label": label, "error_hint": hint}
        return {"error_label": error, "error_hint": "查看 trial.log 与 agent/ 日志定位调用栈"}
    tokens = (trial.get("input_tokens") or 0) + (trial.get("output_tokens") or 0)
    reward = trial.get("reward")
    if tokens == 0 and reward != 1:
        return {"error_label": "疑似首次调用失败", "error_hint": ZERO_TOKEN_HINT}
    return {}
