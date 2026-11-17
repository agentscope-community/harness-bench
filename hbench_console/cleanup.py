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
        label = f"label=com.docker.compose.project={project}"
        name = f"name=^{project}[_-]"
        try:
            containers = sorted(set(_ids(["ps", "-aq", "--filter", label])
                                    + _ids(["ps", "-aq", "--filter", name])))
            _remove(["container", "rm", "-f"], "containers", containers, summary, log)
            networks = sorted(set(_ids(["network", "ls", "-q", "--filter", label])
                                  + _ids(["network", "ls", "-q", "--filter", name])))
            _remove(["network", "rm"], "networks", networks, summary, log)
            volumes = sorted(set(_ids(["volume", "ls", "-q", "--filter", label])))
            _remove(["volume", "rm", "-f"], "volumes", volumes, summary, log)
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
