"""Console settings. Everything defaults under the repository's ignored artifacts/."""
from dataclasses import dataclass
import os
import shlex
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _path(value: str | None, default: Path) -> Path:
    path = Path(value).expanduser() if value else default
    return (path if path.is_absolute() else REPO_ROOT / path).resolve()


def _flag(name: str, default: str = "0") -> bool:
    return (os.environ.get(name) or default).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    repo_root: Path
    data_dir: Path
    jobs_root: Path
    reports_root: Path
    plans_root: Path
    allow_real_runs: bool
    hbench_cmd: tuple[str, ...]
    viewer_url: str | None
    host: str
    port: int
    sync_on_start: bool

    @classmethod
    def from_env(cls, env=None) -> "Settings":
        env = os.environ if env is None else env
        data_dir = _path(env.get("CONSOLE_DATA_DIR"), REPO_ROOT / "artifacts" / "console")
        command = env.get("CONSOLE_HBENCH_CMD") or f"{sys.executable} -m hbench"
        try:
            argv = tuple(shlex.split(command))
            port = int(env.get("CONSOLE_PORT") or 8090)
        except ValueError as error:
            raise ValueError("Invalid console setting") from error
        if not argv:
            raise ValueError("CONSOLE_HBENCH_CMD must not be empty")
        return cls(
            repo_root=REPO_ROOT,
            data_dir=data_dir,
            jobs_root=_path(env.get("CONSOLE_JOBS_ROOT"), REPO_ROOT / "artifacts" / "jobs"),
            reports_root=_path(env.get("CONSOLE_REPORTS_ROOT"), REPO_ROOT / "artifacts" / "reports"),
            plans_root=_path(env.get("CONSOLE_PLANS_ROOT"), REPO_ROOT / "artifacts" / "plans"),
            allow_real_runs=_flag("CONSOLE_ALLOW_REAL_RUNS"),
            hbench_cmd=argv,
            viewer_url=(env.get("CONSOLE_VIEWER_URL") or "").strip() or None,
            host=(env.get("CONSOLE_HOST") or "0.0.0.0").strip(),
            port=port,
            sync_on_start=not _flag("CONSOLE_SKIP_STARTUP_SYNC"),
        )

    @property
    def database(self) -> Path:
        return self.data_dir / "console.db"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"
