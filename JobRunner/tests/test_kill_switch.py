"""
Integration tests for the kill-switch mechanism.

Type      : integration / kill-switch
Why needed: an infinite loop or hung job has no other graceful escape hatch;
            the kill switch is the only control plane operation for stopping
            a running container without direct Docker access.
How tested: start a job that sleeps forever, create kill_switch.txt, verify
            the container is terminated and the DB is updated.

Requires Docker daemon + python:3-alpine image.
"""
from __future__ import annotations

import time

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

_LONG_JOB = "import time\nwhile True:\n    print('running', flush=True)\n    time.sleep(0.5)\n"


class TestKillSwitch:

    def test_kill_switch_terminates_long_running_container(self, ensure_killed):
        """
        Why needed: without the kill switch, an infinite-loop job would exhaust
                    Docker resources on the host indefinitely.
        """
        job_id = ensure_killed
        write_job_file(JOBS_ROOT / job_id, "job.py", _LONG_JOB)
        run_job(job_id)
        (DATA_ROOT / job_id / "kill_switch.txt").touch()
        job = wait_for_completion(job_id, timeout=30)
        assert job["status"] == "FAILED"

    def test_kill_switch_results_in_failed_status(self, ensure_killed):
        """
        Why needed: a killed job is NOT successful; marking it SUCCEEDED would
                    cause a retry system to think it finished normally.
        """
        job_id = ensure_killed
        write_job_file(JOBS_ROOT / job_id, "job.py", _LONG_JOB)
        run_job(job_id)
        (DATA_ROOT / job_id / "kill_switch.txt").touch()
        job = wait_for_completion(job_id, timeout=30)
        assert job["status"] == "FAILED"

    def test_kill_switch_lifecycle_log_has_die_event(self, ensure_killed):
        """
        Why needed: the 'die' event in the lifecycle proves the container was
                    actually killed rather than just losing DB connectivity.
        """
        job_id = ensure_killed
        write_job_file(JOBS_ROOT / job_id, "job.py", _LONG_JOB)
        run_job(job_id)
        (DATA_ROOT / job_id / "kill_switch.txt").touch()
        wait_for_completion(job_id, timeout=30)
        events = parse_lifecycle_events(DATA_ROOT / job_id / "container_lifecycle.txt")
        assert "die" in {e.get("Action") for e in events}

    def test_kill_switch_captured_stdout_before_kill(self, ensure_killed):
        """
        Why needed: logs written before the kill switch fires must be preserved;
                    the kill must not truncate the log file.
        """
        job_id = ensure_killed
        write_job_file(JOBS_ROOT / job_id, "job.py", _LONG_JOB)
        run_job(job_id)
        time.sleep(2)  # let a few 'running' lines accumulate
        (DATA_ROOT / job_id / "kill_switch.txt").touch()
        wait_for_completion(job_id, timeout=30)
        log = (DATA_ROOT / job_id / "container_logs.txt").read_bytes().decode(
            "utf-8", errors="replace"
        )
        assert "running" in log
