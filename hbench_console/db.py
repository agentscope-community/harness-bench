"""SQLite index for console runs and Harbor evidence.

Evidence stays in artifacts/jobs; the database only holds an index so pages can
be served without rescanning thousands of trial directories on every request.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    name TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    matrix_yaml TEXT NOT NULL,
    plan_path TEXT,
    job_dir TEXT,
    planned_trials INTEGER,
    dry_run INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    exit_code INTEGER,
    error TEXT,
    log_path TEXT,
    pid INTEGER,
    cancel_requested INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS jobs (
    name TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    origin TEXT NOT NULL,
    fixture INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    recorded_trials INTEGER,
    planned_trials INTEGER,
    pass_rate_planned REAL,
    note TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS combos (
    job TEXT NOT NULL,
    agent TEXT NOT NULL,
    model TEXT NOT NULL,
    dataset TEXT NOT NULL,
    planned INTEGER,
    recorded INTEGER NOT NULL,
    missing INTEGER,
    errors INTEGER NOT NULL,
    scored INTEGER NOT NULL,
    passed INTEGER NOT NULL,
    unscored INTEGER NOT NULL,
    pass_rate_planned REAL,
    pass_rate_recorded REAL,
    mean_reward_scored REAL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    input_tokens_missing INTEGER,
    output_tokens_missing INTEGER,
    PRIMARY KEY (job, agent, model, dataset)
);
CREATE TABLE IF NOT EXISTS trials (
    job TEXT NOT NULL,
    trial TEXT NOT NULL,
    task TEXT,
    agent TEXT,
    model TEXT,
    dataset TEXT,
    reward REAL,
    error TEXT,
    error_message TEXT,
    duration_sec REAL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    evidence TEXT,
    evidence_links TEXT,
    diagnostics TEXT,
    PRIMARY KEY (job, trial)
);
CREATE INDEX IF NOT EXISTS trials_job ON trials (job);
CREATE TABLE IF NOT EXISTS missing_trials (
    job TEXT NOT NULL,
    dataset TEXT NOT NULL,
    task TEXT NOT NULL,
    agent TEXT NOT NULL,
    model TEXT NOT NULL,
    missing INTEGER NOT NULL,
    PRIMARY KEY (job, dataset, task, agent, model)
);
"""

