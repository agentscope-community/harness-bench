"""Offline OSS archive tests: a fake bucket and fake metadata service, no network."""
from argparse import Namespace
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import signal
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

from hbench import archive
from hbench.__main__ import main


class FakeBucket:
    def __init__(self, settings=None, fail=()):
        self.name, self.prefix = "bucket", "harness-bench"
        self.fail, self.uploads, self.checked = set(fail), [], False

    def upload(self, key, path):
        if key in self.fail:
            raise RuntimeError("boom")
        self.uploads.append(key)

    def check(self):
        self.checked = True


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.job = self.root / "jobs" / "run-1"
        self.report = self.root / "reports" / "run-1"
        self.write(self.job / "config.json", {"job_name": "run-1"})
        self.write(self.job / "result.json", {"n_total_trials": 2})
        network = patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        stderr = patch("sys.stderr", new_callable=io.StringIO)
        self.stderr = stderr.start()
        self.addCleanup(stderr.stop)

    def write(self, path, value, age=0):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value))
        if age:
            past = time.time() - age
            os.utime(path, (past, past))

    def trial(self, name, finished=True, age=300):
        folder = self.job / name
        self.write(folder / "agent" / "trajectory.json", {"steps": []})
        self.write(folder / "result.json", {"finished_at": "2026-11-12T00:00:00Z" if finished else None}, age)
        return folder

    def keys(self, files):
        return sorted(key for _, key in files)

    def test_collect_skips_running_unsettled_and_secret_files(self):
        self.trial("done")
        self.trial("running", finished=False)
        self.trial("just-finished", age=0)
        self.write(self.job / "done" / "agent" / ".env", "X=1")
        self.write(self.job / "done" / "agent" / "auth.json", "{}")
        self.write(self.job / "job.log", "DEBUG unscrubbed")
        self.write(self.report / "report.html", "<html>")
        partial = self.keys(archive.collect(self.job, self.report))
        self.assertEqual(partial, ["jobs/run-1/config.json", "jobs/run-1/done/agent/trajectory.json",
                                   "jobs/run-1/done/result.json", "jobs/run-1/result.json",
                                   "reports/run-1/report.html"])
        final = self.keys(archive.collect(self.job, self.report, final=True))
        self.assertIn("jobs/run-1/running/result.json", final)
        self.assertIn("jobs/run-1/just-finished/result.json", final)
        self.assertFalse(any(k.endswith((".env", "auth.json", "job.log")) for k in final))

    def test_sync_is_incremental_and_reuploads_changes(self):
        self.trial("done")
        bucket = FakeBucket()
        self.assertTrue(archive.sync(self.job, None, bucket, final=True))
        self.assertEqual(len(bucket.uploads), 4)
        self.assertTrue(all(k.startswith("harness-bench/jobs/run-1/") for k in bucket.uploads))
        bucket.uploads.clear()
        self.assertTrue(archive.sync(self.job, None, bucket, final=True))
        self.assertEqual(bucket.uploads, [])
        self.write(self.job / "result.json", {"n_total_trials": 2, "done": True})
        archive.sync(self.job, None, bucket, final=True)
        self.assertEqual(bucket.uploads, ["harness-bench/jobs/run-1/result.json"])
        self.assertTrue((self.root / "jobs" / ".run-1.oss-sync.json").is_file())

    def test_failures_are_printed_retried_and_never_raised(self):
        self.trial("done")
        bucket = FakeBucket(fail={"harness-bench/jobs/run-1/config.json"})
        self.assertFalse(archive.sync(self.job, None, bucket, final=True))
        self.assertEqual(len(bucket.uploads), 3)
        self.assertIn("[oss] upload failed: jobs/run-1/config.json: RuntimeError: boom", self.stderr.getvalue())
        bucket.fail.clear()
        bucket.uploads.clear()
        self.assertTrue(archive.sync(self.job, None, bucket, final=True))
        self.assertEqual(bucket.uploads, ["harness-bench/jobs/run-1/config.json"])
        with patch.object(archive, "collect", side_effect=OSError("disk")):
            self.assertFalse(archive.sync(self.job, None, bucket))
        self.assertIn("[oss] sync failed: OSError: disk", self.stderr.getvalue())

    def test_consecutive_failures_end_the_pass_early(self):
        for index in range(5):
            self.trial(f"t{index}")
        bucket = FakeBucket()
        bucket.upload = lambda key, path: (_ for _ in ()).throw(OSError("no credentials"))
        self.assertFalse(archive.sync(self.job, None, bucket, final=True))
        self.assertEqual(self.stderr.getvalue().count("upload failed"), archive.MAX_CONSECUTIVE_FAILURES)
        self.assertIn("skipping the rest of this pass", self.stderr.getvalue())

    def test_missing_job_directory_is_not_an_error(self):
        bucket = FakeBucket()
        self.assertTrue(archive.sync(self.root / "jobs" / "not-started", None, bucket))
        self.assertEqual(bucket.uploads, [])

    def test_follow_parent_exit_uploads_settled_trials_only(self):
        self.trial("done")
        self.trial("running", finished=False)
        bucket = FakeBucket()
        with patch.object(archive, "_alive", side_effect=[True, False, False]), \
                patch.object(archive.signal, "signal"), patch.object(archive.time, "sleep"):
            self.assertTrue(archive.follow(self.job, None, bucket, interval=10, parent=123))
        self.assertIn("harness-bench/jobs/run-1/done/result.json", bucket.uploads)
        self.assertFalse(any("/running/" in k for k in bucket.uploads))
        self.assertIn("uploading settled trials only", self.stderr.getvalue())

    def test_follow_sigusr1_uploads_everything(self):
        self.trial("running", finished=False)
        bucket = FakeBucket()
        self.assertTrue(archive.follow(self.job, None, bucket, interval=10, received=[signal.SIGUSR1]))
        self.assertIn("harness-bench/jobs/run-1/running/result.json", bucket.uploads)
        self.assertIn("final upload", self.stderr.getvalue())

    def test_follow_sigterm_is_not_a_final_pass(self):
        self.trial("running", finished=False)
        bucket = FakeBucket()
        archive.follow(self.job, None, bucket, interval=10, received=[signal.SIGTERM])
        self.assertFalse(any("/running/" in k for k in bucket.uploads))

    def test_deadline_stops_the_pass_and_prints_retry(self):
        self.trial("done")
        bucket = FakeBucket()
        self.assertFalse(archive.sync(self.job, None, bucket, final=True, deadline=time.monotonic() - 1))
        self.assertEqual(bucket.uploads, [])
        self.assertIn("time limit reached", self.stderr.getvalue())
        self.assertIn("python -m hbench archive", self.stderr.getvalue())

    def test_settings(self):
        self.assertIsNone(archive.load_settings({}))
        with self.assertRaisesRegex(ValueError, "HB_OSS_REGION"):
            archive.load_settings({"HB_OSS_BUCKET": "b"})
        value = archive.load_settings({"HB_OSS_BUCKET": " b ", "HB_OSS_REGION": "cn-hangzhou", "HB_OSS_PREFIX": "/p/x/"})
        self.assertEqual(value, {"bucket": "b", "region": "cn-hangzhou", "endpoint": None,
                                 "prefix": "p/x", "role_name": None})

    def args(self, **values):
        defaults = {"job": self.job, "report": None, "check": False, "dry_run": False,
                    "follow": False, "interval": 300, "parent": None,
                    "final_timeout": 1800}
        return Namespace(**{**defaults, **values})

    def test_command_paths_never_raise(self):
        env = {"HB_OSS_BUCKET": "bucket", "HB_OSS_REGION": "cn-hangzhou"}
        self.assertEqual(archive.command(self.args(), env={}), 0)
        self.assertEqual(archive.command(self.args(check=True), env={}), 1)
        self.assertEqual(archive.command(self.args(), env={"HB_OSS_BUCKET": "b"}), 1)
        broken = lambda settings: (_ for _ in ()).throw(RuntimeError("no sdk"))
        self.assertEqual(archive.command(self.args(), env=env, bucket_factory=broken), 1)
        self.assertIn("cannot create OSS client: RuntimeError: no sdk", self.stderr.getvalue())
        fake = FakeBucket()
        self.assertEqual(archive.command(self.args(check=True), env=env, bucket_factory=lambda s: fake), 0)
        self.assertTrue(fake.checked)
        self.assertEqual(archive.command(self.args(), env=env, bucket_factory=lambda s: fake), 0)
        self.assertEqual(len(fake.uploads), 2)

    def test_cli_dry_run_lists_keys_without_network(self):
        self.trial("done")
        with patch.dict(os.environ, {}, clear=True), patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(main(["archive", str(self.job), "--dry-run"]), 0)
        self.assertIn("harness-bench/jobs/run-1/done/result.json", stdout.getvalue().splitlines())
        self.assertIn("dry-run: 4 files", self.stderr.getvalue())

    def test_ecs_role_credentials_use_imdsv2_and_cache(self):
        expiration = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        calls = []

        def opener(request, timeout):
            calls.append((request.get_method(), request.full_url, dict(request.header_items())))
            path = request.full_url.removeprefix(archive.METADATA)
            body = {"/latest/api/token": "tok", "/latest/meta-data/ram/security-credentials/": "hbench-role\n",
                    "/latest/meta-data/ram/security-credentials/hbench-role": json.dumps(
                        {"Code": "Success", "AccessKeyId": "id", "AccessKeySecret": "secret",
                         "SecurityToken": "sts", "Expiration": expiration})}[path]
            return FakeResponse(body.encode())

        provider = archive.EcsRoleCredentials(opener=opener)
        first = provider()
        self.assertEqual((first.access_key_id, first.security_token), ("id", "sts"))
        self.assertIs(provider(), first)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0][0], "PUT")
        self.assertEqual(calls[2][2].get("X-aliyun-ecs-metadata-token"), "tok")

    def test_ecs_role_credentials_report_missing_role(self):
        def opener(request, timeout):
            return FakeResponse(b"" if request.get_method() == "GET" else b"tok")

        with self.assertRaisesRegex(RuntimeError, "No RAM role"):
            archive.EcsRoleCredentials(opener=opener)()

    def test_describe_hides_request_details(self):
        error = RuntimeError("x")
        error.code, error.request_id, error.status_code, error.headers = "AccessDenied", "rid", 403, {"Authorization": "s"}
        text = archive.describe(error)
        self.assertIn("code=AccessDenied", text)
        self.assertNotIn("Authorization", text)


if __name__ == "__main__":
    unittest.main()
