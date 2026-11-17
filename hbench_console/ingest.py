"""Scan Harbor job directories, summarize them with hbench and index the result."""
from collections import Counter
from datetime import datetime, timezone
from itertools import islice
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import uuid

from hbench.summary import DIAGNOSTICS_VERSION, aggregate

from .config import Settings
from .db import Database, now


def _timestamp(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _job_evidence_mtime(job_dir: Path) -> float:
    """Return the mtime of the newest evidence file inside a job directory.

    This reflects when the job was actually last modified by a trial, rather
    than when the console last re-indexed it.
    """
    candidates = [
        job_dir / "result.json",
        job_dir / "config.json",
        job_dir / "lock.json",
        job_dir / "fixture.json",
    ]
    candidates.extend(job_dir.glob("*/result.json"))
    mtimes = [_timestamp(p) for p in candidates]
    newest = max(mtimes) if mtimes else 0.0
    return newest if newest > 0 else _timestamp(job_dir)


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
        dataset, task, agent, model = key
        matches = {g["model"] for g in summary.get("groups", [])
                   if g["dataset"] == dataset and g["agent"] == agent
                   and (model == g["model"] or model.endswith("/" + g["model"]))}
        if len(matches) == 1:
            key = (dataset, task, agent, matches.pop())
        remaining[key] = remaining.get(key, 0) + 1
    for trial in summary.get("trials", []):
        key = (trial.get("dataset") or "adhoc", trial.get("task"),
               trial.get("agent"), trial.get("model"))
        # Harbor can record task_name as "<dataset>/<task>" while the lock
        # keeps the dataset and task name in separate fields. Prefer exact IDs.
        dataset, task, agent, model = key
        if key not in remaining and isinstance(task, str) and task.startswith(dataset + "/"):
            key = (dataset, task[len(dataset) + 1:], agent, model)
        if key in remaining:
            remaining[key] -= 1
    return [{"dataset": dataset, "task": task, "agent": agent, "model": model,
             "missing": count}
            for (dataset, task, agent, model), count in sorted(remaining.items()) if count > 0]


def _evidence_name(name) -> bool:
    return (isinstance(name, str) and bool(name.strip()) and name != "."
            and not any(part in name for part in ("..", "/", "\\", "\x00")))


def _evidence_confined(path: Path, boundary: Path) -> bool:
    try:
        return path.resolve().is_relative_to(boundary)
    except (OSError, ValueError, RuntimeError):
        return False


def _evidence_trial_dir(job_dir: Path, trial: str) -> Path | None:
    if not _evidence_name(trial):
        return None
    try:
        base = job_dir.resolve() / trial
    except (OSError, ValueError, RuntimeError):
        return None
    # Keep the lexical trial boundary: a symlink to a sibling trial is not evidence.
    return base if _evidence_confined(base, base) else None


def _read_evidence_result(path: Path, boundary: Path) -> dict | None:
    if not _evidence_confined(path, boundary):
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RuntimeError):
        return None
    return payload if isinstance(payload, dict) else None


def _recorded_step_names(job_dir: Path, row: dict, job_result: dict | None) -> list[str]:
    base = _evidence_trial_dir(job_dir, row["trial"])
    if base is None:
        return []
    raw = _read_evidence_result(base / "result.json", base)
    steps = raw.get("step_results") if raw is not None else None
    if not isinstance(steps, list):
        embedded = (job_result or {}).get("trial_results")
        if isinstance(embedded, list):
            current = next((item for item in embedded if isinstance(item, dict)
                            and item.get("trial_name") == row["trial"]), {})
            steps = current.get("step_results")
    if not isinstance(steps, list):
        diagnostics = row.get("diagnostics")
        steps = diagnostics.get("steps") if isinstance(diagnostics, dict) else None
    if not isinstance(steps, list):
        return []
    # An explicit empty raw list stays empty; never invent names from step indices.
    return list(dict.fromkeys(step["step_name"] for step in steps
                              if isinstance(step, dict) and _evidence_name(step.get("step_name"))))


