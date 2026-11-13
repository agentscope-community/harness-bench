"""Prepare local Hub packages without downloading or changing their sources."""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import tomllib
from pathlib import Path
from typing import Any

import yaml

TAU3_MAIN_IMAGE = "hbench/tau3-main:0.1.0"
TAU3_RUNTIME_IMAGE = "hbench/tau3-runtime:0.1.0"
RUNTIME_IMAGE = "hbench/harness-runtime:0.1.0"
SUPPORTED_DATASETS = {"tau3-bench", "swe-bench-verified", "terminal-bench"}


def task_directories(dataset: Path) -> list[Path]:
    """Return immediate Harbor task directories (or a single task directory)."""
    dataset = Path(dataset)
    if not dataset.is_dir():
        raise ValueError(f"Dataset directory does not exist: {dataset}")
    if (dataset / "task.toml").is_file():
        return [dataset]
    return sorted(p.parent for p in dataset.glob("*/task.toml") if p.is_file())


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def source_hashes(task: Path) -> dict[str, str]:
    """Hash full original files, including instructions, tests and fixtures."""
    return {
        p.relative_to(task).as_posix(): file_sha256(p)
        for p in sorted(task.rglob("*")) if p.is_file()
    }


def dockerfile_instructions(path: Path, instruction: str) -> list[str]:
    if not path.is_file():
        return []
    text = re.sub(r"\\\r?\n\s*", " ", path.read_text())
    return re.findall(rf"^\s*{re.escape(instruction)}\s+([^\n]+)", text, re.I | re.M)


def dockerfile_base_image(path: Path) -> str:
    """Read the final literal FROM; do not guess ARGs or local build stages."""
    stages: set[str] = set()
    image = ""
    for value in dockerfile_instructions(path, "FROM"):
        words = shlex.split(value)
        words = [word for word in words if not word.startswith("--")]
        if not words:
            raise ValueError(f"Invalid FROM in {path}")
        candidate = words[0]
        if "$" in candidate or candidate in stages:
            raise ValueError(f"Cannot infer a prebuilt final image from {path}: {candidate}")
        image = candidate
        if len(words) >= 3 and words[1].lower() == "as":
            stages.add(words[2])
    if not image:
        raise ValueError(f"No literal FROM image in {path}")
    return image


def _workdir(path: Path, default: str) -> str:
    values = dockerfile_instructions(path, "WORKDIR")
    workdir = shlex.split(values[-1])[0] if values else default
    if not workdir.startswith("/") or "$" in workdir:
        raise ValueError(f"Expected a literal absolute WORKDIR in {path}")
    return workdir


