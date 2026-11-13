"""Best-effort upload of Harbor job evidence to Alibaba Cloud OSS.

Archiving runs beside an evaluation, never inside it: every problem is printed
with an "[oss]" prefix and swallowed, so OSS can neither block nor fail a trial.
Credentials come only from the ECS instance RAM role; no keys are configured.
"""
from datetime import datetime, timedelta, timezone
import fnmatch
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
import urllib.request

METADATA = "http://100.100.100.200"
EXCLUDED = (".env", ".env.*", "*.pem", "*.key", "id_rsa*", "auth.json")
# Harbor scrubs secrets only inside trial directories; its job-level DEBUG log stays local.
JOB_EXCLUDED = ("job.log",)
# Harbor scrubs a trial directory shortly after writing result.json (after END hooks).
SETTLE_SECONDS = 120
FINAL_TIMEOUT = 1800
SAVE_EVERY = 20
MAX_CONSECUTIVE_FAILURES = 3


def log(message):
    print(f"[oss] {message}", file=sys.stderr, flush=True)


def describe(error):
    """Short, secret-free error text; never print request snapshots or headers."""
    for _ in range(5):  # SDK errors wrap the service error (UploadError -> OperationError -> ServiceError).
        if getattr(error, "request_id", None) or getattr(error, "code", None) or not hasattr(error, "unwrap"):
            break
        inner = error.unwrap()
        if not isinstance(inner, BaseException):
            break
        error = inner
    if getattr(error, "request_id", None) or getattr(error, "code", None):
        return (f"{type(error).__name__} status={getattr(error, 'status_code', '')} "
                f"code={getattr(error, 'code', '')} request_id={getattr(error, 'request_id', '')}")
    return f"{type(error).__name__}: {str(error)[:300]}"


def load_settings(env=None):
    """Return OSS settings from HB_OSS_*; None when archiving is disabled."""
    env = os.environ if env is None else env
    bucket = (env.get("HB_OSS_BUCKET") or "").strip()
    if not bucket:
        return None
    region = (env.get("HB_OSS_REGION") or "").strip()
    if not region:
        raise ValueError("HB_OSS_REGION is required when HB_OSS_BUCKET is set")
    return {"bucket": bucket, "region": region,
            "endpoint": (env.get("HB_OSS_ENDPOINT") or "").strip() or None,
            "prefix": (env.get("HB_OSS_PREFIX") or "harness-bench").strip().strip("/"),
            "role_name": (env.get("HB_OSS_ROLE_NAME") or "").strip() or None}


def target(settings):
    return f"oss://{settings['bucket']}/{settings['prefix']}/" if settings else "(HB_OSS_BUCKET not set)"


