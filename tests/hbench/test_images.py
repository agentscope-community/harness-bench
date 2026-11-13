from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hbench.datasets import RUNTIME_IMAGE, TAU3_MAIN_IMAGE, TAU3_RUNTIME_IMAGE
from hbench.images import FASTMCP_VERSION, ROOT, TAU2_COMMIT, build, image_inventory, mirror


class ImageTests(unittest.TestCase):
    def setUp(self):
        run = patch("subprocess.run")
        self.run = run.start()
        self.addCleanup(run.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.dataset = self.root / "dataset"
        self.task = self.dataset / "tau3-fixture"
        (self.task / "environment/runtime-server").mkdir(parents=True)
        (self.task / "tests").mkdir()
        (self.task / "instruction.md").write_text("original\n")
        (self.task / "task.toml").write_text('[environment]\ndocker_image = "main:1"\n[verifier.environment]\ndocker_image = "verifier:1"\n')
        (self.task / "environment/Dockerfile").write_text("FROM python:3.12-slim\nWORKDIR /app\n")
        (self.task / "environment/runtime-server/Dockerfile").write_text("FROM python:3.12-slim\n")
        (self.task / "environment/runtime-server/server.py").write_text("print('server')\n")
        (self.task / "environment/runtime-server/task_config.json").write_text('{"id": 1}\n')
        (self.task / "environment/docker-compose.yaml").write_text('services:\n  main: {}\n  tau3-runtime:\n    build: ./runtime-server\n  redis:\n    image: redis:7@sha256:' + "a" * 64 + "\n")

    def test_inventory_main_verifier_sidecars_digest_and_runtime(self):
        result = image_inventory(self.dataset)
        self.assertEqual(result, sorted(["main:1", "verifier:1", "redis:7@sha256:" + "a" * 64, TAU3_RUNTIME_IMAGE, RUNTIME_IMAGE]))
        (self.dataset / "manifest.json").write_text('{"runtime_image": "registry/org/java:2"}')
        self.assertIn("registry/org/java:2", image_inventory(self.dataset))
        self.assertNotIn(RUNTIME_IMAGE, image_inventory(self.dataset))

    def test_inventory_reads_swe_from_without_building(self):
        (self.task / "task.toml").write_text("[environment]\ncpus = 1\n")
        (self.task / "environment/runtime-server/server.py").unlink()
        (self.task / "environment/docker-compose.yaml").unlink()
        (self.task / "environment/Dockerfile").write_text("FROM swebench/sweb.eval.x86_64.example:latest\n")
        self.assertEqual(image_inventory(self.dataset), sorted([RUNTIME_IMAGE, "swebench/sweb.eval.x86_64.example:latest"]))

    def test_mirror_plan_exact_maps_no_collisions_no_execution(self):
        digest = "sha256:" + "a" * 64
        sources = ["one.example/org/same:1", "two.example/org/same:1", "redis:7@" + digest]
        with patch("hbench.images.subprocess.run") as run:
            plan = mirror(sources + sources, "registry.example.com/team")
        run.assert_not_called()
        self.assertEqual(set(plan["image_map"]), set(sources))
        self.assertEqual(len(set(plan["image_map"].values())), 3)
        self.assertTrue(plan["image_map"][sources[-1]].endswith("@" + digest))
        self.assertEqual(len(plan["commands"]), 6)
        for command in plan["commands"][::2]:
            self.assertEqual(command[:4], ["skopeo", "copy", "--all", "--preserve-digests"])
        self.assertFalse(plan["executed"])
        self.assertEqual(plan, mirror(list(reversed(sources)), "registry.example.com/team"))

    def test_mirror_execute_verifies_digest_and_pins_tag(self):
        raw = b'{"schemaVersion":2}'
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        source = "source/image:1@" + digest
        with patch("hbench.images.subprocess.run", side_effect=[subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0, stdout=raw)]) as run:
            plan = mirror([source], "registry.example.com/team", execute=True)
        self.assertEqual(run.call_count, 2)
        self.assertTrue(plan["executed"])
        self.assertTrue(plan["image_map"][source].endswith("@" + digest))
        with patch("hbench.images.subprocess.run", side_effect=[subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0, stdout=raw)]):
            plan = mirror(["source/image:1"], "registry.example.com/team", execute=True)
        self.assertTrue(plan["image_map"]["source/image:1"].endswith("@" + digest))

    def test_mirror_mismatch_and_bad_references(self):
        with patch("hbench.images.subprocess.run", side_effect=[subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0, stdout=b"different")]):
            with self.assertRaises(ValueError):
                mirror(["source/image@sha256:" + "a" * 64], "registry.example.com/team", execute=True)
        for registry in ("https://registry/team", "registry", "registry/team:bad", "registry/team bad"):
            with self.assertRaises(ValueError):
                mirror(["source/image"], registry)
        for source in ("source/image@sha256:bad", "${IMAGE}", "--option", "image;true"):
            with self.assertRaises(ValueError):
                mirror([source], "registry.example.com/team")

    def test_build_stages_assets_and_returns_nonexecuting_plan(self):
        with patch("hbench.images.subprocess.run") as run:
            plan = build("registry/org/java:1", "registry/org/tau3", self.dataset, output=self.root / "build")
        run.assert_not_called()
        self.assertFalse(plan["executed"])
        self.assertEqual(plan["images"]["tau3_main"], "registry/org/tau3-main:0.1.0")
        context = Path(plan["contexts"]["tau3"])
        self.assertTrue(context.name.startswith("tau3-"))
        self.assertEqual((context / "tasks/tau3-fixture.json").read_bytes(), (self.task / "environment/runtime-server/task_config.json").read_bytes())
        self.assertEqual((context / "server.py").read_bytes(), (self.task / "environment/runtime-server/server.py").read_bytes())
        self.assertEqual(plan["metadata"]["tau2_commit"], TAU2_COMMIT)
        self.assertEqual(plan["metadata"]["fastmcp_version"], FASTMCP_VERSION)
        self.assertEqual(plan["commands"][0][3:], ["clean", "package", "dependency:copy-dependencies", "-DincludeScope=runtime"])
        self.assertTrue(all("push" not in command for command in plan["commands"]))
        self.assertEqual(len(plan["commands"]), 4)
        self.assertEqual(plan["image_map"], {RUNTIME_IMAGE: "registry/org/java:1",
                                           TAU3_MAIN_IMAGE: "registry/org/tau3-main:0.1.0",
                                           TAU3_RUNTIME_IMAGE: "registry/org/tau3-runtime:0.1.0"})
        self.assertEqual(Path(plan["plan_path"]), context / "build-plan.json")
        self.assertEqual(plan, json.loads(Path(plan["plan_path"]).read_text()))
        again = build("registry/org/java:1", "registry/org/tau3", self.dataset, output=self.root / "build")
        self.assertEqual(plan["contexts"], again["contexts"])

    def test_runtime_default_and_runtime_only_plan(self):
        self.assertEqual(RUNTIME_IMAGE, "hbench/harness-runtime:0.1.0")
        for runtime_image in (RUNTIME_IMAGE, "registry.example/team/runtime:published"):
            with self.subTest(runtime_image=runtime_image), patch("hbench.images._tau3_context") as tau3:
                plan = build(runtime_image=runtime_image, dataset=None, output=self.root / "runtime-only")
            tau3.assert_not_called()
            self.run.assert_not_called()
            self.assertEqual(plan["images"], {"runtime": runtime_image})
            self.assertEqual(plan["image_map"], {RUNTIME_IMAGE: runtime_image})
            self.assertIsNone(plan["contexts"]["tau3"])
            self.assertEqual(plan["metadata"], {})
            self.assertEqual(plan["commands"], [
                ["mvn", "-f", str(ROOT / "adapters/agentscope-java/pom.xml"), "clean", "package",
                 "dependency:copy-dependencies", "-DincludeScope=runtime"],
                ["docker", "build", "-f", str(ROOT / "environments/hbench/Dockerfile.runtime"),
                 "-t", runtime_image, str(ROOT)],
            ])
            self.assertFalse(plan["executed"])
            plan_path = Path(plan["plan_path"])
            self.assertEqual(plan_path.parent, self.root / "runtime-only")
            self.assertTrue(plan_path.name.startswith("runtime-"))
            self.assertEqual(plan, json.loads(plan_path.read_text()))

    def test_runtime_only_execute_is_mocked_and_plan_updated(self):
        plan = build(execute=True, output=self.root / "runtime-only")
        self.assertTrue(plan["executed"])
        self.assertEqual([call.args[0] for call in self.run.call_args_list], plan["commands"])
        self.assertEqual(self.run.call_count, 2)
        for call in self.run.call_args_list:
            self.assertEqual(call.kwargs, {"check": True, "cwd": ROOT})
        self.assertEqual(plan, json.loads(Path(plan["plan_path"]).read_text()))

    def test_build_execute_runs_maven_then_docker_only(self):
        original_is_file = Path.is_file
        runtime_dockerfile = ROOT / "environments/hbench/Dockerfile.runtime"

        def is_file(path):
            return path == runtime_dockerfile or original_is_file(path)

        with patch("hbench.images.subprocess.run") as run, patch("hbench.images.Path.is_file", autospec=True, side_effect=is_file):
            plan = build(dataset=self.dataset, execute=True, output=self.root / "build")
        self.assertTrue(plan["executed"])
        self.assertEqual([call.args[0] for call in run.call_args_list], plan["commands"])

    def test_build_rejects_nonuniform_or_modified_assets(self):
        other = self.dataset / "tau3-other"
        import shutil
        shutil.copytree(self.task, other)
        (other / "environment/runtime-server/server.py").write_text("different\n")
        with self.assertRaises(ValueError):
            build(dataset=self.dataset, output=self.root / "build")
        shutil.rmtree(other)
        plan = build(dataset=self.dataset, output=self.root / "build")
        (Path(plan["contexts"]["tau3"]) / "server.py").write_text("tampered\n")
        with self.assertRaises(ValueError):
            build(dataset=self.dataset, output=self.root / "build")

    def test_inventory_separate_verifier_and_compose_is_bounded(self):
        digest = "sha256:" + "b" * 64
        main = "registry.example/team/main:1@" + digest
        sidecar = "registry.example/team/sidecar:1@" + digest
        verifier = "registry.example/team/verifier:1@" + digest
        (self.task / "environment/runtime-server/server.py").unlink()
        (self.task / "task.toml").write_text(
            f'[environment]\ndocker_image = "{main}"\n[verifier]\nenvironment_mode = "separate"\n')
        (self.task / "environment/docker-compose.yaml").write_text(f'services:\n  sidecar:\n    image: {sidecar}\n')
        (self.task / "tests/Dockerfile").write_text(f"FROM {verifier}\n")
        self.assertEqual(image_inventory(self.dataset), sorted([RUNTIME_IMAGE, main, sidecar, verifier]))
        self.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
