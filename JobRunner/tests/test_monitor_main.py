"""
Unit tests for container_monitor.main() — full mocked end-to-end.

Type      : unit / end-to-end with full mocks
Why needed: main() orchestrates three threads and multiple Docker API calls;
            integration bugs between them (wrong argument order, missed
            mark_job_completed call) need to be caught without a real container.
How tested: patch docker.from_env and db.mark_job_completed; drive sys.argv;
            assert the DB function is called with the correct arguments.

No Docker daemon required.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.container_monitor import main
from tests.conftest import _closeable_iter


def _run_main(
    tmp_path: Path,
    container_id: str = "deadbeef",
    job_id: str = "job-001",
    exit_code: int = 0,
    wait_raises: Exception | None = None,
):
    """
    Wire up all mocks, run main(), and return (mock_mark, mock_container).
    """
    log_path = tmp_path / "logs.txt"
    kill_path = tmp_path / "kill_switch.txt"
    lc_path = tmp_path / "lifecycle.txt"

    container = MagicMock()
    container.id = container_id
    if wait_raises is not None:
        container.wait.side_effect = wait_raises
    else:
        container.wait.return_value = {"StatusCode": exit_code}
    container.logs.return_value = iter([b"2024-01-01T00:00:00Z hello\n"])
    container.status = "exited"
    container.reload.return_value = None

    mock_client = MagicMock()
    mock_client.containers.get.return_value = container
    mock_client.events.return_value = _closeable_iter([])

    argv = ["container_monitor", container_id,
            str(log_path), str(kill_path), str(lc_path), "0", job_id]

    with (
        patch("core.container_monitor.docker.from_env", return_value=mock_client),
        patch("core.container_monitor.mark_job_completed") as mock_mark,
        patch("sys.argv", argv),
    ):
        main()

    return mock_mark, container


class TestMonitorMain:

    def test_main_calls_mark_job_completed_on_success(self, tmp_path):
        """
        Why needed: if mark_job_completed is not called after the container
                    exits, the job stays in RUNNING forever.
        """
        mock_mark, _ = _run_main(tmp_path, exit_code=0)
        mock_mark.assert_called_once()
        args = mock_mark.call_args[0]
        assert args[0] == "job-001"    # job_id
        assert args[1] == "SUCCEEDED"  # status
        assert args[2] == 0            # exit_code

    def test_main_marks_failed_on_nonzero_exit(self, tmp_path):
        """
        Why needed: a non-zero exit code is the canonical signal that the
                    container's process failed; FAILED status must be stored.
        """
        mock_mark, _ = _run_main(tmp_path, exit_code=1)
        args = mock_mark.call_args[0]
        assert args[1] == "FAILED"
        assert args[2] == 1

    def test_main_marks_failed_when_container_wait_raises(self, tmp_path):
        """
        Type      : error – container.wait() failure
        Why needed: if the Docker daemon drops the connection mid-wait (e.g.
                    daemon restart), container.wait() raises.  The monitor must
                    still mark the job FAILED and not leave it in RUNNING.
        """
        mock_mark, _ = _run_main(tmp_path, wait_raises=Exception("connection reset"))
        args = mock_mark.call_args[0]
        assert args[1] == "FAILED"

    def test_main_removes_container_after_completion(self, tmp_path):
        """
        Why needed: orphaned containers accumulate on the host and exhaust disk
                    and process resources; each run must clean up after itself.
        """
        _, container = _run_main(tmp_path, exit_code=0)
        container.remove.assert_called_once()

    def test_main_removes_container_force_on_exception(self, tmp_path):
        """
        Why needed: a container that is stuck (not in 'exited' state) cannot
                    be removed without force=True; leaving it running wastes
                    resources and breaks subsequent runs with the same name.
        """
        _, container = _run_main(tmp_path, wait_raises=RuntimeError("timeout"))
        container.remove.assert_called_once()
        _, kwargs = container.remove.call_args
        assert kwargs.get("force") is True

    def test_main_writes_container_logs_to_file(self, tmp_path):
        """
        Why needed: container_logs.txt is the artifact most users look at first;
                    if the monitor fails to write it, debugging is impossible.
        """
        log_path = tmp_path / "logs.txt"
        kill_path = tmp_path / "kill_switch.txt"
        lc_path = tmp_path / "lifecycle.txt"

        container = MagicMock()
        container.wait.return_value = {"StatusCode": 0}
        container.logs.return_value = iter([
            b"2024-01-01T00:00:00Z line one\n",
            b"2024-01-01T00:00:01Z line two\n",
        ])
        container.status = "exited"
        container.reload.return_value = None

        mock_client = MagicMock()
        mock_client.containers.get.return_value = container
        mock_client.events.return_value = _closeable_iter([])

        argv = ["container_monitor", "logtest",
                str(log_path), str(kill_path), str(lc_path), "0", "job-log"]

        with (
            patch("core.container_monitor.docker.from_env", return_value=mock_client),
            patch("core.container_monitor.mark_job_completed"),
            patch("sys.argv", argv),
        ):
            main()

        assert log_path.exists()
        content = log_path.read_bytes()
        assert b"line one" in content
        assert b"line two" in content

    def test_main_sets_error_message_for_nonzero_exit(self, tmp_path):
        """
        Why needed: error_message is what the API returns to the caller; for a
                    non-zero exit it must contain the exit code so the caller
                    can distinguish 'OOM' (137) from 'user error' (1).
        """
        mock_mark, _ = _run_main(tmp_path, exit_code=137)
        error_msg = mock_mark.call_args[0][3]
        assert error_msg is not None
        assert "137" in error_msg

    def test_main_closes_docker_client(self, tmp_path):
        """
        Why needed: an unclosed Docker client holds open sockets; in a
                    long-running scheduler with many jobs this exhausts file
                    descriptors.
        """
        log_path = tmp_path / "logs2.txt"
        kill_path = tmp_path / "kill_switch2.txt"
        lc_path = tmp_path / "lifecycle2.txt"

        container2 = MagicMock()
        container2.wait.return_value = {"StatusCode": 0}
        container2.logs.return_value = iter([])
        container2.status = "exited"
        container2.reload.return_value = None

        mock_client2 = MagicMock()
        mock_client2.containers.get.return_value = container2
        mock_client2.events.return_value = _closeable_iter([])

        argv = ["container_monitor", "c99",
                str(log_path), str(kill_path), str(lc_path), "0", "j99"]

        with (
            patch("core.container_monitor.docker.from_env", return_value=mock_client2),
            patch("core.container_monitor.mark_job_completed"),
            patch("sys.argv", argv),
        ):
            main()

        mock_client2.close.assert_called_once()
