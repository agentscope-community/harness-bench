"""Command line for preparing, planning and running Harbor harness comparisons."""
import argparse
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import yaml

from .jobs import ROOT, load_profile, render_job, validate_execution

MATRIX_FIELDS = frozenset({"name", "cluster", "dataset_root", "benchmarks", "models", "harnesses",
                           "concurrency", "attempts", "selection", "image_map"})
SELECTION_FIELDS = frozenset({"n_tasks", "tasks", "exclude_gpu"})


def harbor_command():
    try:
        installed = version("harbor")
    except PackageNotFoundError as error:
        raise ValueError("Harbor is missing; install requirements-evals.txt") from error
    if installed != "0.25.0":
        raise ValueError("This integration requires harbor==0.25.0")
    path = Path(sys.executable).parent / "harbor"
    command = str(path) if path.is_file() else shutil.which("harbor")
    if not command:
        raise ValueError("harbor executable is missing; install requirements-evals.txt")
    return command


def names(value):
    return [n.strip() for n in value.split(",") if n.strip()]


def repo_path(value):
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


def load_image_map(path):
    mapping = json.loads(path.read_text()) if path else None
    if mapping and "image_map" in mapping:
        mapping = mapping["image_map"]
    return mapping


def load_matrix(path, cluster=None):
    """Read a dataset x model x harness matrix; relative paths are repository-relative."""
    value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Matrix configuration must be an object")
    if set(value) - MATRIX_FIELDS:
        raise ValueError("Unknown matrix settings: " + ", ".join(sorted(set(value) - MATRIX_FIELDS)))
    selection = value.get("selection") or {}
    if not isinstance(selection, dict) or set(selection) - SELECTION_FIELDS:
        raise ValueError("Matrix selection supports only n_tasks, tasks and exclude_gpu")
    lists = {}
    for key in ("benchmarks", "models", "harnesses"):
        items = value.get(key)
        if not isinstance(items, list) or not items or not all(isinstance(i, str) and i for i in items):
            raise ValueError(f"Matrix {key} must be a non-empty list of profile names or paths")
        if len(set(items)) != len(items):
            raise ValueError(f"Matrix {key} must not repeat entries")
        lists[key] = items
    cluster = cluster or value.get("cluster")
    if not isinstance(cluster, str) or not cluster:
        raise ValueError("Matrix cluster must name an execution profile (docker, kubernetes, ack, acs or a path)")
    for key in ("concurrency", "attempts"):
        if not isinstance(value.get(key, 1), int) or isinstance(value.get(key, 1), bool):
            raise ValueError(f"Matrix {key} must be an integer")
    tasks = selection.get("tasks")
    if tasks is not None and not (isinstance(tasks, list) and all(isinstance(t, str) for t in tasks)):
        raise ValueError("Matrix selection tasks must be a list of directory names")
    name = value.get("name") or Path(path).stem

    def profile(kind, item):
        # Bare names come from configs/<kind>; anything path-like is repository-relative.
        return load_profile(kind, repo_path(item) if item.endswith((".yaml", ".yml")) or "/" in item else item)

    return {
        "name": name,
        "benchmarks": [profile("benchmarks", n) for n in lists["benchmarks"]],
        "harnesses": [profile("harnesses", n) for n in lists["harnesses"]],
        "models": [profile("models", n) for n in lists["models"]],
        "cluster": profile("clusters", cluster),
        "dataset_root": repo_path(value.get("dataset_root") or "artifacts/datasets"),
        "image_map": load_image_map(repo_path(value["image_map"])) if value.get("image_map") else None,
        "n_tasks": selection.get("n_tasks"), "task_names": tasks or None,
        "exclude_gpu": bool(selection.get("exclude_gpu", False)),
        "concurrency": value.get("concurrency", 1), "attempts": value.get("attempts", 1),
    }


