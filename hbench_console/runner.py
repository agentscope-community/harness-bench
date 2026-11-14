"""Single-worker queue that drives one hbench job at a time.

Harbor owns scheduling inside a job. The console only decides which job runs
next, records state, and forwards cancellation to the hbench process group.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import threading
import time

from .config import Settings
from .db import Database, now
from .ingest import Ingestor

NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
ACTIVE = ("planning", "running", "summarizing", "archiving")
CANCEL_GRACE_SEC = 120


class RunnerError(ValueError):
    pass


class Runner:
    def __init__(self, settings: Settings, database: Database, ingestor: Ingestor):
        self.settings = settings
        self.database = database
        self.ingestor = ingestor
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._process: subprocess.Popen | None = None
        self._process_lock = threading.Lock()
        self._active: str | None = None

    # ------------------------------------------------------------- lifecycle
    def start(self):
        self.database.mark_interrupted()
        for name in self.database.queued_runs():
            self._queue.put(name)
        self._thread = threading.Thread(target=self._loop, name="hbench-console-worker", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._terminate()
        self._queue.put(None)

    def status(self):
        return {"active": self._active, "queued": self._queue.qsize(),
                "allow_real_runs": self.settings.allow_real_runs}

    # -------------------------------------------------------------- submit
    def submit(self, name: str, matrix_yaml: str, dry_run: bool):
        if not NAME.match(name):
            raise RunnerError("运行名称只能使用小写字母、数字、点、下划线和连字符")
        matrix_path = self.settings.plans_root / f"{name}.matrix.yaml"
        plan_path = self.settings.plans_root / f"{name}.yaml"
        job_dir = self.settings.jobs_root / name
        if self.database.get_run(name) or matrix_path.exists() or plan_path.exists() or job_dir.exists():
            raise RunnerError("同名运行或作业已存在，请换一个名称")
        if not dry_run and not self.settings.allow_real_runs:
            raise RunnerError("真实运行未授权：需要设置 CONSOLE_ALLOW_REAL_RUNS=1")
        self.settings.plans_root.mkdir(parents=True, exist_ok=True)
        matrix_path.write_text(matrix_yaml, encoding="utf-8")
        self.database.create_run(name, matrix_yaml, dry_run)
        self._queue.put(name)
        return self.database.get_run(name)

    # -------------------------------------------------------------- cancel
    def cancel(self, name: str):
        run = self.database.get_run(name)
        if run is None:
            raise RunnerError("运行不存在")
        if run["status"] == "queued":
            self.database.update_run(name, status="cancelled", cancel_requested=1, finished_at=now())
            return self.database.get_run(name)
        if run["status"] in ACTIVE:
            self.database.update_run(name, cancel_requested=1)
            self._terminate()
            return self.database.get_run(name)
        raise RunnerError("运行已结束，无法取消")

    def _terminate(self):
        with self._process_lock:
            process = self._process
        if process is not None and process.poll() is None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except OSError:
                process.terminate()

    # ---------------------------------------------------------------- loop
    def _loop(self):
        while not self._stop.is_set():
            try:
                name = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if name is None:
                break
            run = self.database.get_run(name)
            if run is None:
                continue
            if run["status"] != "queued":
                continue
            self._active = name
            try:
                self._execute(run)
            except Exception as error:  # the worker must survive one bad job
                self._finish_failed(run, f"{type(error).__name__}: {str(error)[:300]}")
            finally:
                self._active = None
                self._process = None
        return

    # ------------------------------------------------------------- execution
    def _execute(self, run):
        name = run["name"]
        matrix_path = self.settings.plans_root / f"{name}.matrix.yaml"
        plan_path = self.settings.plans_root / f"{name}.yaml"
        job_dir = self.settings.jobs_root / name
        report_dir = self.settings.reports_root / name
        log_path = self.settings.logs_dir / f"{name}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.jobs_root.mkdir(parents=True, exist_ok=True)

        with log_path.open("a", encoding="utf-8") as log:
            self._log(log, f"--- {name} queued {run['created_at']} ---")
            self.database.update_run(name, status="planning", log_path=str(log_path))
            code = self._spawn([*self.settings.hbench_cmd, "matrix", "--config", str(matrix_path),
                                "--output", str(plan_path), "--name", name], log)
            if self._cancel_requested(name):
                self.database.update_run(name, status="cancelled", finished_at=now())
                return
            if code != 0:
                return self._finish_failed(run, "计划渲染失败，见日志")
            contract_path = plan_path.with_suffix(".plan.json")
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            self.database.update_run(name, status="running", started_at=now(),
                                     plan_path=str(plan_path), job_dir=str(job_dir),
                                     planned_trials=contract["planned_trials"])
            self._log(log, f"plan: {contract['planned_trials']} trials, "
                           f"{len(contract.get('combinations', []))} combinations, "
                           f"backend={contract.get('backend')}, dry_run={bool(run['dry_run'])}")

            command = [*self.settings.hbench_cmd, "run", "--config", str(plan_path)]
            if run["dry_run"]:
                command.append("--dry-run")
            code = self._spawn(command, log)
            cancelled = self._cancel_requested(name)
            if cancelled:
                self.database.update_run(name, status="cancelled", exit_code=code, finished_at=now())
                self._log(log, "cancelled")
                return
            if code != 0:
                return self._finish_failed(run, f"hbench run 退出码 {code}，见日志")
            if run["dry_run"]:
                self.database.update_run(name, status="succeeded", exit_code=0, finished_at=now())
                self._log(log, "dry-run succeeded")
                return
            return self._after_run(name, log, report_dir)

    def _after_run(self, name: str, log, report_dir: Path):
        self.database.update_run(name, status="summarizing")
        code = self._spawn([*self.settings.hbench_cmd, "summary",
                            str(self.settings.jobs_root / name), "--output", str(report_dir)], log)
        if self._cancel_requested(name):
            self.database.update_run(name, status="cancelled", finished_at=now())
            return
        if code != 0:
            return self._finish_failed(self.database.get_run(name), "结果汇总失败，见日志")
        if os.environ.get("HB_OSS_BUCKET", "").strip():
            self.database.update_run(name, status="archiving")
            self._log(log, "archiving to OSS (best effort)")
            code = self._spawn([*self.settings.hbench_cmd, "archive",
                                str(self.settings.jobs_root / name), "--report", str(report_dir)], log)
            if code != 0:
                # Archiving never changes the evaluation outcome.
                self._log(log, "archive failed; local evidence is intact")
        self.database.update_run(name, status="succeeded", finished_at=now())
        self._log(log, "succeeded")
        try:
            self.ingestor.sync()
        except Exception as error:
            self._log(log, f"index refresh failed: {type(error).__name__}")

    def _cancel_requested(self, name: str) -> bool:
        run = self.database.get_run(name)
        return bool(run and run["cancel_requested"])

    def _finish_failed(self, run, message: str):
        name = run["name"]
        self.database.update_run(name, status="failed", error=message, finished_at=now())
        log_path = self.database.get_run(name).get("log_path")
        if log_path:
            with Path(log_path).open("a", encoding="utf-8") as log:
                self._log(log, f"failed: {message}")

    # ------------------------------------------------------------- subprocess
    def _spawn(self, command: list[str], log) -> int:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = (str(self.settings.repo_root) + os.pathsep
                                     + environment.get("PYTHONPATH", ""))
        self._log(log, "$ " + " ".join(command))
        log.flush()
        process = subprocess.Popen(command, cwd=self.settings.repo_root, env=environment,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        with self._process_lock:
            self._process = process
        self.database.update_run(self._active, pid=process.pid)
        terminated_at = None
        while True:
            code = process.poll()
            if code is not None:
                break
            if terminated_at is None and self._active and self._cancel_requested(self._active):
                terminated_at = time.monotonic()
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                except OSError:
                    process.terminate()
            elif terminated_at is not None and time.monotonic() - terminated_at > CANCEL_GRACE_SEC:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except OSError:
                    process.kill()
            time.sleep(0.5)
        with self._process_lock:
            self._process = None
        self.database.update_run(self._active, pid=None)
        self._log(log, f"[exit {code}]")
        return code

    @staticmethod
    def _log(log, message: str):
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        log.write(f"{stamp} {message}\n")
        log.flush()
