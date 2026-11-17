"""Profile composition: emit ordinary Harbor jobs without persisting credentials."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re

import yaml
from hbench.registry import harness_registry

ROOT = Path(__file__).resolve().parents[1]
ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SECRET_REFERENCE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::-)?\}")
MODEL_FIELDS = frozenset({"name", "base_url", "api_key_name", "api_formats",
                          "skip_model_parsing", "environment_env", "verifier_env"})
ENVIRONMENT_IMPORT_PATHS = {
    "kubernetes": "hbench.environment:KubernetesEnvironment",
    "docker": "hbench.docker_environment:FrozenDockerEnvironment",
}
KUBERNETES_ONLY_KWARGS = frozenset({
    "namespace", "kubeconfig", "context", "registry", "skip_image_check",
    "image_pull_secret", "compose", "network_enforcement", "transfer",
    "pod_ready_timeout_sec", "memory_limit_multiplier", "pod_overrides",
    "node_selector", "gpu_resource", "gpu_type_node_selectors", "use_sandbox_claim",
})


def _check_non_secret(value, path=()):
    """Reject credential fields/references before resolving ordinary settings."""
    from harbor.utils.env import is_sensitive_header_name
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("Profile setting names must be strings")
            location = path + (key,)
            if key in ("api_key_name", "model_api_key_name"):
                if not isinstance(item, str) or not ENV_NAME.fullmatch(item):
                    raise ValueError("API key names must be literal environment variable names")
                continue
            # These Kubernetes fields carry a resource name or a boolean, not a credential.
            resource_name = location == ("cluster", "kwargs", "image_pull_secret")
            token_flag = key == "automountServiceAccountToken" and isinstance(item, bool)
            if not resource_name and not token_flag and (key == "env" or is_sensitive_header_name(key)):
                raise ValueError("Credentials are not allowed in non-env profile settings")
            _check_non_secret(item, location)
    elif isinstance(value, list):
        for item in value:
            _check_non_secret(item, path)
    elif isinstance(value, str):
        for name, _ in ENV_PATTERN.findall(value):
            resource_name = path == ("cluster", "kwargs", "image_pull_secret") and name == "HB_IMAGE_PULL_SECRET"
            if is_sensitive_header_name(name) and not resource_name:
                raise ValueError("Secret variables must not be expanded in non-env profile settings")


def _check_env(env):
    from harbor.utils.env import is_sensitive_header_name
    if not isinstance(env, dict):
        raise ValueError("Profile env must be a string mapping")
    for key, value in env.items():
        sensitive = isinstance(key, str) and is_sensitive_header_name(key)
        if isinstance(value, str):
            sensitive = sensitive or any(is_sensitive_header_name(name) for name, _ in ENV_PATTERN.findall(value))
        if sensitive and not (isinstance(value, str) and (value == "" or SECRET_REFERENCE.fullmatch(value))):
            raise ValueError("Sensitive env values must use a variable reference without a secret default")
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("Profile env must be a string mapping")


def load_profile(kind, name):
    path = Path(name).expanduser()
    if not path.is_file():
        if kind == "harnesses":
            registry = harness_registry()
            if str(name) in registry:
                return deepcopy(registry[str(name)])
        path = ROOT / "configs" / kind / (str(name) + ".yaml")
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Profile must be an object: {path}")
    return value


def expand(value):
    """Resolve non-secret settings; call only outside env dictionaries."""
    if isinstance(value, dict):
        return {k: expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand(v) for v in value]
    if not isinstance(value, str):
        return value

    def replace(match):
        name, default = match.groups()
        if os.environ.get(name):
            return os.environ[name]
        if default is not None:
            return default
        raise ValueError(f"Required configuration variable is missing: {name}")
    return ENV_PATTERN.sub(replace, value)


def selected_tasks(dataset, n_tasks=None, task_names=None, exclude_gpu=False):
    names = []
    import tomllib
    for folder in sorted(dataset.iterdir()):
        if not folder.is_dir() or not (folder / "task.toml").is_file():
            continue
        if task_names and folder.name not in task_names:
            continue
        if exclude_gpu:
            value = tomllib.loads((folder / "task.toml").read_text())
            if value.get("environment", {}).get("gpus", 0) or value.get("verifier", {}).get("environment", {}).get("gpus", 0):
                continue
        names.append(folder.name)
    if task_names and set(task_names) - set(names):
        raise ValueError("Unknown or excluded task names: " + ", ".join(sorted(set(task_names) - set(names))))
    if n_tasks is not None:
        if n_tasks < 1:
            raise ValueError("n_tasks must be positive")
        names = names[:n_tasks]
    if not names:
        raise ValueError(f"No prepared tasks selected in {dataset}")
    return names


def render_job(benchmarks, harnesses, model, cluster, dataset_root, *, job_name=None,
               n_tasks=None, task_names=None, exclude_gpu=False, concurrency=1, attempts=1,
               image_map=None):
    if concurrency < 1 or attempts < 1:
        raise ValueError("concurrency and attempts must be positive")
    if not benchmarks or not harnesses:
        raise ValueError("Select at least one benchmark and one harness")
    # One profile keeps the original contract; a list expands models x harnesses.
    models = model if isinstance(model, list) else [model]
    if not models:
        raise ValueError("Select at least one model")
    for item in models:
        if not isinstance(item, dict) or set(item) - MODEL_FIELDS:
            raise ValueError("Unsupported model settings; credentials must use api_key_name or env references")
    model = models[0]
    # Environment/verifier env is job-wide in Harbor, so it must not differ per model.
    if any(item.get("environment_env", {}) != model.get("environment_env", {})
           or item.get("verifier_env", {}) != model.get("verifier_env", {}) for item in models):
        raise ValueError("All models in one job must share environment_env and verifier_env")
    env = deepcopy(model.get("environment_env", {}))
    verifier_overrides = deepcopy(model.get("verifier_env", {}))
    # Validate both dictionaries before merging: overrides must not hide unsafe values.
    _check_env(env)
    _check_env(verifier_overrides)
    verifier_env = {**env, **verifier_overrides}
    raw_models = [{k: v for k, v in item.items() if k not in ("environment_env", "verifier_env")}
                  for item in models]
    for raw_model_settings in raw_models:
        _check_non_secret(raw_model_settings, ("model",))
    _check_non_secret(benchmarks, ("benchmarks",))
    _check_non_secret(harnesses, ("harnesses",))
    _check_non_secret(cluster, ("cluster",))
    if image_map is not None:
        _check_non_secret(image_map, ("image_map",))
    backend = cluster.get("backend", "kubernetes")
    if not isinstance(backend, str) or backend not in ENVIRONMENT_IMPORT_PATHS:
        raise ValueError("Unsupported execution backend; choose docker or kubernetes")
    if backend == "docker" and KUBERNETES_ONLY_KWARGS.intersection(cluster.get("kwargs", {})):
        raise ValueError("Docker execution profile must not contain Kubernetes-only settings")
    agent_configs = []
    all_settings = [expand(item) for item in raw_models]
    model_names = [item["name"] for item in all_settings]
    # Results are grouped by (harness, model name, dataset); names must stay distinct.
    if len(set(model_names)) != len(model_names):
        raise ValueError("Model names in one job must be distinct")
    for model_settings in all_settings:
        for harness in harnesses:
            api_format = model_settings.get("api_formats", {}).get(harness["name"])
            if api_format not in harness["supported_api_formats"]:
                raise ValueError(f"{harness['name']} does not support model API format {api_format!r}")
            agent_configs.append({
                "import_path": harness["import_path"],
                "model_name": model_settings["name"],
                "base_url": model_settings["base_url"],
                "model_api_key_name": model_settings["api_key_name"],
                "model_api_format": api_format,
                "skip_model_parsing": model_settings.get("skip_model_parsing", True),
                "kwargs": deepcopy(harness.get("kwargs", {})),
            })
    datasets = []
    sources = []
    for benchmark in benchmarks:
        path = Path(dataset_root).resolve() / benchmark["name"]
        manifest_path = path / "manifest.json"
        if not manifest_path.is_file():
            raise ValueError(f"Dataset is not prepared: {path}; run datasets prepare first")
        manifest = json.loads(manifest_path.read_text())
        # Prepared source identity must match the requested profile, not just its name.
        identity = manifest.get("profile", manifest.get("dataset", {}))
        if isinstance(identity, dict):
            if identity.get("version") != benchmark["version"] or identity.get("package") != benchmark["package"]:
                raise ValueError(f"Prepared source identity differs from profile: {path}")
        else:
            raise ValueError(f"Prepared manifest lacks source profile: {path}")
        names = selected_tasks(path, n_tasks, task_names, exclude_gpu)
        datasets.append({"path": str(path), "task_names": names})
        sources.append({"name": benchmark["name"], "package": benchmark["package"],
                        "version": benchmark["version"], "tasks": names})
    kwargs = expand(cluster.get("kwargs", {}))
    kwargs = {k: v for k, v in kwargs.items() if v != ""}
    if image_map is not None:
        kwargs["image_map"] = image_map
    if kwargs.get("use_sandbox_claim"):
        raise ValueError("The unified environment does not support SandboxClaim")
    name = job_name or "hbench-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        raise ValueError("job_name must be a simple directory name")
    job = {
        "job_name": name, "jobs_dir": str(ROOT / "artifacts" / "jobs"),
        "n_attempts": attempts, "n_concurrent_trials": concurrency,
        "retry": {"max_retries": 0},
        "environment": {"import_path": ENVIRONMENT_IMPORT_PATHS[backend], "force_build": False,
                        "delete": True, "env": env, "kwargs": kwargs},
        "agents": agent_configs, "datasets": datasets, "verifier": {"env": verifier_env},
    }
    from harbor.models.job.config import JobConfig
    JobConfig.model_validate(job)
    plan = {"schema_version": 1, "harbor_version": "0.25.0", "job_name": name,
            "datasets": sources, "harnesses": [h["name"] for h in harnesses],
            "model": all_settings[0], "models": all_settings,
            "combinations": [{"dataset": s["name"], "model": m, "harness": h["name"],
                              "planned_trials": len(s["tasks"]) * attempts}
                             for s in sources for m in model_names for h in harnesses],
            "cluster": cluster["name"], "backend": backend, "attempts": attempts,
            "concurrency": concurrency,
            "planned_trials": sum(len(s["tasks"]) for s in sources) * len(harnesses) * len(models) * attempts,
            "execution": "not_executed", "note": "Adapted environments; not an official upstream leaderboard score."}
    return job, plan


def validate_execution(job):
    """Reject example profiles before any cloud/model operation."""
    for agent in job["agents"]:
        if "example" in agent["model_name"] or "example.invalid" in agent["base_url"]:
            raise ValueError("Replace the example model name/base URL before execution")
        key = agent.get("model_api_key_name")
        if not key or not os.environ.get(key):
            raise ValueError(f"Model API key environment variable is missing: {key}")
    runtime = job["environment"]["kwargs"].get("harness_runtime_image", "")
    if not runtime or "example.invalid" in runtime:
        raise ValueError("Configure a real, built harness_runtime_image before execution")
    from harbor.utils.env import resolve_env_vars
    resolved = resolve_env_vars(job["environment"].get("env", {}))
    if any("tau3" in d["path"] for d in job["datasets"]):
        if not resolved.get("OPENAI_API_KEY") or not resolved.get("OPENAI_BASE_URL"):
            raise ValueError("tau3 requires HB_TAU_API_KEY and HB_TAU_BASE_URL for its simulated user/assertions")
        if any("example" in resolved.get(k, "") for k in ("TAU2_USER_MODEL", "TAU2_NL_ASSERTIONS_MODEL")):
            raise ValueError("Configure actual tau3 simulated-user and assertions model IDs")
