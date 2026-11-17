"""FastAPI control plane and read-only evidence API."""
from contextlib import asynccontextmanager
import fnmatch
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import uuid

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import yaml

from . import __version__
from .config import REPO_ROOT, Settings
from .db import Database
from .diagnosis import diagnose
from .ingest import Ingestor
from .runner import NAME, Runner

MATRIX_KEYS = {"name", "cluster", "dataset_root", "benchmarks", "models", "harnesses",
               "concurrency", "attempts", "selection", "image_map"}
OVERRIDE_KEYS = {"cluster", "concurrency", "attempts", "n_tasks", "tasks", "exclude_gpu"}
EVIDENCE_LIMIT = 32 * 1024 * 1024
PRIVATE_FIELDS = {"env", "extra_env", "headers", "extra_headers", "extra_body", "auth", "credentials", "token", "cookie", "cookies"}
SECRET_FIELD = re.compile(r"(?:api[_-]?key|access[_-]?key(?:[_-]?(?:id|secret))?|password|passwd|secret|authorization|access[_-]?token|refresh[_-]?token|private[_-]?key)$", re.I)


def _redact_evidence_text(text, secrets):
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)\bBearer\s+[^\s\"',;}]+", "Bearer [REDACTED]", text)
    text = re.sub(r"(?i)(\bauthorization[\"']?\s*[=:]\s*[\"']?)[^\r\n\"']+", r"\1[REDACTED]", text)
    return re.sub(r"(?i)((?:api[_-]?key|password|secret|access[_-]?token)[\"']?\s*[=:]\s*[\"']?)[^\s\"',;}]+", r"\1[REDACTED]", text)


