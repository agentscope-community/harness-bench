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


HARNESS_NAMES = {"hbench.agents:FrozenCodex": "codex", "hbench.agents:FrozenPi": "pi",
                 "hbench.agents:AgentScopeJava": "agentscope-java"}


def _agent_identity(agent: dict) -> tuple[str, str]:
    identity = agent.get("import_path") or agent.get("name") or ""
    name = HARNESS_NAMES.get(identity) or identity.rsplit(":", 1)[-1].lower()
    return name, agent.get("model_name") or "unknown"


def _dataset_name(dataset: dict) -> str | None:
    path = dataset.get("path")
    if path:
        return Path(path).expanduser().name
    return dataset.get("name")


def _planned_trials(job_dir: Path) -> list[tuple[str, str, str, str]] | None:
    """Enumerate planned (dataset, task, harness, model) from the job lock/config.

    Returns None when the plan cannot be enumerated faithfully (whole datasets or
    globs); the combo table still reports the missing counts in that case.
    """
    lock_path = job_dir / "lock.json"
    if lock_path.is_file():
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        planned = []
        for trial in payload.get("trials") or []:
            task = trial.get("task") or {}
            agent = trial.get("agent") or {}
            name = task.get("name") or (Path(task["path"]).name if task.get("path") else None)
            source = task.get("source") or (Path(task["path"]).name if task.get("path") else "adhoc")
            if not name:
                return None
            planned.append((source, name, *_agent_identity(agent)))
        return planned
    config_path = job_dir / "config.json"
    if not config_path.is_file():
        return None
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if config.get("source_jobs"):
        return None
    attempts = max(1, int(config.get("n_attempts") or 1))
    datasets = []
    for dataset in config.get("datasets") or []:
        names = dataset.get("task_names") or []
        count = dataset.get("n_tasks")
        if (not names or dataset.get("exclude_task_names")
                or any(any(c in name for c in "*?[") for name in names)
                or (count is not None and count < len(set(names)))):
            return None
        datasets.append((_dataset_name(dataset) or "adhoc", list(dict.fromkeys(names))))
    for task in config.get("tasks") or []:
        name = task.get("name") or (Path(task["path"]).name if task.get("path") else None)
        if not name:
            return None
        datasets.append((task.get("source") or (Path(task["path"]).name if task.get("path") else "adhoc"),
                         [name]))
    planned = []
    for agent in config.get("agents") or []:
        name, model = _agent_identity(agent)
        for source, names in datasets:
            for task_name in names:
                planned.extend((source, task_name, name, model) for _ in range(attempts))
    return planned


def _missing_trials(job_dir: Path, summary: dict) -> list[dict]:
    try:
        planned = _planned_trials(job_dir)
    except (OSError, ValueError, TypeError, KeyError):
        # Missing detail is diagnostic only; never let it block indexing.
        return []
    if planned is None:
        return []
    remaining = {}
    for key in planned:
        remaining[key] = remaining.get(key, 0) + 1
    for trial in summary.get("trials", []):
        key = (trial.get("dataset") or "adhoc", trial.get("task"),
               trial.get("agent"), trial.get("model"))
        if key in remaining:
            remaining[key] -= 1
    return [{"dataset": dataset, "task": task, "agent": agent, "model": model,
             "missing": count}
            for (dataset, task, agent, model), count in sorted(remaining.items()) if count > 0]


def _evidence_links(job_dir: Path, trial: str) -> list[dict]:
    candidates = (("result.json", f"{trial}/result.json"), ("trial.log", f"{trial}/trial.log"),
                  ("agent/native.jsonl", f"{trial}/agent/native.jsonl"))
    links = [{"label": label, "path": path} for label, path in candidates
             if (job_dir / path).is_file()]
    # Embedded trials only have the job-level copy of result.json.
    if not (job_dir / f"{trial}/result.json").is_file() and (job_dir / "result.json").is_file():
        links.insert(0, {"label": "result.json (job)", "path": "result.json"})
    return links


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
        summary_path = report / "summary.json"
        if not summary_path.is_file():
            return True
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return True
        # Reports generated before diagnostics existed lack this key.
        if any("error_message" not in trial for trial in summary.get("trials", [])):
            return True
        newest = max([_timestamp(job_dir / "result.json"), _timestamp(job_dir / "config.json"),
                      _timestamp(job_dir / "lock.json")]
                     + [_timestamp(path) for path in job_dir.glob("*/result.json")])
        return newest > _timestamp(summary_path)

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
            links = _evidence_links(job_dir, row["trial"])
            trials.append({
                "trial": row["trial"], "task": row.get("task"),
                "agent": row.get("agent"), "model": row.get("model"),
                "dataset": row.get("dataset"), "reward": row.get("reward"),
                "error": row.get("error"), "error_message": row.get("error_message"),
                "duration_sec": row.get("duration_sec"),
                "input_tokens": row.get("input_tokens") or 0,
                "output_tokens": row.get("output_tokens") or 0,
                "evidence": links[0]["path"] if links else None,
                "evidence_links": json.dumps(links, ensure_ascii=False) if links else None,
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
        }, combos, trials, _missing_trials(job_dir, summary))

    def _prune(self, seen: set[str]):
        for row in self.database.list_jobs():
            if row["name"] not in seen and not Path(row["path"]).is_dir():
                self.database.delete_job(row["name"])
