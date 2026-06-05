"""
Unit tests for low-overhead request-path profiling helpers.

Type      : unit / profiling metadata
Why needed: the benchmark summary depends on stable per-request phase timings;
            regressions here silently break offline profiling.
How tested: run_job() is executed with a mocked Docker client so the test can
            validate emitted timing fields without a live daemon.
"""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest


def _load_runner_module():
    docker_module = types.ModuleType('docker')
    docker_module.from_env = lambda timeout=10: _FakeDockerClient()
    sys.modules['docker'] = docker_module
    sys.modules.pop('core.runner', None)
    return importlib.import_module('core.runner')


class _FakeContainer:

    def __init__(self):
        self.id = 'container-123'

    def put_archive(self, target: str, archive_bytes: bytes) -> None:
        assert target == '/'
        assert archive_bytes

    def start(self) -> None:
        return None


class _FakeContainers:

    def create(self, **kwargs):
        return _FakeContainer()


class _FakeDockerClient:

    def __init__(self):
        self.containers = _FakeContainers()


class TestRequestProfiling:

    def test_run_job_populates_profile_timings(self, tmp_path, monkeypatch):
        """
        Why needed: the offline profile summary expects archive size, Docker
                    metadata, and runner phase timings on each submission.
        """
        jobs_root = tmp_path / 'jobs'
        data_root = tmp_path / 'data'
        job_id = 'profile_job'
        job_dir = jobs_root / job_id
        job_dir.mkdir(parents=True)
        (job_dir / 'job.py').write_text("print('hello')\n", encoding='utf-8')

        runner = _load_runner_module()

        monkeypatch.setattr(runner, 'jobs_root', jobs_root)
        monkeypatch.setattr(runner, 'data_root', data_root)
        monkeypatch.setattr(runner, 'client', _FakeDockerClient())
        monkeypatch.setattr(runner, '_start_container_monitor', lambda *args, **kwargs: None)
        monkeypatch.setattr(runner, 'upsert_job', lambda *args, **kwargs: None)
        monkeypatch.setattr(runner, 'update_job', lambda *args, **kwargs: None)

        profile: dict[str, object] = {'phases_ms': {}}

        desc = runner.run_job(job_id, profile=profile)

        assert desc['container_id'] == 'container-123'
        assert profile['archive_bytes'] > 0
        assert profile['container_id'] == 'container-123'
        phases = profile['phases_ms']
        for phase_name in (
            'data_dir_prepare_ms',
            'db_mark_starting_ms',
            'archive_build_ms',
            'docker_create_ms',
            'put_archive_ms',
            'monitor_launch_ms',
            'container_start_ms',
            'container_desc_write_ms',
            'db_mark_running_ms',
            'runner_total_ms',
        ):
            assert phase_name in phases
            assert phases[phase_name] >= 0

        container_desc_path = Path(profile['data_dir']) / 'container_desc.txt'
        assert container_desc_path.exists()