class EcsRoleCredentials:
    """Temporary credentials of the ECS instance RAM role (IMDSv2 token first)."""

    def __init__(self, role_name=None, opener=None):
        self.role_name = role_name
        self._open = opener or urllib.request.urlopen
        self._lock = threading.Lock()
        self._cached = None

    def _get(self, path, method="GET", headers=None):
        request = urllib.request.Request(METADATA + path, method=method, headers=headers or {})
        with self._open(request, timeout=3) as response:
            return response.read().decode()

    def __call__(self):
        from alibabacloud_oss_v2.credentials import Credentials
        with self._lock:
            if self._cached and self._cached.expiration - datetime.now(timezone.utc) > timedelta(minutes=10):
                return self._cached
            try:
                token = self._get("/latest/api/token", "PUT", {"X-aliyun-ecs-metadata-token-ttl-seconds": "21600"})
                headers = {"X-aliyun-ecs-metadata-token": token}
            except OSError:
                headers = {}  # Instances that still allow IMDSv1.
            base = "/latest/meta-data/ram/security-credentials/"
            role = self.role_name
            if not role:
                names = self._get(base, headers=headers).split()
                if not names:
                    raise RuntimeError("No RAM role is attached to this ECS instance")
                role = names[0]
            data = json.loads(self._get(base + role, headers=headers))
            if data.get("Code") != "Success":
                raise RuntimeError(f"ECS metadata returned no credentials for role {role}")
            expiration = datetime.strptime(data["Expiration"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            self._cached = Credentials(data["AccessKeyId"], data["AccessKeySecret"], data["SecurityToken"], expiration)
            return self._cached


class Bucket:
    """Thin wrapper over the OSS v2 SDK; the only place that talks to OSS."""

    def __init__(self, settings):
        try:
            import alibabacloud_oss_v2 as oss
        except ImportError as error:
            raise RuntimeError("alibabacloud-oss-v2 is not installed; pip install -r requirements-evals.txt") from error
        config = oss.config.load_default()
        config.region = settings["region"]
        if settings["endpoint"]:
            config.endpoint = settings["endpoint"]
        else:
            config.use_internal_endpoint = True  # Same-region ECS traffic: no Internet egress.
        config.credentials_provider = oss.credentials.CredentialsProviderFunc(EcsRoleCredentials(settings["role_name"]))
        self._oss, self._client = oss, oss.Client(config)
        self._uploader = self._client.uploader()  # Single PUT below 6 MiB, multipart above; SDK retries.
        self.name, self.prefix = settings["bucket"], settings["prefix"]

    def upload(self, key, path):
        self._uploader.upload_file(self._oss.PutObjectRequest(bucket=self.name, key=key), str(path))

    def check(self):
        self._client.list_objects_v2(self._oss.ListObjectsV2Request(bucket=self.name, prefix=self.prefix + "/", max_keys=1))


def _excluded(path):
    return path.is_symlink() or any(fnmatch.fnmatch(path.name, pattern) for pattern in EXCLUDED)


def _trial_ready(folder, now, settle):
    result = folder / "result.json"
    try:
        if now - result.stat().st_mtime < settle:
            return False
        return bool(json.loads(result.read_text()).get("finished_at"))
    except (OSError, ValueError, AttributeError):
        return False


def collect(job, report=None, *, final=False, now=None, settle=SETTLE_SECONDS):
    """Files to archive as (path, relative key), mirroring artifacts/{jobs,reports}/<name>/.

    Before the final pass only job-level files and finished, settled trial
    directories are included, so unscrubbed in-progress output is never sent.
    """
    now = time.time() if now is None else now
    files = []
    job = Path(job)
    if job.is_dir():
        for child in sorted(job.iterdir()):
            if child.is_symlink():
                continue
            if child.is_file():
                if child.name not in JOB_EXCLUDED:
                    files.append((child, f"jobs/{job.name}/{child.name}"))
            elif child.is_dir() and (final or _trial_ready(child, now, settle)):
                files += [(p, f"jobs/{job.name}/{p.relative_to(job).as_posix()}")
                          for p in sorted(child.rglob("*")) if p.is_file()]
    if report and Path(report).is_dir():
        report = Path(report)
        files += [(p, f"reports/{report.name}/{p.relative_to(report).as_posix()}")
                  for p in sorted(report.rglob("*")) if p.is_file()]
    return [(path, key) for path, key in files if not _excluded(path)]


def _state_path(job):
    job = Path(job)
    return job.parent / f".{job.name}.oss-sync.json"


def _save(path, state):
    try:
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(state))
        temporary.replace(path)
    except OSError as error:
        log(f"cannot save upload state {path}: {describe(error)}")


def sync(job, report, bucket, *, final=False, deadline=None):
    """Upload new or changed files once. Never raises; returns True if everything was sent."""
    state_path = _state_path(job)
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    uploaded = failed = streak = 0
    try:
        for path, relative in collect(job, report, final=final):
            if deadline is not None and time.monotonic() > deadline:
                failed += 1
                log("time limit reached; the rest stays local. Retry: python -m hbench archive "
                    f"{job}" + (f" --report {report}" if report else ""))
                break
            try:
                stat = path.stat()
                signature = [stat.st_size, stat.st_mtime_ns]
                if state.get(relative) == signature:
                    continue
                bucket.upload(f"{bucket.prefix}/{relative}" if bucket.prefix else relative, path)
                state[relative] = signature
                uploaded += 1
                streak = 0
                if uploaded % SAVE_EVERY == 0:
                    _save(state_path, state)
            except Exception as error:  # One bad file must not stop the rest.
                failed += 1
                streak += 1
                log(f"upload failed: {relative}: {describe(error)}")
                if streak >= MAX_CONSECUTIVE_FAILURES:
                    # Likely credentials/network/permission: do not grind through every file.
                    log(f"{streak} consecutive failures; skipping the rest of this pass")
                    break
    except Exception as error:
        failed += 1
        log(f"sync failed: {describe(error)}")
    if uploaded:
        _save(state_path, state)
    if uploaded or failed or final:
        log(f"{Path(job).name}: uploaded {uploaded}, failed {failed} -> oss://{bucket.name}/{bucket.prefix}/")
    return failed == 0


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def install_stop_signals():
    """SIGUSR1 = evaluation finished (full final pass); SIGTERM/SIGINT = stop early."""
    received = []

    def stop(signum, _frame):
        if received:  # A second signal means "quit now".
            os._exit(128 + signum)
        received.append(signum)

    for signum in (signal.SIGUSR1, signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stop)
    return received


def follow(job, report, bucket, *, interval, parent=None, received=None, final_timeout=FINAL_TIMEOUT):
    """Upload settled trials every interval until a stop signal or the parent exits.

    Only SIGUSR1, sent by run-matrix.sh after the run and summary, uploads
    everything. On SIGTERM/SIGINT or parent exit Harbor may still be writing
    unscrubbed output, so a last settled-only pass runs instead.
    """
    received = install_stop_signals() if received is None else received
    log(f"background sync every {interval}s -> oss://{bucket.name}/{bucket.prefix}/")
    while not received and (parent is None or _alive(parent)):
        sync(job, report, bucket)
        deadline = time.monotonic() + interval
        while not received and time.monotonic() < deadline and (parent is None or _alive(parent)):
            time.sleep(1)
    final = received[:1] == [signal.SIGUSR1]
    log("final upload of all job files and the report" if final
        else "stopped before the evaluation finished; uploading settled trials only")
    return sync(job, report, bucket, final=final, deadline=time.monotonic() + final_timeout)


def command(args, env=None, bucket_factory=Bucket):
    """Entry point of `python -m hbench archive`; returns an exit code and never raises."""
    try:
        settings = load_settings(env)
    except ValueError as error:
        log(str(error))
        return 1
    if args.dry_run:
        if args.job is None:
            log("a job directory is required")
            return 1
        prefix = settings["prefix"] if settings else "harness-bench"
        files = collect(args.job, args.report, final=True)
        for path, relative in files:
            print(f"{prefix}/{relative}")
        log(f"dry-run: {len(files)} files, {sum(p.stat().st_size for p, _ in files)} bytes -> {target(settings)}")
        return 0
    # Install handlers before the slow SDK import so an early SIGUSR1 is not fatal.
    received = install_stop_signals() if args.follow else None
    if settings is None:
        log("HB_OSS_BUCKET is not set; archiving disabled")
        return 0 if not args.check else 1
    try:
        bucket = bucket_factory(settings)
    except Exception as error:
        log(f"cannot create OSS client: {describe(error)}")
        return 1
    if args.check:
        try:
            bucket.check()
        except Exception as error:
            log(f"check failed for {target(settings)}: {describe(error)}")
            return 1
        log(f"check OK: {target(settings)} is reachable with the ECS RAM role")
        return 0
    if args.job is None:
        log("a job directory is required")
        return 1
    if args.follow:
        ok = follow(args.job, args.report, bucket, interval=max(args.interval, 10), parent=args.parent,
                    received=received, final_timeout=args.final_timeout)
    else:
        ok = sync(args.job, args.report, bucket, final=True)
    return 0 if ok else 1