def _redact_evidence(value, secrets):
    """Redact a response copy; raw local evidence is never rewritten."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if isinstance(key, str) and (key.lower() in PRIVATE_FIELDS or SECRET_FIELD.search(key)):
                def collect(item, explicit=False):
                    # Generic env/body values include flags such as "1". They must
                    # not be globally substituted into counters or unrelated text.
                    if isinstance(item, str) and item and (explicit or len(item) >= 4):
                        secrets.add(item)
                    elif isinstance(item, dict):
                        for name, nested in item.items():
                            collect(nested, explicit or bool(SECRET_FIELD.search(str(name))))
                    elif isinstance(item, list):
                        for nested in item:
                            collect(nested, explicit)
                collect(item, bool(SECRET_FIELD.search(key)))
                result[key] = "[REDACTED]"
            else:
                result[key] = _redact_evidence(item, secrets)
        return result
    if isinstance(value, list):
        return [_redact_evidence(item, secrets) for item in value]
    if isinstance(value, str):
        return _redact_evidence_text(value, secrets)
    return value


def _public_evidence(target, root):
    if target.suffix.lower() not in {".json", ".jsonl", ".log", ".txt", ".yaml", ".yml"}:
        raise HTTPException(403, "仅提供脱敏文本证据；其他原始文件请在本机查看")
    if target.stat().st_size > EVIDENCE_LIMIT:
        raise HTTPException(413, "证据超过脱敏预览上限，请在本机查看原始文件")
    text = target.read_text(encoding="utf-8", errors="replace")
    secrets = {value for key, value in os.environ.items() if value and SECRET_FIELD.search(key)}
    # Credentials may appear in a log as well as the nearby recorded configuration.
    sources = {root / "config.json", root / "lock.json", target}
    for parent in (target.parent, *target.parents):
        if not parent.is_relative_to(root):
            break
        candidate = parent / "result.json"
        if candidate.is_file():
            sources.add(candidate)
            break
    for source in sources:
        if not source.is_file() or not source.resolve().is_relative_to(root):
            continue
        if source.stat().st_size > EVIDENCE_LIMIT:
            raise HTTPException(413, "配置超过脱敏预览上限，请在本机查看原始证据")
        try:
            _redact_evidence(json.loads(source.read_text(encoding="utf-8")), secrets)
        except (ValueError, UnicodeError):
            continue
    try:
        if target.suffix.lower() == ".json":
            text = json.dumps(_redact_evidence(json.loads(text), secrets), ensure_ascii=False, indent=2)
        elif target.suffix.lower() == ".jsonl":
            records = []
            for line in text.splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    record = {"type": "console.evidence_redacted", "reason": "malformed_jsonl"}
                _redact_evidence(record, secrets)
                records.append(record)
            # Collect from every record before serializing; decoded string values
            # are redacted before JSON escaping, including quotes and newlines.
            text = "\n".join(json.dumps(_redact_evidence(record, secrets), ensure_ascii=False)
                             for record in records) + "\n"
        elif target.suffix.lower() in {".yaml", ".yml"}:
            value = yaml.safe_load(text)
            _redact_evidence(value, secrets)
            text = yaml.safe_dump(_redact_evidence(value, secrets), allow_unicode=True)
    except (ValueError, yaml.YAMLError):
        raise HTTPException(403, "结构化证据无法安全脱敏，请在本机查看原始文件")
    if target.suffix.lower() in {".log", ".txt"}:
        text = _redact_evidence_text(text, secrets)
    return Response(text, media_type="text/plain", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


STATIC = Path(__file__).resolve().parent / "static"
DENIED = (".env", ".env.*", "*.pem", "*.key", "id_rsa*", "auth.json", "*.p12", "*.jks")


class MatrixRequest(BaseModel):
    matrix: str = Field(min_length=1)
    overrides: dict = Field(default_factory=dict)


class RunRequest(MatrixRequest):
    name: str
    dry_run: bool = True


def apply_overrides(text: str, overrides: dict) -> tuple[str, str | None]:
    """Return (yaml text, matrix name) with form overrides validated and applied."""
    unknown = set(overrides) - OVERRIDE_KEYS
    if unknown:
        raise HTTPException(422, "不支持的覆盖项: " + ", ".join(sorted(unknown)))
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError:
        raise HTTPException(422, "矩阵 YAML 无效")
    if not isinstance(value, dict):
        raise HTTPException(422, "矩阵必须是对象")
    selection = dict(value.get("selection") or {})
    for key in ("cluster", "concurrency", "attempts"):
        if overrides.get(key) not in (None, ""):
            value[key] = overrides[key]
    for key in ("n_tasks", "tasks", "exclude_gpu"):
        if overrides.get(key) not in (None, ""):
            selection[key] = overrides[key]
    if "n_tasks" in overrides and overrides.get("n_tasks") in ("", None):
        selection.pop("n_tasks", None)
    if "tasks" in overrides:
        tasks = overrides.get("tasks")
        if isinstance(tasks, str):
            tasks = [t.strip() for t in tasks.split(",") if t.strip()]
        if tasks:
            selection["tasks"] = tasks
        else:
            selection.pop("tasks", None)
    if selection:
        value["selection"] = selection
    if set(value) - MATRIX_KEYS:
        raise HTTPException(422, "矩阵包含未知字段: " + ", ".join(sorted(set(value) - MATRIX_KEYS)))
    return yaml.safe_dump(value, sort_keys=False), value.get("name")


class Console:
    """Shared state for the app factory and the worker thread."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.database = Database(settings.database)
        self.database.initialize()
        self.ingestor = Ingestor(settings, self.database)
        self.runner = Runner(settings, self.database, self.ingestor)
        self.render_lock = threading.Lock()

    def render(self, text: str, name: str | None) -> dict:
        """Render a plan with the hbench CLI and return its contract."""
        self.settings.tmp_dir.mkdir(parents=True, exist_ok=True)
        work = self.settings.tmp_dir / f"render-{uuid.uuid4().hex[:8]}"
        work.mkdir(parents=True)
        matrix_path, plan_path = work / "matrix.yaml", work / "job.yaml"
        matrix_path.write_text(text, encoding="utf-8")
        job_name = name if name and NAME.match(name) else f"preview-{uuid.uuid4().hex[:8]}"
        command = [*self.settings.hbench_cmd, "matrix", "--config", str(matrix_path),
                   "--output", str(plan_path), "--name", job_name]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        try:
            with self.render_lock:
                completed = subprocess.run(command, cwd=REPO_ROOT, env=env,
                                           capture_output=True, text=True, timeout=180)
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip().splitlines()
                raise HTTPException(422, detail[-1] if detail else "计划渲染失败")
            contract = json.loads(plan_path.with_suffix(".plan.json").read_text(encoding="utf-8"))
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return {"planned_trials": contract.get("planned_trials"),
                "combinations": contract.get("combinations", []),
                "backend": contract.get("backend"),
                "job_name": job_name,
                "matrix": text}


