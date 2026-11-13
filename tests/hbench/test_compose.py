import copy
from pathlib import Path
import tempfile
import unittest

from hbench.compose import healthcheck_probe, interpolate, load_compose, map_image, merge_pod_overrides, translate_compose
from hbench.runtime_mount import SETUP_READY, add_setup_gate, runtime_overrides


class ComposeTests(unittest.TestCase):
    def plan(self, services, **kwargs):
        return translate_compose([{"services": services}], main_image="task:1", **kwargs)

    def test_idle_main_keepalive_and_sidecar_image_defaults(self):
        for service in ({}, {"entrypoint": None}, {"command": None}, {"entrypoint": None, "command": None}):
            for explicit_main in (False, True):
                services = {"db": {"image": "postgres:16"}}
                if explicit_main:
                    services["main"] = service
                with self.subTest(service=service, explicit_main=explicit_main):
                    main, sidecar = self.plan(services).pod_overrides["spec"]["containers"]
                    self.assertIsNone(main["command"])
                    self.assertEqual(main["args"], ["sleep", "infinity"])
                    self.assertIsNone(sidecar["command"])
                    self.assertIsNone(sidecar["args"])

    def test_workload_main_preserves_entrypoint_and_cmd(self):
        for service in ({}, {"entrypoint": None}, {"command": None}, {"entrypoint": None, "command": None}):
            for explicit_main in (False, True):
                services = {"db": {"image": "postgres:16"}}
                if explicit_main:
                    services["main"] = service
                with self.subTest(service=service, explicit_main=explicit_main):
                    main, sidecar = self.plan(services, main_keepalive=False).pod_overrides["spec"]["containers"]
                    for container in (main, sidecar):
                        self.assertIsNone(container["command"])
                        self.assertIsNone(container["args"])

    def test_explicit_command_entrypoint_unchanged_by_keepalive(self):
        cases = [({"entrypoint": ["/app/entrypoint.sh"], "command": "serve --port 8080"},
                  ["/app/entrypoint.sh"], ["serve", "--port", "8080"]),
                 ({"command": ["serve"]}, None, ["serve"]),
                 ({"entrypoint": "/app/start --verbose"}, ["/app/start", "--verbose"], None),
                 ({"entrypoint": None, "command": "serve"}, None, ["serve"]),
                 ({"entrypoint": [], "command": None}, [], None),
                 ({"entrypoint": ""}, [], None),
                 ({"command": []}, None, []),
                 ({"command": ""}, None, [])]
        for keepalive in (True, False):
            for service, command, args in cases:
                with self.subTest(keepalive=keepalive, service=service):
                    main, sidecar = self.plan({"main": service, "db": {"image": "db", **service}},
                                              main_keepalive=keepalive).pod_overrides["spec"]["containers"]
                    for container in (main, sidecar):
                        self.assertEqual(container["command"], command)
                        self.assertEqual(container["args"], args)

    def test_runtime_and_compose_share_workload_keepalive_flag(self):
        for main_entrypoint in ([], ["/app/start"]):
            keepalive = not main_entrypoint
            for service in ({}, {"command": ["serve"]}):
                with self.subTest(main_entrypoint=main_entrypoint, service=service):
                    runtime = runtime_overrides(None, main_image="task:1", startup_env={}, main_keepalive=keepalive)
                    compose = self.plan({"main": service}, main_keepalive=keepalive).pod_overrides
                    main = merge_pod_overrides(runtime, compose)["spec"]["containers"][0]
                    self.assertIsNone(main["command"])
                    self.assertEqual(main["args"], ["serve"] if service else ["sleep", "infinity"] if keepalive else None)

    def test_interpolation_and_shell_dollars(self):
        self.assertEqual(interpolate("${SET}-${EMPTY:-fallback}-${ABSENT:-ok}-$$USER-$SET", {"SET": "v", "EMPTY": ""}), "v-fallback-ok-$USER-v")
        spec = self.plan({"main": {"environment": ["VALUE=${SET}", "DEFAULT=${MISSING:-yes}", "PASS"],
                                  "healthcheck": {"test": ["CMD-SHELL", 'test "$$VALUE" = "${SET}"']}}}, variables={"SET": "v", "PASS": "present"}, main_env={"TASK": "real"}).pod_overrides["spec"]
        env = {item["name"]: item["value"] for item in spec["containers"][0]["env"]}
        self.assertEqual(env, {"VALUE": "v", "DEFAULT": "yes", "PASS": "present", "TASK": "real"})
        self.assertEqual(spec["containers"][0]["readinessProbe"]["exec"]["command"], ["sh", "-c", 'test "$VALUE" = "v"'])

    def test_healthcheck_cmd_and_duration(self):
        probe = healthcheck_probe({"test": ["CMD", "redis-cli", "ping"], "interval": "500ms", "timeout": "2s", "start_period": "1m", "retries": 5})
        self.assertEqual(probe["exec"]["command"], ["redis-cli", "ping"])
        self.assertEqual((probe["periodSeconds"], probe["timeoutSeconds"], probe["initialDelaySeconds"], probe["failureThreshold"]), (1, 2, 60, 5))
        self.assertIsNone(healthcheck_probe({"test": ["NONE"]}))
        with self.assertRaises(ValueError):
            healthcheck_probe({"test": ["BOGUS", "true"]})

    def test_shared_localhost_ports_not_published_and_health_gate(self):
        plan = self.plan({"main": {"depends_on": {"db": {"condition": "service_healthy"}}, "ports": ["1234:1234"]},
                          "db": {"image": "db:1", "expose": [5432], "healthcheck": {"test": ["CMD", "true"]}}})
        self.assertEqual(plan.pod_overrides["spec"]["hostAliases"], [{"ip": "127.0.0.1", "hostnames": ["main", "db"]}])
        self.assertTrue(plan.warnings)
        self.assertNotIn("ports", plan.pod_overrides["spec"]["containers"][0])
        with self.assertRaisesRegex(ValueError, "no healthcheck"):
            self.plan({"main": {"depends_on": {"db": {"condition": "service_healthy"}}}, "db": {"image": "db"}})

    def test_rejects_postgres_target_overlap_across_all_port_syntaxes(self):
        declarations = [{"ports": [5432]}, {"ports": ["15432:5432"]},
                        {"ports": ["127.0.0.1:25432:5432/tcp"]},
                        {"ports": ["[::1]:35432:5432"]},
                        {"ports": [{"target": 5432, "published": "45432", "protocol": "tcp"}]},
                        {"expose": [5432]}, {"expose": ["5432/tcp"]},
                        {"expose": [{"target": "5432", "protocol": "TCP"}]}]
        for first in declarations:
            for second in declarations:
                with self.subTest(first=first, second=second), self.assertRaisesRegex(ValueError, "overlap.*db1.*db2"):
                    self.plan({"db1": {"image": "postgres:16", **first},
                               "db2": {"image": "postgres:16", **second}})
        with self.assertRaisesRegex(ValueError, "overlap.*main.*db"):
            self.plan({"main": {"ports": ["15432:5432"]}, "db": {"image": "db", "expose": [5432]}})

    def test_target_ranges_overlap_by_protocol_including_boundaries(self):
        cases = [({"ports": ["18000-18010:8000-8010"]}, {"expose": ["8010-8020"]}),
                 ({"expose": ["8000-8010"]}, {"ports": [{"target": "8005-8008"}]}),
                 ({"expose": [{"target": "8000-8010"}]}, {"ports": ["8000"]}),
                 ({"ports": [{"target": "1-65535"}]}, {"expose": [65535]}),
                 ({"ports": ["8000-8010/udp"]}, {"expose": [{"target": "8010-8020", "protocol": "UDP"}]}),
                 ({"expose": ["8000-8010/sctp"]}, {"ports": [{"target": 8005, "protocol": "sctp"}]})]
        for first, second in cases:
            with self.subTest(first=first, second=second), self.assertRaisesRegex(ValueError, "overlap"):
                self.plan({"one": {"image": "one", **first}, "two": {"image": "two", **second}})

    def test_distinct_targets_and_protocols_and_same_service_duplicates(self):
        cases = [({"ports": ["15432:5432"]}, {"ports": ["15432:5433"]}),
                 ({"ports": [{"target": 5432, "published": "5433"}]}, {"expose": [5433]}),
                 ({"expose": ["8000-8010"]}, {"ports": [{"target": "8011-8020"}]}),
                 ({"ports": ["8000-8010/tcp"]}, {"expose": ["8000-8010/udp"]}),
                 ({"expose": [{"target": 5432, "protocol": "udp"}]}, {"ports": ["5432/sctp"]}),
                 ({"expose": ["5432/sctp"]}, {"ports": [{"target": 5432, "protocol": "tcp"}]}),
                 ({"ports": [5432, "15432:5432", {"target": "5430-5440"}],
                   "expose": ["5432/tcp", {"target": 5432}]}, {}),
                 ({"ports": [{"target": 5432, "published": "10000-20000", "host_ip": "::1",
                              "name": "db", "app_protocol": "postgres", "mode": "host"}]}, {})]
        for first, second in cases:
            with self.subTest(first=first, second=second):
                spec = self.plan({"one": {"image": "one", **first}, "two": {"image": "two", **second}}).pod_overrides["spec"]
                for container in spec["containers"]:
                    self.assertNotIn("ports", container)
                    self.assertNotIn("expose", container)
        self.plan({"one": {"image": "postgres:16"}, "two": {"image": "postgres:16"}})  # Undeclared listeners cannot be detected.

    def test_port_validation_after_interpolation_and_service_overlay(self):
        documents = [{"services": {"main": {"expose": [5432]}, "db": {"image": "db", "expose": [5432]}}},
                     {"services": {"main": {"expose": ["${PORT}/udp"]}}}]
        pristine = copy.deepcopy(documents)
        plan = translate_compose(documents, main_image="task", variables={"PORT": "5432"})
        self.assertEqual(len(plan.pod_overrides["spec"]["containers"]), 2)
        self.assertEqual(documents, pristine)
        with self.assertRaisesRegex(ValueError, "overlap"):
            self.plan({"main": {"ports": ["${PUBLISHED}:${TARGET}"]}, "db": {"image": "db", "expose": [5432]}},
                      variables={"PUBLISHED": "15432", "TARGET": "5432"})

    def test_rejects_malformed_and_unsupported_port_declarations(self):
        invalid = [True, False, None, 0, -1, 65536, 1.5, [], "", "http", "0", "65536", "9999999999999999999999",
                   "9-8", "1-65536", "1-2-3", "1-", " 5432", "+5432", "5432/", "5432/http", "5432/tcp/udp",
                   {}, {"target": None}, {"target": True}, {"target": "5432/udp"}, {"target": "5-4"},
                   {"target": 5432, "protocol": None}, {"target": 5432, "protocol": "http"},
                   {"target": 5432, "unknown": True}]
        for field in ("ports", "expose"):
            for declaration in invalid:
                with self.subTest(field=field, declaration=declaration), self.assertRaisesRegex(ValueError, "Compose.*port"):
                    self.plan({"main": {field: [declaration]}})
            for declarations in ("5432", 5432, False, {}, (5432,)):
                with self.subTest(field=field, declarations=declarations), self.assertRaisesRegex(ValueError, "must be a list"):
                    self.plan({"main": {field: declarations}})
        invalid_ports = ["localhost:15432:5432", "127.0.0.999:15432:5432", "::1:15432:5432", "[::1]:5432",
                         "[bad]:15432:5432", "127.0.0.1:15432:5432:1234", ":5432", "15432:",
                         "15432:5432-5433", "15432-15433:5432", "0:5432",
                         {"published": "15432"}, {"target": 5432, "published": None},
                         {"target": 5432, "published": "5-4"}, {"target": 5432, "host_ip": "localhost"},
                         {"target": 5432, "host_ip": True}, {"target": 5432, "mode": "bridge"},
                         {"target": 5432, "name": []}, {"target": 5432, "app_protocol": 123}]
        for declaration in invalid_ports:
            with self.subTest(declaration=declaration), self.assertRaisesRegex(ValueError, "Compose.*port"):
                self.plan({"main": {"ports": [declaration]}})
        for declaration in ("15432:5432", {"target": 5432, "published": 15432}, {"target": 5432, "host_ip": "127.0.0.1"}):
            with self.subTest(declaration=declaration), self.assertRaisesRegex(ValueError, "Compose.*port"):
                self.plan({"main": {"expose": [declaration]}})

    def test_resources_capabilities_and_log_mounts(self):
        plan = self.plan({"main": {"cpus": 0.5, "shm_size": "256m", "cap_add": ["SYS_PTRACE"],
                                  "volumes": ["${HOST_AGENT_LOGS_PATH}:/logs/agent"]},
                          "db": {"image": "db", "volumes": [{"type": "bind", "source": "/host/log", "target": "/var/log/service", "read_only": True}]}},
                         variables={"HOST_AGENT_LOGS_PATH": "/host/log"}, log_sources={"/host/log": "/logs/agent"})
        spec = plan.pod_overrides["spec"]
        self.assertEqual(spec["containers"][0]["resources"]["limits"]["cpu"], "0.5")
        self.assertEqual(spec["volumes"][1]["emptyDir"], {"medium": "Memory", "sizeLimit": "256Mi"})
        self.assertEqual(spec["containers"][1]["volumeMounts"][0], {"name": "hbench-logs", "mountPath": "/var/log/service", "readOnly": True, "subPath": "agent"})

    def test_named_log_volume(self):
        plan = translate_compose([{"volumes": {"logs": None}, "services": {"main": {"volumes": ["logs:/logs/app"]}}}], main_image="task")
        self.assertEqual(plan.pod_overrides["spec"]["containers"][0]["volumeMounts"][0]["subPath"], "app")

    def test_reject_unsafe_and_unsupported(self):
        invalid = [{"network_mode": "host"}, {"privileged": True}, {"volumes": ["/tmp/data:/data"]},
                   {"volumes": ["anonymous"]}, {"devices": ["/dev/gpu"]}, {"env_file": ".env"},
                   {"depends_on": {"db": {"condition": "service_completed_successfully"}}},
                   {"user": "postgres"}, {"restart": "always"}, {"init": True}]
        for service in invalid:
            with self.subTest(service=service), self.assertRaises(ValueError):
                self.plan({"main": service, "db": {"image": "db"}})
        with self.assertRaisesRegex(ValueError, "prepared image"):
            self.plan({"sidecar": {"build": "."}})
        with self.assertRaises(ValueError):
            translate_compose([{"services": {}, "networks": {"external": {}}}], main_image="task")

    def test_build_with_prepared_image_is_not_executed(self):
        self.assertEqual(self.plan({"main": {"build": "."}, "tau3": {"build": ".", "image": "hbench/tau3-runtime:0.1.0", "command": ["--task-config", "selected.json"]}}).pod_overrides["spec"]["containers"][1]["args"], ["--task-config", "selected.json"])

    def test_overlays_merge_env_and_volume_targets(self):
        original = {"services": {"main": {"environment": ["ONE=1", "TWO=2"], "volumes": ["/logs/agent:/logs/a"]}}}
        pristine = copy.deepcopy(original)
        overlay = {"services": {"main": {"environment": {"TWO": 3}, "volumes": ["/logs/verifier:/logs/a"], "command": ["sleep", "infinity"]}}}
        main = translate_compose([original, overlay], main_image="task").pod_overrides["spec"]["containers"][0]
        self.assertEqual(main["env"], [{"name": "ONE", "value": "1"}, {"name": "TWO", "value": "3"}])
        self.assertEqual(main["volumeMounts"][0]["subPath"], "verifier")
        self.assertEqual(original, pristine)

    def test_merge_by_name_and_mount_path(self):
        original = {"spec": {"containers": [{"name": "main", "env": [{"name": "A", "value": "1"}], "volumeMounts": [{"name": "old", "mountPath": "/logs"}]}, {"name": "db", "image": "db"}],
                             "initContainers": [{"name": "first", "image": "first"}], "volumes": [{"name": "logs", "emptyDir": {}}]}}
        override = {"spec": {"containers": [{"name": "db", "env": [{"name": "B", "value": "2"}]}, {"securityContext": {"runAsUser": 1000}, "env": [{"name": "A", "value": "3"}, {"name": "C", "value": "4"}], "volumeMounts": [{"name": "logs", "mountPath": "/logs"}]}],
                             "initContainers": [{"name": "second", "image": "second"}], "volumes": [{"name": "runtime", "emptyDir": {}}]}}
        merged = merge_pod_overrides(original, override)["spec"]
        self.assertEqual([c["name"] for c in merged["containers"]], ["main", "db"])
        self.assertNotIn("securityContext", merged["containers"][1])
        self.assertEqual(merged["containers"][0]["env"], [{"name": "A", "value": "3"}, {"name": "C", "value": "4"}])
        self.assertEqual(len(merged["containers"][0]["volumeMounts"]), 1)
        self.assertEqual(len(merged["initContainers"]), 2)
        self.assertEqual(len(merged["volumes"]), 2)
        self.assertEqual(original["spec"]["containers"][0]["env"][0]["value"], "1")
        with self.assertRaises(ValueError):
            merge_pod_overrides({}, {"spec": {"containers": [{"name": "main"}, {"name": "main"}]}})

    def test_image_prefix_mapping(self):
        self.assertEqual(map_image("hbench/tau3-runtime:0.1.0", {"hbench/": "registry/base/", "hbench/tau3": "registry/specific/tau3"}), "registry/specific/tau3-runtime:0.1.0")
        main = self.plan({"main": {}}, image_map={"task": "task/mirror"}).pod_overrides["spec"]["containers"][0]
        self.assertEqual(main["image"], "task:1")  # Already-resolved main is not mapped twice.

    def test_service_network_mode_is_shared_without_host_network(self):
        plan = self.plan({"main": {"network_mode": "service:api"}, "api": {"image": "api"}})
        self.assertNotIn("hostNetwork", plan.pod_overrides["spec"])
        with self.assertRaises(ValueError):
            self.plan({"main": {"network_mode": "service:missing"}})

    def test_read_primary_and_extra(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "docker-compose.yaml").write_text("services:\n  main:\n    image: task\n")
            (root / "extra.yaml").write_text("services:\n  db:\n    image: db\n")
            self.assertEqual(len(load_compose(root, [root / "extra.yaml"])), 2)

    def test_service_name_collision(self):
        with self.assertRaises(ValueError):
            self.plan({"a_b": {"image": "a"}, "a-b": {"image": "b"}})

    def test_runtime_mount_and_setup_gate_preserve_entrypoint(self):
        runtime = runtime_overrides("runtime:1", main_image="task", startup_env={"SELECTED": "task"})
        main = runtime["spec"]["containers"][0]
        self.assertIsNone(main["command"])
        self.assertEqual([i["name"] for i in runtime["spec"]["initContainers"]], ["hbench-logs-init", "hbench-runtime-init"])
        self.assertTrue(all(not item["securityContext"]["privileged"] for item in runtime["spec"]["initContainers"]))
        original = {"name": "main", "command": None, "args": ["serve"], "securityContext": {"runAsUser": 1000},
                    "readinessProbe": {"tcpSocket": {"port": 8080}},
                    "lifecycle": {"postStart": {"exec": {"command": ["sh", "-c", "setup"]}}}}
        gated = add_setup_gate(original, 1200)
        self.assertIsNone(gated["command"])
        self.assertEqual(gated["args"], ["serve"])
        self.assertEqual(gated["readinessProbe"], original["readinessProbe"])
        self.assertIn(SETUP_READY, gated["lifecycle"]["postStart"]["exec"]["command"][2])
        self.assertEqual(gated["startupProbe"]["failureThreshold"], 1200)
        self.assertNotIn("startupProbe", original)


if __name__ == "__main__":
    unittest.main()
