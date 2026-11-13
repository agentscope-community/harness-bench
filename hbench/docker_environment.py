"""Frozen runtime on Harbor's native Docker/Compose backend (Linux only).

Task Compose files remain inputs to Harbor, never translated or rewritten.
Only an isolated one-shot runtime service, its project-local volumes, image
mirrors and main's root/setup environment are layered on top.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import shlex
import subprocess
import tomllib
import uuid

import yaml

from harbor.environments.base import ExecResult
from harbor.environments.docker.docker import DockerEnvironment, _sanitize_docker_compose_project_name
from harbor.models.task.config import EnvironmentConfig, TaskOS
from harbor.utils.env import resolve_embedded_env_vars, resolve_env_vars

from hbench.compose import map_image
from hbench.runtime_mount import RUNTIME_ROOT, runtime_copy_script, runtime_env

_RUNTIME_SERVICE = "hbench-runtime-init"
_RUNTIME_MOUNTS = (
    ("hbench-runtime", RUNTIME_ROOT, "/payload"),
    ("hbench-runtime-harness", "/opt/harness", "/aliases/harness"),
    ("hbench-runtime-pi-mcp-adapter", "/opt/pi-mcp-adapter", "/aliases/pi-mcp-adapter"),
    ("hbench-runtime-pi-mcp-adapter", "/tmp/harbor-pi-mcp-adapter", "/aliases/pi-mcp-adapter"),
    ("hbench-runtime-nvm", "/root/.nvm", "/aliases/nvm"),
)
_RESERVED_VOLUMES = {name for name, _, _ in _RUNTIME_MOUNTS}
_RESERVED_PATHS = [target for _, target, _ in _RUNTIME_MOUNTS] + ["/root/.local/bin/uv", "/root/.local/bin/uvx"]


def _user_key(user: str | int | None) -> str:
    key = str(user if user is not None else "root").split(":", 1)[0]
    return "root" if key.isdecimal() and int(key) == 0 else key


def _home(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("/") or "\n" in value or "\x00" in value:
        raise ValueError("Agent HOME must be an absolute single-line path")
    return posixpath.normpath(value)


def _check_mount(source: str | None, target: str, reserved_names: set[str]) -> None:
    if source in reserved_names:
        raise ValueError(f"Reserved runtime volume: {source}")
    if not isinstance(target, str) or not target.startswith("/") or "$" in target:
        raise ValueError("Runtime mount validation requires a resolved absolute target")
    path = PurePosixPath(posixpath.normpath(target))
    if any(path.is_relative_to(reserved) or PurePosixPath(reserved).is_relative_to(path)
           for reserved in _RESERVED_PATHS):
        raise ValueError(f"Reserved runtime path conflicts with mount: {target}")


class FrozenDockerEnvironment(DockerEnvironment):
    def __init__(self, environment_dir: Path, environment_name: str, session_id: str,
                 trial_paths, task_env_config: EnvironmentConfig, *args,
                 harness_runtime_image: str,
                 image_map: dict[str, str] | str | Path | None = None, **kwargs):
        if not isinstance(harness_runtime_image, str) or not harness_runtime_image.strip() or "$" in harness_runtime_image:
            raise ValueError("harness_runtime_image must be an already-built image reference")
        if task_env_config.os != TaskOS.LINUX:
            raise ValueError("Frozen Docker runtime requires Linux/glibc task images")
        if image_map is not None and not isinstance(image_map, dict):
            image_map = json.loads(Path(image_map).expanduser().read_text())
        self.image_map = deepcopy(image_map) if image_map is not None else {}
        if not isinstance(self.image_map, dict) or any(
            not isinstance(k, str) or not k or not isinstance(v, str) or not v
            for k, v in self.image_map.items()
        ):
            raise ValueError("image_map must map nonempty image prefixes to nonempty prefixes")
        self.harness_runtime_image = map_image(harness_runtime_image, self.image_map)
        self._runtime_compose_path: Path | None = None
        self._user_homes: dict[str, str] = {"root": "/root"}
        self._agent_user: str | int | None = None
        self._main_home: str | None = None
        config = deepcopy(task_env_config)
        if config.docker_image:
            config.docker_image = map_image(config.docker_image, self.image_map)
        kwargs["persistent_env"] = resolve_env_vars(deepcopy(kwargs.get("persistent_env") or {}))
        # Docker's native constructor defaults to public; honor the task baseline
        # when the runner did not supply a role-specific policy positionally.
        if len(args) < 2 and kwargs.get("network_policy") is None:
            kwargs["network_policy"] = config.resolve_baseline()
        super().__init__(environment_dir, environment_name, session_id, trial_paths, config, *args, **kwargs)
        # Keep creation/ENTRYPOINT PATH native, including on a later restart.
        self._startup_path = self._persistent_env.get("PATH")
        self._persistent_env["JAVA_HOME"] = RUNTIME_ROOT + "/java"

    @property
    def _environment_docker_compose_path(self) -> Path:
        path = super()._environment_docker_compose_path
        alternate = self.environment_dir / "docker-compose.yml"
        return alternate if not path.exists() and alternate.exists() else path

    def _validate_definition(self):
        if self._environment_docker_compose_path.is_file():
            return
        super()._validate_definition()

    @property
    def _docker_compose_paths(self) -> list[Path]:
        paths = super()._docker_compose_paths
        if self._runtime_compose_path is not None:
            paths.append(self._runtime_compose_path)
        return paths

    @property
    def _is_main_task_environment(self) -> bool:
        return self.environment_dir.name == "environment" and "__verifier__" not in self.session_id

    def _setup_agent_user(self) -> str | int | None:
        user = self.default_user
        path = self.environment_dir.parent / "task.toml"
        if user is None and self._is_main_task_environment and path.is_file():
            agent = tomllib.loads(path.read_text()).get("agent", {})
            if not isinstance(agent, dict):
                raise ValueError("task.toml agent must be a table")
            user = agent.get("user")
        if user is not None and (isinstance(user, bool) or not isinstance(user, (str, int))
                                 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*|[0-9]+(?::[0-9]+)?", str(user))):
            raise ValueError("task agent.user must be a username or nonnegative UID")
        return user

    def _workload_entrypoint(self) -> bool:
        if not self._is_main_task_environment:
            return False
        path = self.environment_dir.parent / "hbench.json"
        if not path.is_file():
            return False
        document = json.loads(path.read_text())
        if not isinstance(document, dict):
            raise ValueError("hbench.json must be an object")
        entrypoint = document.get("main_entrypoint", [])
        if not isinstance(entrypoint, list) or not all(isinstance(item, str) for item in entrypoint):
            raise ValueError("hbench.json main_entrypoint must be a string list")
        return bool(entrypoint)

    def _task_image_overrides(self) -> tuple[dict, bool]:
        """Inspect ordinary YAML only; Compose still owns merging and execution.

        Includes/extends and volumes_from can hide reserved resource collisions;
        reject those rather than attempting our own Compose resolver.
        """
        services = {}
        explicit_command = False
        variables = self._compose_env_vars()
        expand = lambda value: resolve_embedded_env_vars(value, variables.get)
        project = _sanitize_docker_compose_project_name(self.session_id)
        reserved = _RESERVED_VOLUMES | {f"{project}_{name}" for name in _RESERVED_VOLUMES}
        for mount in self._host_mounts:
            _check_mount(mount.get("source"), mount["target"], reserved)
        for path in self._user_compose_paths():
            document = yaml.safe_load(path.read_text())
            if not isinstance(document, dict):
                raise ValueError(f"Compose file must be an ordinary YAML mapping: {path}")
            if document.get("include"):
                raise ValueError("Compose include is unsupported by reserved runtime validation")
            volumes = document.get("volumes") or {}
            definitions = document.get("services") or {}
            if not isinstance(volumes, dict) or not isinstance(definitions, dict):
                raise ValueError("Compose services/volumes must be mappings")
            for name, spec in volumes.items():
                if expand(str(name)) in reserved or (isinstance(spec, dict) and (
                    expand(str(spec.get("name", ""))) in reserved or
                    isinstance(spec.get("external"), dict) and expand(str(spec["external"].get("name", ""))) in reserved
                )):
                    raise ValueError(f"Reserved runtime volume declaration: {name}")
            for name, spec in definitions.items():
                if name == _RUNTIME_SERVICE:
                    raise ValueError(f"Reserved runtime service: {name}")
                if not isinstance(name, str) or not isinstance(spec, dict):
                    raise ValueError("Compose service definitions must be mappings")
                if spec.get("extends") or spec.get("volumes_from"):
                    raise ValueError("Compose extends/volumes_from is unsupported by reserved runtime validation")
                if _RUNTIME_SERVICE in (spec.get("depends_on") or {}):
                    raise ValueError("Reserved runtime service dependency")
                if name == "main" and "command" in spec:
                    explicit_command = True
                tmpfs = spec.get("tmpfs") or []
                for target in ([tmpfs] if isinstance(tmpfs, str) else tmpfs):
                    _check_mount(None, expand(target).split(":", 1)[0], reserved)
                for kind in ("configs", "secrets"):
                    for mount in spec.get(kind) or []:
                        if isinstance(mount, dict) and "target" in mount:
                            target = expand(str(mount["target"]))
                            _check_mount(None, target if target.startswith("/") else "/" + target, reserved)
                for mount in spec.get("volumes") or []:
                    if isinstance(mount, str):
                        parts = expand(mount).split(":")
                        source, target = (None, parts[0]) if len(parts) == 1 else parts[:2]
                    elif isinstance(mount, dict):
                        source = expand(str(mount.get("source", "")))
                        target = expand(str(mount.get("target", "")))
                    else:
                        raise ValueError("Compose volume must use short or long syntax")
                    _check_mount(source, target, reserved)
                if self.image_map and "image" in spec:
                    if not isinstance(spec["image"], str):
                        raise ValueError("Compose service image must be a string")
                    image = expand(spec["image"])
                    if "$" in image:
                        raise ValueError("Image mapping requires a resolved image reference")
                    # Record the last declared image even if unmapped: it can
                    # override a mapped image in an earlier task/extra file.
                    services[name] = {"image": map_image(image, self.image_map)}
        return services, explicit_command

    def _prepare_runtime_overlay(self) -> dict:
        services, explicit_command = self._task_image_overrides()
        main = services.setdefault("main", {})
        main.update({"user": "0", "depends_on": {
            _RUNTIME_SERVICE: {"condition": "service_completed_successfully"}},
            "volumes": [{"type": "volume", "source": name, "target": target,
                         "read_only": True, "volume": {"nocopy": True}}
                        for name, target, _ in _RUNTIME_MOUNTS]})
        if self._workload_entrypoint() and not explicit_command:
            main["command"] = None
        destinations = dict.fromkeys(destination for _, _, destination in _RUNTIME_MOUNTS)
        # Retained project volumes can be reused: clear only our dedicated
        # nocopy volumes before the unchanged shared copy script runs again.
        script = "set -eu\n"
        for destination in destinations:
            script += f"rm -rf {destination}/* {destination}/.[!.]* {destination}/..?*\n"
        script += runtime_copy_script()
        for payload, alias in (("harness", "harness"), ("pi-mcp-adapter", "pi-mcp-adapter"), ("nvm", "nvm")):
            script += f"cp -a /payload/{payload}/. /aliases/{alias}/\n"
        script += "chmod -R a+rX /payload /aliases/harness /aliases/pi-mcp-adapter /aliases/nvm\n"
        services[_RUNTIME_SERVICE] = {
            "image": self.harness_runtime_image, "entrypoint": ["sh", "-ec"],
            # Escape Compose interpolation, including shell $HOME etc.
            "command": [script.replace("$", "$$")], "user": "0", "environment": {},
            "network_mode": "none", "restart": "no",
            "volumes": [{"type": "volume", "source": name, "target": destination, "volume": {"nocopy": True}}
                        for name, destination in dict((name, dest) for name, _, dest in _RUNTIME_MOUNTS).items()],
        }
        return {"services": services, "volumes": {name: {} for name in sorted(_RESERVED_VOLUMES)}}

    def _check_host_gpu_types(self) -> None:
        required = self.task_env_config.gpu_types
        if not required or self._effective_gpus <= 0:
            return
        if any(not item.strip() for item in required):
            raise ValueError("gpu_types must contain nonempty GPU names")
        try:
            result = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                                    check=True, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError("Cannot validate gpu_types using host nvidia-smi; refusing GPU trial") from error
        visible = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        patterns = [re.compile(r"(?<![a-z0-9])" + re.escape(item.strip()) + r"(?![a-z0-9])", re.I)
                    for item in required]
        if len(visible) < self._effective_gpus or any(not any(pattern.search(name) for pattern in patterns) for name in visible):
            raise RuntimeError(f"Host GPUs {visible!r} do not satisfy count={self._effective_gpus}, gpu_types={required!r}; mixed/wrong GPU hosts are refused")

    async def _capture_docker_compose_command(self, command: list[str], *, timeout_sec: int) -> ExecResult:
        """Capture read-only probes without mixing Compose warnings into data."""
        runtime = type(self).runtime()
        full_command = [*runtime.compose, "--project-name", _sanitize_docker_compose_project_name(self.session_id)]
        if runtime.supports_compose_project_directory:
            full_command.extend(["--project-directory", str(self.environment_dir.resolve().absolute())])
        for path in self._docker_compose_paths:
            full_command.extend(["-f", str(path.resolve().absolute())])
        full_command.extend(command)
        process = await asyncio.create_subprocess_exec(
            *full_command, env=self._compose_env_vars(include_os_env=True),
            cwd=str(self.environment_dir.resolve().absolute()) if self.environment_dir.is_dir() else None,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            # Native buffering already terminates on timeout; no output callback,
            # logging or diagnostic containing the config/credential-bearing stderr.
            return await self._collect_buffered_output(process, timeout_sec=timeout_sec)
        except asyncio.CancelledError:
            try:
                await self._terminate_process(process)
            except Exception:
                # Cleanup is best effort; cancellation must remain authoritative.
                pass
            raise

    async def _capture_main_environment(self) -> None:
        # Bypass exec's persistent/scoped env and output callbacks. Compose
        # owns the effective config (env_file, interpolation and all overlays).
        try:
            result = await self._capture_docker_compose_command(
                ["exec", "-T", "-u", "root", "main", "sh", "-c", 'printf "%s" "$PATH"'],
                timeout_sec=10)
            path = result.stdout
            if result.return_code or not isinstance(path, str) or any(char in path for char in "\n\r\x00"):
                raise ValueError("Invalid native PATH probe")
        except Exception:
            raise RuntimeError("Cannot probe native Docker main PATH") from None
        try:
            result = await self._capture_docker_compose_command(
                ["config", "--format", "json"], timeout_sec=30)
            if result.return_code:
                raise ValueError("Compose config failed")
            main = json.loads(result.stdout)["services"]["main"]
            environment = main.get("environment", {})
            if not isinstance(environment, dict):
                raise ValueError("Invalid main environment")
            home = _home(environment["HOME"]) if "HOME" in environment else None
        except Exception:
            # Never expose the full config or credentials in native diagnostics.
            raise RuntimeError("Cannot resolve effective Docker main HOME") from None
        self._main_home = home
        self._persistent_env.update(runtime_env({"PATH": path}))

    def _declared_agent_home(self) -> str | None:
        # Explicit task/trial HOME remains authoritative; image HOME is never
        # treated as an agent declaration, especially for non-root agents.
        return self._startup_env().get("HOME", self._main_home)

    async def _home_for_user(self, user: str | int | None) -> str:
        key = _user_key(user)
        if key not in self._user_homes:
            try:
                result = await self._capture_docker_compose_command(
                    ["exec", "-T", "-u", "root", "-w", "/", "main", "sh", "-c", "getent passwd " + shlex.quote(key)],
                    timeout_sec=10)
                fields = (result.stdout or "").strip().split(":")
                if result.return_code or len(fields) != 7:
                    raise ValueError("Invalid passwd lookup")
                home = _home(fields[5])
            except Exception:
                raise RuntimeError(f"Cannot resolve HOME for Docker exec user {user!r}") from None
            self._user_homes[key] = home
            self._user_homes[fields[0]] = home
            self._user_homes[fields[2]] = home
        return self._user_homes[key]

    async def exec(self, command: str, cwd: str | None = None, env: dict[str, str] | None = None,
                   timeout_sec: int | None = None, user: str | int | None = None) -> ExecResult:
        # Docker -u leaves the image HOME intact. Only add an exec env hook;
        # Harbor retains its shell, user, callbacks, timeout and transport.
        resolved = self._resolve_user(user)
        explicit_home = env is not None and "HOME" in env or any("HOME" in scope for scope in self._exec_env_overlays.get())
        overlay = dict(env or {})
        if not explicit_home:
            task_home = self._declared_agent_home()
            use_task_home = user is None and _user_key(resolved) == _user_key(self._agent_user)
            overlay["HOME"] = _home(task_home) if use_task_home and task_home is not None else await self._home_for_user(resolved)
        return await super().exec(command, cwd=cwd, env=overlay, timeout_sec=timeout_sec, user=user)

    async def _setup_runtime(self) -> None:
        home = self._declared_agent_home()
        home = _home(home) if home is not None else await self._home_for_user(self._agent_user)
        user = shlex.quote(_user_key(self._agent_user))
        script = f"""set -eu
