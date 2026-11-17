"""Live progress read from a Harbor job directory.

Harbor renders progress only inside its own Rich console and exposes trial
lifecycle hooks to in-process callers. There is no HTTP API or `job stop`
command; cancellation means signalling the `harbor run` process (SIGTERM is
converted to KeyboardInterrupt). The console therefore derives progress from
the job directory:

* a trial directory is created when the trial actually starts;
* ``<trial>/result.json`` appears when it ends;
* the job-level ``config.json``/``lock.json`` are present before that.

The scan is deliberately shallow (one directory level) so it stays cheap even
for several thousand trials.
"""
from datetime import datetime, timezone
import time
from pathlib import Path


def _timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")


def _parse_time(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def _task_name(trial: str) -> str:
    # Harbor names a trial directory ``<task>__<short-suffix>``. Sanitized task
    # names cannot contain ``__`` in practice, but keep the split tolerant.
    return trial.split("__", 1)[0] or trial


def scan_job(job_dir: Path, planned: int | None = None,
             started_at: str | None = None, running: bool | None = None) -> dict | None:
    """Return a progress snapshot for ``job_dir``.

    ``planned`` comes from the rendered contract or lock.json; when absent the
    number of existing trial directories is used so imported jobs still show a
    meaningful value. ``running=False`` freezes the ETA fields for a finished
    job.
    """
    job_dir = Path(job_dir)
    if not job_dir.is_dir():
        return None
    completed = 0
    pending: list[dict] = []
    try:
        entries = list(job_dir.iterdir())
    except OSError:
        return None
    for entry in entries:
        try:
            if entry.name.startswith(".") or not entry.is_dir():
                continue
            result = entry / "result.json"
            if result.is_file():
                completed += 1
            else:
                mtime = entry.stat().st_mtime
                pending.append({"trial": entry.name, "task": _task_name(entry.name),
                                "started_at": _timestamp(mtime)})
        except OSError:
            continue
    pending.sort(key=lambda item: item["started_at"], reverse=True)
    total = int(planned) if isinstance(planned, int) and planned > 0 else completed + len(pending)
    total = max(total, completed)

    now = time.time()
    start = _parse_time(started_at)
    if start is None:
        try:
            start = (job_dir / "config.json").stat().st_mtime
        except OSError:
            start = now
    elapsed = max(0.0, now - start)
    rate = completed / elapsed if elapsed > 0 and completed else None
    remaining = max(0, total - completed)
    eta = remaining / rate if rate else None
    active = bool(running) if running is not None else bool(pending)
    return {
        "planned": total,
        "recorded": completed,
        "running": len(pending),
        "queued": max(0, total - completed - len(pending)),
        "percent": round(100.0 * completed / total, 1) if total else None,
        "started_at": _timestamp(start),
        "elapsed_sec": int(elapsed),
        "rate_per_hour": round(rate * 3600, 2) if rate else None,
        "eta_sec": int(eta) if eta is not None and active else None,
        "active": pending[:50],
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
