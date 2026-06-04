"""
Integration tests for job failure modes.

Type      : integration / failure-mode
Why needed: every failure mode must be captured cleanly so that automated
            systems can retry, alert, and diagnose without human inspection.

Covers: unhandled exceptions, explicit sys.exit codes, syntax errors,
        missing imports, error_message population, and log content.

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


class TestErrorScenarios:

    def test_unhandled_python_exception_results_in_failed(self, job_id):
        """
        Type      : error – unhandled exception
        Why needed: a job that crashes with a traceback exits with code 1; the
                    runner must translate this into FAILED, not leave it in RUNNING.
        How tested: raise RuntimeError inside job.py, assert FAILED status.
        """
        write_job_file(
            JOBS_ROOT / job_id,
            "job.py",
            "raise RuntimeError('intentional test error')",
        )
        run_job(job_id)
        job = wait_for_completion(job_id)
        assert job["status"] == "FAILED"

    def test_unhandled_exception_exit_code_is_nonzero(self, job_id):
        """
        Why needed: exit_code drives retry logic; a zero exit_code on a
                    crashed job would suppress retries.
        """
        write_job_file(JOBS_ROOT / job_id, "job.py", "raise ValueError('boom')")
        run_job(job_id)
        job = wait_for_completion(job_id)
        assert job["exit_code"] != 0

    def test_explicit_nonzero_exit_results_in_failed(self, job_id):
        """
        Type      : error – explicit sys.exit
        Why needed: sys.exit(1) is the standard way for CLI scripts to signal
                    failure; it must be caught and stored.
        """
        write_job_file(JOBS_ROOT / job_id, "job.py", "import sys; sys.exit(1)")
        run_job(job_id)
        job = wait_for_completion(job_id)
        assert job["status"] == "FAILED"
        assert job["exit_code"] == 1

    def test_specific_exit_code_is_captured_verbatim(self, job_id):
        """
        Type      : error – non-standard exit code
        Why needed: specific exit codes carry machine-readable semantics (e.g.
                    Slurm, Kubernetes). They must pass through unchanged.
        How tested: job exits with code 42; assert DB stores exactly 42.
        """
        write_job_file(JOBS_ROOT / job_id, "job.py", "import sys; sys.exit(42)")
        run_job(job_id)
        job = wait_for_completion(job_id)
        assert job["status"] == "FAILED"
        assert job["exit_code"] == 42

    def test_syntax_error_job_results_in_failed(self, job_id):
        """
        Type      : error – syntax error
        Why needed: a syntax error causes Python to exit before executing any
                    user code; the runner must detect this via the non-zero exit.
        How tested: job.py contains invalid Python syntax.
        """
        write_job_file(
            JOBS_ROOT / job_id,
            "job.py",
            "def broken(:\n    pass",  # intentional syntax error
        )
        run_job(job_id)
        job = wait_for_completion(job_id)
        assert job["status"] == "FAILED"
        assert job["exit_code"] != 0

    def test_missing_import_results_in_failed(self, job_id):
        """
        Type      : error – missing dependency
        Why needed: if a job imports a package not present in python:3-alpine,
                    it fails at startup. Must be captured as FAILED, not RUNNING.
        How tested: import a nonexistent package name.
        """
        write_job_file(
            JOBS_ROOT / job_id,
            "job.py",
            "import nonexistent_package_xyz_abc_123",
        )
        run_job(job_id)
        job = wait_for_completion(job_id)
        assert job["status"] == "FAILED"

    def test_error_message_is_set_in_db_on_failure(self, job_id):
        """
        Why needed: error_message is surfaced in the API and alert notifications;
                    a NULL value gives operators nothing to act on.
        """
        write_job_file(JOBS_ROOT / job_id, "job.py", "import sys; sys.exit(1)")
        run_job(job_id)
        job = wait_for_completion(job_id)
        assert job["error_message"] is not None
        assert len(job["error_message"]) > 0

    def test_failed_job_logs_contain_traceback(self, job_id):
        """
        Why needed: tracebacks in container_logs.txt are the first place an
                    engineer looks; if they're missing the failure is opaque.
        """
        write_job_file(
            JOBS_ROOT / job_id,
            "job.py",
            "raise RuntimeError('UNIQUE_TRACEBACK_MARKER_567')",
        )
        run_job(job_id)
        wait_for_completion(job_id)
        log = (DATA_ROOT / job_id / "container_logs.txt").read_bytes().decode(
            "utf-8", errors="replace"
        )
        assert "UNIQUE_TRACEBACK_MARKER_567" in log

    def test_failed_job_lifecycle_has_die_event(self, job_id):
        """
        Why needed: the lifecycle log must record a 'die' event even when the
                    container fails, so the timeline is complete.
        """
        write_job_file(JOBS_ROOT / job_id, "job.py", "raise RuntimeError('fail')")
        run_job(job_id)
        wait_for_completion(job_id)
        events = parse_lifecycle_events(DATA_ROOT / job_id / "container_lifecycle.txt")
        assert "die" in {e.get("Action") for e in events}
