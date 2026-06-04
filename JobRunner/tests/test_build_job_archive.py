"""
Unit tests for _build_job_archive() — pure tar logic, no Docker required.

Type      : unit / archive correctness
Why needed: if the tar archive is malformed or missing files, the container
            will fail silently at import time instead of with a clear error.
How tested: build an archive from a temp directory and inspect its contents
            with the stdlib tarfile module.
"""
from __future__ import annotations

import tarfile
import tempfile
from io import BytesIO
from pathlib import Path

import pytest

try:
    from core.runner import _build_job_archive
except Exception as _e:
    pytest.skip(str(_e), allow_module_level=True)


class TestBuildJobArchive:

    def test_archive_contains_app_directory_entry(self):
        """
        Why needed: the container runs `python /app/job.py`; the archive must
                    place files under the 'app/' prefix.
        """
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            (job_dir / "job.py").write_text("print('hi')", encoding="utf-8")
            raw = _build_job_archive(job_dir)
            with tarfile.open(fileobj=BytesIO(raw)) as tf:
                names = tf.getnames()
        assert any(n == "app" or n.startswith("app/") for n in names)

    def test_archive_includes_all_files_from_job_dir(self):
        """
        Why needed: missing files in the archive cause ImportError inside the
                    container with no indication of which file is absent.
        """
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            (job_dir / "job.py").write_text("from helper import run\nrun()", encoding="utf-8")
            (job_dir / "helper.py").write_text("def run(): pass", encoding="utf-8")
            raw = _build_job_archive(job_dir)
            with tarfile.open(fileobj=BytesIO(raw)) as tf:
                names = tf.getnames()
        assert any("job.py" in n for n in names)
        assert any("helper.py" in n for n in names)

    def test_archive_includes_nested_subdirectory_files(self):
        """
        Why needed: jobs may organize code in packages; subdirectories must be
                    preserved so Python's package import mechanism works.
        """
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            sub = job_dir / "utils"
            sub.mkdir()
            (sub / "__init__.py").write_text("", encoding="utf-8")
            (sub / "math.py").write_text("def add(a, b): return a + b", encoding="utf-8")
            (job_dir / "job.py").write_text("from utils.math import add", encoding="utf-8")
            raw = _build_job_archive(job_dir)
            with tarfile.open(fileobj=BytesIO(raw)) as tf:
                names = tf.getnames()
        assert any("utils/math.py" in n or "utils\\math.py" in n for n in names)
