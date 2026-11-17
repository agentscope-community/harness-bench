"""Offline tests: only Compose config may reach the Docker CLI, never its daemon."""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import yaml

from harbor.environments.base import ExecResult
from harbor.environments.docker.docker import DockerEnvironment, _sanitize_docker_compose_project_name
from harbor.models.task.config import EnvironmentConfig, NetworkPolicy
from harbor.models.trial.paths import TrialPaths

from hbench.docker_environment import FrozenDockerEnvironment
from hbench.runtime_mount import RUNTIME_ROOT, add_setup_gate, runtime_copy_script, runtime_overrides

ARTIFACTS = Path(__file__).resolve().parents[2] / "artifacts"
REAL_RUN = subprocess.run
REAL_CREATE_SUBPROCESS_EXEC = asyncio.create_subprocess_exec
PATH_PROBE = ["exec", "-T", "-u", "root", "main", "sh", "-c", 'printf "%s" "$PATH"']
CONFIG_PROBE = ["config", "--format", "json"]


def getent_probe(key):
    return ["exec", "-T", "-u", "root", "-w", "/", "main", "sh", "-c", "getent passwd " + shlex.quote(key)]


def local_shell_only(command, *args, **kwargs):
    if command[0] not in ("sh", "bash"):
        raise AssertionError(f"Offline test tried a host command: {command}")
    return REAL_RUN(command, *args, **kwargs)


class DockerFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="docker-env-test-", dir=ARTIFACTS)
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        # Even native Harbor's generated overrides stay under ignored artifacts.
        self.patch(patch.object(tempfile, "tempdir", str(self.root)))
        self.patch(patch("subprocess.run", side_effect=local_shell_only))
        self.patch(patch("asyncio.create_subprocess_exec", side_effect=AssertionError("No subprocesses in offline Docker tests")))
        self.patch(patch.object(DockerEnvironment, "_egress_control_kernel_support", return_value=True))
        self.patch(patch.object(DockerEnvironment, "_validate_daemon_mode"))
        self.patch(patch.object(DockerEnvironment, "_validate_image_os", AsyncMock()))
        self.patch(patch.object(DockerEnvironment, "_ensure_egress_control_sidecar_image_built", AsyncMock()))
        self.environment = self.root / "environment"
        self.environment.mkdir()

    def patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def make_env(self, config=None, **kwargs):
        env = FrozenDockerEnvironment(
            kwargs.pop("environment_dir", self.environment), "task", kwargs.pop("session_id", "trial-one__env"),
            TrialPaths(self.root / "trial"), config or EnvironmentConfig(docker_image="hbench/task:1"),
            harness_runtime_image=kwargs.pop("harness_runtime_image", "hbench/runtime:1"), **kwargs)
        for name in ("_cleanup_mounts_compose_file", "_cleanup_resources_compose_file", "_cleanup_env_compose_file",
                     "_cleanup_egress_control_services_compose_file"):
            self.addCleanup(getattr(env, name))
        return env

    def compose_file(self, document, name="docker-compose.yaml"):
        path = self.environment / name
        path.write_text(json.dumps(document))
        return path

    def metadata(self, value):
        (self.root / "hbench.json").write_text(json.dumps(value))

    def task_user(self, user):
        (self.root / "task.toml").write_text("[agent]\nuser = " + json.dumps(user) + "\n")

    def mock_engine(self, env, *, main_environment=None, image_path="/usr/local/bin:/usr/bin:/bin"):
        # main_environment represents Compose's resolved task/extra/env_file
        # input; Harbor's actual startup override is merged last, not exec env.
        def effective_environment():
            environment = dict(main_environment or {})
            if env._env_compose_path is not None:
                document = yaml.safe_load(env._env_compose_path.read_text())
                environment.update(document["services"]["main"].get("environment", {}))
            return environment

        async def capture(argv, **kwargs):
            if argv == CONFIG_PROBE:
                return ExecResult(return_code=0, stdout=json.dumps({"services": {
                    "main": {"image": "task:1", "environment": effective_environment()},
                    "db": {"image": "db:1", "environment": {"TOKEN": "config-secret-sentinel"}}}}),
                    stderr="Compose warning: config-secret-sentinel")
            if argv == PATH_PROBE:
                return ExecResult(return_code=0, stdout=effective_environment().get("PATH", image_path),
                                  stderr="Compose warning: config-secret-sentinel")
            if argv in (getent_probe("agent"), getent_probe("1000")):
                return ExecResult(return_code=0, stdout="agent:x:1000:1000::/home/agent:/bin/bash\n",
                                  stderr='Compose warning: version is obsolete; "OPTIONAL" is not set; config-secret-sentinel')
            raise AssertionError(f"Unexpected read-only Compose probe: {argv}")

        async def command(argv, **kwargs):
            if argv in (CONFIG_PROBE, PATH_PROBE) or "getent passwd" in argv[-1]:
                raise AssertionError("Read-only probes must not use native merged output")
            if argv[0] not in ("exec", "up", "down", "cp", "stop", "build"):
                raise AssertionError(f"Unexpected native Compose command: {argv}")
            return ExecResult(return_code=0)
        env._capture_docker_compose_command = AsyncMock(side_effect=capture)
        env._run_docker_compose_command = AsyncMock(side_effect=command)
        env.prepare_logs_for_host = AsyncMock()
        return env._run_docker_compose_command


