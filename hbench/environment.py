"""Harbor 0.25 Kubernetes/ACK/ACS environment using ordinary Pods.

NetworkPolicy enforcement requires a dedicated, isolated namespace and a CNI
which enforces egress policy, with no other additive allow policies selecting
these Pods. Hostname/IPv6 allowlists are intentionally rejected; DNS is only
an optional TCP/UDP 53 exception for kube-system/kube-dns Pods. This adapter
never uses host networking, privileged DinD, or SandboxClaim.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shlex
import time
import tomllib
from typing import Any

from harbor.environments.ack import ACKEnvironment
from harbor.environments.base import ExecResult
from harbor.environments.capabilities import EnvironmentCapabilities, EnvironmentResourceCapabilities
from harbor.models.task.config import NetworkMode, NetworkPolicy
from harbor.models.trial.config import ResourceMode
from harbor.utils.env import resolve_env_vars
from kubernetes import client as k8s_client
from kubernetes.client.exceptions import ApiException
from kubernetes.stream import stream
from kubernetes.utils.quantity import parse_quantity

from hbench.compose import healthcheck_probe, load_compose, map_image, merge_pod_overrides, translate_compose
from hbench.runtime_mount import add_setup_gate, runtime_overrides
from hbench.transfers import TransferMixin

EXEC_CONTAINER: ContextVar[str] = ContextVar("hbench_exec_container", default="main")
POLICY_LABEL = "hbench.io/pod"

# Linux namespace PID 1 ignores unhandled signals, even SIGKILL from inside
# the namespace. Preserve only an inert sleep/tail PID 1, kill every other
# workload process, and verify /proc instead of trusting kill's exit status.
_KEEPALIVE_ARGS = ["sleep", "infinity"]
_STOP_MAIN_SCRIPT = r'''
set -eu
protected=" "
p=$$
while [ "$p" -gt 1 ]; do
    protected="$protected$p "
    IFS= read -r stat < "/proc/$p/stat" || break
    rest=${stat##*) }
    read -r state p rest <<< "$rest"
done
collect() {
    targets=()
    for file in /proc/[0-9]*/stat; do
        pid=${file#/proc/}; pid=${pid%/stat}
        case " $protected" in *" $pid "*) continue ;; esac
        [ "$pid" != 1 ] || continue
        IFS= read -r stat < "$file" 2>/dev/null || continue
        rest=${stat##*) }
        read -r state rest <<< "$rest"
        case "$state" in Z|X) continue ;; esac
        targets+=("$pid")
    done
}
collect
for pid in "${targets[@]}"; do kill -TERM "$pid" 2>/dev/null || true; done
sleep 0.2
for round in 1 2 3 4 5; do
    collect
    [ "${#targets[@]}" -ne 0 ] || break
    for pid in "${targets[@]}"; do kill -KILL "$pid" 2>/dev/null || true; done
    sleep 0.2
done
collect
[ "${#targets[@]}" -eq 0 ] || { printf '%s\n' 'Main workload processes survived termination' >&2; exit 1; }
mapfile -d '' -t init_argv < /proc/1/cmdline
case "${init_argv[*]}" in
    'sleep infinity'|'/bin/sleep infinity'|'/usr/bin/sleep infinity'|'tail -f /dev/null'|'/bin/tail -f /dev/null'|'/usr/bin/tail -f /dev/null')
        printf '%s\n' HBENCH_WORKLOAD_STOPPED ;;
    *)
        kill -TERM 1 2>/dev/null || true
        kill -INT 1 2>/dev/null || true
        printf '%s\n' 'PID 1 is not an inert keepalive; container termination must be confirmed' >&2
        exit 1 ;;
esac
'''


class ContainerCoreV1Api(k8s_client.CoreV1Api):
    """Keep a real bound method: Kubernetes stream needs __self__.api_client."""
    def connect_get_namespaced_pod_exec(self, *args, **kwargs):
        kwargs.setdefault("container", EXEC_CONTAINER.get())
        return super().connect_get_namespaced_pod_exec(*args, **kwargs)


def _boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if str(value).lower() in ("true", "1", "yes"):
        return True
    if str(value).lower() in ("false", "0", "no"):
        return False
    raise ValueError(f"Expected a boolean, got {value!r}")


def _mapping(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return deepcopy(value)
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("Expected a JSON mapping")
    return parsed


class KubernetesEnvironment(TransferMixin, ACKEnvironment):
    def __init__(self, *args, harness_runtime_image: str | None = None,
                 image_map: dict[str, str] | str | Path | None = None,
                 network_enforcement: str = "none", transfer: str = "stream",
                 compose: bool = True, gpu_resource: str = "nvidia.com/gpu",
                 gpu_type_node_selectors: dict | str | None = None,
                 pod_ready_timeout_sec: float | str | None = None,
                 allow_cluster_dns: bool = True, **kwargs):
        # BaseEnvironment accesses capabilities and validators during ACK init.
        self.network_enforcement = network_enforcement
        self.transfer = transfer
        self._compose_enabled = _boolean(compose)
        self.gpu_resource = gpu_resource
        self.harness_runtime_image = harness_runtime_image
        self.allow_cluster_dns = _boolean(allow_cluster_dns)
        self._gpu_profiles = _mapping(gpu_type_node_selectors)
        self._configured_node_selector = _mapping(kwargs.get("node_selector"))
        self._configured_node_selector.update(_mapping(kwargs.get("pod_overrides")).get("spec", {}).get("nodeSelector", {}))
        self._gpu_selector: dict[str, str] = {}
        if network_enforcement not in ("none", "networkpolicy"):
            raise ValueError("network_enforcement must be none or networkpolicy")
        if transfer not in ("stream", "chunked"):
            raise ValueError("transfer must be stream or chunked")
        if _boolean(kwargs.get("use_sandbox_claim", False)):
            raise ValueError("SandboxClaim is unsupported; use ordinary Kubernetes Pods")
        if not re.fullmatch(r"[a-z0-9.-]+/[A-Za-z0-9_.-]+", gpu_resource):
            raise ValueError("gpu_resource must be a qualified extended-resource name")
        if image_map is not None and not isinstance(image_map, dict):
            image_map = json.loads(Path(image_map).expanduser().read_text())
        self.image_map = _mapping(image_map)
        if any(not isinstance(key, str) or not key or not isinstance(val, str) or not val for key, val in self.image_map.items()):
            raise ValueError("image_map must map nonempty image prefixes to nonempty prefixes")
        self._ready_timeout = None if pod_ready_timeout_sec is None else float(pod_ready_timeout_sec)
        if self._ready_timeout is not None and (not math.isfinite(self._ready_timeout) or self._ready_timeout <= 0):
            raise ValueError("pod_ready_timeout_sec must be a positive finite number")
        self._client_init_lock = asyncio.Lock()
        self._infrastructure_root: ContextVar[bool] = ContextVar("hbench_infrastructure_root", default=False)
        self._policy_api = None
        self._policy_created = False
        self._services = {"main": "main"}
        self._stopped_services: set[str] = set()
        # ACK's fifth positional argument is task_env_config; persistent_env
        # is forwarded to Base by keyword. Resolve a copy before either init.
        kwargs["persistent_env"] = resolve_env_vars(deepcopy(kwargs.get("persistent_env") or {}))
        config = kwargs.get("task_env_config") or (args[4] if len(args) > 4 else None)
        if config is not None and kwargs.get("network_policy") is None:
            kwargs["network_policy"] = config.resolve_baseline()
        super().__init__(*args, **kwargs)
        if self._host_mounts:
            raise ValueError("Host mounts are unsupported; bake non-log inputs into prepared images")
        self._user_pod_overrides = deepcopy(self.pod_overrides)
        self._policy_name = ("hbench-" + self.pod_name)[:63].rstrip("-")

    @staticmethod
    def type() -> str:
        return "kubernetes"

    @classmethod
    def resource_capabilities(cls) -> EnvironmentResourceCapabilities:
        return EnvironmentResourceCapabilities(cpu_limit=True, cpu_request=True, memory_limit=True,
                                               memory_request=True, fractional_cpus=True)

    @property
    def capabilities(self) -> EnvironmentCapabilities:
        enforced = self.network_enforcement == "networkpolicy"
        return EnvironmentCapabilities(gpus=True, docker_compose=self._compose_enabled,
                                       disable_internet=enforced, network_allowlist=enforced,
                                       network_allowlist_ipv4_addresses=enforced,
                                       network_allowlist_ipv4_cidrs=enforced,
                                       dynamic_network_policy=enforced)

    @property
    def _uses_compose(self) -> bool:
        return self._compose_enabled and (bool(self.extra_docker_compose_paths) or any(
            (self.environment_dir / name).is_file() for name in ("docker-compose.yaml", "docker-compose.yml")))

    def _validate_definition(self):
        if not self._compose_enabled and any((self.environment_dir / name).exists()
                                            for name in ("docker-compose.yaml", "docker-compose.yml")):
            raise ValueError("Compose task requires compose=true")
        if self.task_env_config.docker_image:
            return
        super()._validate_definition()

    def _validate_gpu_support(self):
        super()._validate_gpu_support()
        required = self.task_env_config.gpu_types or []
        if not required or not self._effective_gpus:
            return
        for gpu_type in required:
            selector = self._gpu_profiles.get(gpu_type)
            if selector:
                if not isinstance(selector, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in selector.items()):
                    raise ValueError("GPU profiles must contain nodeSelector mappings")
                if any(k in self._configured_node_selector and self._configured_node_selector[k] != v for k, v in selector.items()):
                    continue
                self._gpu_selector = selector.copy()
                return
        if self._configured_node_selector.get("nvidia.com/gpu.product") in required:
            return
        raise ValueError("gpu_types requires an explicit matching nvidia.com/gpu.product nodeSelector or gpu_type_node_selectors profile")

    def _get_image_url(self) -> str:
        return map_image(super()._get_image_url(), self.image_map)

    async def _ensure_client(self):
        async with self._client_init_lock:
            await super()._ensure_client()
            if not isinstance(self._exec_api, ContainerCoreV1Api):
                configuration = deepcopy(self._core_api.api_client.configuration)
                if self._exec_api is not None:
                    self._exec_api.api_client.close()
                self._exec_api = ContainerCoreV1Api(k8s_client.ApiClient(configuration=configuration))
            if self.network_enforcement == "networkpolicy" and self._policy_api is None:
                self._policy_api = k8s_client.NetworkingV1Api(self._core_api.api_client)

    def _timeout(self) -> float:
        return self._ready_timeout if self._ready_timeout is not None else max(1, self.task_env_config.build_timeout_sec - 30)

    def _compose_variables(self) -> tuple[dict, dict]:
        logs = {"AGENT": (self.trial_paths.agent_dir, "/logs/agent"),
                "VERIFIER": (self.trial_paths.verifier_dir, "/logs/verifier"),
                "ARTIFACTS": (self.trial_paths.artifacts_dir, "/logs/artifacts"),
                "USER_AGENT": (self.trial_paths.trial_dir / "user-agent", "/logs/user-agent")}
        variables = {**os.environ, **self._startup_env()}
        sources = {}
        for label, (host, sandbox) in logs.items():
            variables["HOST_" + label + "_LOGS_PATH"] = str(host.resolve())
            variables["ENV_" + label + "_LOGS_PATH"] = sandbox
            sources[str(host.resolve())] = sandbox
            sources[str(host)] = sandbox
        return variables, sources

    def _task_resources(self) -> dict:
        result = {"requests": {}, "limits": {}}
        for resource, quantity in (("cpu", lambda v: str(v)), ("memory", lambda v: f"{v}Mi")):
            for mode, key in ((ResourceMode.REQUEST, "requests"), (ResourceMode.LIMIT, "limits")):
                value = self._resource_policy_value(resource, target=mode, auto_mode=ResourceMode.REQUEST)
                if value is not None:
                    result[key][resource] = quantity(value)
        if self._effective_gpus:
            result["limits"][self.gpu_resource] = str(self._effective_gpus)
            result["requests"][self.gpu_resource] = str(self._effective_gpus)
        # ACK always supplies request keys; zero is the absence of a reservation.
        self.cpu_request = result["requests"].get("cpu", "0")
        self.memory_request = result["requests"].get("memory", "0")
        self.ephemeral_storage_request = f"{self.task_env_config.storage_mb}Mi" if self.task_env_config.storage_mb is not None else None
        if self._memory_resource_mode == ResourceMode.IGNORE:
            self.memory_limit = None
        # Include ACK's fallback resources here too, so validation sees the
        # final budget rather than a fragment later merged at Pod creation.
        result["requests"].setdefault("cpu", self.cpu_request)
        result["requests"].setdefault("memory", self.memory_request)
        if self.ephemeral_storage_request:
            result["requests"]["ephemeral-storage"] = self.ephemeral_storage_request
        if self.memory_limit:
            result["limits"].setdefault("memory", self.memory_limit)
        return result

    @property
    def _is_main_task_environment(self) -> bool:
        # Harbor can reuse environment/ for a separate verifier fallback.
        return self.environment_dir.name == "environment" and "__verifier__" not in self.session_id

    def _main_keepalive(self) -> bool:
        if not self._is_main_task_environment:
            return True
        metadata = self.environment_dir.parent / "hbench.json"
        if not metadata.is_file():
            return True
        entrypoint = json.loads(metadata.read_text()).get("main_entrypoint", [])
        if not isinstance(entrypoint, list) or not all(isinstance(item, str) for item in entrypoint):
            raise ValueError("hbench.json main_entrypoint must be a string list")
        # Metadata indicates an image workload ENTRYPOINT, not a replacement
        # command. Its image CMD must survive; compatibility needs target tests.
        return not bool(entrypoint)

    def _setup_agent_user(self) -> str | int | None:
        if self.default_user is not None:
            return self.default_user
        config = self.environment_dir.parent / "task.toml"
        if self._is_main_task_environment and config.is_file():
            # Harbor only sets default_user after environment startup.
            return tomllib.loads(config.read_text()).get("agent", {}).get("user")
        return None

    @staticmethod
    def _validate_container_resources(containers: list[dict]):
        for container in containers:
            resources = container.get("resources", {})
            parsed = {}
            for kind in ("requests", "limits"):
                parsed[kind] = {}
                for resource, value in resources.get(kind, {}).items():
                    quantity = parse_quantity(value)
                    if not quantity.is_finite() or quantity < 0:
                        raise ValueError(f"Container {container['name']} has invalid {kind} {resource}: {value}")
                    parsed[kind][resource] = quantity
            for resource in parsed["requests"].keys() & parsed["limits"].keys():
                if parsed["requests"][resource] > parsed["limits"][resource]:
                    raise ValueError(f"Container {container['name']} {resource} request exceeds limit")

    def _prepare_pod_overrides(self) -> dict:
        variables, sources = self._compose_variables()
        main_image = self._get_image_url()
        main_keepalive = self._main_keepalive()
        base = {"spec": {"hostNetwork": False, "restartPolicy": "Never", "automountServiceAccountToken": False,
                         "containers": [
            {"name": "main", "securityContext": {"privileged": False, "runAsUser": 0},
             "resources": self._task_resources()}]}}
        if self._uses_compose:
            plan = translate_compose(load_compose(self.environment_dir, self.extra_docker_compose_paths),
                                     main_image=main_image, variables=variables, main_env=self._startup_env(),
                                     image_map=self.image_map, log_sources=sources, main_keepalive=main_keepalive)
            self._services = plan.services
            for warning in plan.warnings:
                self.logger.warning(warning)
            base = merge_pod_overrides(base, plan.pod_overrides)
        else:
            self._services = {"main": "main"}
        subpaths = [mount["subPath"] for container in base["spec"]["containers"]
                    for mount in container.get("volumeMounts", [])
                    if mount.get("name") == "hbench-logs" and "subPath" in mount]
        runtime = runtime_overrides(map_image(self.harness_runtime_image, self.image_map) if self.harness_runtime_image else None,
                                    main_image=main_image, startup_env=self._startup_env(), log_subpaths=subpaths,
                                    main_keepalive=main_keepalive)
        # Runtime must not reset Compose command/args or replace its task env.
        runtime_main = runtime["spec"]["containers"][0]
        if self._uses_compose:
            runtime_main.pop("command", None)
            runtime_main.pop("args", None)
        base = merge_pod_overrides(base, runtime)
        if self._gpu_selector:
            base = merge_pod_overrides(base, {"spec": {"nodeSelector": self._gpu_selector}})
        merged = merge_pod_overrides(base, self._user_pod_overrides)
        spec = merged["spec"]
        containers = spec["containers"]
        if not containers or containers[0].get("name") != "main":
            raise ValueError("Pod must include main as its first container")
        # An explicit user command replaces the image entrypoint, so injected
        # keepalive args must not become arguments of that command.
        user_main = next((c for c in ((self._user_pod_overrides or {}).get("spec") or {}).get("containers") or []
                          if isinstance(c, dict) and c.get("name", "main") == "main"), {})
        base_main = base["spec"]["containers"][0]
        if (user_main.get("command") is not None and "args" not in user_main
                and base_main.get("args") == _KEEPALIVE_ARGS):
            containers[0].pop("args", None)
        for container in containers:
            self._services.setdefault(container["name"], container["name"])
        if spec.get("hostNetwork") or spec.get("hostPID") or spec.get("hostIPC") or spec.get("shareProcessNamespace"):
            raise ValueError("Host/shared process namespaces are unsupported")
        if any("hostPath" in volume for volume in spec.get("volumes", [])):
            raise ValueError("hostPath volumes are unsupported; Pods must have isolated filesystems")
        if spec.get("restartPolicy") != "Never":
            raise ValueError("restartPolicy must be Never for independently stoppable services")
        if any(k in spec.get("nodeSelector", {}) and spec["nodeSelector"][k] != v for k, v in self._gpu_selector.items()):
            raise ValueError("Pod overrides conflict with required GPU type selector")
        main = containers[0]
        for security in (spec.get("securityContext", {}), main.get("securityContext", {})):
            if security.get("runAsUser", 0) != 0 or _boolean(security.get("runAsNonRoot", False)):
                raise ValueError("Main Pod/container must run as root for runtime/Harbor setup; use task agent.user via su for non-root agents")
        self._validate_container_resources([*containers, *spec.get("initContainers", [])])
        if not self._uses_compose and self.task_env_config.healthcheck and "readinessProbe" not in main:
            hc = self.task_env_config.healthcheck
            main["readinessProbe"] = healthcheck_probe({"test": ["CMD-SHELL", hc.command],
                "interval": hc.interval_sec, "timeout": hc.timeout_sec, "retries": hc.retries,
                "start_period": hc.start_period_sec})
        # Add after user merge to preserve their lifecycle/readiness/securityContext.
        containers[0] = add_setup_gate(main, self._timeout(), agent_user=self._setup_agent_user(),
                                       agent_home=self._startup_env().get("HOME"))
        if self.network_enforcement == "networkpolicy":
            all_containers = [*containers, *spec.get("initContainers", [])]
            for container in all_containers:
                security = container.get("securityContext", {})
                caps = {cap.upper().removeprefix("CAP_") for cap in security.get("capabilities", {}).get("add", [])}
                if security.get("privileged") or caps & {"ALL", "NET_ADMIN", "SYS_ADMIN", "BPF", "NET_RAW"}:
                    raise ValueError("Privileged/network-administration containers bypass the NetworkPolicy security contract")
            if any("hostPath" in volume for volume in spec.get("volumes", [])):
                raise ValueError("hostPath volumes are unsupported under NetworkPolicy enforcement")
        metadata = merged.setdefault("metadata", {})
        if metadata.get("name", self.pod_name) != self.pod_name or metadata.get("namespace", self.namespace) != self.namespace:
            raise ValueError("Pod name/namespace overrides would break exec and policy selection")
        metadata.setdefault("labels", {})[POLICY_LABEL] = self.pod_name
        # Base exec overlays otherwise re-export the task's pre-injection PATH,
        # masking the runtime PATH which is present in the Pod's process env.
        for entry in containers[0].get("env", []):
            if entry["name"] in ("PATH", "JAVA_HOME") and "value" in entry:
                self._persistent_env[entry["name"]] = entry["value"]
        return merged

    async def start(self, force_build: bool = False):
        self._stopped_services.clear()
        self.pod_overrides = self._prepare_pod_overrides()
        await self._ensure_client()
        if self.network_enforcement == "networkpolicy":
            # Install the fixed selector BEFORE creating any executable Pod.
            await self._apply_network_policy(self.network_policy)
        # ACK's implicit mkdir/chmod must run as root, even when Harbor has
        # already selected a non-root agent. Keep that default task-local.
        token = self._infrastructure_root.set(True)
        try:
            await super().start(force_build=force_build)
        finally:
            self._infrastructure_root.reset(token)

    def _network_policy_body(self, policy: NetworkPolicy) -> dict:
        self.validate_network_policy_support(policy)
        if policy.network_mode == NetworkMode.PUBLIC:
            egress = [{}]
        elif policy.network_mode == NetworkMode.NO_NETWORK:
            egress = []
        else:
            blocks = []
            for entry in policy.allowed_hosts:
                network = ipaddress.ip_network(entry if "/" in entry else entry + "/32", strict=True)
                if network.version != 4:
                    raise ValueError("Only literal IPv4 addresses/CIDRs can be enforced exactly")
                blocks.append({"ipBlock": {"cidr": str(network)}})
            egress = [{"to": blocks}] if blocks else []
            if self.allow_cluster_dns:
                egress.append({"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                                        "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
                               "ports": [{"protocol": protocol, "port": 53} for protocol in ("UDP", "TCP")]})
        return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                "metadata": {"name": self._policy_name, "namespace": self.namespace},
                "spec": {"podSelector": {"matchLabels": {POLICY_LABEL: self.pod_name}},
                         "policyTypes": ["Egress"], "egress": egress}}

    async def _apply_network_policy(self, network_policy: NetworkPolicy):
        if self.network_enforcement != "networkpolicy":
            raise ValueError("Dynamic network policy requires network_enforcement=networkpolicy")
        self.validate_network_policy_support(network_policy)
        await self._ensure_client()
        body = self._network_policy_body(network_policy)
        if self._policy_created:
            # Include resourceVersion for clusters enforcing optimistic concurrency.
            current = await asyncio.to_thread(self._policy_api.read_namespaced_network_policy,
                                             name=self._policy_name, namespace=self.namespace)
            if current.metadata.resource_version:
                body["metadata"]["resourceVersion"] = current.metadata.resource_version
            await asyncio.to_thread(self._policy_api.replace_namespaced_network_policy,
                                    name=self._policy_name, namespace=self.namespace, body=body)
        else:
            # A collision is not safe to overwrite: reject rather than broaden another Pod's policy.
            async def create_policy():
                try:
                    await asyncio.to_thread(self._policy_api.create_namespaced_network_policy,
                                            namespace=self.namespace, body=body)
                except Exception as error:
                    # Return the outcome so a cancelled shield does not report
                    # an expected collision as an unhandled task exception.
                    return error
                return None

            creation = asyncio.create_task(create_policy())
            cancellation = None
            while True:
                try:
                    error = await asyncio.shield(creation)
                    break
                except asyncio.CancelledError as cancelled:
                    # to_thread cannot stop its API call. Drain it before
                    # cleanup, even if the caller cancels again while waiting.
                    cancellation = cancelled
            self._policy_created = error is None
            if cancellation is not None:
                raise cancellation
            if error is not None:
                raise error

    async def stop(self, delete: bool = True):
        if delete and self._policy_created and self._core_api is None:
            # stop(delete=False) releases ACK's client while retaining its Pod.
            # Reacquire before deleting it; never remove the retained Pod's policy alone.
            await self._ensure_client()
        core = self._core_api
        await super().stop(delete=delete)
        if delete and self._policy_created:
            if core is not None:
                try:
                    await asyncio.to_thread(core.read_namespaced_pod, name=self.pod_name, namespace=self.namespace)
                except ApiException as error:
                    if error.status != 404:
                        raise
                else:
                    raise RuntimeError("Pod still exists; retaining its NetworkPolicy to avoid unprotected egress")
            try:
                await asyncio.to_thread(self._policy_api.delete_namespaced_network_policy,
                                        name=self._policy_name, namespace=self.namespace)
            except ApiException as error:
                if error.status != 404:
                    raise
            self._policy_created = False
        if self._exec_api is not None:
            self._exec_api.api_client.close()
            self._exec_api = None
        # Retained Pods keep their policy even with stop(delete=False).
        self._policy_api = None

    def _container_name(self, service: str | None) -> str:
        if service is None:
            return "main"
        if service not in self._services:
            raise ValueError(f"Unknown Compose service: {service}")
        return self._services[service]

    @contextmanager
    def _in_container(self, service: str | None):
        token = EXEC_CONTAINER.set(self._container_name(service))
        try:
            yield
        finally:
            EXEC_CONTAINER.reset(token)

    def _container_uid(self, name: str) -> int | None:
        container = next((item for item in self.pod_overrides.get("spec", {}).get("containers", [])
                          if item.get("name", "main") == name), {})
        security = container.get("securityContext", {})
        pod_security = self.pod_overrides.get("spec", {}).get("securityContext", {})
        return security.get("runAsUser", pod_security.get("runAsUser", 0 if name == "main" else None))

    def _resolve_user(self, user):
        name = EXEC_CONTAINER.get()
        if name == "main":
            if user is not None:
                resolved = user
            elif self._infrastructure_root.get():
                resolved = "root"
            else:
                resolved = self.default_user if self.default_user is not None else "root"
        else:
            resolved = user
        return None if resolved in ("root", 0) and self._container_uid(name) == 0 else resolved

    def _request_sync(self, command: list[str], *, payload: bytes | None = None,
                      binary: bool = False, timeout_sec: float = 600) -> tuple[bytes, bytes, int]:
        # stream mutates bound_method.__self__.api_client.call_api. A NEW ApiClient
        # per request avoids racing execs/chunk batches, not just per environment.
        configuration = deepcopy(self._exec_api.api_client.configuration)
        with k8s_client.ApiClient(configuration=configuration) as api_client:
            api = ContainerCoreV1Api(api_client)
            response = None
            try:
                response = stream(api.connect_get_namespaced_pod_exec, self.pod_name, self.namespace,
                                  command=command, stderr=True, stdin=payload is not None, stdout=True,
                                  tty=False, binary=binary, _preload_content=False)
                deadline = time.monotonic() + timeout_sec
                stdout, stderr = bytearray(), bytearray()
                if payload is not None:
                    for start in range(0, len(payload), 65536):
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Artifact upload timed out")
                        response.write_stdin(payload[start:start + 65536])
                while response.is_open():
                    if time.monotonic() >= deadline:
                        return bytes(stdout), b"Command timed out", 124
                    response.update(timeout=1)
                    if response.peek_stdout():
                        value = response.read_stdout()
                        stdout.extend(value.encode("utf-8", errors="surrogateescape") if isinstance(value, str) else value)
                    if response.peek_stderr():
                        value = response.read_stderr()
                        stderr.extend(value.encode("utf-8", errors="surrogateescape") if isinstance(value, str) else value)
                return_code = response.returncode
                if return_code is None:
                    raise RuntimeError("Exec stream closed without a remote exit status")
                return bytes(stdout), bytes(stderr), return_code
            finally:
                if response is not None:
                    response.close()

    async def exec(self, command: str, cwd: str | None = None, env: dict[str, str] | None = None,
                   timeout_sec: int | None = None, user: str | int | None = None) -> ExecResult:
        name = EXEC_CONTAINER.get()
        if name in self._stopped_services:
            raise RuntimeError(f"Container {name} has been stopped")
        await self._ensure_client()
        resolved = self._resolve_user(user)
        main = name == "main"
        effective_env = self._merge_env(env) if main else env
        effective_cwd = cwd if cwd is not None else (self.task_env_config.workdir if main else None)
        shell = "/bin/bash" if main else "/bin/sh"
        full_command = ("bash -ic " if main else "sh -c ") + shlex.quote(command)
        if effective_env:
            if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in effective_env):
                raise ValueError("Invalid exec environment variable name")
            full_command = "env " + " ".join(shlex.quote(key + "=" + value) for key, value in effective_env.items()) + " " + full_command
        if effective_cwd:
            full_command = "cd " + shlex.quote(effective_cwd) + " && " + full_command
        if resolved is not None:
            user_arg = f"$(getent passwd {resolved} | cut -d: -f1)" if isinstance(resolved, int) else shlex.quote(resolved)
            full_command = f"su {user_arg} -s {shell} -c {shlex.quote(full_command)}"
        try:
            out, err, code = await asyncio.to_thread(self._request_sync, ["sh", "-c", full_command],
                                                   timeout_sec=timeout_sec if timeout_sec is not None else float("inf"))
            return ExecResult(stdout=out.decode("utf-8", errors="replace"), stderr=err.decode("utf-8", errors="replace"), return_code=code)
        except Exception as error:
            return ExecResult(stderr=str(error), return_code=1)

    async def _stream_transfer(self, command: list[str], payload: bytes | None = None) -> bytes:
        await self._ensure_client()
        out, err, code = await asyncio.to_thread(self._request_sync, command, payload=payload, binary=True)
        if code:
            raise RuntimeError(f"Tar transfer failed ({code}): {err.decode('utf-8', errors='replace')}")
        return out

    async def service_exec(self, command: str, *, service: str | None = None, cwd: str | None = None,
                           env: dict[str, str] | None = None, timeout_sec: int | None = None,
                           user: str | int | None = None) -> ExecResult:
        with self._in_container(service):
            return await self.exec(command, cwd=cwd, env=env, timeout_sec=timeout_sec, user=user)

    async def service_download_file(self, source_path, target_path, *, service: str | None = None):
        with self._in_container(service):
            await self.download_file(source_path, target_path)

    async def service_download_dir(self, source_dir, target_dir, *, service: str | None = None):
        with self._in_container(service):
            await self.download_dir(source_dir, target_dir)

    async def stop_service(self, service: str):
        name = self._container_name(service)
        await self._ensure_client()
        # A closed exec stream or successful kill is NOT proof of termination.
        # sleep/tail namespace PID 1 cannot be killed internally, so main may
        # retain only that inert keepalive after ALL workload children are gone.
        with self._in_container(service):
            result = await self.exec(_STOP_MAIN_SCRIPT if name == "main" else "kill -TERM 1; kill -INT 1",
                                     timeout_sec=10, user="root" if name == "main" else None)
        if name == "main" and result.return_code == 0 and "HBENCH_WORKLOAD_STOPPED" in (result.stdout or "").splitlines():
            self._stopped_services.add(name)
            return
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            pod = await asyncio.to_thread(self._core_api.read_namespaced_pod, name=self.pod_name, namespace=self.namespace)
            status = next((status for status in pod.status.container_statuses or [] if status.name == name), None)
            if status and status.state.terminated:
                self._stopped_services.add(name)
                return
            await asyncio.sleep(0.5)
        raise RuntimeError(f"Container {name} did not terminate after workload stop: {result.stderr or result.stdout}")

    async def _wait_for_pod_ready(self, timeout_sec: float | None = None):
        deadline = time.monotonic() + (self._timeout() if timeout_sec is None else timeout_sec)
        last = "not found"
        expected = {item["name"] for item in self.pod_overrides.get("spec", {}).get("containers", [{"name": "main"}])}
        while time.monotonic() < deadline:
            try:
                pod = await asyncio.to_thread(self._core_api.read_namespaced_pod, name=self.pod_name, namespace=self.namespace)
                last = pod.status.phase
                statuses = pod.status.container_statuses or []
                init_statuses = pod.status.init_container_statuses or []
                if last in ("Failed", "Unknown", "Error") or any(
                    status.state.terminated and (status in statuses or status.state.terminated.exit_code != 0)
                    for status in [*statuses, *init_statuses]):
                    raise RuntimeError("Pod failed to start: " + self._get_pod_failure_summary(pod))
                # ErrImagePull/ImagePullBackOff may be transient cold-pull errors.
                # Retry the same Pod until the infrastructure budget expires.
                if last == "Running" and {status.name for status in statuses} == expected and all(status.ready for status in statuses):
                    probe = await self.exec("true", cwd="/", timeout_sec=10, user="root")
                    if probe.return_code == 0:
                        return
            except ApiException as error:
                if error.status != 404:
                    raise
            await asyncio.sleep(1)
        raise TimeoutError(f"Pod {self.pod_name} not ready within infrastructure budget; last phase: {last}")
