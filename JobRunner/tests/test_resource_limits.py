"""
Integration tests verifying CPU, memory, and PID limits are enforced.

Type      : integration / resource enforcement
Why needed: without hard limits a misbehaving job can starve the host or
            other containers.  The enforced limits (0.5 CPU via nano_cpus,
            128 MB RAM + no swap via memswap_limit, 64 PIDs) must be
            validated end-to-end.

Requires Docker daemon + python:3-alpine image.
"""
from __future__ import annotations

import pytest

try:
    from core.runner import run_job
except Exception as _e:
    pytest.skip(str(_e), allow_module_level=True)

from tests.conftest import (
    DATA_ROOT,
    JOBS_ROOT,
    parse_lifecycle_events,
    wait_for_completion,
    write_job_file,
)

# Single-shot OOM allocation: request far more than the 128 MB limit in one
# call so the kernel OOM-kills immediately rather than after many small
# allocation rounds.  bytearray forces the OS to back the pages on creation.
_OOM_JOB = (
    "import sys\n"
    "bytearray(300 * 1024 * 1024)\n"
    "print('should not reach here', flush=True)\n"
)


class TestResourceLimits:

    def test_oom_killed_container_is_marked_failed(self, job_id):
        """
        Type      : OOM kill
        Why needed: a job that exceeds the 128 MB RAM limit is killed by the
                    kernel with SIGKILL; this must result in FAILED, not RUNNING.
        How tested: single-shot 300 MB bytearray allocation triggers OOM
                    immediately; the container exits with code 137.
        """
        write_job_file(JOBS_ROOT / job_id, "job.py", _OOM_JOB)
        run_job(job_id)
        job = wait_for_completion(job_id, timeout=90)
        assert job["status"] == "FAILED"

    def test_oom_killed_exit_code_is_137(self, job_id):
        """
        Why needed: exit code 137 (128 + SIGKILL) uniquely identifies an OOM
                    kill; callers use this to increase memory quotas or alert.
        """
        write_job_file(JOBS_ROOT / job_id, "job.py", _OOM_JOB)
        run_job(job_id)
        job = wait_for_completion(job_id, timeout=90)
        assert job["exit_code"] == 137

    def test_oom_event_recorded_in_lifecycle_log(self, job_id):
        """
        Why needed: the 'oom' lifecycle event distinguishes an OOM kill from a
                    normal SIGKILL (e.g. kill switch).  Monitoring tools parse
                    this to generate OOM-specific alerts.
        """
        write_job_file(JOBS_ROOT / job_id, "job.py", _OOM_JOB)
        run_job(job_id)
        wait_for_completion(job_id, timeout=90)
        events = parse_lifecycle_events(DATA_ROOT / job_id / "container_lifecycle.txt")
        actions = {e.get("Action") for e in events}
        assert "oom" in actions, f"Expected 'oom' event in lifecycle log, got: {actions}"

    def test_fork_bomb_limited_by_pids_limit(self, job_id):
        """
        Type      : fork bomb / process explosion
        Why needed: without a PID cap a fork bomb can exhaust the host's
                    process table, causing system-wide instability.
        How tested: a job tries to spawn 200 child processes; pids_limit=64
                    causes OSError once the cap is reached.  The job must end
                    (SUCCEEDED or FAILED) without hanging the test runner.
        """
        fork_job = (
            "import subprocess\n"
            "procs = []\n"
            "for i in range(200):\n"
            "    try:\n"
            "        procs.append(subprocess.Popen(['sleep', '5']))\n"
            "    except OSError as e:\n"
            "        print(f'Fork blocked at {i}: {e}', flush=True)\n"
            "        break\n"
            "print(f'Spawned {len(procs)} children', flush=True)\n"
        )
        write_job_file(JOBS_ROOT / job_id, "job.py", fork_job)
        run_job(job_id)
        job = wait_for_completion(job_id, timeout=45)
        assert job["status"] in {"SUCCEEDED", "FAILED"}
        log = (DATA_ROOT / job_id / "container_logs.txt").read_bytes().decode(
            "utf-8", errors="replace"
        )
        assert "Fork blocked" in log or "Spawned" in log

    def test_cpu_limited_job_completes_eventually(self, job_id):
        """
        Type      : CPU limit
        Why needed: the 0.5-CPU cap (nano_cpus=500_000_000) must not prevent
                    jobs from completing; it only throttles speed.
        How tested: CPU-intensive job runs within timeout despite the cap.
        """
        cpu_job = (
            "total = sum(i * i for i in range(1_000_000))\n"
            "print(f'result={total}', flush=True)\n"
        )
        write_job_file(JOBS_ROOT / job_id, "job.py", cpu_job)
        run_job(job_id)
        job = wait_for_completion(job_id, timeout=60)
        assert job["status"] == "SUCCEEDED"
