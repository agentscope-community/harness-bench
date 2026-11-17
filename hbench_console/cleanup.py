"""Best-effort reclamation of resources owned by one console run.

Cancelling is forceful: the console signals the hbench process group, escalates
to SIGKILL after a short grace period, then removes every container, network
and volume tied to the job's trial compose projects. Trial directories are left
on disk so completed evidence survives; only runtime resources are reclaimed.

Harbor names docker compose projects after the trial directory name, so the
project list comes from the job directory. Harbor exposes no stop API; see
docs/console-plan.md for the survey behind this approach.
"""
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time

ENGINE = (os.environ.get("HB_CONTAINER_ENGINE") or "docker").strip()
GRACE_SECONDS = 5
_CALL_TIMEOUT = 60


def _sanitize(name: str) -> str:
    """Mirror Harbor's compose project name sanitizer."""
    name = name.lower()
    if not re.match(r"^[a-z0-9]", name):
        name = "0" + name
    return re.sub(r"[^a-z0-9_-]", "-", name)


def project_names(job_dir: Path) -> list[str]:
    """Compose project names for trial directories already created in the job."""
    if not job_dir.is_dir():
        return []
    names = {_sanitize(entry.name) for entry in job_dir.iterdir()
             if entry.is_dir() and not entry.name.startswith(".")}
    return sorted(names)


def scoped_projects(project: str) -> list[str]:
    """Compose projects Harbor derives from one trial directory name.

    The task environment and the verifier run as separate projects, and the
    one-shot runtime service (``hbench-runtime-init``) is a third container in
    the environment project. Label filters must name them exactly; only their
    volumes carry no trial prefix, so the prefix filter alone misses them.
    """
    return [project, f"{project}__env", f"{project}__verifier__trial"]


def _engine(args: list[str]) -> subprocess.CompletedProcess:
    executable = shutil.which(ENGINE)
    if executable is None:
        raise FileNotFoundError(f"{ENGINE} 未安装或不在 PATH 中")
    return subprocess.run([executable, *args], capture_output=True, text=True,
                          timeout=_CALL_TIMEOUT)


def _ids(args: list[str]) -> list[str]:
    result = _engine(args)
    return result.stdout.split()


def _remove(command: list[str], counter: str, identifiers: list[str], summary: dict,
            log) -> None:
    if not identifiers:
        return
    result = _engine([*command, *identifiers])
    if result.returncode == 0:
        summary[counter] += len(identifiers)
        log(f"cleanup: removed {len(identifiers)} {counter}")
        return
    message = (result.stderr or result.stdout).strip().splitlines()
    detail = message[-1] if message else str(result.returncode)
    summary["errors"].append(f"{counter}: {detail}")
    log(f"cleanup: {counter} removal failed: {detail}")


def cleanup_job(job_dir: Path, log=lambda message: None) -> dict:
    """Remove containers, networks and volumes of a job's compose projects."""
    projects = project_names(job_dir)
    summary = {"projects": len(projects), "containers": 0, "networks": 0,
               "volumes": 0, "errors": []}
    if not projects:
        return summary
    if shutil.which(ENGINE) is None:
        summary["errors"].append(f"{ENGINE} 不可用，跳过容器清理")
        log(f"cleanup: {ENGINE} 不可用，跳过容器清理")
        return summary
    for project in projects:
        name = f"name=^{project}[_-]"
        try:
            # The prefix filter also catches the one-shot runtime service
            # (``<trial>__env-hbench-runtime-init-1``) left behind exited.
            containers = set(_ids(["ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"]))
            containers |= set(_ids(["ps", "-aq", "--filter", name]))
            networks = set(_ids(["network", "ls", "-q", "--filter", name]))
            volumes: set[str] = set()
            for scope in scoped_projects(project):
                label = f"label=com.docker.compose.project={scope}"
                containers |= set(_ids(["ps", "-aq", "--filter", label]))
                networks |= set(_ids(["network", "ls", "-q", "--filter", label]))
                volumes |= set(_ids(["volume", "ls", "-q", "--filter", label]))
            _remove(["container", "rm", "-f"], "containers", sorted(containers), summary, log)
            _remove(["network", "rm"], "networks", sorted(networks), summary, log)
            _remove(["volume", "rm", "-f"], "volumes", sorted(volumes), summary, log)
        except (OSError, subprocess.SubprocessError) as error:
            summary["errors"].append(f"{project}: {type(error).__name__}")
            log(f"cleanup: {project}: {type(error).__name__}: {error}")
    return summary


def kill_group(process: subprocess.Popen | None) -> None:
    """SIGKILL the process group, covering children that outlived the parent."""
    if process is None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except OSError:
        try:
            process.kill()
        except OSError:
            pass


def stop_process_group(process: subprocess.Popen | None) -> None:
    """SIGTERM, wait briefly, then SIGKILL the whole process group."""
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except OSError:
        try:
            process.terminate()
        except OSError:
            return
    deadline = time.monotonic() + GRACE_SECONDS
    while time.monotonic() < deadline and process.poll() is None:
        time.sleep(0.2)
    kill_group(process)
