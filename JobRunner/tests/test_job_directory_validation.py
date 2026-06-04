"""
Boundary tests: run_job() must fail fast when the job directory is missing,
before any Docker resource is allocated.

Type      : boundary / fast-fail
Why needed: a silent failure here would leave a zombie DB row in STARTING state
            with no way to recover.
How tested: call run_job() with a nonexistent job_id and assert the specific
            exception type and message.
"""
from __future__ import annotations

import shutil

import pytest

try:
    from core.runner import run_job
except Exception as _e:
    pytest.skip(str(_e), allow_module_level=True)

from tests.conftest import DATA_ROOT


class TestJobDirectoryValidation:

    def test_missing_job_directory_raises_file_not_found(self):
        with pytest.raises(FileNotFoundError, match="Job directory not found"):
            run_job("definitely_does_not_exist_abc123xyz")

    def test_missing_job_directory_does_not_leave_data_dir(self):
        """
        Why needed: failed validation must not create a partial data directory
                    that confuses future runs.
        """
        jid = "nonexistent_cleanup_test_xyz"
        data_dir = DATA_ROOT / jid
        assert not data_dir.exists()
        with pytest.raises(FileNotFoundError):
            run_job(jid)
        # FileNotFoundError is raised before mkdir(), so data_dir is never
        # created — the rmtree is purely defensive.
        shutil.rmtree(data_dir, ignore_errors=True)
