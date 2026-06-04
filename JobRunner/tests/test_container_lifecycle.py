"""
Integration tests for the job lifecycle state machine.

Type      : integration / state-machine
Why needed: the DB must reflect each phase of execution so API consumers
            can track progress and the scheduler can detect stuck jobs.
How tested: start a real container and query the DB at defined points.

Requires Docker daemon + python:3-alpine image.
"""
from __future__ import annotations

import json

import pytest

try:
    from core.runner import run_job
except Exception as _e:
    pytest.skip(str(_e), allow_module_level=True)

from db import get_job
from tests.conftest import DATA_ROOT, JOBS_ROOT, wait_for_completion, write_job_file


class TestContainerLifecycle:

    def test_run_job_returns_container_descriptor_dict(self, job_id):
        """
        Why needed: the return value is forwarded to API callers; missing keys
                    break client SDKs.
        """
        write_job_file(JOBS_ROOT / job_id, "job.py", "print('hello')")
        desc = run_job(job_id)
        for key in ("job_id", "container_id", "container_name", "data_dir",
                    "kill_switch_path", "lifecycle_log_path", "log_path"):
            assert key in desc, f"Missing key {key!r} in descriptor"
        assert desc["job_id"] == job_id

    def test_status_is_running_immediately_after_run_job(self, job_id):
        """
        Why needed: run_job() sets RUNNING before returning; the API's GET
                    /jobs/{id} must never show STARTING after submission.
        """
        write_job_file(JOBS_ROOT / job_id, "job.py", "import time; time.sleep(30)")
        run_job(job_id)
        job = get_job(job_id)
        assert job["status"] == "RUNNING"
        (DATA_ROOT / job_id / "kill_switch.txt").touch()

    def test_successful_job_achieves_succeeded_status(self, job_id):
        """
        Why needed: SUCCEEDED is the terminal state for a correct job;
                    staying in RUNNING after exit is a silent hang.
        """
        write_job_file(JOBS_ROOT / job_id, "job.py", "print('done')")
        run_job(job_id)
        job = wait_for_completion(job_id)
        assert job["status"] == "SUCCEEDED"

    def test_successful_job_exit_code_is_zero(self, job_id):
        """
        Why needed: exit_code is the ground truth for success; a non-zero exit
                    on a SUCCEEDED job is contradictory and confuses dashboards.
        """
        write_job_file(JOBS_ROOT / job_id, "job.py", "print('ok')")
        run_job(job_id)
        job = wait_for_completion(job_id)
        assert job["exit_code"] == 0

    def test_completed_at_timestamp_is_set_after_completion(self, job_id):
        """
        Why needed: completed_at drives SLA calculations and duration metrics;
                    a NULL value prevents those computations.
        """
        write_job_file(JOBS_ROOT / job_id, "job.py", "pass")
        run_job(job_id)
        job = wait_for_completion(job_id)
        assert job["completed_at"] is not None

    def test_container_id_and_name_stored_in_db(self, job_id):
        """
        Why needed: operators need container_id to inspect Docker state with
                    `docker inspect` or `docker logs` when debugging.
        """
        write_job_file(JOBS_ROOT / job_id, "job.py", "pass")
        desc = run_job(job_id)
        job = get_job(job_id)
        assert job["container_id"] == desc["container_id"]
        assert job["container_name"] == desc["container_name"]
        assert job["container_name"].startswith("agent_os_job_")

    def test_data_directory_is_created(self, job_id):
        """
        Why needed: /data is mounted inside the container for artifact output;
                    if the host directory is missing the mount fails silently.
        """
        write_job_file(JOBS_ROOT / job_id, "job.py", "pass")
        run_job(job_id)
        assert (DATA_ROOT / job_id).is_dir()

    def test_container_desc_file_is_written_with_all_fields(self, job_id):
        """
        Why needed: container_desc.txt is the out-of-band record used by
                    external monitoring tools; partial JSON breaks those tools.
        """
        write_job_file(JOBS_ROOT / job_id, "job.py", "pass")
        run_job(job_id)
        desc_path = DATA_ROOT / job_id / "container_desc.txt"
        assert desc_path.exists()
        desc = json.loads(desc_path.read_text(encoding="utf-8"))
        for key in ("job_id", "container_id", "container_name", "data_dir",
                    "kill_switch_path", "lifecycle_log_path", "log_path"):
            assert key in desc