class DockerTests(DockerFixture, unittest.TestCase):
    def test_native_methods_are_not_reimplemented(self):
        self.assertTrue(issubclass(FrozenDockerEnvironment, DockerEnvironment))
        for name in ("service_exec", "upload_file", "upload_dir", "download_file", "download_dir",
                     "service_download_file", "service_download_dir", "stop_service", "_compose_exec",
                     "_apply_network_policy", "ensure_dirs", "_run_docker_compose_command"):
            self.assertIs(getattr(FrozenDockerEnvironment, name), getattr(DockerEnvironment, name), name)

    def test_runtime_overlay_dependency_aliases_readonly_and_project_volumes(self):
        env = self.make_env()
        overlay = env._prepare_runtime_overlay()
        init = overlay["services"]["hbench-runtime-init"]
        main = overlay["services"]["main"]
        self.assertEqual(init["image"], "hbench/runtime:1")
        self.assertEqual(init["entrypoint"], ["sh", "-ec"])
        self.assertEqual(init["user"], "0")
        self.assertEqual(init["network_mode"], "none")
        self.assertEqual(init["restart"], "no")
        self.assertEqual(init["environment"], {})
        self.assertEqual(main["user"], "0")
        self.assertEqual(main["depends_on"], {"hbench-runtime-init": {"condition": "service_completed_successfully"}})
        self.assertNotIn("entrypoint", main)
        self.assertNotIn("command", main)
        targets = {mount["target"]: mount for mount in main["volumes"]}
        self.assertEqual(set(targets), {RUNTIME_ROOT, "/opt/harness", "/opt/pi-mcp-adapter",
                                        "/tmp/harbor-pi-mcp-adapter", "/root/.nvm"})
        self.assertEqual(targets["/opt/pi-mcp-adapter"]["source"], targets["/tmp/harbor-pi-mcp-adapter"]["source"])
        for mount in main["volumes"]:
            self.assertTrue(mount["read_only"])
            self.assertEqual(mount["volume"], {"nocopy": True})
            self.assertNotIn("subpath", mount["volume"])
        self.assertEqual(len(init["volumes"]), 4)
        for spec in overlay["volumes"].values():
            self.assertEqual(spec, {})  # no external/name, hence no cross-trial sharing
        other = self.make_env(session_id="trial-two__env")
        qualified = lambda e: {f"{_sanitize_docker_compose_project_name(e.session_id)}_{name}" for name in overlay["volumes"]}
        self.assertFalse(qualified(env) & qualified(other))
        self.assertIn(runtime_copy_script(), init["command"][0])
        self.assertIn("cp -a /payload/pi-mcp-adapter/. /aliases/pi-mcp-adapter/", init["command"][0])
        self.assertNotIn("install", init["command"][0])
        self.assertNotIn("curl", init["command"][0])

    def test_only_declared_service_images_mapped_and_original_semantics_unchanged(self):
        original = {"services": {"main": {"image": "hbench/main:1", "entrypoint": ["/app/start"],
                     "command": ["serve"], "volumes": ["cache:/app/cache"], "networks": ["backend"],
                     "depends_on": {"db": {"condition": "service_healthy"}}},
                     "db": {"image": "docker.io/db@sha256:abc", "command": ["db"], "user": "999"},
                     "built": {"build": "./built"}}, "volumes": {"cache": {}}, "networks": {"backend": {}}}
        task = self.compose_file(original)
        extra = self.root / "extra.yaml"
        extra.write_text('services:\n  db:\n    image: local/db:2\n  sidecar:\n    image: hbench/side:2\n')
        inputs = [task.read_bytes(), extra.read_bytes()]
        config = EnvironmentConfig(docker_image="hbench/task:1", env={"PATH": "/custom:/bin"})
        saved = deepcopy(config)
        env = self.make_env(config, image_map={"hbench/": "registry/team/", "docker.io/": "mirror/"},
                            extra_docker_compose=[extra], override_cpus=2)
        overlay = env._prepare_runtime_overlay()
        self.assertEqual(env.task_env_config.docker_image, "registry/team/task:1")
        self.assertEqual(env._env_vars.prebuilt_image_name, "registry/team/task:1")
        self.assertEqual(overlay["services"]["hbench-runtime-init"]["image"], "registry/team/runtime:1")
        self.assertEqual(overlay["services"]["main"]["image"], "registry/team/main:1")
        self.assertEqual(overlay["services"]["db"], {"image": "local/db:2"})
        self.assertEqual(overlay["services"]["sidecar"], {"image": "registry/team/side:2"})
        self.assertNotIn("built", overlay["services"])
        for key in ("command", "entrypoint", "networks"):
            self.assertNotIn(key, overlay["services"]["main"])
        self.assertNotIn("backend", overlay.get("networks", {}))
        self.assertEqual([task.read_bytes(), extra.read_bytes()], inputs)
        self.assertEqual(config, saved)

    def test_image_map_file_and_invalid_types(self):
        path = self.root / "map.json"
        path.write_text('{"hbench/":"mirror/"}')
        self.assertEqual(self.make_env(image_map=path).harness_runtime_image, "mirror/runtime:1")
        for mapping in (["wrong"], {"": "bad"}, {"hbench/": 1}):
            path.write_text(json.dumps(mapping))
            with self.subTest(mapping=mapping), self.assertRaises(ValueError):
                self.make_env(image_map=path)
        for image in (None, "", "${RUNTIME}"):
            with self.subTest(image=image), self.assertRaises(ValueError):
                self.make_env(harness_runtime_image=image)
        with self.assertRaisesRegex(ValueError, "Linux"):
            self.make_env(EnvironmentConfig(docker_image="task", os="windows"))

    def test_yml_definition_native_ordering_and_no_task_rewrite(self):
        task = self.compose_file({"services": {"main": {"image": "task"}}}, name="docker-compose.yml")
        env = self.make_env(EnvironmentConfig())
        self.assertEqual(env._environment_docker_compose_path, task)
        self.assertTrue(env._uses_compose)
        for attr, name in (("_resources_compose_path", "resource"), ("_env_compose_path", "env"),
                           ("_mounts_compose_path", "mount"), ("_stream_compose_path", "stream")):
            setattr(env, attr, self.root / name)
        env._runtime_compose_path = self.root / "runtime"
        self.assertEqual(env._docker_compose_paths, [self.root / "resource", env._DOCKER_COMPOSE_BUILD_PATH,
                         task, self.root / "env", self.root / "mount", self.root / "stream", self.root / "runtime"])
        canonical = self.compose_file({"services": {"main": {}}})
        self.assertEqual(env._environment_docker_compose_path, canonical)

    def test_workload_cmd_null_only_when_metadata_and_no_explicit_command(self):
        for entrypoint in ([], ["/app/workload"]):
            self.metadata({"main_entrypoint": entrypoint})
            for command in ("absent", None, [], ["serve"]):
                for in_extra in (False, True):
                    with self.subTest(entrypoint=entrypoint, command=command, extra=in_extra):
                        spec = {"entrypoint": ["/task/entrypoint"]}
                        if command != "absent":
                            spec["command"] = command
                        self.compose_file({"services": {"main": {} if in_extra else spec}})
                        extra = self.root / "cmd-extra.yaml"
                        extra.write_text(json.dumps({"services": {"main": spec}}))
                        env = self.make_env(extra_docker_compose=[extra] if in_extra else [])
                        main = env._prepare_runtime_overlay()["services"]["main"]
                        self.assertEqual("command" in main, bool(entrypoint) and command == "absent")
                        if "command" in main:
                            self.assertIsNone(main["command"])
                        self.assertNotIn("entrypoint", main)
        self.metadata({"main_entrypoint": ["serve"]})
        for kwargs in ({"session_id": "trial__verifier__test"}, {"environment_dir": self.root / "tests"}):
            self.assertNotIn("command", self.make_env(**kwargs)._prepare_runtime_overlay()["services"]["main"])

    def test_metadata_and_task_user_strict_types(self):
        for value in ([], None, {"main_entrypoint": None}, {"main_entrypoint": "serve"}, {"main_entrypoint": [1]}):
            self.metadata(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.make_env()._prepare_runtime_overlay()
        for user in (True, -1, "agent; bad"):
            self.task_user(user)
            with self.subTest(user=user), self.assertRaises(ValueError):
                self.make_env()._setup_agent_user()
        self.task_user(1000)
        env = self.make_env()
        self.assertIsNone(env.default_user)
        self.assertEqual(env._setup_agent_user(), 1000)
        self.assertIsNone(self.make_env(session_id="trial__verifier__test")._setup_agent_user())

    def test_reserved_service_volumes_mounts_and_aliases_refused(self):
        bad_documents = [
            {"services": {"hbench-runtime-init": {"image": "bad"}}},
            {"volumes": {"hbench-runtime": {"external": True}}},
            {"volumes": {"other": {"name": "trial-one__env_hbench-runtime"}}},
            {"volumes": {"other": {"external": {"name": "hbench-runtime-harness"}}}},
            {"services": {"main": {"depends_on": ["hbench-runtime-init"]}}},
            {"services": {"main": {"tmpfs": "/opt/harness:rw"}}},
            {"services": {"main": {"configs": [{"source": "c", "target": "/root/.nvm/nvm.sh"}]}}},
            {"include": ["other.yaml"]},
            {"services": {"main": {"extends": {"file": "base.yaml", "service": "main"}}}},
            {"services": {"main": {"volumes_from": ["other"]}}},
        ]
        for target in (RUNTIME_ROOT, RUNTIME_ROOT + "/bin", "/opt", "/root", "/root/.local/bin/uv",
                       "/opt/harness", "/opt/pi-mcp-adapter", "/tmp/harbor-pi-mcp-adapter", "/root/.nvm", "/opt/other/../harness"):
            bad_documents.append({"services": {"main": {"volumes": ["other:" + target]}}})
        bad_documents += [{"services": {"db": {"volumes": ["hbench-runtime:/data"]}}},
                          {"services": {"main": {"volumes": [{"type": "bind", "source": "/data", "target": "/opt/harness"}]}}}]
        for document in bad_documents:
            with self.subTest(document=document):
                self.compose_file(document)
                with self.assertRaises(ValueError):
                    self.make_env()._prepare_runtime_overlay()
                self.compose_file({"services": {"main": {}}})
                extra = self.root / "reserved-extra.yaml"
                extra.write_text(json.dumps(document))
                with self.assertRaises(ValueError):
                    self.make_env(extra_docker_compose=[extra])._prepare_runtime_overlay()

    def test_explicit_host_and_interpolated_mount_collisions_refused(self):
        for target in ("/opt/harness", "/root/.nvm", "/opt/hbench-runtime/bin"):
            env = self.make_env(mounts=[{"type": "bind", "source": str(self.root), "target": target}])
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "Reserved"):
                env._prepare_runtime_overlay()
        self.compose_file({"services": {"main": {"volumes": ["data:${TARGET}"]}}})
        with self.assertRaisesRegex(ValueError, "Reserved"):
            self.make_env(persistent_env={"TARGET": "/opt/harness"})._prepare_runtime_overlay()

    def test_gpu_cpu_no_query_type_count_boundary_and_host_failures(self):
        query = Mock(return_value=Mock(stdout="NVIDIA H100 80GB HBM3\nNVIDIA H100 PCIe\n"))
        with patch("hbench.docker_environment.subprocess.run", query):
            self.make_env()._check_host_gpu_types()
            self.make_env(EnvironmentConfig(docker_image="task", gpus=0, gpu_types=["H100"]))._check_host_gpu_types()
            self.make_env(EnvironmentConfig(docker_image="task", gpus=2))._check_host_gpu_types()
            query.assert_not_called()
            env = self.make_env(EnvironmentConfig(docker_image="task", gpus=1, gpu_types=["H100"]), override_gpus=2)
            env._check_host_gpu_types()
            self.assertEqual(query.call_args.args[0], ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
            self.assertTrue(query.call_args.kwargs["check"])
            for output in ("NVIDIA A100-SXM4-80GB\n", "NVIDIA H100\nNVIDIA A100\n", "NVIDIA H1000\nNVIDIA H100\n", "", "NVIDIA H100\n"):
                query.return_value.stdout = output
                with self.subTest(output=output), self.assertRaisesRegex(RuntimeError, "Host GPUs"):
                    env._check_host_gpu_types()
            for error in (FileNotFoundError(), subprocess.TimeoutExpired("nvidia-smi", 10), subprocess.CalledProcessError(1, "nvidia-smi")):
                query.side_effect = error
                with self.subTest(error=error), self.assertRaisesRegex(RuntimeError, "Cannot validate"):
                    env._check_host_gpu_types()
        with self.assertRaises(ValueError):
            self.make_env(EnvironmentConfig(docker_image="task", gpus=1, gpu_types=[" "]))._check_host_gpu_types()

    def test_kubernetes_shared_outputs_with_optional_extended_runtime(self):
        values = [runtime_overrides(image, main_image="task:1", startup_env={"PATH": "/custom:/bin", "SECRET": "offline"},
                                    main_keepalive=idle) for image in (None, "runtime:1") for idle in (False, True)]
        values += [add_setup_gate({"name": "main", "args": ["serve"]}, 30, agent_user=user, agent_home=home)
                   for user, home in ((None, None), ("agent", None), (1000, "/home/custom"))]
        self.assertEqual(hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest(),
                         "7c4f00c93fb0a6b379ce7de523757ee42cc195f1439516573f98520c9cdc5d6a")

    def test_init_copy_script_offline_alias_contents_and_idempotence(self):
        script = self.make_env()._prepare_runtime_overlay()["services"]["hbench-runtime-init"]["command"][0]
        source = self.root / "source"
        replacements = {
            "/opt/harness": source / "harness", "/opt/java/openjdk": source / "java",
            "/usr/local/bin": source / "bin", "/usr/local/lib/node_modules": source / "node_modules",
            "/opt/pi-mcp-adapter": source / "pi-mcp-adapter", "/root/.nvm": source / "nvm",
            "/payload": self.root / "payload", "/aliases": self.root / "aliases"}
        files = ["harness/classes/Runner.class", "java/bin/java", "bin/node", "bin/uv", "bin/uvx",
                 "node_modules/npm/bin/npm-cli.js", "node_modules/npm/bin/npx-cli.js",
                 "node_modules/@openai/codex/bin/codex.js",
                 "node_modules/@earendil-works/pi-coding-agent/dist/cli.js",
                 "pi-mcp-adapter/node_modules/pi-mcp-adapter/index.ts", "nvm/nvm.sh"]
        for name in files:
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("frozen\n")
            path.chmod(0o755)
        for destination in ("payload", "aliases/harness", "aliases/pi-mcp-adapter", "aliases/nvm"):
            (self.root / destination).mkdir(parents=True)
        for old, new in replacements.items():
            script = script.replace(old, shlex.quote(str(new)))
        for _ in range(2):
            result = subprocess.run(["sh", "-ec", script], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((self.root / "aliases/harness/classes/Runner.class").read_text(), "frozen\n")
            self.assertEqual((self.root / "aliases/pi-mcp-adapter/node_modules/pi-mcp-adapter/index.ts").read_text(), "frozen\n")
            self.assertEqual((self.root / "aliases/nvm/nvm.sh").read_text(), "frozen\n")
            self.assertEqual(os.readlink(self.root / "payload/bin/codex"), RUNTIME_ROOT + "/node_modules/@openai/codex/bin/codex.js")


class AsyncDockerTests(DockerFixture, unittest.IsolatedAsyncioTestCase):
    async def test_actual_offline_compose_merge_preserves_entrypoint_cmd_dependencies_and_mounts(self):
        # The Docker CLI alone is not enough: the Compose plugin may be absent (and
        # `docker compose ... --help` still exits 0), so probe the resolved command.
        runtime = FrozenDockerEnvironment.runtime()
        if not shutil.which(runtime.compose[0]):
            self.skipTest("Docker Compose CLI unavailable; config-only integration test")
        probe = REAL_RUN([*runtime.compose, "version"], capture_output=True, text=True)
        if probe.returncode:
            self.skipTest("Docker Compose CLI unavailable; config-only integration test")
        for workload, explicit_command in ((False, False), (True, False), (True, True)):
            with self.subTest(workload=workload, explicit_command=explicit_command):
                native_path = "/opt/task/bin:/opt/conda/bin:/opt/task/.venv/bin:/bin"
                secret = "config-secret-sentinel"
                optional = "HB_CAPTURE_OPTIONAL_UNSET"
                env_file = self.environment / "task.env"
                env_file.write_text("HOME=${HB_COMPOSE_HOME}\nPATH=" + native_path + "\nTOKEN=" + secret + "\n")
                main = {"image": "hbench/main:1", "entrypoint": ["/app/start"], "networks": ["backend"],
                        "env_file": [str(env_file)], "volumes": ["cache:/app/cache"],
                        "environment": {"OPTIONAL": "${" + optional + "}"},
                        "depends_on": {"db": {"condition": "service_healthy"}}}
                if explicit_command:
                    main["command"] = ["serve", "--arg"]
                task = self.compose_file({"version": "3.8", "services": {"main": main, "db": {"image": "hbench/db:1",
                                         "healthcheck": {"test": ["CMD", "true"]}, "networks": ["backend"]}},
                                         "volumes": {"cache": {}}, "networks": {"backend": {}}})
                original = task.read_bytes()
                self.metadata({"main_entrypoint": ["/app/start"] if workload else []})
                env = self.make_env(image_map={"hbench/": "mirror/team/"}, persistent_env={"SECRET": "offline-sentinel"})
                command = self.mock_engine(env)
                # Restore the real read-only runner. Lifecycle remains mocked;
                # config must traverse CLI -> asyncio -> native buffering -> capture.
                del env._capture_docker_compose_command
                results, invocations = [], []
                collect = env._collect_buffered_output
                async def collected(process, **kwargs):
                    result = await collect(process, **kwargs)
                    results.append(result)
                    return result

                async def config_or_local_path(*argv, **kwargs):
                    self.assertEqual(list(argv[:2]), list(type(env).runtime().compose))
                    self.assertEqual(kwargs["stdout"], asyncio.subprocess.PIPE)
                    self.assertEqual(kwargs["stderr"], asyncio.subprocess.PIPE)
                    self.assertEqual(kwargs["stdin"], asyncio.subprocess.DEVNULL)
                    invocations.append(argv)
                    if list(argv[-len(CONFIG_PROBE):]) == CONFIG_PROBE:
                        return await REAL_CREATE_SUBPROCESS_EXEC(*argv, **kwargs)
                    if list(argv[-len(PATH_PROBE):]) == PATH_PROBE:
                        # Only the process boundary is faked: a real local shell
                        # writes PATH to stdout and a warning to separate stderr.
                        kwargs["env"] = {**kwargs["env"], "PATH": native_path}
                        return await REAL_CREATE_SUBPROCESS_EXEC(
                            "/bin/sh", "-c", 'printf "%s" "$PATH"; printf "%s\\n" "warning: ' + secret + '" >&2', **kwargs)
                    raise AssertionError("Only config may reach the Docker CLI")

                callback, logger = Mock(), Mock()
                with patch.dict(os.environ, {"HB_COMPOSE_HOME": "/home/custom"}):
                    os.environ.pop(optional, None)
                    with patch("asyncio.create_subprocess_exec", side_effect=config_or_local_path), \
                         patch.object(env, "_collect_buffered_output", AsyncMock(side_effect=collected)) as buffered, \
                         patch.object(env, "logger", logger), env.scoped_output_callback(callback):
                        try:
                            await env.start()
                        except RuntimeError:
                            stderr = results[-1].stderr or "" if results else ""
                            if "is not a docker command" in stderr or "unknown command" in stderr:
                                self.skipTest("Docker Compose plugin unavailable")
                            raise
                self.assertEqual(buffered.await_count, 2)
                callback.assert_not_called()
                self.assertNotIn(secret, str(logger.mock_calls))
                self.assertEqual(len(invocations), 2)
                self.assertEqual(results[0].stdout, native_path)
                self.assertIn("warning", results[0].stderr)
                result = results[1]
                self.assertEqual(result.return_code, 0)
                self.assertIn("version", result.stderr)
                self.assertIn(optional, result.stderr)
                self.assertIn("not set", result.stderr)
                self.assertNotIn("warning", result.stdout)
                self.assertNotIn(secret, env._runtime_compose_path.read_text())
                self.assertNotIn("TOKEN", env._persistent_env)
                self.assertFalse(any(call.args[0] in (PATH_PROBE, CONFIG_PROBE) for call in command.await_args_list))
                merged = json.loads(result.stdout)
                service = merged["services"]["main"]
                self.assertEqual(service["entrypoint"], ["/app/start"])
                self.assertEqual(service["environment"]["PATH"], native_path)
                self.assertEqual(service["environment"]["HOME"], "/home/custom")
                self.assertEqual(env._main_home, service["environment"]["HOME"])
                self.assertTrue(env._persistent_env["PATH"].endswith(native_path))
                expected_cmd = ["serve", "--arg"] if explicit_command else None if workload else ["sh", "-c", "sleep infinity"]
                self.assertEqual(service.get("command"), expected_cmd)
                self.assertEqual(service["image"], "mirror/team/main:1")
                self.assertEqual(merged["services"]["db"]["image"], "mirror/team/db:1")
                self.assertEqual(set(service["depends_on"]), {"db", "hbench-runtime-init"})
                self.assertEqual(service["depends_on"]["db"]["condition"], "service_healthy")
                self.assertEqual(service["depends_on"]["hbench-runtime-init"]["condition"], "service_completed_successfully")
                self.assertIn("backend", service["networks"])
                mounts = {mount["target"]: mount for mount in service["volumes"]}
                self.assertEqual(mounts["/app/cache"]["source"], "cache")
                self.assertTrue(mounts[RUNTIME_ROOT]["read_only"])
                self.assertTrue(mounts[RUNTIME_ROOT]["volume"]["nocopy"])
                init = merged["services"]["hbench-runtime-init"]
                self.assertEqual(init["entrypoint"], ["sh", "-ec"])
                self.assertEqual(init["image"], "mirror/team/runtime:1")
                self.assertFalse(init.get("environment"))
                self.assertEqual(init["network_mode"], "none")
                project = _sanitize_docker_compose_project_name(env.session_id)
                self.assertEqual(merged["volumes"]["hbench-runtime"]["name"], project + "_hbench-runtime")
                self.assertEqual(task.read_bytes(), original)
                await env.stop(delete=True)

    async def test_capture_transport_matches_native_cli_env_and_keeps_streams_separate(self):
        for project_directory, directory_exists in ((True, True), (False, True), (True, False)):
            with self.subTest(project_directory=project_directory, directory_exists=directory_exists):
                task = self.compose_file({"services": {"main": {"image": "task:1"}}})
                extra = self.root / "extra.yaml"
                extra.write_text('services: {main: {environment: {HOME: /home/custom}}}')
                env = self.make_env(EnvironmentConfig(env={"TASK_ENV": "task-value"}),
                                    session_id="TRIAL ! @Capture", extra_docker_compose=[extra],
                                    persistent_env={"PERSISTENT": "persistent-value"})
                env._runtime_compose_path = self.root / "runtime.json"
                env._runtime_compose_path.write_text(json.dumps(env._prepare_runtime_overlay()))
                paths = env._docker_compose_paths
                self.assertIn(task, paths)
                self.assertIn(extra, paths)
                self.assertEqual(paths[-1], env._runtime_compose_path)
                if not directory_exists:
                    env.environment_dir = self.root / "missing"
                expected_env = env._compose_env_vars(include_os_env=True)
                runtime = Mock(compose=["test-engine", "compose-plugin"],
                               supports_compose_project_directory=project_directory)
                expected = [*runtime.compose, "--project-name", _sanitize_docker_compose_project_name(env.session_id)]
                if project_directory:
                    expected.extend(["--project-directory", str(env.environment_dir.resolve().absolute())])
                for path in paths:
                    expected.extend(["-f", str(path.resolve().absolute())])
                processes = []
                def process_boundary(*argv, **kwargs):
                    probe = PATH_PROBE if not processes else CONFIG_PROBE
                    self.assertEqual(list(argv), expected + probe)
                    self.assertEqual(kwargs, {"env": expected_env,
                        "cwd": str(env.environment_dir.resolve().absolute()) if directory_exists else None,
                        "stdin": asyncio.subprocess.DEVNULL, "stdout": asyncio.subprocess.PIPE,
                        "stderr": asyncio.subprocess.PIPE})
                    stdout = b"/opt/task/bin:/bin" if probe == PATH_PROBE else json.dumps({"services": {
                        "main": {"environment": {"HOME": "/home/custom", "TOKEN": "config-secret-sentinel"}}}}).encode()
                    process = Mock(returncode=0, communicate=AsyncMock(return_value=(stdout, b"warning: config-secret-sentinel")))
                    processes.append(process)
                    return process
                env._run_docker_compose_command = AsyncMock(side_effect=AssertionError("Native runner used for capture"))
                callback, logger = Mock(), Mock()
                with patch.object(FrozenDockerEnvironment, "runtime", return_value=runtime), \
                     patch.object(FrozenDockerEnvironment, "_docker_compose_paths", new=property(lambda _: paths)), \
                     patch("asyncio.create_subprocess_exec", side_effect=process_boundary), \
                     patch.object(env, "_collect_buffered_output", AsyncMock(wraps=env._collect_buffered_output)) as buffered, \
                     patch.object(env, "logger", logger), env.scoped_exec_env({"HOME": "/scope", "PATH": "/scope/bin"}), \
                     env.scoped_output_callback(callback):
                    await env._capture_main_environment()
                self.assertEqual(buffered.await_count, 2)
                for process in processes:
                    process.communicate.assert_awaited_once_with(input=None)
                env._run_docker_compose_command.assert_not_awaited()
                callback.assert_not_called()
                self.assertNotIn("config-secret-sentinel", str(logger.mock_calls))
                self.assertEqual(env._main_home, "/home/custom")
                self.assertTrue(env._persistent_env["PATH"].endswith(":/opt/task/bin:/bin"))
                self.assertNotIn("TOKEN", env._persistent_env)

    async def test_getent_capture_transport_uses_only_local_shell_and_separate_stderr(self):
        env = self.make_env(EnvironmentConfig(docker_image="hbench/task:1", env={"HOME": "/task-home", "TASK_ENV": "task-value"}),
                            persistent_env={"HOME": "/default-home", "PERSISTENT": "persistent-value"})
        command = self.mock_engine(env)
        del env._capture_docker_compose_command
        key = "agent'quoted"
        probe = getent_probe(key)
        self.assertEqual(probe[-1], "getent passwd 'agent'\"'\"'quoted'")
        runtime = Mock(compose=["offline-engine", "compose"], supports_compose_project_directory=True)
        expected = [*runtime.compose, "--project-name", _sanitize_docker_compose_project_name(env.session_id),
                    "--project-directory", str(env.environment_dir.resolve().absolute())]
        for path in env._docker_compose_paths:
            expected.extend(["-f", str(path.resolve().absolute())])
        expected_env = env._compose_env_vars(include_os_env=True)
        passwd = "agent:x:1000:1000::/home/agent/../agent:/bin/bash\n"
        warning = 'Compose warning: version is obsolete; "OPTIONAL" is not set; config-secret-sentinel\n'
        async def local_passwd(*argv, **kwargs):
            self.assertEqual(list(argv), expected + probe)
            self.assertEqual(kwargs, {"env": expected_env, "cwd": str(env.environment_dir.resolve().absolute()),
                "stdin": asyncio.subprocess.DEVNULL, "stdout": asyncio.subprocess.PIPE, "stderr": asyncio.subprocess.PIPE})
            # Never launch the received Docker argv or a real getent process.
            return await REAL_CREATE_SUBPROCESS_EXEC(
                "/bin/sh", "-c", 'printf "%s" "$1"; printf "%s" "$2" >&2', "sh", passwd, warning, **kwargs)

        results = []
        native_collect = env._collect_buffered_output
        async def collected(process, **kwargs):
            result = await native_collect(process, **kwargs)
            results.append(result)
            return result

        callback, logger = Mock(), Mock()
        with patch.object(FrozenDockerEnvironment, "runtime", return_value=runtime), \
             patch("asyncio.create_subprocess_exec", side_effect=local_passwd) as spawn, \
             patch.object(env, "_collect_buffered_output", AsyncMock(side_effect=collected)) as buffered, \
             patch.object(env, "logger", logger), env.with_default_user("other"), \
             env.scoped_exec_env({"HOME": "/scope-home", "PATH": "/scope/bin"}), env.scoped_output_callback(callback):
            self.assertEqual(await env._home_for_user(key + ":1000"), "/home/agent")
            for user in (key, "agent", 1000, "1000:1000"):
                self.assertEqual(await env._home_for_user(user), "/home/agent")
            for user in (None, "root", 0, "0:1000"):
                self.assertEqual(await env._home_for_user(user), "/root")
        spawn.assert_awaited_once()
        buffered.assert_awaited_once()
        self.assertEqual(buffered.await_args.kwargs, {"timeout_sec": 10})
        self.assertEqual(results[0].stdout, passwd)
        self.assertEqual(results[0].stderr, warning)
        self.assertEqual(env._user_homes, {"root": "/root", key: "/home/agent", "agent": "/home/agent", "1000": "/home/agent"})
        command.assert_not_awaited()
        callback.assert_not_called()
        self.assertNotIn("config-secret-sentinel", str(logger.mock_calls))

    async def test_capture_transport_failures_timeout_and_cancel_are_redacted_and_cleanup_is_native(self):
        secret = "config-secret-sentinel"
        for stage in ("probe", "config"):
            for failure in ("spawn", "exit", "timeout", "cancel", "cancel-cleanup-error"):
                with self.subTest(stage=stage, failure=failure):
                    env = self.make_env(session_id="trial-" + stage + "-" + failure)
                    command = self.mock_engine(env)
                    del env._capture_docker_compose_command
                    entered = asyncio.Event()
                    processes = []
                    failed_process = None
                    async def blocked(input=None):
                        entered.set()
                        await asyncio.Event().wait()

                    async def process_boundary(*argv, **kwargs):
                        nonlocal failed_process
                        is_probe = list(argv[-len(PATH_PROBE):]) == PATH_PROBE
                        is_config = list(argv[-len(CONFIG_PROBE):]) == CONFIG_PROBE
                        self.assertTrue(is_probe or is_config)
                        targeted = is_probe if stage == "probe" else is_config
                        if targeted and failure == "spawn":
                            raise OSError(secret)
                        stdout = b"/bin" if is_probe else b'{"services": {"main": {"environment": {}}}}'
                        process = Mock(returncode=0, communicate=AsyncMock(return_value=(stdout, secret.encode())))
                        if targeted:
                            failed_process = process
                            if failure == "exit":
                                process.returncode = 1
                                process.communicate.return_value = (secret.encode(), secret.encode())
                            else:
                                process.returncode = None
                                process.communicate.side_effect = blocked
                                async def terminated():
                                    process.returncode = -15
                                    return -15
                                process.wait = AsyncMock(side_effect=terminated)
                                if failure == "cancel-cleanup-error":
                                    process.terminate.side_effect = OSError(secret)
                        processes.append(process)
                        return process

                    native_collect = env._collect_buffered_output
                    async def collect(process, *, timeout_sec):
                        # Exercise a real native wait_for timeout without waiting
                        # the production 10/30 seconds; all processes are fake.
                        timeout = 0.01 if process is failed_process and failure == "timeout" else timeout_sec
                        return await native_collect(process, timeout_sec=timeout)

                    callback, logger = Mock(), Mock()
                    with patch("asyncio.create_subprocess_exec", side_effect=process_boundary), \
                         patch.object(env, "_collect_buffered_output", AsyncMock(side_effect=collect)) as buffered, \
                         patch.object(DockerEnvironment, "_terminate_process", AsyncMock(wraps=DockerEnvironment._terminate_process)) as terminate, \
                         patch.object(env, "logger", logger), env.scoped_output_callback(callback):
                        if failure.startswith("cancel"):
                            start = asyncio.create_task(env.start())
                            await asyncio.wait_for(entered.wait(), timeout=5)
                            start.cancel()
                            with self.assertRaises(asyncio.CancelledError) as caught:
                                await start
                        else:
                            with self.assertRaisesRegex(RuntimeError, "Cannot probe native Docker main PATH" if stage == "probe"
                                                        else "Cannot resolve effective Docker main HOME") as caught:
                                await env.start()
                    self.assertNotIn(secret, str(caught.exception))
                    self.assertIsNone(caught.exception.__cause__)
                    callback.assert_not_called()
                    self.assertNotIn(secret, str(logger.mock_calls))
                    self.assertIsNone(env._main_home)
                    self.assertNotIn("PATH", env._persistent_env)
                    self.assertTrue(env._runtime_compose_path.is_file())
                    for path in self.root.rglob("*"):
                        if path.is_file():
                            self.assertNotIn(secret.encode(), path.read_bytes())
                    expected_collects = (0 if stage == "probe" else 1) if failure == "spawn" else (1 if stage == "probe" else 2)
                    self.assertEqual(buffered.await_count, expected_collects)
                    if failure in ("timeout", "cancel", "cancel-cleanup-error"):
                        terminate.assert_awaited_once_with(failed_process)
                        failed_process.terminate.assert_called_once_with()
                        if failure != "cancel-cleanup-error":
                            failed_process.wait.assert_awaited_once_with()
                            self.assertEqual(failed_process.returncode, -15)
                    else:
                        terminate.assert_not_awaited()
                    self.assertFalse(any("ln -sfn" in call.args[0][-1] for call in command.await_args_list))
                    await env.stop(delete=True)
                    self.assertEqual(command.await_args.args[0], ["down", "--rmi", "local", "--volumes", "--remove-orphans"])
                    self.assertIsNone(env._env_compose_path)
                    self.assertIsNone(env._mounts_compose_path)

    async def test_native_path_is_preserved_for_entrypoint_then_augmented_for_exec_and_restart(self):
        native_path = "/opt/task/bin:/opt/conda/bin:/opt/task/.venv/bin:/bin"
        runtime_prefix = RUNTIME_ROOT + "/bin:" + RUNTIME_ROOT + "/java/bin:"
        for source in ("compose", "image", "task", "persistent", "already-prefixed"):
            with self.subTest(source=source):
                path = runtime_prefix + native_path if source == "already-prefixed" else native_path
                declared = {"PATH": path} if source in ("compose", "already-prefixed") else {}
                task = self.compose_file({"services": {"main": {"image": "task:1",
                    "entrypoint": ["/opt/task/bin/start"], "environment": declared}}})
                saved = task.read_bytes()
                config = EnvironmentConfig(env={"PATH": path} if source == "task" else {})
                env = self.make_env(config, persistent_env={"PATH": path} if source == "persistent" else {})
                if source not in ("task", "persistent"):
                    self.assertNotIn("PATH", env._persistent_env)
                else:
                    self.assertEqual(env._startup_env()["PATH"], path)
                command = self.mock_engine(env, main_environment=declared, image_path=path)
                original = command.side_effect
                starts = []
                async def observed(argv, **kwargs):
                    if argv[0] == "up":
                        startup = yaml.safe_load(env._env_compose_path.read_text())["services"]["main"]["environment"]
                        self.assertEqual(startup.get("PATH"), path if source in ("task", "persistent") else None)
                        starts.append(startup)
                    return await original(argv, **kwargs)
                command.side_effect = observed
                expected = path if path.startswith(runtime_prefix) else runtime_prefix + path
                for _ in range(2):
                    await env.start()
                    self.assertEqual(env._persistent_env["PATH"], expected)
                    await env.exec("agent-command")
                    self.assertIn("PATH=" + expected, command.await_args.args[0])
                    self.assertIn("JAVA_HOME=" + RUNTIME_ROOT + "/java", command.await_args.args[0])
                    self.assertNotIn("environment", json.loads(env._runtime_compose_path.read_text())["services"]["main"])
                    self.assertEqual(task.read_bytes(), saved)
                    await env.stop(delete=True)
                self.assertEqual(len(starts), 2)
                probes = [call for call in env._capture_docker_compose_command.await_args_list if call.args[0] == PATH_PROBE]
                self.assertEqual(len(probes), 2)
                for probe in probes:
                    self.assertEqual(probe.args[0], PATH_PROBE)
                    self.assertEqual(probe.kwargs, {"timeout_sec": 10})

    async def test_compose_home_is_shared_by_setup_and_implicit_agent_only(self):
        self.task_user("agent")
        self.compose_file({"services": {"main": {"image": "task:1", "environment": {"HOME": "/home/custom"}},
                                        "db": {"image": "db:1"}}})
        env = self.make_env(EnvironmentConfig())
        command = self.mock_engine(env, main_environment={"HOME": "/home/custom"})
        # Before capture, Harbor infrastructure must retain its root fallback.
        await env.exec("before-start")
        self.assertIn("HOME=/root", command.await_args.args[0])
        await env.start()
        calls = [call.args[0] for call in command.await_args_list]
        setup = next(argv for argv in calls if "ln -sfn" in argv[-1])
        self.assertIn("agent_home=/home/custom", setup[-1])
        self.assertIn("HOME=/root", setup)
        check = next(argv for argv in calls if 'test -w "$HOME"' in argv[-1])
        self.assertIn("HOME=/home/custom", check)
        self.assertNotIn("HOME", env._persistent_env)
        self.assertEqual(env._main_home, "/home/custom")
        callback = Mock()
        with env.scoped_exec_env({"HOME": "/scoped", "PATH": "/scope/bin"}), env.scoped_output_callback(callback):
            await env._capture_main_environment()
            self.assertEqual(env._main_home, "/home/custom")
            self.assertNotIn("/scope/bin", env._persistent_env["PATH"])
            self.assertEqual(env._capture_docker_compose_command.await_args.args[0], CONFIG_PROBE)
            self.assertEqual(env._capture_docker_compose_command.await_args.kwargs, {"timeout_sec": 30})
        callback.assert_not_called()
        with env.with_default_user("agent"):
            await env.exec("implicit-agent")
            self.assertIn("HOME=/home/custom", command.await_args.args[0])
            for user, home in (("root", "/root"), (0, "/root"), ("agent", "/home/agent"), (1000, "/home/agent")):
                await env.exec("explicit-user", user=user)
                self.assertIn("HOME=" + home, command.await_args.args[0])
            await env.exec("explicit-home", env={"HOME": "/per-exec"})
            self.assertIn("HOME=/per-exec", command.await_args.args[0])
            with env.scoped_exec_env({"HOME": "/scoped", "PATH": "/scope/bin"}):
                await env.exec("scoped-home", env={"HOME": "/per-exec"}, user="root")
                self.assertIn("HOME=/scoped", command.await_args.args[0])
                self.assertIn("PATH=/scope/bin", command.await_args.args[0])
                await env.service_exec("sidecar", service="db")
                self.assertEqual(command.await_args.args[0], ["exec", "db", "sh", "-c", "sidecar"])
        await env.exec("infrastructure-default")
        self.assertIn("HOME=/root", command.await_args.args[0])

    async def test_image_home_is_not_a_nonroot_agent_declaration(self):
        self.task_user(1000)
        env = self.make_env()
        command = self.mock_engine(env)  # config has no HOME; image HOME=/root is not a Compose declaration
        await env.start()
        self.assertIsNone(env._main_home)
        setup = next(call.args[0] for call in command.await_args_list if "ln -sfn" in call.args[0][-1])
        self.assertIn("agent_home=/home/agent", setup[-1])
        with env.with_default_user(1000):
            await env.exec("default-agent")
            self.assertIn("HOME=/home/agent", command.await_args.args[0])

    async def test_nonroot_default_home_uses_passwd_despite_compose_warnings(self):
        self.task_user("agent")
        env = self.make_env(EnvironmentConfig(docker_image="hbench/task:1", env={"TASK_ENV": "task-value"}),
                            persistent_env={"PERSISTENT": "persistent-value"})
        command = self.mock_engine(env)
        await env.start()
        self.assertIsNone(env._main_home)
        self.assertNotIn("HOME", env._startup_env())
        capture = env._capture_docker_compose_command
        self.assertEqual([call.args[0] for call in capture.await_args_list],
                         [PATH_PROBE, CONFIG_PROBE, getent_probe("agent")])
        capture.assert_any_await(getent_probe("agent"), timeout_sec=10)
        setup = next(call.args[0] for call in command.await_args_list if "ln -sfn" in call.args[0][-1])
        self.assertIn("agent_home=/home/agent", setup[-1])
        with env.with_default_user("agent"):
            await env.exec("default-agent")
            self.assertIn("HOME=/home/agent", command.await_args.args[0])
            await env.exec("numeric-agent", user=1000)
            self.assertIn("HOME=/home/agent", command.await_args.args[0])
        self.assertEqual(capture.await_count, 3)  # passwd name and UID share the cache
        self.assertEqual(env._user_homes, {"root": "/root", "agent": "/home/agent", "1000": "/home/agent"})
        self.assertFalse(any("getent passwd" in call.args[0][-1] for call in command.await_args_list))

    async def test_native_start_order_runtime_file_and_loose_upload_then_setup(self):
        self.task_user(1000)
        loose = self.environment / "script.sh"
        loose.write_text("frozen task input\n")
        saved = loose.read_bytes()
        env = self.make_env(EnvironmentConfig(docker_image="task", workdir="/work"), auto_mounting=True)
        command = self.mock_engine(env)
        snapshots = []
        original = command.side_effect
        capture = env._capture_docker_compose_command
        original_capture = capture.side_effect
        async def observed(argv, **kwargs):
            snapshots.append((argv, list(env._docker_compose_paths)))
            self.assertTrue(env._runtime_compose_path.is_file())
            self.assertEqual(env._docker_compose_paths[-1], env._runtime_compose_path)
            return await (original_capture if argv in (PATH_PROBE, CONFIG_PROBE, getent_probe("1000")) else original)(argv, **kwargs)
        command.side_effect = observed
        capture.side_effect = observed
        await env.start()
        self.assertIsNone(env.default_user)  # Harbor configures it only after start
        calls = [argv for argv, paths in snapshots]
        self.assertEqual(calls[0], ["down", "--remove-orphans"])
        self.assertEqual(calls[1], ["up", "--detach", "--wait"])
        self.assertTrue(any(argv[0] == "cp" and argv[2] == "main:/work" for argv in calls))
        setup = next(argv for argv in calls if "ln -sfn" in argv[-1])
        probe = ["exec", "-T", "-u", "root", "main", "sh", "-c", 'printf "%s" "$PATH"']
        config = ["config", "--format", "json"]
        upload_index = next(index for index, argv in enumerate(calls) if argv[0] == "cp")
        self.assertLess(upload_index, calls.index(probe))
        self.assertLess(calls.index(probe), calls.index(config))
        self.assertLess(calls.index(config), calls.index(getent_probe("1000")))
        self.assertLess(calls.index(getent_probe("1000")), calls.index(setup))
        capture.assert_any_await(getent_probe("1000"), timeout_sec=10)
        self.assertFalse(any("getent passwd" in call.args[0][-1] for call in command.await_args_list))
        self.assertEqual(setup[setup.index("-u") + 1], "root")
        self.assertIn("agent_home=/home/agent", setup[-1])
        self.assertIn("chown 1000", setup[-1])
        self.assertIn("/logs/user-agent", setup[-1])
        ensure = next(argv for argv in calls if "mkdir -p /logs/" in argv[-1] and "ln -sfn" not in argv[-1])
        self.assertEqual(ensure[ensure.index("-u") + 1], "root")
        self.assertEqual(loose.read_bytes(), saved)
        self.assertEqual(env._runtime_compose_path.parent, env.trial_paths.trial_dir)
        self.assertIn(env.session_id, env._runtime_compose_path.name)
        self.assertNotIn(["build"], calls)

    async def test_resolve_copy_persistent_before_parent_and_never_secret_in_new_overlay(self):
        raw = {"SECRET": "${HB_DOCKER_TEST_SECRET}", "FALLBACK": "${HB_UNSET:-offline}"}
        config = EnvironmentConfig(docker_image="task", env={"TASK_SECRET": "task-value", "PATH": "/custom:/bin"})
        observed = []
        original_init = DockerEnvironment.__init__
        def checked(instance, *args, **kwargs):
            observed.append(deepcopy(kwargs["persistent_env"]))
            original_init(instance, *args, **kwargs)
        with patch.dict(os.environ, {"HB_DOCKER_TEST_SECRET": "secret-sentinel"}, clear=True), patch.object(DockerEnvironment, "__init__", checked):
            env = self.make_env(config, persistent_env=raw)
        self.assertEqual(observed, [{"SECRET": "secret-sentinel", "FALLBACK": "offline"}])
        self.mock_engine(env)
        await env.start()
        written = env._runtime_compose_path.read_text()
        for value in ("secret-sentinel", "TASK_SECRET", "task-value", "HB_DOCKER_TEST_SECRET", "FALLBACK"):
            self.assertNotIn(value, written)
        self.assertEqual(env._startup_env()["PATH"], RUNTIME_ROOT + "/bin:" + RUNTIME_ROOT + "/java/bin:/custom:/bin")
        self.assertEqual(env._merge_env(None)["JAVA_HOME"], RUNTIME_ROOT + "/java")
        self.assertEqual(raw["SECRET"], "${HB_DOCKER_TEST_SECRET}")
        self.assertEqual(config.env["PATH"], "/custom:/bin")
        with patch.dict(os.environ, {}, clear=True), patch.object(DockerEnvironment, "__init__") as init:
            with self.assertRaisesRegex(ValueError, "HB_MISSING"):
                self.make_env(persistent_env={"SECRET": "${HB_MISSING}"})
            init.assert_not_called()

    async def test_home_default_root_nonroot_numeric_uid_explicit_and_scoped_priority(self):
        self.task_user("agent")
        env = self.make_env(EnvironmentConfig(docker_image="task", env={"HOME": "/home/custom"}))
        command = self.mock_engine(env, main_environment={"HOME": "/compose-home"})
        await env.start()
        setup = next(call.args[0] for call in command.await_args_list if "ln -sfn" in call.args[0][-1])
        self.assertIn("agent_home=/home/custom", setup[-1])
        command.reset_mock()
        with env.with_default_user("agent"):
            await env.exec("agent-default")
            self.assertIn("HOME=/home/custom", command.await_args.args[0])
            await env.exec("root-infrastructure", user="root")
            self.assertIn("HOME=/root", command.await_args.args[0])
            await env.exec("root-uid", user=0)
            self.assertIn("HOME=/root", command.await_args.args[0])
            await env.exec("agent-uid", user=1000)
            self.assertIn("HOME=/home/agent", command.await_args.args[0])
            self.assertEqual(command.await_args.args[0][command.await_args.args[0].index("-u") + 1], "1000")
            await env.exec("explicit", env={"HOME": "/chosen"}, user="root")
            self.assertIn("HOME=/chosen", command.await_args.args[0])
            with env.scoped_exec_env({"HOME": "/scoped"}):
                await env.exec("scoped", env={"HOME": "/per-exec"}, user="root")
                self.assertIn("HOME=/scoped", command.await_args.args[0])
            await env.service_exec("sidecar", service="db")
            self.assertEqual(command.await_args.args[0], ["exec", "db", "sh", "-c", "sidecar"])
        await env.exec("verifier-default")
        self.assertIn("HOME=/root", command.await_args.args[0])
        self.task_user(0)
        root_env = self.make_env(persistent_env={"HOME": "/custom-root"})
        root_command = self.mock_engine(root_env, main_environment={"HOME": "/compose-root"})
        await root_env.start()
        await root_env.exec("default-root")
        self.assertIn("HOME=/custom-root", root_command.await_args.args[0])
        await root_env.exec("explicit-root", user="root")
        self.assertIn("HOME=/root", root_command.await_args.args[0])

    async def test_env_file_home_is_observed_from_effective_config_not_image_or_raw_yaml(self):
        self.task_user("agent")
        env_file = self.environment / "task.env"
        env_file.write_text("HOME=${TASK_HOME}\nTOKEN=config-secret-sentinel\n")
        self.compose_file({"services": {"main": {"image": "task:1", "env_file": [str(env_file)]}}})
        extra = self.root / "extra.yaml"
        extra.write_text("services:\n  main:\n    environment:\n      HOME: ${TASK_HOME}\n")
        env = self.make_env(EnvironmentConfig(), extra_docker_compose=[extra],
                            persistent_env={"TASK_HOME": "/home/from-effective-config"})
        command = self.mock_engine(env, main_environment={"HOME": "/home/from-effective-config",
                                                          "TOKEN": "config-secret-sentinel"})
        await env.start()
        self.assertEqual(env._main_home, "/home/from-effective-config")
        self.assertNotIn("HOME", env._startup_env())
        setup = next(call.args[0] for call in command.await_args_list if "ln -sfn" in call.args[0][-1])
        self.assertIn("agent_home=/home/from-effective-config", setup[-1])
        with env.with_default_user("agent"):
            await env.exec("default-agent")
            self.assertIn("HOME=/home/from-effective-config", command.await_args.args[0])
        config = next(call for call in env._capture_docker_compose_command.await_args_list if call.args[0] == CONFIG_PROBE)
        self.assertEqual(config.kwargs, {"timeout_sec": 30})
        self.assertNotIn("config-secret-sentinel", env._runtime_compose_path.read_text())
        self.assertNotIn("TOKEN", env._persistent_env)

    async def test_native_service_and_file_operations_routing(self):
        env = self.make_env(persistent_env={"MAIN_ONLY": "sentinel"})
        command = self.mock_engine(env)
        env.default_user = "agent"
        await env.service_exec("db-command", service="db", cwd="/db", env={"SIDE": "1"}, user=999, timeout_sec=4)
        argv = command.await_args.args[0]
        self.assertEqual(argv, ["exec", "-w", "/db", "-e", "SIDE=1", "-u", "999", "db", "sh", "-c", "db-command"])
        self.assertEqual(command.await_args.kwargs["timeout_sec"], 4)
        source = self.root / "source.txt"
        source.write_text("offline")
        await env.upload_file(source, "/data/file")
        self.assertEqual(command.await_args.args[0], ["cp", str(source), "main:/data/file"])
        await env.upload_dir(self.root, "/data")
        self.assertEqual(command.await_args.args[0], ["cp", str(self.root) + "/.", "main:/data"])
        await env.download_file("/data/file", self.root / "file")
        self.assertEqual(command.await_args.args[0][1], "main:/data/file")
        await env.download_dir("/data", self.root / "dir")
        self.assertEqual(command.await_args.args[0][1], "main:/data/.")
        await env.service_download_file("/db/file", self.root / "db-file", service="db")
        self.assertEqual(command.await_args.args[0][1], "db:/db/file")
        await env.service_download_dir("/db", self.root / "db-dir", service="db")
        self.assertEqual(command.await_args.args[0][1], "db:/db/.")
        await env.stop_service("db")
        self.assertEqual(command.await_args.args[0], ["stop", "db"])

    async def test_native_upload_tar_fallback_not_replaced(self):
        env = self.make_env()
        source = self.root / "source.txt"
        source.write_text("offline")
        async def command(argv, **kwargs):
            if argv[0] == "cp":
                raise RuntimeError("cp failure")
            return ExecResult(return_code=1 if argv[-3:-1] == ["test", "-d"] else 0)
        env._run_docker_compose_command = AsyncMock(side_effect=command)
        await env.upload_file(source, "/data/renamed")
        calls = env._run_docker_compose_command.await_args_list
        self.assertEqual(calls[-1].args[0], ["exec", "-T", "-u", "root", "main", "tar", "-xf", "-", "-C", "/data"])
        self.assertTrue(calls[-1].kwargs["stdin_data"])

    async def test_native_network_policy_overlay_and_dynamic_switch_delegated(self):
        self.compose_file({"services": {"main": {}, "db": {"image": "db"}, "explicit": {"image": "db", "networks": ["custom"]}},
                           "networks": {"custom": {}}})
        env = self.make_env(EnvironmentConfig(docker_image="task", network_mode="no-network"))
        command = self.mock_engine(env)
        await env.start()
        self.assertTrue(env.capabilities.dynamic_network_policy)
        self.assertEqual(env._egress_controlled_service_names(), ["main", "db"])
        self.assertEqual(env._docker_compose_paths[-3:], [env._DOCKER_COMPOSE_EGRESS_CONTROL_PATH,
                         env._egress_control_services_compose_path, env._runtime_compose_path])
        network = json.loads(env._egress_control_services_compose_path.read_text())
        self.assertNotIn("hbench-runtime-init", network["services"])
        self.assertEqual(json.loads(env._runtime_compose_path.read_text())["services"]["hbench-runtime-init"]["network_mode"], "none")
        await env.set_network_policy(NetworkPolicy(network_mode="allowlist", allowed_hosts=["example.com"]))
        self.assertEqual(command.await_args.args[0], ["exec", "--no-TTY", env._EGRESS_CONTROL_SERVICE_NAME,
                         "network-policy", "allow", "example.com"])
        with patch.object(DockerEnvironment, "_egress_control_kernel_support", return_value=False):
            with self.assertRaisesRegex(ValueError, "not supported"):
                self.make_env(EnvironmentConfig(docker_image="task", network_mode="no-network"))

    async def test_setup_failed_init_failed_and_cancel_keep_overlay_for_native_cleanup(self):
        for failure in ("init", "setup", "agent-check", "cancel"):
            with self.subTest(failure=failure):
                env = self.make_env(session_id="trial-" + failure)
                command = self.mock_engine(env)
                original = command.side_effect
                async def failed(argv, **kwargs):
                    if argv[0] == "up" and failure == "init":
                        raise RuntimeError("runtime init exited 1")
                    if "ln -sfn" in argv[-1]:
                        if failure == "cancel":
                            raise asyncio.CancelledError()
                        if failure == "setup":
                            return ExecResult(return_code=1, stderr="readonly failure")
                    if 'test -w "$HOME"' in argv[-1] and failure == "agent-check":
                        return ExecResult(return_code=1)
                    return await original(argv, **kwargs)
                command.side_effect = failed
                error = asyncio.CancelledError if failure == "cancel" else RuntimeError
                with self.assertRaises(error):
                    await env.start()
                self.assertTrue(env._runtime_compose_path.is_file())
                calls = [item.args[0] for item in command.await_args_list]
                if failure == "init":
                    self.assertFalse(any("ln -sfn" in argv[-1] for argv in calls))
                await env.stop(delete=True)
                self.assertEqual(command.await_args.args[0], ["down", "--rmi", "local", "--volumes", "--remove-orphans"])
                self.assertIsNone(env._env_compose_path)
                self.assertIsNone(env._mounts_compose_path)

    async def test_probe_and_config_fail_closed_without_secrets_and_keep_native_cleanup(self):
        secret = "config-secret-sentinel"
        failures = {
            "probe-exit": ("probe", ExecResult(return_code=1, stdout=secret, stderr=secret)),
            "probe-missing": ("probe", ExecResult(return_code=0)),
            "probe-multiline": ("probe", ExecResult(return_code=0, stdout="/bin\n" + secret)),
            "probe-exception": ("probe", RuntimeError(secret)),
            "probe-cancel": ("probe", asyncio.CancelledError()),
            "config-exit": ("config", ExecResult(return_code=1, stdout=secret, stderr=secret)),
            "config-json": ("config", ExecResult(return_code=0, stdout=secret)),
            "config-shape": ("config", ExecResult(return_code=0, stdout='{"services": {"db": {}}}')),
            "config-environment": ("config", ExecResult(return_code=0, stdout='{"services": {"main": {"environment": []}}}')),
            "config-home": ("config", ExecResult(return_code=0, stdout=json.dumps({"services": {"main": {
                "environment": {"HOME": "relative", "TOKEN": secret}}}}))),
            "config-exception": ("config", RuntimeError(secret)),
            "config-cancel": ("config", asyncio.CancelledError()),
        }
        for name, (stage, failure) in failures.items():
            with self.subTest(failure=name):
                env = self.make_env(session_id="trial-" + name)
                command = self.mock_engine(env)
                capture = env._capture_docker_compose_command
                original = capture.side_effect
                async def failed(argv, **kwargs):
                    is_probe = argv == PATH_PROBE
                    is_config = argv == CONFIG_PROBE
                    if (stage == "probe" and is_probe) or (stage == "config" and is_config):
                        self.assertNotIn("check", kwargs)
                        self.assertNotIn("on_output", kwargs)
                        if isinstance(failure, BaseException):
                            raise failure
                        return failure
                    return await original(argv, **kwargs)
                capture.side_effect = failed
                error = asyncio.CancelledError if isinstance(failure, asyncio.CancelledError) else RuntimeError
                with self.assertRaises(error) as caught:
                    await env.start()
                self.assertNotIn(secret, str(caught.exception))
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(env._main_home)
                self.assertNotIn("PATH", env._persistent_env)
                self.assertTrue(env._runtime_compose_path.is_file())
                self.assertNotIn(secret, env._runtime_compose_path.read_text())
                calls = [call.args[0] for call in command.await_args_list]
                self.assertIn(["up", "--detach", "--wait"], calls)
                self.assertFalse(any("ln -sfn" in argv[-1] for argv in calls))
                if stage == "probe":
                    self.assertNotIn(CONFIG_PROBE, [call.args[0] for call in capture.await_args_list])
                await env.stop(delete=True)
                self.assertEqual(command.await_args.args[0], ["down", "--rmi", "local", "--volumes", "--remove-orphans"])
                self.assertIsNone(env._env_compose_path)
                self.assertIsNone(env._mounts_compose_path)

    async def test_stop_keep_and_delete_are_native_and_restart_overlay_is_unique(self):
        for keep, delete, expected in ((True, True, ["stop"]), (False, False, ["down"]),
                                      (False, True, ["down", "--rmi", "local", "--volumes", "--remove-orphans"])):
            env = self.make_env(keep_containers=keep)
            command = self.mock_engine(env)
            await env.start()
            first = env._runtime_compose_path
            await env.stop(delete=delete)
            self.assertEqual(command.await_args.args[0], expected)
            await env.start()
            self.assertNotEqual(first, env._runtime_compose_path)
            self.assertFalse(first.exists())
            self.assertTrue(env._runtime_compose_path.exists())

    async def test_gpu_and_reserved_errors_prevent_native_start(self):
        env = self.make_env(EnvironmentConfig(docker_image="task", gpus=1, gpu_types=["H100"]))
        self.mock_engine(env)
        with patch("hbench.docker_environment.subprocess.run", return_value=Mock(stdout="NVIDIA A100\n")), patch.object(DockerEnvironment, "start", AsyncMock()) as start:
            with self.assertRaises(RuntimeError):
                await env.start()
            start.assert_not_awaited()
        self.assertIsNone(env._runtime_compose_path)
        with patch.object(DockerEnvironment, "stop", AsyncMock()) as stop:
            await env.stop(delete=True)
            stop.assert_not_awaited()
        self.compose_file({"services": {"main": {"volumes": ["bad:/opt/harness"]}}})
        unsafe = self.make_env()
        with patch.object(DockerEnvironment, "start", AsyncMock()) as start, patch.object(DockerEnvironment, "stop", AsyncMock()) as stop:
            with self.assertRaises(ValueError):
                await unsafe.start()
            start.assert_not_awaited()
            await unsafe.stop(delete=True)
            stop.assert_not_awaited()

    async def test_getent_home_failures_are_redacted_and_cancellation_propagates(self):
        secret = "config-secret-sentinel"
        failures = {
            "exit": ExecResult(return_code=2, stdout=secret, stderr=secret),
            "missing": ExecResult(return_code=0, stderr=secret),
            "malformed": ExecResult(return_code=0, stdout=secret, stderr=secret),
            "relative": ExecResult(return_code=0, stdout=f"agent:x:1234:1234::{secret}:/bin/sh", stderr=secret),
            "multiline": ExecResult(return_code=0, stdout=f"agent:x:1234:1234::/home/agent\n{secret}:/bin/sh", stderr=secret),
            "nul": ExecResult(return_code=0, stdout=f"agent:x:1234:1234::/home/agent\x00{secret}:/bin/sh", stderr=secret),
            "spawn": OSError(secret),
            "timeout": asyncio.TimeoutError(secret),
            "exception": RuntimeError(secret),
            "cancel": asyncio.CancelledError(secret),
        }
        for name, failure in failures.items():
            with self.subTest(failure=name):
                env = self.make_env()
                command = self.mock_engine(env)
                capture = env._capture_docker_compose_command
                capture.side_effect = failure if isinstance(failure, BaseException) else None
                if not isinstance(failure, BaseException):
                    capture.return_value = failure
                callback, logger = Mock(), Mock()
                with patch.object(env, "logger", logger), env.scoped_output_callback(callback):
                    if isinstance(failure, asyncio.CancelledError):
                        with self.assertRaises(asyncio.CancelledError) as caught:
                            await env.exec("agent", user=1234)
                        self.assertIs(caught.exception, failure)
                    else:
                        with self.assertRaises(RuntimeError) as caught:
                            await env.exec("agent", user=1234)
                        self.assertEqual(str(caught.exception), "Cannot resolve HOME for Docker exec user 1234")
                        self.assertNotIn(secret, str(caught.exception))
                        self.assertTrue(caught.exception.__suppress_context__)
                    self.assertIsNone(caught.exception.__cause__)
                capture.assert_awaited_once_with(getent_probe("1234"), timeout_sec=10)
                self.assertEqual(env._user_homes, {"root": "/root"})
                command.assert_not_awaited()
                callback.assert_not_called()
                self.assertNotIn(secret, str(logger.mock_calls))

    async def test_unknown_uid_home_failure_is_not_silent_and_setup_shell_is_valid(self):
        env = self.make_env()
        command = self.mock_engine(env)
        capture = env._capture_docker_compose_command
        capture.side_effect = None
        capture.return_value = ExecResult(return_code=2, stdout="", stderr="unknown uid")
        with self.assertRaisesRegex(RuntimeError, "resolve HOME"):
            await env.exec("agent", user=1234)
        capture.assert_awaited_once_with(getent_probe("1234"), timeout_sec=10)
        command.assert_not_awaited()
        self.assertNotIn("1234", env._user_homes)
        await env._setup_runtime()
        script = next(call.args[0][-1] for call in command.await_args_list if "ln -sfn" in call.args[0][-1])
        result = subprocess.run(["sh", "-n"], input=script, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        with patch.object(DockerEnvironment, "start", AsyncMock()):
            bad = self.make_env(persistent_env={"HOME": "relative"})
            self.mock_engine(bad)
            with self.assertRaisesRegex(ValueError, "HOME"):
                await bad.start()


if __name__ == "__main__":
    unittest.main()
