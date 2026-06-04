"""
Unit tests for _watch_kill_switch().

Type      : unit / kill-switch
Why needed: the kill switch is the only way to stop a runaway container
            without direct Docker access; it must be reliable and must not
            crash on edge cases (container already gone, already exited).
How tested: mock docker.Container and filesystem; control the kill_switch
            file's existence and container.status.

No Docker daemon required.
"""
from __future__ import annotations

import threading
import time

import docker
import pytest
from unittest.mock import MagicMock

from core.container_monitor import _watch_kill_switch


class TestWatchKillSwitch:

    def test_kill_switch_file_triggers_container_kill(self, tmp_path):
        """
        Why needed: creating kill_switch.txt is the operator action; if
                    container.kill() is not called, the container keeps running.
        """
        kill_path = tmp_path / "kill_switch.txt"
        kill_path.touch()

        container = MagicMock()
        container.status = "running"

        _watch_kill_switch(container, kill_path)

        container.kill.assert_called_once()

    def test_kill_switch_returns_cleanly_when_container_not_found(self, tmp_path):
        """
        Type      : NotFound edge-case
        Why needed: the container may be removed between the kill() call and the
                    next loop iteration; NotFound must be swallowed, not crash the
                    monitor process.
        """
        kill_path = tmp_path / "kill_switch.txt"
        kill_path.touch()

        container = MagicMock()
        container.kill.side_effect = docker.errors.NotFound("gone")
        container.status = "running"

        _watch_kill_switch(container, kill_path)  # must not raise

    def test_returns_when_container_status_is_exited(self, tmp_path):
        """
        Type      : early exit
        Why needed: if the container exits before the kill switch fires, the
                    watcher must stop polling instead of running forever.
        """
        kill_path = tmp_path / "kill_switch.txt"
        container = MagicMock()
        container.status = "exited"
        container.reload.side_effect = lambda: None

        thread = threading.Thread(target=_watch_kill_switch, args=(container, kill_path))
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()

    def test_returns_when_container_status_is_dead(self, tmp_path):
        """
        Why needed: 'dead' is a distinct Docker container state; watcher must
                    treat it the same as 'exited'.
        """
        kill_path = tmp_path / "kill_switch.txt"
        container = MagicMock()
        container.status = "dead"
        container.reload.side_effect = lambda: None

        thread = threading.Thread(target=_watch_kill_switch, args=(container, kill_path))
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()

    def test_returns_when_reload_raises_not_found(self, tmp_path):
        """
        Type      : container vanished during polling
        Why needed: a container can be externally removed (e.g. by `docker rm`)
                    while the watcher polls; NotFound during reload must cause a
                    clean return, not a crash.
        """
        kill_path = tmp_path / "kill_switch.txt"
        container = MagicMock()
        container.status = "running"
        container.reload.side_effect = docker.errors.NotFound("removed externally")

        thread = threading.Thread(target=_watch_kill_switch, args=(container, kill_path))
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()

    def test_kill_switch_polls_until_file_appears(self, tmp_path):
        """
        Why needed: the watcher polls every 0.5 s; it must keep running until
                    the file is created, not give up after one check.
        How tested: create the kill switch file after a short delay; assert
                    container.kill() is eventually called.
        """
        kill_path = tmp_path / "kill_switch.txt"
        container = MagicMock()
        container.status = "running"
        container.reload.return_value = None

        def create_switch():
            time.sleep(0.8)
            kill_path.touch()

        creator = threading.Thread(target=create_switch)
        watcher = threading.Thread(target=_watch_kill_switch, args=(container, kill_path))
        creator.start()
        watcher.start()
        watcher.join(timeout=10)
        creator.join(timeout=5)

        assert not watcher.is_alive()
        container.kill.assert_called_once()
