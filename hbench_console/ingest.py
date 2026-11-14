"""Scan Harbor job directories, summarize them with hbench and index the result."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import uuid

from .config import Settings
from .db import Database, now


def _timestamp(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


class Ingestor:
    """Idempotent, serialized scan of CONSOLE_JOBS_ROOT."""

    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database
        self._lock = threading.Lock()
        self.last_sync: str | None = None
        self.last_error: str | None = None

    def sync(self) -> list[dict]:
        with self._lock:
            try:
                results = self._sync()
                self.last_error = None
            except Exception as error:  # a broken mirror must not take the console down
                self.last_error = f"{type(error).__name__}: {str(error)[:300]}"
                results = []
            self.last_sync = now()
            return results

    def _sync(self) -> list[dict]:
        root = self.settings.jobs_root
        root.mkdir(parents=True, exist_ok=True)
        results = []
        seen = set()
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if not self._has_evidence(entry):
                continue
            seen.add(entry.name)
            results.append(self._ingest(entry))
        self._prune(seen)
        return results

    @staticmethod
    def _has_evidence(job_dir: Path) -> bool:
        return (job_dir / "result.json").is_file() or any(job_dir.glob("*/result.json"))

    def _ingest(self, job_dir: Path) -> dict:
        name = job_dir.name
        report = self.settings.reports_root / name
        try:
            if self._needs_refresh(job_dir, report):
                self._summarize(job_dir, report)
            summary = json.loads((report / "summary.json").read_text(encoding="utf-8"))
            self._store(name, job_dir, summary)
            return {"name": name, "status": "indexed",
                    "recorded_trials": summary.get("recorded_trials")}
        except Exception as error:
            message = f"{type(error).__name__}: {str(error)[:300]}"
            self.database.record_job_error(name, str(job_dir), message)
            return {"name": name, "status": "error", "error": message}

    @staticmethod
    def _needs_refresh(job_dir: Path, report: Path) -> bool:
        summary = report / "summary.json"
        if not summary.is_file():
            return True
        newest = max([_timestamp(job_dir / "result.json"), _timestamp(job_dir / "config.json"),
                      _timestamp(job_dir / "lock.json")]
                     + [_timestamp(path) for path in job_dir.glob("*/result.json")])
        return newest > _timestamp(summary)

    def _summarize(self, job_dir: Path, report: Path):
        self.settings.tmp_dir.mkdir(parents=True, exist_ok=True)
        target = self.settings.tmp_dir / f"{job_dir.name}-{uuid.uuid4().hex[:8]}"
        env = dict(os.environ)
        env["PYTHONPATH"] = str(self.settings.repo_root) + os.pathsep + env.get("PYTHONPATH", "")
        try:
            completed = subprocess.run(
                [*self.settings.hbench_cmd, "summary", str(job_dir),
                 "--output", str(target)],
                cwd=self.settings.repo_root, env=env, capture_output=True, text=True, timeout=900)
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip().splitlines()
                raise RuntimeError(detail[-1] if detail else "hbench summary failed")
            report.parent.mkdir(parents=True, exist_ok=True)
            if report.exists():
                shutil.rmtree(report)
            target.rename(report)
        finally:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)

    def _store(self, name: str, job_dir: Path, summary: dict):
        combos = []
        for group in summary.get("groups", []):
            combos.append({
                "agent": group["agent"], "model": group["model"], "dataset": group["dataset"],
                "planned": group.get("planned"), "recorded": group["recorded"],
                "missing": group.get("missing"), "errors": group["errors"],
                "scored": group["scored"], "passed": group["passed"],
                "unscored": group.get("unscored", group["recorded"] - group["scored"]),
                "pass_rate_planned": group.get("pass_rate_planned"),
                "pass_rate_recorded": group.get("pass_rate_recorded"),
                "mean_reward_scored": group.get("mean_reward_scored"),
                "input_tokens": group.get("input_tokens") or 0,
                "output_tokens": group.get("output_tokens") or 0,
            })
        trials = []
        for row in summary.get("trials", []):
            trial_dir = job_dir / row["trial"]
            trials.append({
                "trial": row["trial"], "task": row.get("task"),
                "agent": row.get("agent"), "model": row.get("model"),
                "dataset": row.get("dataset"), "reward": row.get("reward"),
                "error": row.get("error"), "duration_sec": row.get("duration_sec"),
                "input_tokens": row.get("input_tokens") or 0,
                "output_tokens": row.get("output_tokens") or 0,
                "evidence": f"{row['trial']}/result.json" if (trial_dir / "result.json").is_file() else None,
            })
        self.database.replace_job({
            "name": name,
            "path": str(job_dir),
            "origin": "run" if self.database.has_run(name) else "import",
            "fixture": (job_dir / "fixture.json").is_file(),
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "recorded_trials": summary.get("recorded_trials"),
            "planned_trials": summary.get("planned_trials"),
            "pass_rate_planned": summary.get("pass_rate_planned"),
            "note": summary.get("note"),
            "error": None,
        }, combos, trials)

    def _prune(self, seen: set[str]):
        for row in self.database.list_jobs():
            if row["name"] not in seen and not Path(row["path"]).is_dir():
                self.database.delete_job(row["name"])
