"""
Integration tests for concurrent job execution without interference.

Type      : integration / concurrency
Why needed: a job runner must support parallel execution; shared global state
            (DB connections, path variables, Docker client) must not cause
            races between simultaneous runs.
How tested: launch two jobs nearly simultaneously; assert both complete
            independently with the correct status and logs.

Requires Docker daemon + python:3-alpine image.
"""
from __future__ import annotations

import shutil
import uuid

import pytest

try:
    from core.runner import run_job
except Exception as _e:
    pytest.skip(str(_e), allow_module_level=True)

from tests.conftest import DATA_ROOT, JOBS_ROOT, wait_for_completion, write_job_file


class TestConcurrentJobs:

    def test_two_concurrent_jobs_both_succeed(self, job_id):
        """
        Why needed: if there is a naming collision, filesystem race, or DB
                    conflict the second job will fail or overwrite the first.
        """
        jid2 = f"test_{uuid.uuid4().hex[:12]}"
        job_dir2 = JOBS_ROOT / jid2
        job_dir2.mkdir(parents=True, exist_ok=True)
        try:
            write_job_file(JOBS_ROOT / job_id, "job.py", "print('job-1', flush=True)\n")
            write_job_file(job_dir2, "job.py", "print('job-2', flush=True)\n")

            run_job(job_id)
            run_job(jid2)

            j1 = wait_for_completion(job_id)
            j2 = wait_for_completion(jid2)

            assert j1["status"] == "SUCCEEDED"
            assert j2["status"] == "SUCCEEDED"

            log1 = (DATA_ROOT / job_id / "container_logs.txt").read_bytes().decode(
                "utf-8", errors="replace"
            )
            log2 = (DATA_ROOT / jid2 / "container_logs.txt").read_bytes().decode(
                "utf-8", errors="replace"
            )
            assert "job-1" in log1 and "job-2" not in log1
            assert "job-2" in log2 and "job-1" not in log2
        finally:
            shutil.rmtree(job_dir2, ignore_errors=True)
            shutil.rmtree(DATA_ROOT / jid2, ignore_errors=True)

    def test_concurrent_jobs_have_separate_container_ids(self, job_id):
        """
        Why needed: container_name collision would cause Docker to reject the
                    second create() call; unique names are critical.
        """
        jid2 = f"test_{uuid.uuid4().hex[:12]}"
        job_dir2 = JOBS_ROOT / jid2
        job_dir2.mkdir(parents=True, exist_ok=True)
        try:
            write_job_file(JOBS_ROOT / job_id, "job.py", "pass\n")
            write_job_file(job_dir2, "job.py", "pass\n")
            desc1 = run_job(job_id)
            desc2 = run_job(jid2)
            assert desc1["container_id"] != desc2["container_id"]
            assert desc1["container_name"] != desc2["container_name"]
            wait_for_completion(job_id)
            wait_for_completion(jid2)
        finally:
            shutil.rmtree(job_dir2, ignore_errors=True)
            shutil.rmtree(DATA_ROOT / jid2, ignore_errors=True)