RUN_FIELDS = ("name", "status", "matrix_yaml", "plan_path", "job_dir", "planned_trials",
              "dry_run", "created_at", "started_at", "finished_at", "exit_code", "error",
              "log_path", "pid", "cancel_requested")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=30000")
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            # Console databases created before diagnostics existed keep working.
            migrations = {
                "trials": (("error_message", "TEXT"), ("evidence_links", "TEXT"),
                           ("diagnostics", "TEXT")),
                "combos": (("input_tokens_missing", "INTEGER"), ("output_tokens_missing", "INTEGER")),
            }
            for table, fields in migrations.items():
                columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
                for name, kind in fields:
                    if name not in columns:
                        connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")

    # ---------------------------------------------------------------- runs
    def create_run(self, name: str, matrix_yaml: str, dry_run: bool):
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO runs (name, status, matrix_yaml, dry_run, created_at) "
                "VALUES (?, 'queued', ?, ?, ?)",
                (name, matrix_yaml, int(dry_run), now()))

    def update_run(self, name: str, **changes):
        unknown = set(changes) - set(RUN_FIELDS)
        if unknown:
            raise ValueError("Unknown run fields: " + ", ".join(sorted(unknown)))
        if not changes:
            return
        assignments = ", ".join(f"{key} = ?" for key in changes)
        with self.connect() as connection:
            connection.execute(f"UPDATE runs SET {assignments} WHERE name = ?",
                               (*changes.values(), name))

    def get_run(self, name: str):
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None

    def list_runs(self, limit: int = 100):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def queued_runs(self):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT name FROM runs WHERE status = 'queued' ORDER BY created_at").fetchall()
        return [row["name"] for row in rows]

    def mark_interrupted(self):
        with self.connect() as connection:
            connection.execute(
                "UPDATE runs SET status = 'failed', finished_at = ?, "
                "error = '控制台重启，运行已中断；可用 harbor jobs resume 续跑' "
                "WHERE status IN ('planning', 'running', 'summarizing', 'archiving')",
                (now(),))

    # ---------------------------------------------------------------- jobs
    def replace_job(self, job: dict, combos: list[dict], trials: list[dict],
                    missing: list[dict] | None = None):
        with self.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO jobs (name, path, origin, fixture, updated_at, "
                "recorded_trials, planned_trials, pass_rate_planned, note, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job["name"], job["path"], job["origin"], int(job["fixture"]), job["updated_at"],
                 job.get("recorded_trials"), job.get("planned_trials"),
                 job.get("pass_rate_planned"), job.get("note"), job.get("error")))
            connection.execute("DELETE FROM combos WHERE job = ?", (job["name"],))
            connection.execute("DELETE FROM trials WHERE job = ?", (job["name"],))
            connection.execute("DELETE FROM missing_trials WHERE job = ?", (job["name"],))
            connection.executemany(
                "INSERT INTO combos (job, agent, model, dataset, planned, recorded, missing, "
                "errors, scored, passed, unscored, pass_rate_planned, pass_rate_recorded, "
                "mean_reward_scored, input_tokens, output_tokens, input_tokens_missing, output_tokens_missing) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(job["name"], c["agent"], c["model"], c["dataset"], c.get("planned"),
                  c["recorded"], c.get("missing"), c["errors"], c["scored"], c["passed"],
                  c["unscored"], c.get("pass_rate_planned"), c.get("pass_rate_recorded"),
                  c.get("mean_reward_scored"), c.get("input_tokens") or 0, c.get("output_tokens") or 0,
                  c.get("input_tokens_missing"), c.get("output_tokens_missing"))
                 for c in combos])
            connection.executemany(
                "INSERT INTO trials (job, trial, task, agent, model, dataset, reward, error, "
                "error_message, duration_sec, input_tokens, output_tokens, evidence, evidence_links, diagnostics) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(job["name"], t["trial"], t.get("task"), t.get("agent"), t.get("model"),
                  t.get("dataset"), t.get("reward"), t.get("error"), t.get("error_message"),
                  t.get("duration_sec"), t.get("input_tokens") or 0,
                  t.get("output_tokens") or 0, t.get("evidence"), t.get("evidence_links"),
                  t.get("diagnostics"))
                 for t in trials])
            connection.executemany(
                "INSERT INTO missing_trials (job, dataset, task, agent, model, missing) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(job["name"], m["dataset"], m["task"], m["agent"], m["model"], m["missing"])
                 for m in missing or []])

    def record_job_error(self, name: str, path: str, error: str):
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO jobs (name, path, origin, fixture, updated_at, error) "
                "VALUES (?, ?, 'import', 0, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET updated_at = excluded.updated_at, error = excluded.error",
                (name, path, now(), error))

    def delete_job(self, name: str):
        with self.connect() as connection:
            connection.execute("DELETE FROM jobs WHERE name = ?", (name,))
            connection.execute("DELETE FROM combos WHERE job = ?", (name,))
            connection.execute("DELETE FROM trials WHERE job = ?", (name,))
            connection.execute("DELETE FROM missing_trials WHERE job = ?", (name,))

    def job_names(self):
        with self.connect() as connection:
            rows = connection.execute("SELECT name FROM jobs ORDER BY updated_at DESC").fetchall()
        return [row["name"] for row in rows]

    def has_run(self, name: str) -> bool:
        with self.connect() as connection:
            return connection.execute("SELECT 1 FROM runs WHERE name = ?", (name,)).fetchone() is not None

    def list_jobs(self):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT j.*, "
                "(SELECT COUNT(*) FROM combos c WHERE c.job = j.name) AS combos, "
                "(SELECT COALESCE(SUM(c.errors), 0) FROM combos c WHERE c.job = j.name) AS errors "
                "FROM jobs j ORDER BY j.updated_at DESC").fetchall()
        return [dict(row) for row in rows]

    def get_job(self, name: str):
        with self.connect() as connection:
            job = connection.execute("SELECT * FROM jobs WHERE name = ?", (name,)).fetchone()
            if job is None:
                return None
            combos = connection.execute(
                "SELECT * FROM combos WHERE job = ? ORDER BY dataset, agent, model", (name,)).fetchall()
        groups = [dict(row) for row in combos]
        for group in groups:
            for field in ("input_tokens", "output_tokens"):
                missing = group.get(field + "_missing")
                if missing is None or not group["recorded"] or missing == group["recorded"]:
                    group[field] = None
        return {**dict(job), "combos": groups}

    def query_trials(self, job: str, *, agent=None, model=None, dataset=None,
                     outcome=None, query=None, limit=200, offset=0):
        clauses, values = ["job = ?"], [job]
        if agent:
            clauses.append("agent = ?")
            values.append(agent)
        if model:
            clauses.append("model = ?")
            values.append(model)
        if dataset:
            clauses.append("dataset = ?")
            values.append(dataset)
        if query:
            clauses.append("(trial LIKE ? OR task LIKE ? OR error LIKE ? OR error_message LIKE ?)")
            pattern = f"%{query}%"
            values.extend([pattern, pattern, pattern, pattern])
        if outcome == "passed":
            clauses.append("error IS NULL AND reward = 1")
        elif outcome == "failed":
            clauses.append("error IS NULL AND reward IS NOT NULL AND reward != 1")
        elif outcome == "error":
            clauses.append("error IS NOT NULL")
        elif outcome == "unscored":
            clauses.append("error IS NULL AND reward IS NULL")
        where = " AND ".join(clauses)
        with self.connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM trials WHERE {where}", values).fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM trials WHERE {where} "
                "ORDER BY dataset, agent, model, trial LIMIT ? OFFSET ?",
                (*values, limit, offset)).fetchall()
        return total, [self._trial(row) for row in rows]

    @staticmethod
    def _trial(row):
        trial = dict(row)
        raw = trial.get("evidence_links")
        try:
            trial["evidence_links"] = json.loads(raw) if raw else []
        except ValueError:
            trial["evidence_links"] = []
        try:
            diagnostics = json.loads(trial.get("diagnostics") or "{}")
            trial["diagnostics"] = diagnostics if isinstance(diagnostics, dict) else {}
        except ValueError:
            trial["diagnostics"] = {}
        usage = trial["diagnostics"].get("usage") or {}
        # Old NOT NULL columns retain storage compatibility; the API exposes unknown as null.
        for field in ("input_tokens", "output_tokens"):
            if not isinstance(usage, dict) or usage.get(field + "_known") is not True:
                trial[field] = None
        return trial

    def missing_trials(self, job: str, limit: int = 2000):
        with self.connect() as connection:
            total = connection.execute(
                "SELECT COALESCE(SUM(missing), 0) FROM missing_trials WHERE job = ?",
                (job,)).fetchone()[0]
            rows = connection.execute(
                "SELECT dataset, task, agent, model, missing FROM missing_trials "
                "WHERE job = ? ORDER BY dataset, agent, model, task LIMIT ?",
                (job, limit)).fetchall()
        return total, [dict(row) for row in rows]

    def job_trials(self, job: str):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT dataset, task, agent, model, reward, error FROM trials WHERE job = ? "
                "ORDER BY dataset, task, agent, model", (job,)).fetchall()
        return [dict(row) for row in rows]