def _entrypoint(path: Path) -> list[str]:
    values = dockerfile_instructions(path, "ENTRYPOINT")
    if not values:
        return []
    try:
        result = json.loads(values[-1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"Only JSON ENTRYPOINT is supported: {path}") from exc
    if not isinstance(result, list) or not all(isinstance(v, str) for v in result):
        raise ValueError(f"ENTRYPOINT must be a string list: {path}")
    return result


def _set_environment(path: Path, updates: dict[str, str]) -> None:
    # Surgical edits preserve Hub comments and all verifier/fixture configuration.
    text = path.read_text()
    match = re.search(r"^\[environment\][ \t]*\r?$", text, re.M)
    if match:
        end = re.search(r"^\[", text[match.end():], re.M)
        stop = match.end() + end.start() if end else len(text)
        section = text[match.end():stop]
        for key, value in updates.items():
            line = f"{key} = {json.dumps(value)}"
            pattern = rf"^[ \t]*{re.escape(key)}\s*=.*$"
            if re.search(pattern, section, re.M):
                section = re.sub(pattern, lambda _: line, section, flags=re.M)
            else:
                section = "\n" + line + section
        text = text[:match.end()] + section + text[stop:]
    else:
        text += "\n[environment]\n" + "\n".join(
            f"{key} = {json.dumps(value)}" for key, value in updates.items()
        ) + "\n"
    tomllib.loads(text)  # Fail before writing malformed configuration.
    path.write_text(text)


def _compose_path(task: Path) -> Path:
    for name in ("docker-compose.yaml", "docker-compose.yml", "compose.yaml", "compose.yml"):
        path = task / "environment" / name
        if path.is_file():
            return path
    raise ValueError(f"No compose file in {task}")


def _adapt_tau3_verifier(task: Path) -> bool:
    """Make the Hub verifier work with the pinned tau2-bench 2.0 evaluator.

    tau2-bench 2.0 (commit 989d3285) made ``communicate_scoring`` a required
    keyword-only argument of ``evaluate_simulation``; the Hub verifier predates
    that change and would fail every tau3 trial with
    ``tau2_runtime_log_error``. SUBSTRING keeps the original verifier's
    matching semantics -- the semantic default would add its own judge model.
    """
    path = task / "tests" / "evaluate.py"
    if not path.is_file():
        return False
    text = path.read_text()
    if "communicate_scoring=" in text:
        return False
    import_anchor = '    get_now = _import_attr("tau2.utils.utils", "get_now")\n'
    call_anchor = "        solo_mode=False,\n"
    if text.count(import_anchor) != 1 or text.count(call_anchor) != 1:
        raise ValueError(f"Unexpected tau3 verifier layout: {path}")
    text = text.replace(
        import_anchor,
        import_anchor
        + '    CommunicateScoring = _import_attr(\n'
        '        "tau2.data_model.semantic_communication", "CommunicateScoring"\n'
        "    )\n"
        '    CommunicateScorer = _import_attr(\n'
        '        "tau2.data_model.semantic_communication", "CommunicateScorer"\n'
        "    )\n",
    )
    text = text.replace(
        call_anchor,
        call_anchor
        + "        communicate_scoring=CommunicateScoring(scorer=CommunicateScorer.SUBSTRING),\n",
    )
    path.write_text(text)
    return True


def _adapt_task(name: str, task: Path) -> list[dict[str, Any]]:
    adaptations: list[dict[str, Any]] = []
    if name == "swe-bench-verified":
        image = dockerfile_base_image(task / "environment" / "Dockerfile")
        if not image.startswith("swebench/"):
            raise ValueError(f"Expected a swebench image in {task}")
        _set_environment(task / "task.toml", {"docker_image": image, "workdir": "/testbed"})
        adaptations.append({"kind": "prebuilt_main", "docker_image": image, "workdir": "/testbed", "build": False})
    elif name == "tau3-bench":
        workdir = _workdir(task / "environment" / "Dockerfile", "/app")
        _set_environment(task / "task.toml", {"docker_image": TAU3_MAIN_IMAGE, "workdir": workdir})
        path = _compose_path(task)
        compose = yaml.safe_load(path.read_text())
        runtime = compose["services"]["tau3-runtime"]
        runtime.pop("build", None)
        runtime["image"] = TAU3_RUNTIME_IMAGE
        config_path = shlex.quote(f"/app/tasks/{task.name}.json")
        runtime["command"] = ["sh", "-c", f"cp {config_path} /app/task_config.json && exec python3 /app/server.py"]
        path.write_text(yaml.safe_dump(compose, sort_keys=False))
        adaptations.extend([
            {"kind": "shared_main", "docker_image": TAU3_MAIN_IMAGE, "workdir": workdir, "build": False},
            {"kind": "shared_tau3_runtime", "docker_image": TAU3_RUNTIME_IMAGE, "task_config": f"tasks/{task.name}.json", "build": False},
        ])
        if _adapt_tau3_verifier(task):
            adaptations.append({"kind": "tau2_communicate_scoring_substring"})
    else:
        config = tomllib.loads((task / "task.toml").read_text())
        if not config.get("environment", {}).get("docker_image"):
            raise ValueError(f"Terminal task has no prebuilt main image: {task}")
        # The main service is environment/Dockerfile, not a sidecar's Dockerfile.
        entrypoint = _entrypoint(task / "environment" / "Dockerfile")
        adaptations.append({"kind": "preserve_main_entrypoint", "main_entrypoint": entrypoint})
        metadata_path = task / "hbench.json"
        metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
        metadata["main_entrypoint"] = entrypoint
        metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
    return adaptations


def prepare(profile: dict, source: Path, output: Path) -> dict:
    """Copy a pinned cached package into a fresh directory and return its manifest.

    The declared package digest is provenance, not a re-computed Hub package
    digest. Per-file hashes refer to complete ORIGINAL files. Adapted execution
    is explicitly not an official benchmark score.
    """
    profile = dict(profile)
    name = profile.get("name")
    if name not in SUPPORTED_DATASETS:
        raise ValueError(f"Unsupported dataset profile: {name}")
    if not profile.get("package") or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(profile.get("version", ""))):
        raise ValueError("Profile requires package and a pinned sha256 version")
    expected = profile.get("expected_tasks")
    if type(expected) is not int or expected <= 0:
        raise ValueError("expected_tasks must be a positive integer")
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite output: {output}")
    if source == output or source in output.parents or output in source.parents:
        raise ValueError("Source and output must not overlap")
    tasks = task_directories(source)
    if len(tasks) != expected:
        raise ValueError(f"Expected {expected} tasks, found {len(tasks)} in {source}")
    # A package root is required so task directory names remain stable.
    if (source / "task.toml").exists():
        raise ValueError("prepare source must be a package root containing task directories")
    records = [{"name": task.name, "source_hashes": source_hashes(task)} for task in tasks]
    output.parent.mkdir(parents=True, exist_ok=True)
    # Claim ownership before cleanup can run; a competing creator is left untouched.
    output.mkdir(exist_ok=False)
    try:
        shutil.copytree(source, output, dirs_exist_ok=True)
        for record in records:
            record["adaptations"] = _adapt_task(name, output / record["name"])
        manifest = {
            "schema_version": 1,
            "profile": profile,
            "source": str(source),
            "output": str(output),
            "task_count": len(tasks),
            "runtime_image": RUNTIME_IMAGE,
            "official_score": False,
            "score_notice": "Adapted hbench execution; not an official benchmark score.",
            "provenance_notice": "Profile version is the Hub metadata pin; source_hashes hash full original local files, not the Hub archive.",
            "tasks": records,
        }
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    except Exception:
        # Only remove the fresh directory this invocation created.
        shutil.rmtree(output)
        raise
    return manifest
