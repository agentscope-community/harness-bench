import asyncio
import base64
import io
import json
import os
from pathlib import Path
import random
import shlex
import subprocess
import sys
import tarfile
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from harbor.environments.ack import ACKEnvironment
from harbor.environments.base import ExecResult
from harbor.models.task.config import EnvironmentConfig, NetworkMode, NetworkPolicy
from harbor.models.trial.paths import TrialPaths
from kubernetes import client as k8s_client
from kubernetes.client.exceptions import ApiException

from hbench.environment import ContainerCoreV1Api, EXEC_CONTAINER, KubernetesEnvironment, POLICY_LABEL, _STOP_MAIN_SCRIPT
from hbench.runtime_mount import SETUP_READY, add_setup_gate, runtime_overrides
from hbench.transfers import BATCH_SIZE, CHUNK_BYTES, TransferMixin, transfer_batch


def pod_status(phase="Running", *, ready=True, terminated=False, image_pull=False):
    state = NS(terminated=NS(exit_code=137, reason="Killed") if terminated else None,
               waiting=NS(reason="ErrImagePull", message="TLS timeout") if image_pull else None)
    status = NS(name="main", ready=ready, state=state)
    return NS(status=NS(phase=phase, container_statuses=[status], init_container_statuses=[], reason=None, message=None))


class EnvironmentFixture:
    def make_env(self, config=None, **kwargs):
        return KubernetesEnvironment(environment_dir=kwargs.pop("environment_dir", self.root), environment_name="task",
                                     session_id=kwargs.pop("session_id", "trial-one"),
                                     trial_paths=TrialPaths(self.root / "trial"), namespace="isolated",
                                     task_env_config=config or EnvironmentConfig(docker_image="hbench/task:1", cpus=1, memory_mb=512), **kwargs)

    def compose_file(self, services, directory=None):
        ((directory or self.root) / "docker-compose.yaml").write_text(json.dumps({"services": services}))

    def task_environment(self, entrypoint):
        directory = self.root / "environment"
        directory.mkdir()
        (self.root / "hbench.json").write_text(json.dumps({"main_entrypoint": entrypoint}))
        return directory


