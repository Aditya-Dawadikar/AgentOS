"""
Shared fixtures and helpers for JobRunner tests.

Integration tests require a running Docker daemon and the python:3-alpine
image (pulled automatically on first use, may be slow on a cold host).
"""
from __future__ import annotations

import json
import shutil
import sys
import time
import uuid
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

JOBS_ROOT = PROJECT_ROOT / "jobs"
DATA_ROOT = PROJECT_ROOT / "data"


# ── helpers ────────────────────────────────────────────────────────────────────

def write_job_file(job_dir: Path, filename: str, content: str) -> None:
    """Write *content* to job_dir/filename, creating parent dirs as needed."""
    target = job_dir / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def wait_for_job(job_id: str, *, states: set[str], timeout: float = 90.0) -> dict:
    """
    Poll the SQLite DB every 0.5 s until the job is in one of *states*.
    Raises TimeoutError when *timeout* seconds elapse without a match.
    """
    from db import get_job

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = get_job(job_id)
        if job and job["status"] in states:
            return job
        time.sleep(0.5)
    raise TimeoutError(
        f"Job {job_id!r} did not reach {states} within {timeout} s"
    )


def wait_for_completion(job_id: str, timeout: float = 90.0) -> dict:
    """Wait until the job is SUCCEEDED or FAILED."""
    return wait_for_job(job_id, states={"SUCCEEDED", "FAILED"}, timeout=timeout)


def parse_lifecycle_events(path: Path) -> list[dict]:
    """Parse the newline-delimited JSON lifecycle log into a list of event dicts."""
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped:
            events.append(json.loads(stripped))
    return events


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def job_id():
    """
    Yield a unique job_id whose directory is created under JOBS_ROOT.
    Both the job directory and the matching data directory are removed after
    the test, regardless of outcome.
    """
    jid = f"test_{uuid.uuid4().hex[:12]}"
    job_dir = JOBS_ROOT / jid
    job_dir.mkdir(parents=True, exist_ok=True)
    yield jid
    shutil.rmtree(job_dir, ignore_errors=True)
    shutil.rmtree(DATA_ROOT / jid, ignore_errors=True)


def _closeable_iter(items: list) -> MagicMock:
    """
    Return a MagicMock that iterates over *items* and has a .close() method.
    Docker's events() generator supports .close(); plain iter() does not.
    Shared by TestMonitorMain and TestWellKnownContainerFailureModes.
    """
    from unittest.mock import MagicMock
    m = MagicMock()
    m.__iter__ = MagicMock(return_value=iter(items))
    return m


@pytest.fixture()
def ensure_killed(job_id):
    """
    Guarantee that the job associated with *job_id* is stopped even when a test
    fails before it can trigger the kill switch itself.  Use this fixture for any
    test that starts a long-running container.
    """
    yield job_id
    kill_path = DATA_ROOT / job_id / "kill_switch.txt"
    if kill_path.parent.exists() and not kill_path.exists():
        try:
            kill_path.touch()
        except OSError:
            pass
    # Brief grace period so the monitor process can react.
    time.sleep(2)