def write_plan(output, job, contract):
    output.parent.mkdir(parents=True, exist_ok=True)
    contract_path = output.with_suffix(".plan.json")
    if output.exists() or contract_path.exists():
        raise ValueError("Plan output already exists; choose a fresh path")
    output.write_text(yaml.safe_dump(job, sort_keys=False), encoding="utf-8")
    contract_path.write_text(json.dumps(contract, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"config": str(output.resolve()), "planned_trials": contract["planned_trials"],
                      "combinations": len(contract["combinations"]), "executed": False}))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List benchmark, harness, model, cluster and matrix profiles")
    listing.add_argument("kind", choices=["benchmarks", "harnesses", "models", "clusters", "matrices"], nargs="?")
    datasets = commands.add_parser("datasets", help="Pull immutable Hub packages or prepare local task copies")
    dataset_commands = datasets.add_subparsers(dest="action", required=True)
    for action in ("pull", "prepare"):
        sub = dataset_commands.add_parser(action)
        sub.add_argument("benchmark", help="Profile name (or all)")
        sub.add_argument("--root", type=Path, default=ROOT / "artifacts" / "hub")
        if action == "prepare":
            sub.add_argument("--source", type=Path, help="Explicit downloaded dataset directory; single benchmark only")
            sub.add_argument("--output-root", type=Path, default=ROOT / "artifacts" / "datasets")
    plan = commands.add_parser("plan", help="Render a local Harbor job and comparison contract; no execution")
    plan.add_argument("--benchmark", default="terminal-bench", help="Comma-separated benchmark profiles")
    plan.add_argument("--harness", default="codex,pi,agentscope-java", help="Comma-separated harness profiles")
    plan.add_argument("--model", default="example", help="Comma-separated model profile names or YAML paths")
    plan.add_argument("--cluster", default="kubernetes", help="Execution profile: docker, kubernetes, ack, acs, or YAML path")
    plan.add_argument("--dataset-root", type=Path, default=ROOT / "artifacts" / "datasets")
    plan.add_argument("--output", type=Path, required=True, help="New .yaml job path")
    plan.add_argument("--name")
    plan.add_argument("--n-tasks", type=int)
    plan.add_argument("--tasks", help="Comma-separated exact directory names")
    plan.add_argument("--exclude-gpu", action="store_true")
    plan.add_argument("--concurrency", type=int, default=1)
    plan.add_argument("--attempts", type=int, default=1)
    plan.add_argument("--image-map", type=Path, help="JSON from images mirror")
    matrix = commands.add_parser("matrix", help="Render one Harbor job from a dataset x model x harness matrix file; no execution")
    matrix.add_argument("--config", type=Path, required=True, help="Matrix YAML, e.g. configs/matrices/bailian-18.yaml")
    matrix.add_argument("--output", type=Path, required=True, help="New .yaml job path")
    matrix.add_argument("--name", help="Job name; default <matrix name>-<UTC timestamp>")
    matrix.add_argument("--cluster", help="Override the matrix execution profile")
    matrix.add_argument("--concurrency", type=int, help="Override the matrix concurrency")
    matrix.add_argument("--n-tasks", type=int, help="Override selection.n_tasks (per dataset), e.g. 1 for smoke")
    run = commands.add_parser("run", help="Execute an existing job, or validate it without cloud/model calls")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--dry-run", action="store_true")
    summary = commands.add_parser("summary", help="Create offline JSON and HTML from a Harbor job directory")
    summary.add_argument("job", type=Path)
    summary.add_argument("--output", type=Path, required=True)
    archive = commands.add_parser("archive", help="Best-effort upload of a job (and report) to OSS via the ECS RAM role; errors are printed, never fatal")
    archive.add_argument("job", type=Path, nargs="?", help="Job directory, e.g. artifacts/jobs/<name>")
    archive.add_argument("--report", type=Path, help="Report directory, e.g. artifacts/reports/<name>")
    archive.add_argument("--check", action="store_true", help="Verify HB_OSS_* settings, the ECS role and bucket listing; uploads nothing")
    archive.add_argument("--dry-run", action="store_true", help="List the object keys that would be uploaded; no network")
    archive.add_argument("--follow", action="store_true", help="Upload finished trials every --interval seconds; SIGUSR1 then uploads everything, SIGTERM/SIGINT or --parent exit stop after settled trials")
    archive.add_argument("--interval", type=int, default=300, help="Seconds between background syncs (min 10)")
    archive.add_argument("--parent", type=int, help="Stop following when this process exits")
    archive.add_argument("--final-timeout", type=int, default=1800, help="Seconds allowed for the last --follow pass; the rest stays local")
    images = commands.add_parser("images", help="Inventory, build, or mirror evaluation images")
    image_commands = images.add_subparsers(dest="action", required=True)
    image_list = image_commands.add_parser("list")
    image_list.add_argument("dataset", type=Path)
    mirror = image_commands.add_parser("mirror")
    mirror.add_argument("dataset", type=Path)
    mirror.add_argument("--registry", required=True)
    mirror.add_argument("--output", type=Path, required=True)
    mirror.add_argument("--execute", action="store_true", help="Actually copy/push images with skopeo")
    mirror.add_argument("--built-image-map", type=Path, help="Build-plan JSON mapping logical hbench images to published registry images")
    build = image_commands.add_parser("build")
    build.add_argument("--runtime-image", required=True)
    build.add_argument("--tau3-dataset", type=Path)
    build.add_argument("--tau3-image-prefix", default="hbench/tau3")
    build.add_argument("--execute", action="store_true", help="Actually run Maven and docker build (no push)")
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            kinds = [args.kind] if args.kind else ["benchmarks", "harnesses", "models", "clusters", "matrices"]
            print(json.dumps({k: [p.stem for p in sorted((ROOT / "configs" / k).glob("*.yaml"))] for k in kinds}, indent=2))
        elif args.command == "datasets":
            requested = [p.stem for p in sorted((ROOT / "configs/benchmarks").glob("*.yaml"))] if args.benchmark == "all" else [args.benchmark]
            if getattr(args, "source", None) and len(requested) != 1:
                raise ValueError("--source requires a single benchmark")
            for name in requested:
                profile = load_profile("benchmarks", name)
                parent = args.root.resolve() / profile["name"]
                source = parent / profile["package"].split("/")[-1]
                if args.action == "pull":
                    if source.exists():
                        raise ValueError(f"Download directory already exists: {source}; use datasets prepare or a fresh --root")
                    parent.mkdir(parents=True, exist_ok=True)
                    subprocess.run([harbor_command(), "dataset", "download", profile["package"] + "@" + profile["version"], "--output-dir", str(parent), "--export"], check=True, cwd=ROOT)
                    (parent / "source.json").write_text(json.dumps(profile, indent=2) + "\n")
                else:
                    from .datasets import prepare
                    output = args.output_root.resolve() / profile["name"]
                    manifest = prepare(profile, (args.source or source).resolve(), output)
                    print(json.dumps({"benchmark": profile["name"], "output": str(output), "prepared": True}))
        elif args.command == "plan":
            contract_path = args.output.with_suffix(".plan.json")
            if args.output.exists() or contract_path.exists():
                raise ValueError("Plan output already exists; choose a fresh path")
            mapping = load_image_map(args.image_map)
            models = [load_profile("models", n) for n in names(args.model)]
            job, contract = render_job([load_profile("benchmarks", n) for n in names(args.benchmark)],
                [load_profile("harnesses", n) for n in names(args.harness)], models if len(models) > 1 else models[0],
                load_profile("clusters", args.cluster), args.dataset_root, job_name=args.name, n_tasks=args.n_tasks,
                task_names=names(args.tasks) if args.tasks else None, exclude_gpu=args.exclude_gpu,
                concurrency=args.concurrency, attempts=args.attempts, image_map=mapping)
            write_plan(args.output, job, contract)
        elif args.command == "matrix":
            if args.output.exists() or args.output.with_suffix(".plan.json").exists():
                raise ValueError("Plan output already exists; choose a fresh path")
            spec = load_matrix(args.config, cluster=args.cluster)
            if args.concurrency is not None:
                spec["concurrency"] = args.concurrency
            if args.n_tasks is not None:
                spec["n_tasks"] = args.n_tasks
            from datetime import datetime, timezone
            job_name = args.name or spec["name"] + "-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            job, contract = render_job(spec["benchmarks"], spec["harnesses"], spec["models"], spec["cluster"],
                spec["dataset_root"], job_name=job_name, n_tasks=spec["n_tasks"], task_names=spec["task_names"],
                exclude_gpu=spec["exclude_gpu"], concurrency=spec["concurrency"], attempts=spec["attempts"],
                image_map=spec["image_map"])
            contract["matrix"] = str(args.config.resolve())
            write_plan(args.output, job, contract)
        elif args.command == "run":
            job = yaml.safe_load(args.config.read_text())
            from harbor.models.job.config import JobConfig
            JobConfig.model_validate(job)
            # A batch runner has no TTY: Harbor otherwise asks before letting tasks read
            # host variables (tau3's TAU2_*), and that prompt dies under nohup.
            command = [harbor_command(), "run", "-c", str(args.config.resolve())]
            if args.dry_run:
                command += ["--dry-run", "--skip-credential-checks"]
            else:
                command.append("--yes")
                target = Path(job.get("jobs_dir", ROOT / "artifacts/jobs")) / job["job_name"]
                if target.exists():
                    raise ValueError("Job directory already exists; render a new plan before execution")
                validate_execution(job)
            environment = dict(os.environ)
            # tau3 task.toml resolves OPENAI_*/TAU2_* from the controller environment
            # (compose substitution and verifier env), not from the job env only.
            from harbor.utils.env import resolve_env_vars
            job_env = job.get("environment", {}).get("env")
            if isinstance(job_env, dict):
                for key, value in resolve_env_vars(job_env).items():
                    if value:
                        environment.setdefault(key, value)
            environment["PYTHONPATH"] = str(ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
            return subprocess.run(command, cwd=ROOT, env=environment).returncode
        elif args.command == "summary":
            from .summary import summarize_job
            value = summarize_job(args.job, args.output)
            print(json.dumps({"recorded_trials": value["recorded_trials"], "report": str((args.output / "report.html").resolve())}))
        elif args.command == "archive":
            from .archive import command
            return command(args)
        elif args.command == "images":
            from . import images as tools
            if args.action == "list":
                print(json.dumps(tools.image_inventory(args.dataset), indent=2))
            elif args.action == "mirror":
                if args.output.exists():
                    raise ValueError("Image map output already exists; choose a fresh path")
                built = json.loads(args.built_image_map.read_text()) if args.built_image_map else {}
                built = built.get("image_map", built)
                inventory = tools.image_inventory(args.dataset)
                replacements = {source: built.get(source, source) for source in inventory}
                if args.execute and any(image.startswith("hbench/") for image in replacements.values()):
                    raise ValueError("Build and publish logical hbench images first, then supply --built-image-map")
                value = tools.mirror(list(replacements.values()), args.registry, execute=args.execute)
                value["image_map"].update({source: value["image_map"][target] for source, target in replacements.items()})
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("x") as stream:
                    stream.write(json.dumps(value, indent=2) + "\n")
                print(json.dumps({"output": str(args.output.resolve()), "executed": args.execute}))
            else:
                value = tools.build(args.runtime_image, args.tau3_image_prefix, args.tau3_dataset, execute=args.execute)
                print(json.dumps(value, indent=2))
        return 0
    except (ValueError, OSError, subprocess.CalledProcessError, yaml.YAMLError) as error:
        # YAML parsing and Pydantic validation can embed supplied credentials.
        from pydantic import ValidationError
        if isinstance(error, yaml.YAMLError):
            message = "Invalid YAML configuration; check the selected configuration"
        elif isinstance(error, ValidationError):
            message = "Invalid job/profile schema; check the selected configuration"
        else:
            message = str(error)
        parser.exit(2, message + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