def catalog(settings: Settings) -> dict:
    configs = REPO_ROOT / "configs"
    models = []
    for path in sorted((configs / "models").glob("*.yaml")):
        try:
            profile = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            profile = {}
        variable = profile.get("api_key_name")
        models.append({"profile": path.stem, "api_key_name": variable,
                       "configured": bool(variable and os.environ.get(variable))})
    return {
        "benchmarks": [p.stem for p in sorted((configs / "benchmarks").glob("*.yaml"))],
        "harnesses": [p.stem for p in sorted((configs / "harnesses").glob("*.yaml"))],
        "models": models,
        "clusters": [p.stem for p in sorted((configs / "clusters").glob("*.yaml"))],
        "matrices": [p.stem for p in sorted((configs / "matrices").glob("*.yaml"))],
        "allow_real_runs": settings.allow_real_runs,
        "viewer_url": settings.viewer_url,
        "note": "适配评测，非官方榜单；错误、未评分与缺失 trial 均不计为通过。",
    }


def task_matrix(database: Database, job: str) -> dict:
    rows = database.job_trials(job)
    combos = sorted({(row["agent"], row["model"]) for row in rows})
    cells: dict[tuple, dict] = {}
    for row in rows:
        key = (row["dataset"], row["task"] or row["trial"], row["agent"], row["model"])
        cell = cells.setdefault(key, {"recorded": 0, "passed": 0, "failed": 0,
                                      "error": 0, "unscored": 0})
        cell["recorded"] += 1
        if row["error"] is not None:
            cell["error"] += 1
        elif row["reward"] == 1:
            cell["passed"] += 1
        elif row["reward"] is not None:
            cell["failed"] += 1
        else:
            cell["unscored"] += 1
    tasks = []
    for dataset, task, _, _ in sorted({(row["dataset"], row["task"], 0, 0) for row in rows}):
        empty = {"recorded": 0, "passed": 0, "failed": 0, "error": 0, "unscored": 0}
        tasks.append({
            "dataset": dataset, "task": task,
            "cells": [{"agent": agent, "model": model,
                       **cells.get((dataset, task, agent, model), empty)}
                      for agent, model in combos],
        })
    return {"combos": [{"agent": agent, "model": model} for agent, model in combos], "tasks": tasks}


