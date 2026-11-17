"""Harness profiles are the shared registry for execution and console identity."""
from pathlib import Path

import yaml

PROFILE_ROOT = Path(__file__).resolve().parent.parent / "configs" / "harnesses"


def harness_registry(root: Path = PROFILE_ROOT) -> dict[str, dict]:
    entries = {}
    identities = set()
    for path in sorted(root.glob("*.yaml")):
        profile = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(profile, dict) or profile.get("name") != path.stem:
            raise ValueError(f"Harness name must match profile filename: {path}")
        identity = profile.get("import_path")
        if not isinstance(identity, str) or ":" not in identity or identity in identities:
            raise ValueError(f"Missing or duplicate harness import_path: {path}")
        if not profile.get("supported_api_formats"):
            raise ValueError(f"Harness must declare supported_api_formats: {path}")
        identities.add(identity)
        entries[profile["name"]] = profile
    return entries


def harness_name(identity: str) -> str | None:
    for name, profile in harness_registry().items():
        if identity in (name, profile["import_path"]):
            return name
    return None


def harness_evidence_files() -> tuple[str, ...]:
    files = {"native.jsonl", "trajectory.json"}
    for profile in harness_registry().values():
        for name in profile.get("evidence_files", []):
            if not isinstance(name, str) or Path(name).name != name or name in (".", ".."):
                raise ValueError("Harness evidence_files must contain plain filenames")
            files.add(name)
    return tuple(sorted(files))
