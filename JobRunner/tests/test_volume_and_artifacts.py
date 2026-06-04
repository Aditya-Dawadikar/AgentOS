"""
Integration tests for /data volume mounts and job artifact isolation.

Type      : integration / volume mount
Why needed: the /data volume is the only channel for jobs to persist
            artifacts.  A mount failure or permission issue silently
            discards output.

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


class TestVolumeAndArtifacts:

    def test_job_can_write_file_to_data_volume(self, job_id):
        """
        Why needed: if the /data mount is broken, no artifacts are produced
                    and the API returns an empty list – with no error.
        How tested: job writes a known file; assert it appears on the host.
        """
        write_job_file(
            JOBS_ROOT / job_id,
            "job.py",
            "from pathlib import Path\nPath('/data/output.txt').write_text('artifact', encoding='utf-8')\n",
        )
        run_job(job_id)
        wait_for_completion(job_id)
        artifact = DATA_ROOT / job_id / "output.txt"
        assert artifact.exists()
        assert artifact.read_text(encoding="utf-8") == "artifact"

    def test_job_can_write_multiple_artifacts_in_subdirectory(self, job_id):
        """
        Why needed: many ML jobs write models, metrics, and plots to
                    subdirectories; nested paths must be writable in /data.
        """
        write_job_file(
            JOBS_ROOT / job_id,
            "job.py",
            (
                "from pathlib import Path\n"
                "d = Path('/data/results')\n"
                "d.mkdir()\n"
                "(d / 'metrics.json').write_text('{\"loss\": 0.1}', encoding='utf-8')\n"
                "(d / 'model.bin').write_bytes(b'\\x00\\x01\\x02')\n"
            ),
        )
        run_job(job_id)
        wait_for_completion(job_id)
        assert (DATA_ROOT / job_id / "results" / "metrics.json").exists()
        assert (DATA_ROOT / job_id / "results" / "model.bin").exists()

    def test_multi_module_job_imports_helper_successfully(self, job_id):
        """
        Type      : multi-file job
        Why needed: real jobs organize code across modules; the tar archive
                    must bundle all files so imports work inside the container.
        How tested: job.py imports helper.py; job succeeds and uses the helper.
        """
        write_job_file(
            JOBS_ROOT / job_id,
            "helper.py",
            "def greet(name):\n    return f'Hello, {name}!'\n",
        )
        write_job_file(
            JOBS_ROOT / job_id,
            "job.py",
            "from helper import greet\nprint(greet('World'), flush=True)\n",
        )
        run_job(job_id)
        job = wait_for_completion(job_id)
        assert job["status"] == "SUCCEEDED"
        log = (DATA_ROOT / job_id / "container_logs.txt").read_bytes().decode(
            "utf-8", errors="replace"
        )
        assert "Hello, World!" in log

    def test_data_directories_are_isolated_between_jobs(self, job_id):
        """
        Type      : isolation
        Why needed: each job's /data must be independent; if two jobs share a
                    data directory they can corrupt each other's artifacts.
        How tested: run two jobs; verify each only contains its own artifact.
        """
        jid2 = f"test_{uuid.uuid4().hex[:12]}"
        job_dir2 = JOBS_ROOT / jid2
        job_dir2.mkdir(parents=True, exist_ok=True)
        try:
            write_job_file(
                JOBS_ROOT / job_id,
                "job.py",
                "from pathlib import Path\nPath('/data/file_a.txt').write_text('A')\n",
            )
            write_job_file(
                job_dir2,
                "job.py",
                "from pathlib import Path\nPath('/data/file_b.txt').write_text('B')\n",
            )
            run_job(job_id)
            run_job(jid2)
            wait_for_completion(job_id)
            wait_for_completion(jid2)

            dir1 = DATA_ROOT / job_id
            dir2 = DATA_ROOT / jid2
            assert dir1 != dir2
            assert (dir1 / "file_a.txt").exists()
            assert not (dir1 / "file_b.txt").exists()
            assert (dir2 / "file_b.txt").exists()
            assert not (dir2 / "file_a.txt").exists()
        finally:
            shutil.rmtree(job_dir2, ignore_errors=True)
            shutil.rmtree(DATA_ROOT / jid2, ignore_errors=True)