def create_app(settings: Settings, console: Console | None = None,
               start_worker: bool = True, serve_static: bool = True) -> FastAPI:
    state = console or Console(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_worker:
            state.runner.start()
            if settings.sync_on_start:
                threading.Thread(target=state.ingestor.sync, name="console-initial-sync",
                                 daemon=True).start()
            if settings.sync_interval_seconds > 0:
                def _sync_loop():
                    while True:
                        time.sleep(settings.sync_interval_seconds)
                        try:
                            state.ingestor.sync()
                        except Exception:
                            pass

                threading.Thread(target=_sync_loop, name="console-sync-poller",
                                 daemon=True).start()
        yield
        if start_worker:
            state.runner.stop()

    app = FastAPI(title="hbench console", version=__version__, lifespan=lifespan)
    app.state.console = state

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": __version__,
                "allow_real_runs": settings.allow_real_runs,
                "sync": {"last": state.ingestor.last_sync, "error": state.ingestor.last_error},
                "runner": state.runner.status()}

    @app.get("/api/catalog")
    def get_catalog():
        return catalog(settings)

    @app.get("/api/matrices/{name}")
    def get_matrix(name: str):
        if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$", name):
            raise HTTPException(404, "矩阵不存在")
        path = REPO_ROOT / "configs" / "matrices" / f"{name}.yaml"
        if not path.is_file():
            raise HTTPException(404, "矩阵不存在")
        return {"name": name, "yaml": path.read_text(encoding="utf-8")}

    @app.post("/api/runs/preview")
    def preview_run(request: MatrixRequest):
        text, _ = apply_overrides(request.matrix, request.overrides)
        contract = state.render(text, None)
        contract["matrix"] = text
        return contract

    @app.post("/api/runs")
    def create_run(request: RunRequest):
        text, _ = apply_overrides(request.matrix, request.overrides)
        if not request.dry_run and not settings.allow_real_runs:
            raise HTTPException(403, "真实运行未授权：需要设置 CONSOLE_ALLOW_REAL_RUNS=1")
        state.render(text, request.name)
        try:
            run = state.runner.submit(request.name, text, request.dry_run)
        except ValueError as error:
            raise HTTPException(400, str(error))
        return run

    @app.get("/api/runs")
    def list_runs(limit: int = 100):
        return {"runs": state.database.list_runs(max(1, min(limit, 500)))}

    @app.get("/api/runs/{name}")
    def get_run(name: str):
        run = state.database.get_run(name)
        if run is None:
            raise HTTPException(404, "运行不存在")
        return run

    @app.post("/api/runs/{name}/cancel")
    def cancel_run(name: str):
        try:
            return state.runner.cancel(name)
        except ValueError as error:
            raise HTTPException(400, str(error))

    @app.get("/api/runs/{name}/log")
    def run_log(name: str, offset: int = 0, limit: int = 200_000):
        run = state.database.get_run(name)
        if run is None or not run.get("log_path"):
            raise HTTPException(404, "没有运行日志")
        path = Path(run["log_path"])
        if not path.is_file():
            return {"offset": offset, "text": "", "eof": True}
        size = path.stat().st_size
        offset = max(0, min(offset, size))
        with path.open("rb") as stream:
            stream.seek(offset)
            data = stream.read(max(0, min(limit, 2_000_000)))
        return {"offset": offset + len(data), "text": data.decode("utf-8", errors="replace"),
                "eof": offset + len(data) >= size}

    @app.post("/api/jobs/sync")
    def sync_jobs():
        return {"results": state.ingestor.sync(), "last": state.ingestor.last_sync}

    def _job_status(path: str) -> str:
        try:
            job_path = Path(path)
        except (OSError, ValueError):
            return "unknown"
        if (job_path / "result.json").is_file():
            return "indexed"
        if (job_path / "config.json").is_file():
            return "running"
        return "unknown"

    @app.get("/api/jobs")
    def list_jobs():
        jobs = state.database.list_jobs()
        for job in jobs:
            job["status"] = _job_status(job["path"])
        return {"jobs": jobs,
                "sync": {"last": state.ingestor.last_sync, "error": state.ingestor.last_error}}

    @app.get("/api/jobs/{name}")
    def get_job(name: str):
        job = state.database.get_job(name)
        if job is None:
            raise HTTPException(404, "作业不存在")
        job["status"] = _job_status(job["path"])
        job["agents"] = sorted({c["agent"] for c in job["combos"]})
        job["models"] = sorted({c["model"] for c in job["combos"]})
        job["datasets"] = sorted({c["dataset"] for c in job["combos"]})
        return job

    @app.get("/api/jobs/{name}/trials")
    def job_trials(name: str, agent: str | None = None, model: str | None = None,
                   dataset: str | None = None, outcome: str | None = None,
                   q: str | None = None, limit: int = 200, offset: int = 0):
        if state.database.get_job(name) is None:
            raise HTTPException(404, "作业不存在")
        total, rows = state.database.query_trials(
            name, agent=agent, model=model, dataset=dataset, outcome=outcome, query=q,
            limit=max(1, min(limit, 1000)), offset=max(0, offset))
        return {"total": total, "trials": [{**row, **diagnose(row)} for row in rows]}

    @app.get("/api/jobs/{name}/missing")
    def missing_trials(name: str, limit: int = 2000):
        if state.database.get_job(name) is None:
            raise HTTPException(404, "作业不存在")
        total, rows = state.database.missing_trials(name, max(1, min(limit, 10000)))
        return {"total": total, "missing": rows,
                "note": "缺失由 job 的 config/lock 与已记录 trial 求差集得到；"
                        "若作业使用整库或 glob，无法逐题枚举，只能看组合汇总的计数。"}

    @app.get("/api/jobs/{name}/tasks")
    def job_tasks(name: str):
        if state.database.get_job(name) is None:
            raise HTTPException(404, "作业不存在")
        return task_matrix(state.database, name)

    @app.get("/api/compare")
    def compare(jobs: str = Query(..., description="Comma-separated job names")):
        names = [n.strip() for n in jobs.split(",") if n.strip()]
        if not names:
            raise HTTPException(422, "至少选择一个作业")
        result = []
        for name in names:
            job = state.database.get_job(name)
            if job is None:
                raise HTTPException(404, f"作业不存在: {name}")
            result.append({"job": name, "fixture": job["fixture"], "combos": job["combos"]})
        return {"jobs": result}

    @app.get("/api/evidence/{job}/{path:path}")
    def evidence(job: str, path: str):
        record = state.database.get_job(job)
        if record is None:
            raise HTTPException(404, "作业不存在")
        root = Path(record["path"]).resolve()
        target = (root / path).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise HTTPException(404, "证据文件不存在")
        if any(fnmatch.fnmatch(target.name, pattern) for pattern in DENIED):
            raise HTTPException(403, "拒绝访问敏感文件")
        return _public_evidence(target, root)

    if serve_static:
        app.mount("/static", StaticFiles(directory=STATIC), name="static")

        @app.get("/")
        def index():
            return FileResponse(STATIC / "index.html")

    return app
