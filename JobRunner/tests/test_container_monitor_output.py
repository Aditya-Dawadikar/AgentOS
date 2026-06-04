"""
Integration tests verifying that the container monitor subprocess writes
logs and lifecycle events correctly.

Type      : integration / observability
Why needed: the monitor subprocess is the only component that writes logs
            and lifecycle events; if it silently fails, debugging is blind.
How tested: wait for job completion and assert on file existence and content.

Requires Docker daemon + python:3-alpine image.
"""
from __future__ import annotations

import json

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


class TestContainerMonitorOutput:

    def test_container_logs_file_is_created(self, job_id):
        """
        Why needed: container_logs.txt must exist for the artifacts API and for
                    operators who tail the file.
        """
        write_job_file(JOBS_ROOT / job_id, "job.py", "print('logging test')")
        run_job(job_id)
        wait_for_completion(job_id)
        assert (DATA_ROOT / job_id / "container_logs.txt").exists()

    def test_container_logs_capture_stdout(self, job_id):
        """
        Why needed: if stdout is not captured, users have no way to see their
                    job's printed output via the API.
        How tested: job prints a sentinel string; assert it appears in the log.
        """
        sentinel = "SENTINEL_OUTPUT_XYZ_42"
        write_job_file(
            JOBS_ROOT / job_id, "job.py", f"print('{sentinel}', flush=True)"
        )
        run_job(job_id)
        wait_for_completion(job_id)
        log = (DATA_ROOT / job_id / "container_logs.txt").read_bytes().decode(
            "utf-8", errors="replace"
        )
        assert sentinel in log

    def test_container_logs_capture_stderr(self, job_id):
        """
        Why needed: tracebacks and warnings go to stderr; if only stdout is
                    captured, Python exceptions are invisible in the log.
        """
        write_job_file(
            JOBS_ROOT / job_id,
            "job.py",
            "import sys; print('err line', file=sys.stderr, flush=True)",
        )
        run_job(job_id)
        wait_for_completion(job_id)
        log = (DATA_ROOT / job_id / "container_logs.txt").read_bytes().decode(
            "utf-8", errors="replace"
        )
        assert "err line" in log

    def test_container_logs_include_timestamps(self, job_id):
        """
        Why needed: Docker appends RFC 3339 timestamps when streaming with
                    timestamps=True; missing timestamps break log correlation
                    with external observability tools.
        How tested: logs contain at least one 'Z' or '+00:00' timestamp suffix.
        """
        write_job_file(JOBS_ROOT / job_id, "job.py", "print('ts check', flush=True)")
        run_job(job_id)
        wait_for_completion(job_id)
        raw = (DATA_ROOT / job_id / "container_logs.txt").read_bytes().decode(
            "utf-8", errors="replace"
        )
        assert "Z" in raw or "+00" in raw

    def test_lifecycle_log_file_is_created(self, job_id):
        """
        Why needed: without the lifecycle file, there is no record of what
                    Docker events occurred (start, die, OOM, destroy).
        """
        write_job_file(JOBS_ROOT / job_id, "job.py", "pass")
        run_job(job_id)
        wait_for_completion(job_id)
        assert (DATA_ROOT / job_id / "container_lifecycle.txt").exists()

    def test_lifecycle_log_contains_start_event(self, job_id):
        """
        Why needed: the 'start' action in the lifecycle log is proof that the
                    container actually started (not just created).
        """
        write_job_file(JOBS_ROOT / job_id, "job.py", "pass")
        run_job(job_id)
        wait_for_completion(job_id)
        events = parse_lifecycle_events(DATA_ROOT / job_id / "container_lifecycle.txt")
        assert "start" in {e.get("Action") for e in events}

    def test_lifecycle_log_contains_die_event(self, job_id):
        """
        Why needed: 'die' is the event where Docker records the exit code; its
                    absence means the monitor may have missed container termination.
        """
        write_job_file(JOBS_ROOT / job_id, "job.py", "pass")
        run_job(job_id)
        wait_for_completion(job_id)
        events = parse_lifecycle_events(DATA_ROOT / job_id / "container_lifecycle.txt")
        assert "die" in {e.get("Action") for e in events}

    def test_lifecycle_events_are_valid_json_objects(self, job_id):
        """
        Why needed: external tools parse the lifecycle log as NDJSON; a
                    malformed line breaks the entire stream parser.
        """
        write_job_file(JOBS_ROOT / job_id, "job.py", "pass")
        run_job(job_id)
        wait_for_completion(job_id)
        lc_path = DATA_ROOT / job_id / "container_lifecycle.txt"
        for line in lc_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                assert isinstance(json.loads(line), dict)

    def test_exit_code_zero_stored_in_db_for_success(self, job_id):
        """
        Why needed: the DB exit_code column is the canonical answer for
                    automated decision-making; a NULL or wrong value silently
                    mislabels job outcomes.
        """
        write_job_file(JOBS_ROOT / job_id, "job.py", "pass")
        run_job(job_id)
        job = wait_for_completion(job_id)
        assert job["exit_code"] == 0
