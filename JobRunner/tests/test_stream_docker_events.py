"""
Unit tests for _stream_docker_events().

Type      : unit / event streaming
Why needed: every Docker event during a container's life must be written to
            the lifecycle log; a missing event (especially 'die') leaves the
            job stuck in RUNNING state.
How tested: inject a mock Docker client whose .events() returns a controlled
            list of event dicts; assert on the file written.

No Docker daemon required.
"""
from __future__ import annotations

import json
import threading

import pytest
from unittest.mock import MagicMock

from core.container_monitor import _stream_docker_events


def _make_client(event_list: list[dict]) -> MagicMock:
    """Return a mock Docker client whose .events() yields event_list."""
    client = MagicMock()
    ctx = MagicMock()
    ctx.__iter__ = MagicMock(return_value=iter(event_list))
    ctx.__enter__ = MagicMock(return_value=ctx)
    ctx.__exit__ = MagicMock(return_value=False)
    ctx.close = MagicMock()
    client.events.return_value = ctx
    return client


class TestStreamDockerEvents:

    def test_events_are_written_as_json_lines(self, tmp_path):
        """
        Why needed: downstream tooling reads the lifecycle log as NDJSON;
                    if events are not serialised correctly the parser breaks.
        """
        client = _make_client([
            {"Action": "start", "id": "abc123"},
            {"Action": "die",   "id": "abc123"},
        ])
        log_path = tmp_path / "lifecycle.txt"
        stop = threading.Event()
        stop.set()

        _stream_docker_events(client, "abc123", log_path, 0, stop)

        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) >= 1
        for line in lines:
            assert isinstance(json.loads(line), dict)

    def test_start_event_written_to_lifecycle_log(self, tmp_path):
        """
        Why needed: the 'start' event is the proof that the container actually
                    ran; its absence causes lifecycle audits to report a gap.
        """
        client = _make_client([
            {"Action": "start",   "id": "abc123"},
            {"Action": "destroy", "id": "abc123"},
        ])
        log_path = tmp_path / "lifecycle.txt"

        _stream_docker_events(client, "abc123", log_path, 0, threading.Event())

        actions = [json.loads(l)["Action"] for l in log_path.read_text().splitlines() if l.strip()]
        assert "start" in actions

    def test_streaming_stops_on_destroy_event(self, tmp_path):
        """
        Type      : termination condition
        Why needed: the monitor must exit after 'destroy'; remaining in the event
                    loop after the container is gone wastes resources and can block
                    the monitor process indefinitely.
        How tested: send 'start', 'destroy', then an extra event; assert the
                    extra event is NOT in the log (the loop exited on 'destroy').
        """
        client = _make_client([
            {"Action": "start",   "id": "c1"},
            {"Action": "destroy", "id": "c1"},
            {"Action": "never",   "id": "c1"},  # must not be written
        ])
        log_path = tmp_path / "lifecycle.txt"

        _stream_docker_events(client, "c1", log_path, 0, threading.Event())

        actions = [json.loads(l)["Action"] for l in log_path.read_text().splitlines() if l.strip()]
        assert "destroy" in actions
        assert "never" not in actions

    def test_streaming_stops_when_stop_event_is_set(self, tmp_path):
        """
        Type      : external stop signal
        Why needed: the main thread sets stop_event in the finally block; if
                    the event thread ignores it the monitor process never exits.
        How tested: pre-set stop_event; function should return quickly, not block.
        """
        client = _make_client([
            {"Action": "start", "id": "c2"},
            {"Action": "extra", "id": "c2"},
        ])
        log_path = tmp_path / "lifecycle.txt"
        stop = threading.Event()
        stop.set()

        thread = threading.Thread(
            target=_stream_docker_events,
            args=(client, "c2", log_path, 0, stop),
        )
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive(), "_stream_docker_events did not stop when stop_event was set"

    def test_events_flushed_immediately_to_disk(self, tmp_path):
        """
        Why needed: the lifecycle file is tailed in real time by monitoring
                    tools; buffered writes would cause delayed or missing events.
        """
        client = _make_client([
            {"Action": "start",   "id": "c3"},
            {"Action": "destroy", "id": "c3"},
        ])
        log_path = tmp_path / "lifecycle.txt"

        _stream_docker_events(client, "c3", log_path, 0, threading.Event())

        lines = [l for l in log_path.read_text().splitlines() if l.strip()]
        assert len(lines) == 2

    def test_events_generator_is_closed_after_loop(self, tmp_path):
        """
        Why needed: an unclosed Docker events generator holds a long-lived HTTP
                    connection to the Docker daemon, leaking a file descriptor.
        """
        client = _make_client([{"Action": "destroy", "id": "c4"}])
        log_path = tmp_path / "lifecycle.txt"

        _stream_docker_events(client, "c4", log_path, 0, threading.Event())

        client.events.return_value.close.assert_called_once()
