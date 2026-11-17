"""Console outcome counts with unfinished trials separated from missing results."""
from pathlib import Path

from .ingest import _agent_identity, _read_evidence_result
from .progress import scan_job


def job_status(path: str, run: dict | None = None) -> str:
    job_dir = Path(path)
    # A cancelled/interrupted console run can leave an unfinished Harbor result.
    if run and run["status"] in {"failed", "cancelled", "succeeded"}:
        return "indexed"
    result = _read_evidence_result(job_dir / "result.json", job_dir.resolve())
    if result is not None:
        if "finished_at" in result and result["finished_at"] is None:
            return "running"
        return "indexed"
    if (job_dir / "result.json").exists():
        return "unknown"
    return "running" if (job_dir / "config.json").is_file() else "unknown"


def job_statistics(job: dict, run: dict | None = None) -> dict:
    """Enrich an index snapshot without changing archived benchmark scores.

    Missing means no result in a finished job. While it is active, unrecorded
    attempts are either running (a trial directory with config) or queued.
    """
    job = {**job, "combos": [dict(group) for group in job["combos"]]}
    job["status"] = job_status(job["path"], run)
    live = job["status"] == "running"
    groups = job["combos"]
    for group in groups:
        group["failed"] = group["scored"] - group["passed"]
        # Unlike the archive's unscored field, console categories do not overlap.
        group["unscored"] = max(0, group["recorded"] - group["scored"] - group["errors"])
        group["running"] = 0
        group["queued"] = 0
    if live:
        job_dir = Path(job["path"])
        progress = scan_job(job_dir, job.get("planned_trials"), None, True)
        job["progress"] = progress
        # Do not use progress.active: that display list is capped at 50 trials.
        for entry in job_dir.iterdir():
            if entry.name.startswith(".") or not entry.is_dir() or (entry / "result.json").is_file():
                continue
            config = _read_evidence_result(entry / "config.json", job_dir.resolve())
            if not config:
                continue
            agent, model = _agent_identity(config.get("agent") or {})
            task = config.get("task") or {}
            source = task.get("source") or "adhoc"
            candidates = [g for g in groups if g["agent"] == agent and g["dataset"] == source
                          and (model == g["model"] or model.endswith("/" + g["model"]))]
            if len(candidates) == 1:
                candidates[0]["running"] += 1
        for group in groups:
            remaining = group.get("missing")
            if remaining is not None:
                group["running"] = min(group["running"], remaining)
                group["queued"] = max(0, remaining - group["running"])
            else:
                group["queued"] = None
            group["missing"] = 0
    job["counts"] = {key: sum(g.get(key) or 0 for g in groups)
                     for key in ("scored", "passed", "failed", "errors", "unscored", "running", "queued", "missing")}
    # Plans that cannot yet be expanded still have useful job-level progress.
    if live and not groups and job.get("progress"):
        job["counts"].update(running=job["progress"]["running"], queued=job["progress"]["queued"])
    return job
