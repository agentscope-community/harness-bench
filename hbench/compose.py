"""Strict, pure Docker Compose -> same-Pod translation (not a Docker daemon)."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from ipaddress import ip_address
import math
from pathlib import Path, PurePosixPath
import re
import shlex
from typing import Any, Mapping, Sequence

import yaml


_NAMED_LISTS = {"containers": "name", "initContainers": "name", "volumes": "name",
                "env": "name", "volumeMounts": "mountPath", "imagePullSecrets": "name"}


def merge_pod_overrides(base: dict, override: dict) -> dict:
    """Merge Kubernetes lists by identity, never by container position.

    An unnamed container override means main, for ACK CLI compatibility.
    Inputs are not mutated; main is always first for Harbor ACK's final merge.
    """
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict):
            result[key] = merge_pod_overrides(result[key] if isinstance(result.get(key), dict) else {}, value)
        elif key in _NAMED_LISTS and isinstance(value, list):
            identity = _NAMED_LISTS[key]
            merged = deepcopy(result.get(key, []))
            seen = set()
            for raw in value:
                if not isinstance(raw, dict):
                    raise ValueError(f"Kubernetes {key} entries must be mappings")
                item = deepcopy(raw)
                if key == "containers" and "name" not in item:
                    item["name"] = "main"
                name = item.get(identity)
                if not name or name in seen:
                    raise ValueError(f"{key} requires unique {identity}: {name!r}")
                seen.add(name)
                index = next((i for i, old in enumerate(merged) if old.get(identity) == name), None)
                if index is None:
                    merged.append(item)
                else:
                    merged[index] = merge_pod_overrides(merged[index], item)
            if key == "containers":
                merged.sort(key=lambda item: item.get("name") != "main")
            result[key] = merged
        else:
            result[key] = deepcopy(value)
    return result


_VARIABLE = re.compile(r"\$\$|\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:-|-)([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def interpolate(value: Any, variables: Mapping[str, str]) -> Any:
    """Expand Compose variables once; $$ becomes a literal container-shell $."""
    if isinstance(value, str):
        def replace(match):
            if match.group(0) == "$$":
                return "$"
            name = match.group(1) or match.group(4)
            current = variables.get(name)
            operator = match.group(2)
            if current is None or (operator == ":-" and current == ""):
                return match.group(3) or ""
            return str(current)
        return _VARIABLE.sub(replace, value)
    if isinstance(value, list):
        return [interpolate(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: interpolate(item, variables) for key, item in value.items()}
    return value


def _environment(value: Any, variables: Mapping[str, str]) -> dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, list):
        parsed = {}
        for item in value:
            if not isinstance(item, str):
                raise ValueError("Compose environment list entries must be strings")
            key, separator, val = item.partition("=")
            parsed[key] = val if separator else variables.get(key, "")
    elif isinstance(value, dict):
        parsed = {key: variables.get(key, "") if val is None else str(val) for key, val in value.items()}
    else:
        raise ValueError("Compose environment must be a mapping or list")
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in parsed):
        raise ValueError("Invalid Compose environment variable name")
    return parsed


def load_compose(environment_dir: Path, extra_paths: Sequence[Path] = ()) -> list[dict]:
    """Read files separately from the pure translation function."""
    primary = next((environment_dir / name for name in ("docker-compose.yaml", "docker-compose.yml")
                    if (environment_dir / name).is_file()), None)
    paths = ([primary] if primary else []) + list(extra_paths)
    documents = []
    for path in paths:
        document = yaml.safe_load(path.read_text())
        if not isinstance(document, dict):
            raise ValueError(f"Compose file must contain a mapping: {path}")
        documents.append(document)
    return documents


def _merge_compose_service(base: dict, override: dict) -> dict:
    # Compose volume strings are NOT Kubernetes named volume dictionaries.
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_compose_service(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _argv(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return shlex.split(value)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value.copy()
    raise ValueError("Compose command/entrypoint must be a string or string list")


def _seconds(value: Any, default: int) -> int:
    if value is None:
        return default
    match = re.fullmatch(r"([0-9.]+)(ns|us|ms|s|m|h)?", str(value))
    if not match:
        raise ValueError(f"Unsupported healthcheck duration: {value}")
    factor = {None: 1, "ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1, "m": 60, "h": 3600}[match[2]]
    return max(1, math.ceil(float(match[1]) * factor))


def healthcheck_probe(value: dict | None) -> dict | None:
    if not value:
        return None
    unknown = set(value) - {"test", "disable", "interval", "timeout", "retries", "start_period", "start_interval"}
    if unknown:
        raise ValueError(f"Unsupported healthcheck keys: {sorted(unknown)}")
    test = value.get("test")
    if value.get("disable") or test == ["NONE"]:
        return None
    if isinstance(test, str):
        command = ["sh", "-c", test]
    elif isinstance(test, list) and test and test[0] == "CMD":
        command = test[1:]
    elif isinstance(test, list) and len(test) == 2 and test[0] == "CMD-SHELL":
        command = ["sh", "-c", test[1]]
    else:
        raise ValueError("healthcheck requires CMD or CMD-SHELL")
    if not command or not all(isinstance(item, str) for item in command):
        raise ValueError("Invalid healthcheck command")
    return {"exec": {"command": command}, "periodSeconds": _seconds(value.get("interval"), 10),
            "timeoutSeconds": _seconds(value.get("timeout"), 5),
            "failureThreshold": max(1, int(value.get("retries", 3))),
            "initialDelaySeconds": _seconds(value.get("start_period"), 0) if value.get("start_period") else 0}


def _quantity(value: Any) -> str:
    text = str(value)
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([kKmMgG])(?:[bB])?", text)
    return match[1] + {"k": "Ki", "m": "Mi", "g": "Gi"}[match[2].lower()] if match else text


def _port_range(value: Any, context: str) -> tuple[int, int]:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"Invalid Compose port/range in {context}: {value!r}")
    match = re.fullmatch(r"([0-9]{1,5})(?:-([0-9]{1,5}))?", str(value))
    if not match:
        raise ValueError(f"Invalid Compose port/range in {context}: {value!r}")
    start, end = int(match[1]), int(match[2] or match[1])
    if not 1 <= start <= end <= 65535:
        raise ValueError(f"Invalid Compose port/range in {context}: {value!r}")
    return start, end


def _port_protocol(value: Any, context: str) -> str:
    if not isinstance(value, str) or value.lower() not in {"tcp", "udp", "sctp"}:
        raise ValueError(f"Unsupported Compose port protocol in {context}: {value!r}")
    return value.lower()


def _port_host(value: Any, context: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"Invalid Compose port host IP in {context}: {value!r}")
    try:
        ip_address(value)
    except ValueError as exc:
        raise ValueError(f"Invalid Compose port host IP in {context}: {value!r}") from exc


def _declared_ports(service: dict, name: str) -> list[tuple[str, int, int]]:
    """Parse target intervals only; publishing does not isolate Pod listeners."""
    intervals = []
    for field in ("ports", "expose"):
        declarations = service.get(field)
        if declarations is None:
            continue
        if not isinstance(declarations, list):
            raise ValueError(f"Compose {name}.{field} must be a list")
        for declaration in declarations:
            context = f"{name}.{field}"
            if isinstance(declaration, dict):
                allowed = {"target", "protocol"}
                if field == "ports":
                    allowed |= {"published", "host_ip", "mode", "name", "app_protocol"}
                unknown = set(declaration) - allowed
                if unknown:
                    raise ValueError(f"Unsupported Compose port keys in {context}: {sorted(unknown, key=str)}")
                target = _port_range(declaration.get("target"), context)
                protocol = _port_protocol(declaration.get("protocol", "tcp"), context)
                if "published" in declaration:
                    _port_range(declaration["published"], context)
                if "host_ip" in declaration:
                    _port_host(declaration["host_ip"], context)
                if "mode" in declaration and declaration["mode"] not in ("host", "ingress"):
                    raise ValueError(f"Unsupported Compose port mode in {context}: {declaration['mode']!r}")
                for key in ("name", "app_protocol"):
                    if key in declaration and (not isinstance(declaration[key], str) or not declaration[key]):
                        raise ValueError(f"Invalid Compose port {key} in {context}: {declaration[key]!r}")
            elif isinstance(declaration, (int, str)) and not isinstance(declaration, bool):
                text, separator, suffix = str(declaration).partition("/")
                protocol = _port_protocol(suffix if separator else "tcp", context)
                parts = text.split(":")
                if field == "ports" and text.startswith("["):
                    match = re.fullmatch(r"\[([^\]]+)\]:([^:]+):([^:]+)", text)
                    if not match or ":" not in match[1]:
                        raise ValueError(f"Unsupported Compose port declaration in {context}: {declaration!r}")
                    _port_host(match[1], context)
                    parts = [match[2], match[3]]
                elif field == "ports" and len(parts) == 3:
                    _port_host(parts[0], context)
                    parts = parts[1:]
                if len(parts) not in ((1, 2) if field == "ports" else (1,)):
                    raise ValueError(f"Unsupported Compose port declaration in {context}: {declaration!r}")
                target = _port_range(parts[-1], context)
                if len(parts) == 2:
                    published = _port_range(parts[0], context)
                    if published[1] - published[0] != target[1] - target[0]:
                        raise ValueError(f"Compose port ranges must have equal lengths in {context}: {declaration!r}")
            else:
                raise ValueError(f"Unsupported Compose port declaration in {context}: {declaration!r}")
            intervals.append((protocol, *target))
    return intervals


@dataclass(frozen=True)
class ComposePlan:
    pod_overrides: dict[str, Any]
    services: dict[str, str]
    warnings: tuple[str, ...] = ()


_SERVICE_KEYS = {"image", "build", "command", "entrypoint", "environment", "working_dir", "user",
                 "healthcheck", "ports", "expose", "depends_on", "volumes", "shm_size", "cpus",
                 "mem_limit", "cap_add", "cap_drop", "privileged", "container_name", "labels", "restart", "network_mode"}


def translate_compose(documents: Sequence[dict], *, main_image: str,
                      variables: Mapping[str, str] | None = None,
                      main_env: Mapping[str, str] | None = None,
                      image_map: Mapping[str, str] | None = None,
                      log_sources: Mapping[str, str] | None = None,
                      main_keepalive: bool = True) -> ComposePlan:
    """Translate only the supported subset, rejecting host/network/data mounts.

    All services share localhost. Ports are not published; overlapping declared
    targets are rejected per protocol. Undeclared image listeners cannot be
    detected. depends_on is a whole-Pod readiness gate, NOT ordered startup.
    Only log volumes become shared emptyDir; other bind/named volumes must be
    prepared into images. Set main_keepalive=False for a recorded workload
    main_entrypoint to preserve image ENTRYPOINT/CMD when Compose omits them.
    """
    variables, log_sources = variables or {}, log_sources or {}
    services: dict[str, dict] = {}
    named_volumes: dict[str, dict] = {}
    for raw in documents:
        unknown = set(raw) - {"services", "version", "name", "volumes"}
        if unknown:
            raise ValueError(f"Unsupported Compose top-level keys: {sorted(unknown)}")
        document = interpolate(raw, variables)
        for name, definition in (document.get("volumes") or {}).items():
            if definition not in (None, {}):
                raise ValueError(f"External/driver Compose volumes are unsupported: {name}")
            named_volumes[name] = {}
        if not isinstance(document.get("services", {}), dict):
            raise ValueError("Compose services must be a mapping")
        for name, definition in document.get("services", {}).items():
            if not isinstance(definition, dict):
                raise ValueError(f"Invalid service: {name}")
            old = services.get(name, {})
            new = deepcopy(definition)
            if "environment" in new:
                new["environment"] = {**_environment(old.get("environment"), variables),
                                      **_environment(new["environment"], variables)}
            if "volumes" in old and "volumes" in new:
                def target(mount):
                    if isinstance(mount, dict):
                        return mount.get("target")
                    parts = str(mount).split(":")
                    if len(parts) < 2:
                        raise ValueError(f"Unsupported Compose volume: {mount}")
                    return parts[1]
                new["volumes"] = list({target(m): m for m in [*old["volumes"], *new["volumes"]]}.values())
            services[name] = _merge_compose_service(old, new)
    if "main" not in services:
        services = {"main": {}, **services}
    names = {name: re.sub(r"[^a-z0-9-]", "-", name.lower()) for name in services}
    if (len(set(names.values())) != len(names) or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", name)
                                                   for name in names.values())):
        raise ValueError("Compose service names collide or are invalid Kubernetes container names")
    containers, volumes = [], [{"name": "hbench-logs", "emptyDir": {}}]
    warnings = []
    declared_ports: dict[str, list[tuple[str, int, int]]] = {}
    for name in ["main", *(name for name in services if name != "main")]:
        service = services[name]
        unknown = {key for key in service if key not in _SERVICE_KEYS and not key.startswith("x-")}
        if unknown:
            raise ValueError(f"Unsupported Compose service keys for {name}: {sorted(unknown)}")
        for protocol, start, end in _declared_ports(service, name):
            previous = declared_ports.setdefault(protocol, [])
            for other, other_start, other_end in previous:
                if other != name and start <= other_end and other_start <= end:
                    raise ValueError(f"Declared target port overlap on {protocol} in shared Pod: "
                                     f"{other} ({other_start}-{other_end}) and {name} ({start}-{end})")
            previous.append((name, start, end))
        network_mode = service.get("network_mode")
        if network_mode is not None:
            if not isinstance(network_mode, str) or not network_mode.startswith("service:") or network_mode[8:] not in services:
                raise ValueError("Only network_mode=service:<known-service> maps to the shared Pod network; host/none/custom modes are unsupported")
        if service.get("privileged"):
            raise ValueError("Privileged Compose services are unsupported")
        if service.get("restart") not in (None, "no"):
            raise ValueError("Compose restart policies are unsupported (Pod restartPolicy=Never)")
        image = main_image if name == "main" else service.get("image")
        if not image:
            raise ValueError(f"Service {name} requires a prepared image; build-only sidecars are unsupported")
        # main_image is already resolved by the environment. Mapping twice can
        # duplicate a destination prefix when source and destination overlap.
        if name != "main":
            image = map_image(image, image_map or {})
        container = {"name": names[name], "image": image, "securityContext": {"privileged": False}}
        # Explicit null overrides ACK defaults without replacing image ENTRYPOINT.
        container["command"] = _argv(service.get("entrypoint"))
        container["args"] = _argv(service.get("command"))
        if name == "main" and main_keepalive and container["command"] is None and container["args"] is None:
            container["args"] = ["sleep", "infinity"]
        if "working_dir" in service:
            container["workingDir"] = service["working_dir"]
        env = _environment(service.get("environment"), variables)
        if name == "main":
            env.update(main_env or {})
        container["env"] = [{"name": key, "value": val} for key, val in env.items()]
        if service.get("user") is not None:
            uid, _, gid = str(service["user"]).partition(":")
            if not uid.isdigit() or (gid and not gid.isdigit()):
                raise ValueError("Compose user must be numeric UID[:GID] on Kubernetes")
            container["securityContext"]["runAsUser"] = int(uid)
            if gid:
                container["securityContext"]["runAsGroup"] = int(gid)
        for key, kube_key in (("cap_add", "add"), ("cap_drop", "drop")):
            if service.get(key):
                if not isinstance(service[key], list):
                    raise ValueError(f"{key} must be a list")
                container["securityContext"].setdefault("capabilities", {})[kube_key] = service[key]
        probe = healthcheck_probe(service.get("healthcheck"))
        if probe:
            container["readinessProbe"] = probe
        dependencies = service.get("depends_on") or {}
        if isinstance(dependencies, list):
            dependencies = dict.fromkeys(dependencies, {})
        if not isinstance(dependencies, dict):
            raise ValueError("depends_on must be a list or mapping")
        for dependency, options in dependencies.items():
            if dependency not in services:
                raise ValueError(f"Unknown depends_on service: {dependency}")
            if not isinstance(options, dict) or set(options) - {"condition", "required"}:
                raise ValueError("Unsupported depends_on options")
            condition = options.get("condition", "service_started")
            if condition not in ("service_started", "service_healthy") or options.get("required") is False:
                raise ValueError("Only required started/healthy dependencies are supported")
            if condition == "service_healthy" and not healthcheck_probe(services[dependency].get("healthcheck")):
                raise ValueError(f"Healthy dependency {dependency} has no healthcheck")
        if dependencies:
            warnings.append(f"{name}: depends_on gates total Pod readiness, not startup order")
        mounts = []
        for mount in service.get("volumes") or []:
            if isinstance(mount, str):
                parts = mount.split(":")
                if len(parts) not in (2, 3) or (len(parts) == 3 and parts[2] not in ("ro", "rw")):
                    raise ValueError(f"Unsupported Compose volume: {mount}")
                source, target = parts[:2]
                readonly = len(parts) == 3 and parts[2] == "ro"
            elif isinstance(mount, dict) and not set(mount) - {"type", "source", "target", "read_only"}:
                if mount.get("type", "bind") not in ("bind", "volume"):
                    raise ValueError("Only log bind/volume mounts are supported")
                source, target, readonly = mount.get("source"), mount.get("target"), mount.get("read_only", False)
            else:
                raise ValueError(f"Unsupported Compose volume: {mount}")
            if not source or not target or not str(target).startswith("/"):
                raise ValueError("Compose volumes need a source and absolute target")
            log_path = log_sources.get(str(source))
            if log_path is None and str(source).startswith("/logs"):
                log_path = str(source)
            if log_path is None and source in named_volumes and str(target).startswith("/logs"):
                log_path = str(target)
            path = PurePosixPath(log_path or "/")
            if not path.is_relative_to("/logs") or ".." in path.parts:
                raise ValueError(f"Non-log volume must be baked into a prepared image: {source}:{target}")
            subpath = str(path.relative_to("/logs"))
            entry = {"name": "hbench-logs", "mountPath": target, "readOnly": bool(readonly)}
            if subpath != ".":
                entry["subPath"] = subpath
            mounts.append(entry)
        if name == "main" and not any(m["mountPath"] == "/logs" for m in mounts):
            mounts.append({"name": "hbench-logs", "mountPath": "/logs"})
        if "shm_size" in service:
            volume_name = f"shm-{names[name]}"
            volumes.append({"name": volume_name, "emptyDir": {"medium": "Memory", "sizeLimit": _quantity(service["shm_size"])}})
            mounts.append({"name": volume_name, "mountPath": "/dev/shm"})
        container["volumeMounts"] = mounts
        limits = {}
        if "cpus" in service:
            limits["cpu"] = str(service["cpus"])
        if "mem_limit" in service:
            limits["memory"] = _quantity(service["mem_limit"])
        if limits:
            container["resources"] = {"limits": limits}
        containers.append(container)
    return ComposePlan({"spec": {"containers": containers, "volumes": volumes,
                                "hostAliases": [{"ip": "127.0.0.1", "hostnames": list(services)}]}}, names, tuple(warnings))


def map_image(image: str, mapping: Mapping[str, str]) -> str:
    """Use the longest configured prefix (exact image mappings also work)."""
    for prefix in sorted(mapping, key=len, reverse=True):
        if image.startswith(prefix):
            return mapping[prefix] + image[len(prefix):]
    return image
