"""Image inventory, digest-preserving mirrors, and opt-in build plans."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import yaml

from hbench.datasets import (
    RUNTIME_IMAGE,
    TAU3_MAIN_IMAGE,
    TAU3_RUNTIME_IMAGE,
    dockerfile_base_image,
    file_sha256,
    task_directories,
)

ROOT = Path(__file__).resolve().parents[1]
# Resolved read-only with git ls-remote of the original Dockerfile's repository.
TAU2_COMMIT = "c06b07dd7c4d4ec61ba6ff9bcdf0b15dd7ef36d4"
FASTMCP_VERSION = "3.0.2"


def _configured_images(value: Any) -> set[str]:
    images: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "docker_image" and isinstance(item, str) and item:
                images.add(item)
            else:
                images.update(_configured_images(item))
    elif isinstance(value, list):
        for item in value:
            images.update(_configured_images(item))
    return images


def image_inventory(dataset: Path) -> list[str]:
    """Inventory effective main/verifier/compose images plus Java runtime.

    Kept Dockerfiles are not build requests. SWE's literal FROM is used only
    when task.toml has not yet been adapted to its prebuilt image.
    """
    dataset = Path(dataset)
    tasks = task_directories(dataset)
    if not tasks:
        raise ValueError(f"No tasks in {dataset}")
    images = {RUNTIME_IMAGE}
    manifest_path = dataset / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text())
        images.discard(RUNTIME_IMAGE)
        images.add(manifest.get("runtime_image", RUNTIME_IMAGE))
    for task in tasks:
        config = tomllib.loads((task / "task.toml").read_text())
        images.update(_configured_images(config))
        is_tau3 = (task / "environment/runtime-server/server.py").is_file()
        if not config.get("environment", {}).get("docker_image"):
            if is_tau3:
                images.add(TAU3_MAIN_IMAGE)
            else:
                images.add(dockerfile_base_image(task / "environment/Dockerfile"))
        for folder in (task / "environment", task / "tests"):
            for pattern in ("*compose*.yaml", "*compose*.yml"):
                for path in sorted(folder.rglob(pattern)):
                    compose = yaml.safe_load(path.read_text()) or {}
                    for service, spec in compose.get("services", {}).items():
                        if spec.get("image"):
                            images.add(str(spec["image"]))
                        elif is_tau3 and service == "tau3-runtime":
                            images.add(TAU3_RUNTIME_IMAGE)
        verifier = config.get("verifier", {})
        if verifier.get("environment_mode") == "separate" and not verifier.get("environment", {}).get("docker_image"):
            images.add(dockerfile_base_image(task / "tests/Dockerfile"))
    if any(not image or "$" in image for image in images):
        raise ValueError("Inventory contains an unresolved image reference")
    return sorted(images)


def _image_reference(image: str) -> tuple[str, str | None]:
    if not isinstance(image, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@+-]*", image):
        raise ValueError(f"Invalid image reference: {image!r}")
    reference, at, digest = image.partition("@")
    if at and not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError(f"Expected a sha256 image digest: {image}")
    last = reference.rsplit("/", 1)[-1].split(":", 1)[0]
    if not last:
        raise ValueError(f"Missing image name: {image}")
    return last, digest if at else None


def mirror(images: list[str], registry: str, execute: bool = False) -> dict:
    """Return exact reference mappings and argv commands; copy only on opt-in.

    Destinations contain readable names and a hash of the FULL source reference
    to avoid cross-registry/tag collisions. Digest references stay pinned. For
    tag-only inputs, execute additionally pins the copied destination digest.
    No container inspection, builds, or implicit pushes are performed.
    """
    registry = registry.rstrip("/")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.\-_:]*(?:/[a-z0-9][a-z0-9._-]*)+", registry):
        raise ValueError("registry must be a registry/org path without a URL scheme or tag")
    image_map: dict[str, str] = {}
    commands: list[list[str]] = []
    transfers = []
    for source in sorted(set(images)):
        name, digest = _image_reference(source)
        suffix = hashlib.sha256(source.encode()).hexdigest()[:16]
        readable = re.sub(r"[^a-z0-9._-]", "-", name.lower())[:80]
        target = f"{registry}/{readable}-{suffix}:mirror"
        image_map[source] = target + ("@" + digest if digest else "")
        copy = ["skopeo", "copy", "--all", "--preserve-digests", f"docker://{source}", f"docker://{target}"]
        inspect = ["skopeo", "inspect", "--raw", f"docker://{target}"]
        commands.extend([copy, inspect])
        transfers.append((source, target, digest, copy, inspect))
    if execute:
        for source, target, digest, copy, inspect in transfers:
            subprocess.run(copy, check=True)
            raw = subprocess.run(inspect, check=True, stdout=subprocess.PIPE).stdout
            actual = "sha256:" + hashlib.sha256(raw).hexdigest()
            if digest and actual != digest:
                raise ValueError(f"Mirrored digest mismatch for {source}: {actual} != {digest}")
            image_map[source] = target + "@" + actual
    return {"image_map": image_map, "commands": commands, "executed": execute}


def _tau3_context(dataset: Path, output: Path, tau2_commit: str) -> tuple[Path, dict]:
    tasks = task_directories(dataset)
    if not tasks:
        raise ValueError(f"No tau3 tasks in {dataset}")
    common: dict[str, str] = {}
    configs: dict[str, Path] = {}
    config_hashes: dict[str, str] = {}
    for task in tasks:
        for relative in ("environment/Dockerfile", "environment/runtime-server/Dockerfile", "environment/runtime-server/server.py"):
            path = task / relative
            if not path.is_file():
                raise ValueError(f"Missing common tau3 source: {path}")
            digest = file_sha256(path)
            if relative in common and common[relative] != digest:
                raise ValueError(f"Tau3 sources are not identical: {relative} in {task}")
            common[relative] = digest
        config = task / "environment/runtime-server/task_config.json"
        if not config.is_file():
            raise ValueError(f"Missing tau3 task config: {config}")
        json.loads(config.read_text())
        configs[task.name] = config
        config_hashes[task.name] = file_sha256(config)
    metadata = {
        "tau2_commit": tau2_commit,
        "fastmcp_version": FASTMCP_VERSION,
        "task_count": len(tasks),
        "common_source_hashes": common,
        "task_config_hashes": config_hashes,
        "dependency_lock_notice": "Direct dependencies and tau2 commit pinned; transitive dependencies resolved at build time and pip freeze --all saved in /opt/hbench/requirements.freeze.txt. Not a pre-resolved full dependency lock.",
    }
    templates = {}
    for kind in ("main", "runtime"):
        templates[kind] = ROOT / f"environments/hbench/Dockerfile.tau3-{kind}"
        metadata[f"{kind}_dockerfile_sha256"] = file_sha256(templates[kind])
    fingerprint = hashlib.sha256(json.dumps(metadata, sort_keys=True).encode()).hexdigest()[:16]
    context = output / f"tau3-{fingerprint}"
    if context.exists():
        # Reusing an identical staged context is safe, but never overwrite it.
        stored = json.loads((context / "context.json").read_text())
        if stored != metadata:
            raise ValueError(f"Existing context metadata differs: {context}")
        expected_files = {"server.py": common["environment/runtime-server/server.py"]}
        expected_files.update({f"tasks/{name}.json": digest for name, digest in config_hashes.items()})
        expected_files.update({f"Dockerfile.tau3-{kind}": metadata[f"{kind}_dockerfile_sha256"] for kind in templates})
        for relative, digest in expected_files.items():
            if file_sha256(context / relative) != digest:
                raise ValueError(f"Staged context was modified: {context / relative}")
        return context, metadata
    (context / "tasks").mkdir(parents=True)
    shutil.copy2(tasks[0] / "environment/runtime-server/server.py", context / "server.py")
    for name, config in configs.items():
        shutil.copy2(config, context / "tasks" / f"{name}.json")
    for kind, template in templates.items():
        shutil.copy2(template, context / f"Dockerfile.tau3-{kind}")
    (context / "context.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return context, metadata


def build(
    runtime_image: str = RUNTIME_IMAGE,
    tau3_image_prefix: str = "hbench/tau3",
    dataset: Path | None = None,
    execute: bool = False,
    *,
    output: Path | None = None,
    tau2_commit: str = TAU2_COMMIT,
) -> dict:
    """Stage common tau3 assets and return a Maven + Docker build-only plan.

    Omitting dataset builds only the harness runtime. output defaults to
    artifacts/build; supplying output permits isolated tests. Image prefix is
    the repository prefix WITHOUT a tag (e.g. registry/org/tau3). No pushes.
    """
    _image_reference(runtime_image)
    if "@" in runtime_image:
        raise ValueError("A build target must be a tag, not a digest")
    _image_reference(tau3_image_prefix)
    if ":" in tau3_image_prefix.rsplit("/", 1)[-1] or "@" in tau3_image_prefix:
        raise ValueError("tau3_image_prefix must not include a tag or digest")
    if not re.fullmatch(r"[0-9a-f]{40}", tau2_commit):
        raise ValueError("tau2_commit must be a full 40-character git commit")
    output = Path(output).resolve() if output is not None else ROOT / "artifacts/build"
    output.mkdir(parents=True, exist_ok=True)
    context, metadata = _tau3_context(Path(dataset), output, tau2_commit) if dataset is not None else (None, {})
    images = {"runtime": runtime_image}
    image_map = {RUNTIME_IMAGE: runtime_image}
    # clean prevents stale SNAPSHOT/BETA JARs from coexisting on the wildcard classpath.
    commands = [["mvn", "-f", str(ROOT / "adapters/agentscope-java/pom.xml"), "clean", "package", "dependency:copy-dependencies", "-DincludeScope=runtime"]]
    runtime_dockerfile = ROOT / "environments/hbench/Dockerfile.runtime"
    commands.append(["docker", "build", "-f", str(runtime_dockerfile), "-t", runtime_image, str(ROOT)])
    if context is not None:
        images.update(tau3_main=f"{tau3_image_prefix}-main:0.1.0", tau3_runtime=f"{tau3_image_prefix}-runtime:0.1.0")
        image_map.update({TAU3_MAIN_IMAGE: images["tau3_main"], TAU3_RUNTIME_IMAGE: images["tau3_runtime"]})
        for kind in ("main", "runtime"):
            commands.append(["docker", "build", "--build-arg", f"TAU2_BENCH_COMMIT={tau2_commit}", "-f", str(context / f"Dockerfile.tau3-{kind}"), "-t", images[f"tau3_{kind}"], str(context)])
    plan = {
        "images": images, "image_map": image_map, "commands": commands,
        "contexts": {"runtime": str(ROOT), "tau3": str(context) if context else None},
        "metadata": metadata,
        "notes": ["Build and push are separate: this plan never pushes.", "Runtime injects uv 0.7.13 at /root/.local/bin/uv; kept task Dockerfiles are not uploaded.", metadata.get("dependency_lock_notice", "Java dependency versions follow candidate.version in the adapter POM.")],
        "executed": False,
    }
    fingerprint = hashlib.sha256(runtime_image.encode()).hexdigest()[:16]
    plan_path = context / "build-plan.json" if context else output / f"runtime-{fingerprint}.build-plan.json"
    plan["plan_path"] = str(plan_path)
    plan_path.write_text(json.dumps(plan, indent=2) + "\n")
    if execute:
        if not runtime_dockerfile.is_file():
            raise FileNotFoundError(f"Java runtime Dockerfile is not available: {runtime_dockerfile}")
        for command in commands:
            subprocess.run(command, check=True, cwd=ROOT)
        plan["executed"] = True
        plan_path.write_text(json.dumps(plan, indent=2) + "\n")
    return plan