def _evidence_links(job_dir: Path, trial: str, step_names: list[str] | tuple[str, ...] = ()) -> list[dict]:
    trial_dir = _evidence_trial_dir(job_dir, trial)
    if trial_dir is None:
        return []
    root = trial_dir.parent
    links = []

    def add_file(path: Path):
        if not _evidence_confined(path, trial_dir):
            return
        try:
            if path.is_file():
                links.append({"label": path.relative_to(trial_dir).as_posix(),
                              "path": path.relative_to(root).as_posix()})
        except (OSError, ValueError, RuntimeError):
            pass

    for name in ("result.json", "trial.log"):
        add_file(trial_dir / name)
    agents = [trial_dir / "agent"]
    agents.extend(trial_dir / "steps" / name / "agent"
                  for name in dict.fromkeys(name for name in step_names if _evidence_name(name)))
    for agent in agents:
        if not _evidence_confined(agent, trial_dir):
            continue
        for filename in ("native.jsonl", "codex.txt", "pi.txt", "trajectory.json"):
            add_file(agent / filename)
        # Preserve the bounded native-session scan, only beneath recorded steps.
        for directory in ("sessions", "pi/sessions"):
            base = agent / directory
            if not _evidence_confined(base, trial_dir):
                continue
            try:
                for path in sorted(islice(base.rglob("*.jsonl"), 20)):
                    add_file(path)
            except (OSError, ValueError, RuntimeError):
                # Evidence discovery is diagnostic only and must not block indexing.
                continue
    # Embedded trials only have the job-level copy of result.json.
    if not any(link["label"] == "result.json" for link in links):
        path = root / "result.json"
        try:
            if _evidence_confined(path, root) and path.is_file():
                links.insert(0, {"label": "result.json (job)", "path": "result.json"})
        except (OSError, ValueError, RuntimeError):
            pass
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
            if not self._is_job_dir(entry):
                continue
            seen.add(entry.name)
            results.append(self._ingest(entry))
        self._prune(seen)
        return results

    @staticmethod
    def _is_job_dir(job_dir: Path) -> bool:
        return (
            (job_dir / "config.json").is_file()
            or (job_dir / "result.json").is_file()
            or any(job_dir.glob("*/result.json"))
        )

    def _ingest(self, job_dir: Path) -> dict:
        name = job_dir.name
        report = self.settings.reports_root / name
        try:
            # A running job has config.json (and usually lock.json) but no result.json yet.
            # Index it with zero recorded trials so the console shows it before completion.
            if ((job_dir / "config.json").is_file() and not (job_dir / "result.json").is_file()
                    and not any(job_dir.glob("*/result.json"))):
                return self._ingest_running(job_dir)
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

    def _ingest_running(self, job_dir: Path) -> dict:
        name = job_dir.name
        try:
            config = json.loads((job_dir / "config.json").read_text(encoding="utf-8"))
            planned = config.get("n_trials")
        except (OSError, ValueError):
            planned = None
        summary = {
            "recorded_trials": 0,
            "planned_trials": planned,
            "groups": [],
            "trials": [],
            "pass_rate_planned": None,
            "note": "Job is running; no trial results yet.",
        }
        try:
            trials = _planned_trials(job_dir)
            if trials:
                expected = Counter((agent, model, dataset) for dataset, _, agent, model in trials)
                summary.update(aggregate([], expected=expected))
        except (OSError, ValueError, TypeError, KeyError):
            pass
        self._store(name, job_dir, summary)
        return {"name": name, "status": "running", "recorded_trials": 0}

    @staticmethod
    def _needs_refresh(job_dir: Path, report: Path) -> bool:
        summary_path = report / "summary.json"
        if not summary_path.is_file():
            return True
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return True
        # Regenerate old reports even when result timestamps did not change.
        if summary.get("diagnostics_version") != DIAGNOSTICS_VERSION:
            return True
        if any("diagnostics" not in trial for trial in summary.get("trials", [])):
            return True
        return _job_evidence_mtime(job_dir) > _timestamp(summary_path)

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
                "input_tokens": group.get("input_tokens"),
                "output_tokens": group.get("output_tokens"),
                "input_tokens_missing": group.get("input_tokens_missing"),
                "output_tokens_missing": group.get("output_tokens_missing"),
            })
        trials = []
        try:
            job_result = _read_evidence_result(job_dir / "result.json", job_dir.resolve())
        except (OSError, ValueError, RuntimeError):
            job_result = None
        for row in summary.get("trials", []):
            links = _evidence_links(job_dir, row["trial"], _recorded_step_names(job_dir, row, job_result))
            trials.append({
                "trial": row["trial"], "task": row.get("task"),
                "agent": row.get("agent"), "model": row.get("model"),
                "dataset": row.get("dataset"), "reward": row.get("reward"),
                "error": row.get("error"), "error_message": row.get("error_message"),
                "duration_sec": row.get("duration_sec"),
                "input_tokens": row.get("input_tokens"),
                "output_tokens": row.get("output_tokens"),
                "diagnostics": json.dumps(row.get("diagnostics") or {}, ensure_ascii=False),
                "evidence": links[0]["path"] if links else None,
                "evidence_links": json.dumps(links, ensure_ascii=False) if links else None,
            })
        self.database.replace_job({
            "name": name,
            "path": str(job_dir),
            "origin": "run" if self.database.has_run(name) else "import",
            "fixture": (job_dir / "fixture.json").is_file(),
            "updated_at": datetime.fromtimestamp(
                _job_evidence_mtime(job_dir), timezone.utc
            ).isoformat(timespec="seconds"),
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
