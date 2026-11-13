from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from hbench.datasets import (
    TAU3_MAIN_IMAGE,
    TAU3_RUNTIME_IMAGE,
    dockerfile_base_image,
    file_sha256,
    prepare,
    source_hashes,
    task_directories,
)

ROOT = Path(__file__).resolve().parents[2]
PINS = {
    "tau3-bench": (375, "sha256:a57304f682894ac061090769af771a3617664f3ff6e5417d4eadf8e30433e4d9"),
    "swe-bench-verified": (500, "sha256:b934b0cc3dc800fe945eaf9f1623329db97ee3133c706d20644524c7759fb341"),
    "terminal-bench": (66, "sha256:5881c1b1c74a96f07baef00a7c869e09802704bc0dc912c870e7acccc11440da"),
}


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.task = self.source / "example-task"
        (self.task / "environment").mkdir(parents=True)
        (self.task / "tests/fixtures").mkdir(parents=True)
        (self.task / "tests/test.sh").write_bytes(b"#!/bin/sh\nexit 0\n")
        (self.task / "tests/fixtures/data.bin").write_bytes(b"\x00\xfffixture\n")
        (self.task / "instruction.md").write_text("Original instruction\n" * 200)
        (self.task / "task.toml").write_text(
            '# preserve comments\nschema_version = "1.1"\n[task]\nname = "example"\n'
            '[verifier]\ntimeout_sec = 20\n[environment]\ncpus = 2\n'
            '[environment.env]\nTOKEN = "${TOKEN}"\n'
        )
        self.output = self.root / "prepared"

    def profile(self, name):
        return {"name": name, "package": name, "version": PINS[name][1], "expected_tasks": 1}

    def test_swe_prebuilt_and_full_original_hashes(self):
        (self.task / "environment/Dockerfile").write_text(
            '# FROM fake:latest\nFROM swebench/sweb.eval.x86_64.example:latest\nWORKDIR /testbed\n'
        )
        before = source_hashes(self.task)
        manifest = prepare(self.profile("swe-bench-verified"), self.source, self.output)
        self.assertEqual(before, source_hashes(self.task))
        self.assertEqual(before, manifest["tasks"][0]["source_hashes"])
        config = tomllib.loads((self.output / self.task.name / "task.toml").read_text())
        self.assertEqual(config["environment"]["docker_image"], "swebench/sweb.eval.x86_64.example:latest")
        self.assertEqual(config["environment"]["workdir"], "/testbed")
        self.assertEqual(config["environment"]["env"], {"TOKEN": "${TOKEN}"})
        self.assertIn("# preserve comments", (self.output / self.task.name / "task.toml").read_text())
        for relative in ("instruction.md", "tests/test.sh", "tests/fixtures/data.bin", "environment/Dockerfile"):
            self.assertEqual(before[relative], file_sha256(self.output / self.task.name / relative))
        self.assertFalse(manifest["official_score"])
        self.assertEqual(manifest, json.loads((self.output / "manifest.json").read_text()))

    def test_tau3_preserves_compose_settings_and_selects_exact_task(self):
        (self.task / "environment/Dockerfile").write_text("FROM python:3.12-slim\nWORKDIR /app\n")
        original = {"services": {"main": {"depends_on": {"tau3-runtime": {"condition": "service_healthy"}}}, "tau3-runtime": {
            "build": {"context": "./runtime-server"}, "command": ["python3", "server.py"],
            "environment": ["OPENAI_API_KEY=${OPENAI_API_KEY:-}"],
            "volumes": ["${HOST_AGENT_LOGS_PATH}:${ENV_AGENT_LOGS_PATH}"],
            "healthcheck": {"test": ["CMD", "true"], "interval": "2s"}, "expose": ["8000"],
        }}}
        (self.task / "environment/docker-compose.yaml").write_text(yaml.safe_dump(original))
        before = source_hashes(self.task)
        prepare(self.profile("tau3-bench"), self.source, self.output)
        self.assertEqual(before, source_hashes(self.task))
        result = yaml.safe_load((self.output / self.task.name / "environment/docker-compose.yaml").read_text())
        runtime = result["services"]["tau3-runtime"]
        self.assertNotIn("build", runtime)
        self.assertEqual(runtime["image"], TAU3_RUNTIME_IMAGE)
        self.assertEqual(runtime["command"], ["sh", "-c", "cp /app/tasks/example-task.json /app/task_config.json && exec python3 /app/server.py"])
        for key in ("environment", "volumes", "healthcheck", "expose"):
            self.assertEqual(runtime[key], original["services"]["tau3-runtime"][key])
        self.assertEqual(result["services"]["main"], original["services"]["main"])
        config = tomllib.loads((self.output / self.task.name / "task.toml").read_text())
        self.assertEqual(config["environment"]["docker_image"], TAU3_MAIN_IMAGE)
        self.assertEqual(config["environment"]["workdir"], "/app")

    def test_tau3_copy_failure_does_not_start_server(self):
        (self.task / "environment/Dockerfile").write_text("FROM python:3.12-slim\nWORKDIR /app\n")
        (self.task / "environment/docker-compose.yaml").write_text(
            yaml.safe_dump({"services": {"tau3-runtime": {}}})
        )
        prepare(self.profile("tau3-bench"), self.source, self.output)
        compose = yaml.safe_load((self.output / self.task.name / "environment/docker-compose.yaml").read_text())
        command = compose["services"]["tau3-runtime"]["command"]
        app = self.root / "local app"
        (app / "tasks").mkdir(parents=True)
        config = app / "task_config.json"
        config.write_text('{"stale": true}')
        marker = app / "server-started"
        (app / "server.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('started')\n"
        )
        # Execute the generated shell logic locally; only relocate /app and Python.
        script = command[2].replace("/app/", shlex.quote(str(app)) + "/")
        script = script.replace("exec python3 ", f"exec {shlex.quote(sys.executable)} ")
        result = subprocess.run([*command[:2], script], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("example-task.json", result.stderr)
        self.assertFalse(marker.exists(), "Server started after the task config copy failed")
        self.assertEqual(config.read_text(), '{"stale": true}')

    def test_terminal_keeps_all_originals_and_records_main_entrypoint(self):
        with (self.task / "task.toml").open("a") as stream:
            stream.write('\n[verifier.environment]\ndocker_image = "verifier@sha256:' + "2" * 64 + '"\n')
        text = (self.task / "task.toml").read_text().replace("cpus = 2", 'cpus = 2\ndocker_image = "main@sha256:' + "1" * 64 + '"')
        (self.task / "task.toml").write_text(text)
        (self.task / "environment/Dockerfile").write_text('FROM ubuntu:24.04\nENTRYPOINT \\\n ["/app/entrypoint.sh"]\n')
        (self.task / "environment/sidecar").mkdir()
        (self.task / "environment/sidecar/Dockerfile").write_text('FROM ubuntu:24.04\nENTRYPOINT ["different"]\n')
        (self.task / "hbench.json").write_text('{"existing": true}')
        before = source_hashes(self.task)
        prepare(self.profile("terminal-bench"), self.source, self.output)
        self.assertEqual(before, source_hashes(self.task))
        metadata = json.loads((self.output / self.task.name / "hbench.json").read_text())
        self.assertEqual(metadata, {"existing": True, "main_entrypoint": ["/app/entrypoint.sh"]})
        for relative in before:
            if relative != "hbench.json":
                self.assertEqual(before[relative], file_sha256(self.output / self.task.name / relative))

    def test_existing_output_and_wrong_count_fail_without_changes(self):
        self.output.mkdir()
        marker = self.output / "keep"
        marker.write_text("untouched")
        with self.assertRaises(FileExistsError):
            prepare(self.profile("terminal-bench"), self.source, self.output)
        self.assertEqual(marker.read_text(), "untouched")
        profile = self.profile("terminal-bench") | {"expected_tasks": 2}
        with self.assertRaises(ValueError):
            prepare(profile, self.source, self.root / "absent")
        self.assertFalse((self.root / "absent").exists())

    def test_copy_failure_cleans_partial_output_without_source_changes(self):
        before = source_hashes(self.task)
        copyfile = shutil.copyfile

        def fail_after_copy(src, dst, **kwargs):
            result = copyfile(src, dst, **kwargs)
            if Path(src).name == "data.bin":
                self.assertEqual(Path(dst).read_bytes(), (self.task / "tests/fixtures/data.bin").read_bytes())
                raise OSError("injected copy failure")
            return result

        with patch("hbench.datasets.shutil.copyfile", side_effect=fail_after_copy) as copy:
            with self.assertRaisesRegex(shutil.Error, "injected copy failure"):
                prepare(self.profile("terminal-bench"), self.source, self.output)
        self.assertTrue(copy.called)
        self.assertFalse(self.output.exists())
        self.assertEqual(before, source_hashes(self.task))

    def test_concurrently_created_output_is_not_copied_or_removed(self):
        before = source_hashes(self.task)
        marker = self.output / "keep"

        def create_competing_output(task):
            hashes = source_hashes(task)
            self.output.mkdir()
            marker.write_text("concurrent output")
            return hashes

        with patch("hbench.datasets.source_hashes", side_effect=create_competing_output):
            with patch("hbench.datasets.shutil.copytree", wraps=shutil.copytree) as copy:
                with patch("hbench.datasets.shutil.rmtree") as remove:
                    with self.assertRaises(FileExistsError):
                        prepare(self.profile("terminal-bench"), self.source, self.output)
        copy.assert_not_called()
        remove.assert_not_called()
        self.assertEqual(marker.read_text(), "concurrent output")
        self.assertEqual(before, source_hashes(self.task))

    def test_invalid_adaptation_cleans_only_fresh_output(self):
        (self.task / "environment/Dockerfile").write_text("FROM ${UNKNOWN}\n")
        before = source_hashes(self.task)
        with self.assertRaises(ValueError):
            prepare(self.profile("swe-bench-verified"), self.source, self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual(before, source_hashes(self.task))

    def test_overlap_and_unpinned_profiles_rejected(self):
        with self.assertRaises(ValueError):
            prepare(self.profile("tau3-bench"), self.source, self.source / "child")
        with self.assertRaises(ValueError):
            prepare(self.profile("tau3-bench") | {"version": "latest"}, self.source, self.output)
        with self.assertRaises(ValueError):
            prepare(self.profile("tau3-bench") | {"name": "unknown"}, self.source, self.output)

    def test_from_parser_rejects_local_build_stages(self):
        path = self.task / "environment/Dockerfile"
        path.write_text("FROM ubuntu:24.04 AS base\nFROM base\n")
        with self.assertRaises(ValueError):
            dockerfile_base_image(path)


class CachedDatasetTests(unittest.TestCase):
    def test_all_cached_task_counts_and_prebuilt_inputs(self):
        for name, (expected, _) in PINS.items():
            dataset = ROOT / "artifacts/hub" / name / name
            if not dataset.exists():
                continue
            tasks = task_directories(dataset)
            self.assertEqual(len(tasks), expected, name)
            for task in tasks:
                config = tomllib.loads((task / "task.toml").read_text())
                if name == "terminal-bench":
                    self.assertIn("@sha256:", config["environment"]["docker_image"])
                elif name == "swe-bench-verified":
                    self.assertTrue(dockerfile_base_image(task / "environment/Dockerfile").startswith("swebench/"))
                else:
                    self.assertTrue((task / "environment/runtime-server/task_config.json").is_file())

    def test_prepare_real_cached_small_samples_without_source_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            for name, (_, pin) in PINS.items():
                dataset = ROOT / "artifacts/hub" / name / name
                if not dataset.exists():
                    continue
                task = dataset / "kv-live-surgery" if name == "terminal-bench" else task_directories(dataset)[0]
                before = source_hashes(task)
                sample = temporary / name / "source"
                shutil.copytree(task, sample / task.name)
                output = temporary / name / "prepared"
                manifest = prepare({"name": name, "package": name, "version": pin, "expected_tasks": 1}, sample, output)
                self.assertEqual(manifest["task_count"], 1)
                self.assertEqual(before, manifest["tasks"][0]["source_hashes"])
                self.assertEqual(before, source_hashes(task))
                if name == "terminal-bench":
                    metadata = json.loads((output / task.name / "hbench.json").read_text())
                    self.assertEqual(metadata["main_entrypoint"], ["/app/entrypoint.sh"])
                    self.assertEqual(before["task.toml"], file_sha256(output / task.name / "task.toml"))
                for relative, digest in before.items():
                    if relative == "instruction.md" or relative.startswith("tests/"):
                        self.assertEqual(digest, file_sha256(output / task.name / relative))


if __name__ == "__main__":
    unittest.main()