class EnvironmentTests(EnvironmentFixture, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def test_prebuilt_without_dockerfile_and_image_mapping(self):
        env = self.make_env(image_map={"hbench/": "registry/private/"})
        self.assertEqual(env._get_image_url(), "registry/private/task:1")
        (self.root / "images.json").write_text(json.dumps({"hbench/": "registry/"}))
        self.assertEqual(self.make_env(image_map=self.root / "images.json")._get_image_url(), "registry/task:1")
        with self.assertRaises(FileNotFoundError):
            self.make_env(EnvironmentConfig())

    def test_persistent_env_resolved_before_ack_init_and_copied(self):
        raw = {"SECRET": "${HB_TEST_SECRET}", "FALLBACK": "${HB_TEST_UNSET:-fallback}", "LITERAL": "value"}
        config = EnvironmentConfig(docker_image="task", env={"SECRET": "task-value"})
        original_init = ACKEnvironment.__init__
        observed = []

        def checked_init(instance, *args, **kwargs):
            observed.append(dict(kwargs["persistent_env"]))
            original_init(instance, *args, **kwargs)

        with patch.dict(os.environ, {"HB_TEST_SECRET": "resolved"}, clear=True), patch.object(ACKEnvironment, "__init__", checked_init):
            # Exercise Harbor ACK's actual positional signature too.
            env = KubernetesEnvironment(self.root, "task", "trial", TrialPaths(self.root / "trial"), config,
                                        "isolated", persistent_env=raw, harness_runtime_image="runtime")
        expected = {"SECRET": "resolved", "FALLBACK": "fallback", "LITERAL": "value"}
        self.assertEqual(observed, [expected])
        self.assertEqual(env._startup_env(), expected)
        self.assertEqual(env._merge_env(None), expected)
        main = env._prepare_pod_overrides()["spec"]["containers"][0]
        self.assertEqual({item["name"]: item["value"] for item in main["env"]}["SECRET"], "resolved")
        env._persistent_env["LITERAL"] = "changed"
        self.assertEqual(raw, {"SECRET": "${HB_TEST_SECRET}", "FALLBACK": "${HB_TEST_UNSET:-fallback}", "LITERAL": "value"})

    def test_missing_persistent_env_fails_before_ack_init(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(ACKEnvironment, "__init__") as init:
            with self.assertRaisesRegex(ValueError, "HB_TEST_MISSING"):
                self.make_env(persistent_env={"SECRET": "${HB_TEST_MISSING}"})
        init.assert_not_called()

    def test_resolved_persistent_env_used_for_compose_and_exec(self):
        self.compose_file({"main": {}, "db": {"image": "db", "command": ["echo", "${SECRET}"],
                                             "environment": {"COPY": "${SECRET}"}}})
        with patch.dict(os.environ, {"HB_TEST_SECRET": "resolved"}, clear=True):
            env = self.make_env(persistent_env={"SECRET": "${HB_TEST_SECRET}"})
        main, db = env._prepare_pod_overrides()["spec"]["containers"]
        self.assertEqual({item["name"]: item["value"] for item in main["env"]}["SECRET"], "resolved")
        self.assertEqual({item["name"]: item["value"] for item in db["env"]}, {"COPY": "resolved"})
        self.assertEqual(db["args"], ["echo", "resolved"])
        self.assertEqual(env._merge_env(None)["SECRET"], "resolved")

    def test_capabilities_and_network_fail_closed(self):
        env = self.make_env()
        self.assertFalse(env.capabilities.disable_internet)
        self.assertFalse(env.capabilities.network_allowlist)
        self.assertFalse(env.capabilities.stream)
        self.assertTrue(env.capabilities.docker_compose)
        for policy in [NetworkPolicy(network_mode=NetworkMode.NO_NETWORK), NetworkPolicy(network_mode=NetworkMode.ALLOWLIST, allowed_hosts=["192.0.2.1"])]:
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                self.make_env(network_policy=policy)
        with self.assertRaises(ValueError):
            self.make_env(EnvironmentConfig(docker_image="task", network_mode="no-network"))
        for host in ["example.com", "*.example.com", "2001:db8::1", "2001:db8::/32"]:
            with self.subTest(host=host), self.assertRaises(ValueError):
                self.make_env(network_enforcement="networkpolicy", network_policy=NetworkPolicy(network_mode="allowlist", allowed_hosts=[host]))

    def test_reject_claim_and_invalid_settings(self):
        for kwargs in [{"use_sandbox_claim": True}, {"use_sandbox_claim": "true"}, {"transfer": "invalid"},
                       {"network_enforcement": "pretend"}, {"pod_ready_timeout_sec": 0},
                       {"pod_ready_timeout_sec": "nan"}, {"image_map": {"": "bad"}}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.make_env(**kwargs)

    def test_gpu_limits_and_type_selector(self):
        config = EnvironmentConfig(docker_image="task", cpus=0.5, memory_mb=256, gpus=2, gpu_types=["A100"])
        with self.assertRaisesRegex(ValueError, "gpu_types"):
            self.make_env(config)
        env = self.make_env(config, gpu_type_node_selectors={"A100": {"accelerator": "nvidia-a100"}})
        spec = env._prepare_pod_overrides()["spec"]
        self.assertEqual(spec["nodeSelector"], {"accelerator": "nvidia-a100"})
        self.assertEqual(spec["containers"][0]["resources"]["limits"]["nvidia.com/gpu"], "2")
        env = self.make_env(config, node_selector={"nvidia.com/gpu.product": "A100"})
        self.assertTrue(env.capabilities.gpus)

    def test_security_context_preserved_and_main_not_misaligned(self):
        self.compose_file({"main": {}, "db": {"image": "db"}})
        override = {"spec": {"containers": [{"name": "db", "securityContext": {"runAsUser": 999}},
                                            {"name": "main", "securityContext": {"capabilities": {"drop": ["ALL"]}, "allowPrivilegeEscalation": False}}]}}
        env = self.make_env(pod_overrides=override, harness_runtime_image="runtime")
        spec = env._prepare_pod_overrides()["spec"]
        self.assertEqual([c["name"] for c in spec["containers"]], ["main", "db"])
        self.assertEqual(spec["containers"][1]["securityContext"]["runAsUser"], 999)
        self.assertFalse(spec["containers"][0]["securityContext"]["privileged"])
        self.assertEqual(spec["containers"][0]["securityContext"]["capabilities"], {"drop": ["ALL"]})
        self.assertFalse(spec["containers"][0]["securityContext"]["allowPrivilegeEscalation"])

    def test_networkpolicy_rejects_bypasses(self):
        for spec in [{"hostNetwork": True}, {"hostPID": True}, {"shareProcessNamespace": True},
                     {"containers": [{"securityContext": {"privileged": True}}]},
                     {"containers": [{"securityContext": {"capabilities": {"add": ["NET_ADMIN"]}}}]},
                     {"volumes": [{"name": "host", "hostPath": {"path": "/"}}]}]:
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                self.make_env(network_enforcement="networkpolicy", pod_overrides={"spec": spec})._prepare_pod_overrides()

    def test_main_compose_entrypoint_and_environment_reach_pod(self):
        self.compose_file({"main": {"entrypoint": ["/app/entrypoint.sh"], "environment": {"COMPOSE_ONLY": "yes"},
                                  "healthcheck": {"test": ["CMD-SHELL", "test -f /app/ready"]}},
                           "tau3": {"image": "hbench/tau3-runtime:0.1.0", "command": ["--task-config", "selected.json"],
                                    "volumes": ["${HOST_AGENT_LOGS_PATH}:/service/logs"]}})
        env = self.make_env(EnvironmentConfig(docker_image="task", env={"SELECTED": "selected.json"}), harness_runtime_image="runtime")
        main, tau3 = env._prepare_pod_overrides()["spec"]["containers"]
        self.assertEqual(main["command"], ["/app/entrypoint.sh"])
        self.assertEqual({item["name"]: item["value"] for item in main["env"]}["SELECTED"], "selected.json")
        self.assertEqual(main["readinessProbe"]["exec"]["command"], ["sh", "-c", "test -f /app/ready"])
        self.assertIn("startupProbe", main)
        self.assertEqual(tau3["volumeMounts"][0]["subPath"], "agent")
        self.assertEqual(tau3["args"], ["--task-config", "selected.json"])

    def test_idle_images_keepalive_with_and_without_runtime(self):
        for image in (None, "runtime"):
            with self.subTest(image=image):
                main = self.make_env(harness_runtime_image=image)._prepare_pod_overrides()["spec"]["containers"][0]
                self.assertIsNone(main["command"])
                self.assertEqual(main["args"], ["sleep", "infinity"])
                fragment = runtime_overrides(image, main_image="task", startup_env={})
                self.assertEqual(fragment["spec"]["containers"][0]["args"], ["sleep", "infinity"])
                workload = runtime_overrides(image, main_image="task", startup_env={}, main_keepalive=False)
                self.assertIsNone(workload["spec"]["containers"][0]["args"])

    def test_main_workload_metadata_preserves_image_entrypoint_and_cmd(self):
        directory = self.task_environment(["/app/workload"])
        for compose in (False, True):
            with self.subTest(compose=compose):
                if compose:
                    self.compose_file({"main": {}, "db": {"image": "db"}}, directory)
                main = self.make_env(environment_dir=directory, harness_runtime_image="runtime")._prepare_pod_overrides()["spec"]["containers"][0]
                self.assertIsNone(main["command"])
                self.assertIsNone(main["args"])
        self.assertEqual(json.loads((self.root / "hbench.json").read_text()), {"main_entrypoint": ["/app/workload"]})

    def test_empty_entrypoint_metadata_keeps_idle_main_alive_in_compose(self):
        directory = self.task_environment([])
        self.compose_file({"main": {}, "db": {"image": "db"}}, directory)
        main = self.make_env(environment_dir=directory)._prepare_pod_overrides()["spec"]["containers"][0]
        self.assertIsNone(main["command"])
        self.assertEqual(main["args"], ["sleep", "infinity"])

    def test_separate_verifier_ignores_main_workload_metadata(self):
        directory = self.task_environment(["/app/workload"])
        tests = self.root / "tests"
        tests.mkdir()
        for env_dir, session in ((tests, "trial__verifier__test"), (directory, "trial__verifier__test")):
            for compose in (False, True):
                with self.subTest(env_dir=env_dir, compose=compose):
                    if compose:
                        self.compose_file({"main": {}}, env_dir)
                    env = self.make_env(environment_dir=env_dir, session_id=session)
                    main = env._prepare_pod_overrides()["spec"]["containers"][0]
                    self.assertIsNone(main["command"])
                    self.assertEqual(main["args"], ["sleep", "infinity"])

    def test_explicit_compose_and_user_commands_win_over_keepalive(self):
        for service, expected_command, expected_args in (
            ({"command": ["serve"]}, None, ["serve"]),
            ({"entrypoint": ["/app/entrypoint"]}, ["/app/entrypoint"], None),
            ({"entrypoint": ["/app/entrypoint"], "command": ["serve"]}, ["/app/entrypoint"], ["serve"]),
        ):
            with self.subTest(service=service):
                self.compose_file({"main": service})
                main = self.make_env(harness_runtime_image="runtime")._prepare_pod_overrides()["spec"]["containers"][0]
                self.assertEqual(main["command"], expected_command)
                self.assertEqual(main["args"], expected_args)
        override = {"spec": {"containers": [{"command": ["custom"], "args": ["arg"]}]}}
        main = self.make_env(pod_overrides=override)._prepare_pod_overrides()["spec"]["containers"][0]
        self.assertEqual((main["command"], main["args"]), (["custom"], ["arg"]))
        (self.root / "docker-compose.yaml").unlink()
        for kwargs in ({}, {"harness_runtime_image": "runtime"}):
            with self.subTest(command_only=kwargs):
                override = {"spec": {"containers": [{"command": ["custom"]}]}}
                main = self.make_env(pod_overrides=override, **kwargs)._prepare_pod_overrides()["spec"]["containers"][0]
                self.assertEqual(main["command"], ["custom"])
                self.assertNotIn("args", main)
        self.compose_file({"main": {}})
        override = {"spec": {"containers": [{"name": "main", "command": ["custom"]}]}}
        main = self.make_env(pod_overrides=override)._prepare_pod_overrides()["spec"]["containers"][0]
        self.assertEqual(main["command"], ["custom"])
        self.assertNotIn("args", main)
        self.compose_file({"main": {"command": ["serve"]}})
        main = self.make_env(pod_overrides=override)._prepare_pod_overrides()["spec"]["containers"][0]
        self.assertEqual((main["command"], main["args"]), (["custom"], ["serve"]))

    def test_resource_modes(self):
        env = self.make_env(cpu_enforcement_policy="guarantee", memory_enforcement_policy="limit")
        spec = env._prepare_pod_overrides()["spec"]
        self.assertEqual(spec["containers"][0]["resources"]["requests"]["cpu"], "1")
        self.assertEqual(spec["containers"][0]["resources"]["limits"], {"cpu": "1", "memory": "512Mi"})
        env = self.make_env(cpu_enforcement_policy="ignore", memory_enforcement_policy="ignore")
        env._prepare_pod_overrides()
        self.assertEqual((env.cpu_request, env.memory_request), ("0", "0"))

    def test_final_resource_conflicts_rejected_after_compose_and_user_merge(self):
        config = EnvironmentConfig(docker_image="task", cpus=2, memory_mb=1024)
        for service, resource in (({"cpus": 1}, "cpu"), ({"mem_limit": "512m"}, "memory")):
            with self.subTest(service=service):
                self.compose_file({"main": service})
                with self.assertRaisesRegex(ValueError, resource + " request exceeds limit"):
                    self.make_env(config)._prepare_pod_overrides()
        (self.root / "docker-compose.yaml").unlink()
        for name, resources in (
            ("main", {"limits": {"cpu": "500m"}}),
            ("main", {"requests": {"memory": "2Gi"}, "limits": {"memory": "1024Mi"}}),
            ("db", {"requests": {"cpu": "1e0"}, "limits": {"cpu": "999m"}}),
            ("db", {"requests": {"ephemeral-storage": "2Gi"}, "limits": {"ephemeral-storage": "1G"}}),
        ):
            with self.subTest(name=name, resources=resources), self.assertRaisesRegex(ValueError, "request exceeds limit"):
                self.make_env(pod_overrides={"spec": {"containers": [{"name": name, "resources": resources}]}})._prepare_pod_overrides()
        with self.assertRaisesRegex(ValueError, "memory request exceeds limit"):
            self.make_env(memory_limit_multiplier=0.5)._prepare_pod_overrides()

    def test_final_resource_quantities_use_kubernetes_parser(self):
        resources = {"requests": {"cpu": "1000m", "memory": "1Gi", "ephemeral-storage": "1e9", "nvidia.com/gpu": "1"},
                     "limits": {"cpu": "1e0", "memory": "1024Mi", "ephemeral-storage": "1G", "nvidia.com/gpu": "1"}}
        env = self.make_env(pod_overrides={"spec": {"containers": [{"resources": resources}]}})
        self.assertEqual(env._prepare_pod_overrides()["spec"]["containers"][0]["resources"], resources)
        for value in ("invalid", "NaN", "Infinity", "-1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.make_env(pod_overrides={"spec": {"containers": [{"resources": {"limits": {"cpu": value}}}]}})._prepare_pod_overrides()

    def test_user_resolution_root_skips_su(self):
        env = self.make_env()
        self.assertIsNone(env._resolve_user(None))
        self.assertIsNone(env._resolve_user("root"))
        env.default_user = "agent"
        self.assertEqual(env._resolve_user(None), "agent")
        self.assertEqual(env._resolve_user(1000), 1000)
        self.assertIsNone(env._resolve_user("root"))

    def test_nonroot_main_pod_and_container_explicitly_rejected(self):
        for spec in (
            {"securityContext": {"runAsUser": 1000, "runAsGroup": 1000}},
            {"securityContext": {"runAsNonRoot": True}},
            {"securityContext": {"runAsUser": 1000}, "containers": [{"securityContext": {"runAsUser": 0}}]},
            {"containers": [{"securityContext": {"runAsUser": 2000}}]},
            {"containers": [{"securityContext": {"runAsNonRoot": True}}]},
        ):
            with self.subTest(spec=spec), self.assertRaisesRegex(ValueError, "must run as root"):
                self.make_env(pod_overrides={"spec": spec})._prepare_pod_overrides()
        with self.assertRaisesRegex(ValueError, "must run as root"):
            self.make_env(pod_run_as_user=1000)._prepare_pod_overrides()
        self.compose_file({"main": {"user": "1000:1000"}})
        with self.assertRaisesRegex(ValueError, "must run as root"):
            self.make_env()._prepare_pod_overrides()

    def test_nonroot_default_agent_user_keeps_root_container_and_su(self):
        env = self.make_env()
        env.default_user = "agent"
        main = env._prepare_pod_overrides()["spec"]["containers"][0]
        self.assertEqual(main["securityContext"]["runAsUser"], 0)
        self.assertEqual(env._resolve_user(None), "agent")
        setup = main["lifecycle"]["postStart"]["exec"]["command"][2]
        self.assertIn("agent_user=agent", setup)
        self.assertIn('su "$agent_user"', setup)
        self.assertLess(setup.index('test -w "$HOME"'), setup.index("touch " + SETUP_READY))

    def test_task_agent_user_initialized_before_harbor_sets_default_user(self):
        directory = self.task_environment([])
        (self.root / "task.toml").write_text('[agent]\nuser = 1000\n')
        env = self.make_env(environment_dir=directory, harness_runtime_image="runtime")
        self.assertIsNone(env.default_user)
        main = env._prepare_pod_overrides()["spec"]["containers"][0]
        setup = main["lifecycle"]["postStart"]["exec"]["command"][2]
        self.assertIn("getent passwd 1000", setup)
        self.assertIn('test -w "$HOME"', setup)
        self.assertIn("/logs/agent", setup)
        self.assertIn({"name": "hbench-runtime", "mountPath": "/root/.nvm", "subPath": "nvm", "readOnly": True}, main["volumeMounts"])
        verifier = self.root / "tests"
        verifier.mkdir()
        verifier_env = self.make_env(environment_dir=verifier)
        self.assertIsNone(verifier_env._setup_agent_user())

    def test_setup_nvm_created_only_when_missing_before_readiness(self):
        home = self.root / "agent-home"
        logs = self.root / "logs"
        for name in ("agent", "verifier", "artifacts", "user-agent"):
            (logs / name).mkdir(parents=True)
        ready = self.root / "ready"
        previous = ["sh", "-c", "test ! -e " + shlex.quote(str(ready))]
        main = {"name": "main", "args": ["serve"], "lifecycle": {"postStart": {"exec": {"command": previous}}}}
        gated = add_setup_gate(main, 30, agent_user="agent", agent_home=str(home))
        script = gated["lifecycle"]["postStart"]["exec"]["command"][2]
        script = script.replace("/logs/", str(logs) + "/").replace(SETUP_READY, str(ready))
        # Local shell stand-ins avoid root/real-user requirements; execute the
        # actual generated filesystem setup as the current offline test user.
        shim = 'su() { [ "$1" = agent ] || return 1; /bin/sh -c "$5"; }\nchown() { :; }\n'
        for existing in (False, True):
            with self.subTest(existing=existing):
                ready.unlink(missing_ok=True)
                nvm = home / ".nvm" / "nvm.sh"
                if existing:
                    nvm.write_text("existing nvm shim\n")
                result = subprocess.run(["sh", "-ec", shim + script], text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(nvm.read_text(), "existing nvm shim\n" if existing else "")
                self.assertTrue(ready.exists())
        self.assertEqual(main["lifecycle"]["postStart"]["exec"]["command"], previous)
        self.assertEqual(gated["args"], ["serve"])

    def test_runtime_path_remains_available_to_main_exec(self):
        env = self.make_env(EnvironmentConfig(docker_image="task", env={"PATH": "/custom/bin:/bin"}), harness_runtime_image="runtime")
        spec = env._prepare_pod_overrides()["spec"]
        expected = "/opt/hbench-runtime/bin:/opt/hbench-runtime/java/bin:/custom/bin:/bin"
        self.assertEqual(env._merge_env(None)["PATH"], expected)
        self.assertEqual(env._prepare_pod_overrides()["spec"]["containers"][0]["env"], spec["containers"][0]["env"])
        mounts = {m["mountPath"] for m in spec["containers"][0]["volumeMounts"]}
        self.assertTrue({"/tmp/harbor-pi-mcp-adapter", "/opt/pi-mcp-adapter", "/root/.local/bin/uv", "/opt/harness"}.issubset(mounts))

    def test_compose_can_be_disabled(self):
        self.compose_file({"main": {}})
        with self.assertRaisesRegex(ValueError, "compose=true"):
            self.make_env(compose="false")

    def test_policy_public_none_and_allowlist(self):
        env = self.make_env(network_enforcement="networkpolicy")
        public = env._network_policy_body(NetworkPolicy())
        self.assertEqual(public["spec"]["egress"], [{}])
        self.assertEqual(env._network_policy_body(NetworkPolicy(network_mode="no-network"))["spec"]["egress"], [])
        allow = env._network_policy_body(NetworkPolicy(network_mode="allowlist", allowed_hosts=["192.0.2.3", "198.51.100.0/24"]))
        self.assertEqual(allow["spec"]["egress"][0]["to"], [{"ipBlock": {"cidr": "192.0.2.3/32"}}, {"ipBlock": {"cidr": "198.51.100.0/24"}}])
        self.assertEqual(allow["spec"]["egress"][1]["ports"], [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}])
        self.assertEqual(public["spec"]["podSelector"]["matchLabels"], {POLICY_LABEL: env.pod_name})


class AsyncEnvironmentTests(EnvironmentFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def mock_client(self, env):
        env._ensure_client = AsyncMock()
        env._exec_api = ContainerCoreV1Api(k8s_client.ApiClient())
        self.addCleanup(env._exec_api.api_client.close)
        return env

    def mock_start_client(self, env):
        self.mock_client(env)
        env._core_api = NS(create_namespaced_pod=Mock(),
                           read_namespaced_pod=Mock(return_value=pod_status()))
        env._batch_api = Mock()
        env._image_exists = AsyncMock(side_effect=AssertionError("Offline test must not check registry"))
        env._build_and_push_image = AsyncMock(side_effect=AssertionError("Offline test must not build images"))
        env._upload_environment_dir_after_start = AsyncMock()
        return env

    async def test_inherited_start_log_setup_is_root_and_explicit_users_are_preserved(self):
        env = self.mock_start_client(self.make_env(pod_overrides={"spec": {"containers": [
            {"name": "db", "image": "db", "securityContext": {"runAsUser": 999}}]}}))
        env.default_user = "agent"
        pod = env._core_api.read_namespaced_pod.return_value
        pod.status.container_statuses.append(NS(name="db", ready=True, state=NS(terminated=None)))
        other = self.mock_client(self.make_env(session_id="other"))
        other.default_user = "agent"
        commands = []

        def fake_stream(method, *args, **kwargs):
            self.assertEqual(env.default_user, "agent")
            commands.append((EXEC_CONTAINER.get(), kwargs["command"][2]))
            return NS(returncode=0, is_open=lambda: False, close=Mock())

        async def check_start_users():
            for result in (await env.exec("explicit-agent", user="agent"),
                           await env.exec("explicit-uid", user=1000),
                           await env.service_exec("sidecar-default", service="db"),
                           await env.service_exec("sidecar-agent", service="db", user="agent"),
                           await env.service_exec("sidecar-root", service="db", user="root"),
                           await other.exec("other-environment")):
                self.assertEqual(result.return_code, 0)

        env._upload_environment_dir_after_start.side_effect = check_start_users
        # Do not patch ACK.start: exercise its actual Pod merge and log setup.
        with patch("hbench.environment.stream", side_effect=fake_stream):
            await env.start()
            result = await env.exec("after-start")
        self.assertEqual(result.return_code, 0)
        env._core_api.create_namespaced_pod.assert_called_once()
        env._upload_environment_dir_after_start.assert_awaited_once()
        setup = [command for _, command in commands if "mkdir -p" in command or "chmod 777" in command]
        self.assertEqual(len(setup), 2)
        self.assertIn("mkdir -p /logs/agent /logs/verifier", setup[0])
        self.assertIn("chmod 777 /logs/agent /logs/verifier", setup[1])
        for command in setup:
            self.assertNotIn("su ", command)
        for marker in ("explicit-agent", "sidecar-agent", "other-environment", "after-start"):
            self.assertIn("su agent", next(command for _, command in commands if marker in command))
        uid_command = next(command for _, command in commands if "explicit-uid" in command)
        self.assertIn("su $(getent passwd 1000 | cut -d: -f1)", uid_command)
        self.assertIn(("db", "sh -c sidecar-default"), commands)
        self.assertIn("su root", next(command for _, command in commands if "sidecar-root" in command))
        self.assertEqual(env.default_user, "agent")
        self.assertEqual(env._resolve_user(None), "agent")

    async def test_inherited_start_log_failure_restores_implicit_agent(self):
        for failing_command, message in (("mkdir -p", "Failed to create log directories"),
                                         ("chmod 777", "Failed to chmod log directories")):
            with self.subTest(failing_command=failing_command):
                env = self.mock_start_client(self.make_env())
                env.default_user = "agent"
                commands = []

                def fake_stream(method, *args, **kwargs):
                    self.assertEqual(env.default_user, "agent")
                    command = kwargs["command"][2]
                    commands.append(command)
                    return NS(returncode=1 if failing_command in command else 0,
                              is_open=lambda: False, close=Mock())

                with patch("hbench.environment.stream", side_effect=fake_stream):
                    with self.assertRaisesRegex(RuntimeError, message):
                        await env.start()
                    self.assertEqual(env._resolve_user(None), "agent")
                    result = await env.exec("after-failure")
                self.assertEqual(result.return_code, 0)
                setup = [command for command in commands if "mkdir -p" in command or "chmod 777" in command]
                self.assertEqual(len(setup), 1 if failing_command == "mkdir -p" else 2)
                self.assertTrue(all("su " not in command for command in setup))
                self.assertIn("su agent", commands[-1])
                env._upload_environment_dir_after_start.assert_not_awaited()

    async def test_inherited_start_cancellation_restores_agent_and_is_task_local(self):
        env = self.mock_start_client(self.make_env())
        env.default_user = "agent"
        entered = asyncio.Event()
        commands = []

        async def pause_upload():
            entered.set()
            await asyncio.Event().wait()

        async def start_and_probe():
            try:
                await env.start()
            finally:
                # Check in the cancelled task, not only the caller's context.
                self.assertEqual(env._resolve_user(None), "agent")
                self.assertEqual((await env.exec("after-cancellation")).return_code, 0)

        def fake_stream(method, *args, **kwargs):
            self.assertEqual(env.default_user, "agent")
            commands.append(kwargs["command"][2])
            return NS(returncode=0, is_open=lambda: False, close=Mock())

        env._upload_environment_dir_after_start.side_effect = pause_upload
        with patch("hbench.environment.stream", side_effect=fake_stream):
            operation = asyncio.create_task(start_and_probe())
            try:
                await asyncio.wait_for(entered.wait(), timeout=2)
                self.assertEqual((await env.exec("concurrent-agent")).return_code, 0)
                self.assertIn("su agent", commands[-1])
                operation.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await operation
                self.assertEqual((await env.exec("after-start-task")).return_code, 0)
            finally:
                if not operation.done():
                    operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
        setup = [command for command in commands if "mkdir -p" in command or "chmod 777" in command]
        self.assertEqual(len(setup), 2)
        self.assertTrue(all("su " not in command for command in setup))
        self.assertIn("su agent", next(command for command in commands if "after-cancellation" in command))
        self.assertIn("su agent", commands[-1])
        self.assertEqual(env.default_user, "agent")

    async def test_final_pod_disables_service_account_token_unless_explicitly_overridden(self):
        for override, expected in ((None, False),
                                   ({"metadata": {"labels": {"profile": "custom"}},
                                     "spec": {"nodeSelector": {"hbench.io/pool": "custom"}}}, False),
                                   ({"spec": {"automountServiceAccountToken": False}}, False),
                                   ({"spec": {"automountServiceAccountToken": True}}, True)):
            with self.subTest(override=override):
                env = self.mock_start_client(self.make_env(pod_overrides=override))
                response = NS(returncode=0, is_open=lambda: False, close=Mock())
                with patch("hbench.environment.stream", return_value=response):
                    await env.start()
                env._core_api.create_namespaced_pod.assert_called_once()
                pod = env._core_api.create_namespaced_pod.call_args.kwargs["body"]
                self.assertIs(pod["spec"]["automountServiceAccountToken"], expected)
                self.assertEqual(pod["spec"]["containers"][0]["securityContext"]["runAsUser"], 0)
                if override and "metadata" in override:
                    self.assertEqual(pod["metadata"]["labels"]["profile"], "custom")
                    self.assertEqual(pod["spec"]["nodeSelector"], override["spec"]["nodeSelector"])
                    self.assertNotIn("automountServiceAccountToken", override["spec"])
                env._image_exists.assert_not_awaited()
                env._build_and_push_image.assert_not_awaited()

    async def test_ensure_client_replaces_exec_api_with_bound_subclass(self):
        env = self.make_env()
        env._core_api = k8s_client.CoreV1Api(k8s_client.ApiClient())
        env._exec_api = k8s_client.CoreV1Api(k8s_client.ApiClient())
        self.addCleanup(env._core_api.api_client.close)
        with patch.object(ACKEnvironment, "_ensure_client", AsyncMock()):
            await env._ensure_client()
        self.addCleanup(env._exec_api.api_client.close)
        self.assertIsInstance(env._exec_api, ContainerCoreV1Api)
        self.assertIs(env._exec_api.connect_get_namespaced_pod_exec.__self__, env._exec_api)

    async def test_parallel_stream_requests_have_independent_clients_and_container_context(self):
        env = self.mock_client(self.make_env())
        env._services["db"] = "db"
        env.default_user = "agent"
        env.task_env_config.workdir = "/main/work"
        env._persistent_env = {"MAIN_ONLY": "secret"}
        barrier = threading.Barrier(2)
        clients, commands, containers = [], [], []

        class Response:
            returncode = 0
            def is_open(self): return False
            def close(self): pass

        def generated(api, *args, **kwargs):
            containers.append(kwargs["container"])
            return Response()

        def fake_stream(method, *args, **kwargs):
            clients.append(method.__self__.api_client)
            commands.append((EXEC_CONTAINER.get(), kwargs["command"][2]))
            method.__self__.api_client.call_api = object()
            barrier.wait(timeout=10)
            return method(*args, **kwargs)

        with patch("hbench.environment.stream", side_effect=fake_stream), patch.object(k8s_client.CoreV1Api, "connect_get_namespaced_pod_exec", generated):
            results = await asyncio.gather(env.exec("true"), env.service_exec("true", service="db"))
        self.assertTrue(all(result.return_code == 0 for result in results))
        self.assertIsNot(clients[0], clients[1])
        self.assertEqual(set(containers), {"main", "db"})
        main_command = next(command for container, command in commands if container == "main")
        db_command = next(command for container, command in commands if container == "db")
        self.assertIn("/main/work", main_command)
        self.assertIn("MAIN_ONLY", main_command)
        self.assertIn("su agent", main_command)
        self.assertEqual(db_command, "sh -c true")
        self.assertEqual(EXEC_CONTAINER.get(), "main")

    async def test_exec_exports_resolved_persistent_env_for_nonroot_agent(self):
        with patch.dict(os.environ, {"HB_TEST_SECRET": "resolved"}, clear=True):
            env = self.mock_client(self.make_env(persistent_env={"SECRET": "${HB_TEST_SECRET}"}))
        env.default_user = "agent"
        env._request_sync = Mock(return_value=(b"resolved", b"", 0))
        result = await env.exec("printenv SECRET")
        command = env._request_sync.call_args.args[0][2]
        self.assertEqual(result.stdout, "resolved")
        self.assertIn("su agent", command)
        self.assertIn("SECRET=resolved", command)
        self.assertNotIn("${HB_TEST_SECRET}", command)

    async def test_service_download_routes_stream_and_does_not_need_python(self):
        env = self.mock_client(self.make_env())
        env._services["db"] = "db"
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            member = tarfile.TarInfo("artifact.bin")
            data = b"\x00\xff\x80binary"
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        captured = []
        async def download(command, payload=None):
            captured.append((EXEC_CONTAINER.get(), command))
            return buffer.getvalue()
        env._stream_transfer = download
        target = self.root / "download.bin"
        await env.service_download_file("/data/artifact.bin", target, service="db")
        self.assertEqual(target.read_bytes(), data)
        self.assertEqual(captured[0], ("db", ["tar", "cf", "-", "-C", "/data", "artifact.bin"]))
        self.assertEqual(EXEC_CONTAINER.get(), "main")

    async def test_networkpolicy_created_before_pod_and_dynamic_replace(self):
        env = self.make_env(network_enforcement="networkpolicy", pod_overrides={"metadata": {"labels": {POLICY_LABEL: "wrong"}}})
        env._ensure_client = AsyncMock()
        operations = []
        env._policy_api = NS(create_namespaced_network_policy=lambda **kw: operations.append(("policy", kw)),
                             read_namespaced_network_policy=lambda **kw: NS(metadata=NS(resource_version="7")),
                             replace_namespaced_network_policy=lambda **kw: operations.append(("replace", kw)))
        async def start(*args, **kwargs):
            operations.append(("pod", env.pod_overrides))
        with patch.object(ACKEnvironment, "start", start):
            await env.start()
        self.assertEqual([op[0] for op in operations], ["policy", "pod"])
        self.assertEqual(env.pod_overrides["metadata"]["labels"][POLICY_LABEL], env.pod_name)
        await env.set_network_policy(NetworkPolicy(network_mode="no-network"))
        body = operations[-1][1]["body"]
        self.assertEqual(body["spec"]["egress"], [])
        self.assertEqual(body["metadata"]["resourceVersion"], "7")
        self.assertEqual(env.network_policy.network_mode, NetworkMode.NO_NETWORK)

    async def test_policy_failure_prevents_pod_start(self):
        env = self.make_env(network_enforcement="networkpolicy")
        env._ensure_client = AsyncMock()
        env._policy_api = NS(create_namespaced_network_policy=Mock(side_effect=ApiException(status=403)))
        with patch.object(ACKEnvironment, "start", AsyncMock()) as start:
            with self.assertRaises(ApiException):
                await env.start()
            start.assert_not_awaited()

    async def test_invalid_root_or_resource_budget_fails_before_client_and_pod(self):
        for spec in ({"securityContext": {"runAsUser": 1000}},
                     {"containers": [{"resources": {"limits": {"cpu": "500m"}}}]}):
            with self.subTest(spec=spec):
                env = self.make_env(network_enforcement="networkpolicy", pod_overrides={"spec": spec})
                env._ensure_client = AsyncMock()
                with patch.object(ACKEnvironment, "start", AsyncMock()) as start:
                    with self.assertRaises(ValueError):
                        await env.start()
                env._ensure_client.assert_not_awaited()
                start.assert_not_awaited()
                self.assertFalse(env._policy_created)

    async def test_policy_creation_drained_after_timeout_and_only_owned_policy_deleted(self):
        for collision in (False, True):
            with self.subTest(collision=collision):
                env = self.make_env(network_enforcement="networkpolicy")
                env._ensure_client = AsyncMock()
                env._core_api = NS(read_namespaced_pod=Mock(side_effect=ApiException(status=404)))
                entered, release, completed = threading.Event(), threading.Event(), threading.Event()
                events = []

                def create(**kwargs):
                    entered.set()
                    if not release.wait(timeout=1):
                        raise RuntimeError("test did not release policy creation")
                    completed.set()
                    events.append("collision" if collision else "created")
                    if collision:
                        raise ApiException(status=409)

                delete = Mock(side_effect=lambda **kwargs: events.append("deleted"))
                env._policy_api = NS(create_namespaced_network_policy=create, delete_namespaced_network_policy=delete)
                with patch.object(ACKEnvironment, "start", AsyncMock()) as start, patch.object(ACKEnvironment, "stop", AsyncMock()):
                    operation = asyncio.create_task(asyncio.wait_for(env.start(), timeout=0.05))
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                        # Complete after the startup timeout, but bound the test.
                        timer = asyncio.get_running_loop().call_later(0.1, release.set)
                        with self.assertRaises(TimeoutError):
                            await asyncio.wait_for(operation, timeout=2)
                        timer.cancel()
                        self.assertTrue(completed.is_set())
                        self.assertEqual(env._policy_created, not collision)
                        start.assert_not_awaited()
                        await env.stop()
                    finally:
                        release.set()
                        if not operation.done():
                            operation.cancel()
                            await asyncio.gather(operation, return_exceptions=True)
                self.assertEqual(events, ["collision"] if collision else ["created", "deleted"])
                self.assertEqual(delete.call_count, 0 if collision else 1)
                self.assertFalse(env._policy_created)

    async def test_stop_deletes_policy_only_after_pod_disappears(self):
        env = self.make_env(network_enforcement="networkpolicy")
        env._policy_created = True
        env._core_api = NS(read_namespaced_pod=Mock(side_effect=ApiException(status=404)))
        delete = Mock()
        env._policy_api = NS(delete_namespaced_network_policy=delete)
        with patch.object(ACKEnvironment, "stop", AsyncMock()):
            await env.stop(delete=True)
        delete.assert_called_once()
        self.assertFalse(env._policy_created)

    async def test_retained_pod_keeps_policy(self):
        env = self.make_env(network_enforcement="networkpolicy")
        env._policy_created = True
        delete = Mock()
        env._policy_api = NS(delete_namespaced_network_policy=delete)
        with patch.object(ACKEnvironment, "stop", AsyncMock()):
            await env.stop(delete=False)
        delete.assert_not_called()
        self.assertTrue(env._policy_created)

    async def test_stop_service_checks_termination_even_when_exec_closed(self):
        env = self.mock_client(self.make_env())
        env.exec = AsyncMock(return_value=ExecResult(return_code=1, stderr="stream closed"))
        env._core_api = NS(read_namespaced_pod=lambda **kwargs: pod_status(terminated=True))
        await env.stop_service("main")
        self.assertEqual(env.exec.await_args.args[0], _STOP_MAIN_SCRIPT)
        self.assertIn("main", env._stopped_services)

    async def test_stop_main_with_inert_pid_one_verifies_children_and_disables_exec(self):
        env = self.mock_client(self.make_env())
        env.exec = AsyncMock(return_value=ExecResult(return_code=0, stdout="HBENCH_WORKLOAD_STOPPED\n"))
        env._core_api = NS(read_namespaced_pod=Mock())
        await env.stop_service("main")
        env._core_api.read_namespaced_pod.assert_not_called()
        self.assertIn("main", env._stopped_services)
        check = subprocess.run(["bash", "-n"], input=_STOP_MAIN_SCRIPT, text=True, capture_output=True)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn('/proc/1/cmdline', _STOP_MAIN_SCRIPT)
        self.assertIn('Main workload processes survived termination', _STOP_MAIN_SCRIPT)

    async def test_stop_service_does_not_claim_running_container_stopped(self):
        env = self.mock_client(self.make_env())
        env.exec = AsyncMock(return_value=ExecResult(return_code=1, stderr="permission denied"))
        env._core_api = NS(read_namespaced_pod=lambda **kwargs: pod_status())
        with patch("hbench.environment.time.monotonic", side_effect=[0, 1, 31]), patch("hbench.environment.asyncio.sleep", AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, "did not terminate"):
                await env.stop_service("main")
        self.assertNotIn("main", env._stopped_services)

    async def test_readiness_retries_cold_pull_and_checks_exec(self):
        env = self.mock_client(self.make_env(pod_ready_timeout_sec=1200))
        pods = [pod_status("Pending", ready=False, image_pull=True), pod_status()]
        env._core_api = NS(read_namespaced_pod=Mock(side_effect=pods))
        env.exec = AsyncMock(return_value=ExecResult(return_code=0))
        with patch("hbench.environment.asyncio.sleep", AsyncMock()):
            await env._wait_for_pod_ready()
        env.exec.assert_awaited_once()
        self.assertEqual(env._timeout(), 1200)

    async def test_failed_pod_raises_instead_of_retrying(self):
        env = self.mock_client(self.make_env())
        env._core_api = NS(read_namespaced_pod=lambda **kwargs: pod_status("Failed", terminated=True))
        with self.assertRaisesRegex(RuntimeError, "Pod failed"):
            await env._wait_for_pod_ready()

    async def test_exec_unknown_status_and_transfer_failure_are_not_success(self):
        env = self.mock_client(self.make_env())
        class Response:
            returncode = None
            def is_open(self): return False
            def close(self): pass
        with patch("hbench.environment.stream", return_value=Response()):
            result = await env.exec("true")
        self.assertEqual(result.return_code, 1)
        env._request_sync = Mock(return_value=(b"", b"tar: missing file", 2))
        with self.assertRaisesRegex(RuntimeError, "Tar transfer failed"):
            await env._stream_transfer(["tar", "cf", "-", "missing"])


class TransferTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunked_roundtrip_binary_nested_and_empty_dirs(self):
        class LocalTransfer(TransferMixin):
            transfer = "chunked"
            async def exec(self, command, **kwargs):
                argv = shlex.split(command)
                argv[0] = sys.executable
                process = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                out, err = await process.communicate()
                return ExecResult(stdout=out.decode(), stderr=err.decode(), return_code=process.returncode)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            (source / "empty").mkdir()
            data = random.Random(123).randbytes(150000)  # Incompressible: exercises multiple four-chunk batches.
            (source / "binary").write_bytes(data)
            transport = LocalTransfer()
            await transport.upload_dir(source, str(root / "remote"))
            await transport.download_dir(str(root / "remote"), root / "download")
            self.assertEqual((root / "download" / "binary").read_bytes(), data)
            self.assertTrue((root / "download" / "empty").is_dir())
            await transport.upload_file(source / "binary", str(root / "remote" / "renamed"))
            await transport.download_file(str(root / "remote" / "renamed"), root / "renamed-download")
            self.assertEqual((root / "renamed-download").read_bytes(), data)
        self.assertEqual((CHUNK_BYTES, BATCH_SIZE), (24576, 4))

    async def test_chunked_truncation_and_checksum_rejected(self):
        class BadTransfer(TransferMixin):
            transfer = "chunked"
            def __init__(self, truncated): self.truncated = truncated
            async def _transfer_python(self, code, *args):
                if "json.dumps" in code:
                    return json.dumps({"bytes": 3, "sha256": "bad"})
                if "b64encode" in code:
                    return base64.b64encode(b"xx" if self.truncated else b"xxx").decode()
                return ""
        for truncated, message in [(True, "Truncated"), (False, "checksum")]:
            with self.subTest(truncated=truncated), self.assertRaisesRegex(RuntimeError, message):
                await BadTransfer(truncated)._download_archive("/artifact", True)

    async def test_batch_drains_before_error_cleanup(self):
        events = []
        async def failed():
            events.append("failed")
            raise RuntimeError("chunk failed")
        async def pending():
            await asyncio.sleep(0)
            events.append("drained")
        with self.assertRaisesRegex(RuntimeError, "chunk failed"):
            await transfer_batch([failed(), pending()])
        self.assertEqual(events, ["failed", "drained"])

    async def test_tar_path_escape_rejected(self):
        class UnsafeTransfer(TransferMixin):
            async def _download_archive(self, *args):
                buffer = io.BytesIO()
                with tarfile.open(fileobj=buffer, mode="w") as archive:
                    member = tarfile.TarInfo("../escape")
                    member.size = 1
                    archive.addfile(member, io.BytesIO(b"x"))
                return buffer.getvalue()
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(tarfile.TarError):
            await UnsafeTransfer().download_dir("/tmp", Path(temp) / "target")


if __name__ == "__main__":
    unittest.main()