mkdir -p /root/.local/bin
ln -sfn {RUNTIME_ROOT}/bin/uv /root/.local/bin/uv
ln -sfn {RUNTIME_ROOT}/bin/uvx /root/.local/bin/uvx
test -x /root/.local/bin/uv
test -x /root/.local/bin/uvx
agent_home={shlex.quote(home)}
mkdir -p "$agent_home"
chown {user} "$agent_home"
chmod u+w "$agent_home"
if [ ! -d "$agent_home/.nvm" ]; then mkdir -p "$agent_home/.nvm"; fi
if [ ! -e "$agent_home/.nvm/nvm.sh" ]; then touch "$agent_home/.nvm/nvm.sh"; fi
if [ "$agent_home/.nvm" != /root/.nvm ]; then chown -R {user} "$agent_home/.nvm"; fi
test -r "$agent_home/.nvm/nvm.sh"
mkdir -p /logs/agent /logs/verifier /logs/artifacts /logs/user-agent
chmod 777 /logs/agent /logs/verifier /logs/artifacts /logs/user-agent
"""
        # The script itself is trivial; a loaded daemon can still queue an exec well
        # past 60s, so keep this generous instead of failing the trial at startup.
        result = await self.exec(script, cwd="/", env={"HOME": "/root"}, user="root", timeout_sec=300)
        if result.return_code:
            raise RuntimeError(f"Frozen Docker runtime setup failed ({result.return_code}): {result.stderr or result.stdout}")
        check = 'test -w "$HOME" && test -r "$HOME/.nvm/nvm.sh"'
        check += " && " + " && ".join(f"test -w /logs/{name}" for name in ("agent", "verifier", "artifacts", "user-agent"))
        result = await self.exec(check, cwd="/", env={"HOME": home}, user=self._agent_user or "root", timeout_sec=10)
        if result.return_code:
            raise RuntimeError("Frozen Docker agent HOME/logs are not usable by task agent.user")

    async def start(self, force_build: bool = False):
        if self._startup_path is None:
            self._persistent_env.pop("PATH", None)
        else:
            self._persistent_env["PATH"] = self._startup_path
        self._main_home = None
        self._agent_user = self._setup_agent_user()
        overlay = self._prepare_runtime_overlay()
        await asyncio.to_thread(self._check_host_gpu_types)
        # A session-labelled, unique file in trial output; never task inputs.
        path = self.trial_paths.trial_dir / f"hbench-runtime-{_sanitize_docker_compose_project_name(self.session_id)}-{uuid.uuid4().hex}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x") as output:
            json.dump(overlay, output, indent=2)
            output.write("\n")
        previous = self._runtime_compose_path
        self._runtime_compose_path = path
        if previous is not None:
            previous.unlink(missing_ok=True)
        # Keep the overlay on failure/cancellation: the runner's native stop
        # must still be able to delete the exact services/volumes it started.
        await super().start(force_build=force_build)
        await self._capture_main_environment()
        await self._setup_runtime()

    async def stop(self, delete: bool):
        # Rejected task inputs/GPU preflight created no Compose resources. In
        # particular, never run native down against an unsafe reserved-volume
        # declaration after validation failed before an overlay was produced.
        if self._runtime_compose_path is not None:
            await super().stop(delete=delete)
