"""
Unit tests for well-known container failure modes handled by the monitor.

Type      : unit / edge-case coverage
Why needed: containers can fail in ways that are not directly caused by the
            user's code – daemon restarts, image eviction, network partitions.
            The monitor must handle all of them gracefully.

No Docker daemon required.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import docker
import pytest

from core.container_monitor import main
from tests.conftest import _closeable_iter


class TestWellKnownContainerFailureModes:

    def test_monitor_handles_container_already_removed_at_startup(self, tmp_path):
        """
        Type      : container gone before monitor starts
        Why needed: if the container exits and is cleaned up before the monitor
                    process initialises (e.g. very short job), containers.get()
                    raises NotFound.  The job must be marked FAILED, not crash.
        """
        log_path = tmp_path / "logs.txt"
        kill_path = tmp_path / "kill_switch.txt"
        lc_path = tmp_path / "lifecycle.txt"

        mock_client = MagicMock()
        mock_client.containers.get.side_effect = docker.errors.NotFound("gone")

        argv = ["container_monitor", "gone_id",
                str(log_path), str(kill_path), str(lc_path), "0", "job-gone"]

        with (
            patch("core.container_monitor.docker.from_env", return_value=mock_client),
            patch("core.container_monitor.mark_job_completed") as mock_mark,
            patch("sys.argv", argv),
        ):
            main()  # must not raise

        mock_mark.assert_called_once()
        assert mock_mark.call_args[0][1] == "FAILED"

    def test_monitor_handles_docker_api_error_during_log_streaming(self, tmp_path):
        """
        Type      : transient API error
        Why needed: Docker's log stream can raise APIError mid-stream if the
                    daemon is under load; partial logs must be preserved and the
                    job marked FAILED, not silently ignored.
        """
        log_path = tmp_path / "logs.txt"
        kill_path = tmp_path / "kill_switch.txt"
        lc_path = tmp_path / "lifecycle.txt"

        def exploding_logs(**kw):
            yield b"2024-01-01T00:00:00Z partial line\n"
            raise docker.errors.APIError("stream broken")

        container = MagicMock()
        container.wait.return_value = {"StatusCode": 0}
        container.logs.return_value = exploding_logs()
        container.status = "exited"
        container.reload.return_value = None

        mock_client = MagicMock()
        mock_client.containers.get.return_value = container
        mock_client.events.return_value = _closeable_iter([])

        argv = ["container_monitor", "api_err",
                str(log_path), str(kill_path), str(lc_path), "0", "job-api"]

        with (
            patch("core.container_monitor.docker.from_env", return_value=mock_client),
            patch("core.container_monitor.mark_job_completed") as mock_mark,
            patch("sys.argv", argv),
        ):
            main()

        mock_mark.assert_called_once()
        if log_path.exists():
            assert b"partial line" in log_path.read_bytes()

    def test_monitor_still_removes_container_after_api_error(self, tmp_path):
        """
        Why needed: even when the log stream errors out, the container must be
                    removed to avoid orphaned containers on the host.
        """
        log_path = tmp_path / "logs.txt"
        kill_path = tmp_path / "kill_switch.txt"
        lc_path = tmp_path / "lifecycle.txt"

        container = MagicMock()
        container.wait.side_effect = docker.errors.APIError("wait failed")
        container.logs.return_value = iter([])
        container.status = "running"
        container.reload.return_value = None

        mock_client = MagicMock()
        mock_client.containers.get.return_value = container
        mock_client.events.return_value = _closeable_iter([])

        argv = ["container_monitor", "orphan",
                str(log_path), str(kill_path), str(lc_path), "0", "job-orphan"]

        with (
            patch("core.container_monitor.docker.from_env", return_value=mock_client),
            patch("core.container_monitor.mark_job_completed"),
            patch("sys.argv", argv),
        ):
            main()

        container.remove.assert_called_once()
        _, kwargs = container.remove.call_args
        assert kwargs.get("force") is True

    def test_monitor_ignores_not_found_when_removing_container(self, tmp_path):
        """
        Type      : container removed externally before cleanup
        Why needed: an external operator may run `docker rm` on a container that
                    the monitor is about to remove; the resulting NotFound must be
                    swallowed so the monitor exits cleanly.
        """
        log_path = tmp_path / "logs.txt"
        kill_path = tmp_path / "kill_switch.txt"
        lc_path = tmp_path / "lifecycle.txt"

        container = MagicMock()
        container.wait.return_value = {"StatusCode": 0}
        container.logs.return_value = iter([])
        container.status = "exited"
        container.reload.return_value = None
        container.remove.side_effect = docker.errors.NotFound("already gone")

        mock_client = MagicMock()
        mock_client.containers.get.return_value = container
        mock_client.events.return_value = _closeable_iter([])

        argv = ["container_monitor", "ext_rm",
                str(log_path), str(kill_path), str(lc_path), "0", "job-extrm"]

        with (
            patch("core.container_monitor.docker.from_env", return_value=mock_client),
            patch("core.container_monitor.mark_job_completed") as mock_mark,
            patch("sys.argv", argv),
        ):
            main()  # must not raise

        mock_mark.assert_called_once()
        assert mock_mark.call_args[0][1] == "SUCCEEDED"
