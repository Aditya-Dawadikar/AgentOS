"""
Locust load generator for AgentOS JobRunner benchmark (Part 1).

Implements the submission-and-poll pattern required by BENCHMARK.md:
  - POST /jobs with the configured workload
  - poll GET /jobs/{job_id}/status until terminal state
  - record per-job lifecycle timestamps to a JSONL events file

The BenchmarkShape class drives the default concurrency ladder defined
in BENCHMARK.md: 1, 2, 4, 8, 16, 32, 64, 128 users with warmup /
measurement / cooldown stages per level.

Environment variables:
  BENCHMARK_WORKLOAD     workload name (default: sleep_short)
  BENCHMARK_EVENTS_FILE  path for JSONL output (default: benchmark_events.jsonl)
  BENCHMARK_POLL_INTERVAL seconds between status polls (default: 0.5)
  BENCHMARK_SINGLE_LEVEL single concurrency level to test; skips ladder
"""

import json
import os
import threading
import time

from locust import HttpUser, constant, events, task, LoadTestShape

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WORKLOAD = os.environ.get("BENCHMARK_WORKLOAD", "sleep_short")
EVENTS_FILE = os.environ.get("BENCHMARK_EVENTS_FILE", "benchmark_events.jsonl")
POLL_INTERVAL = float(os.environ.get("BENCHMARK_POLL_INTERVAL", "0.5"))
SINGLE_LEVEL = os.environ.get("BENCHMARK_SINGLE_LEVEL")

TERMINAL_STATES = {"SUCCEEDED", "FAILED"}

# Concurrency ladder from BENCHMARK.md
CONCURRENCY_LADDER = [1, 2, 4, 8, 16, 32, 64, 128]

# Stage timing in seconds (per concurrency level)
WARMUP_SECONDS = 60
MEASUREMENT_SECONDS = 300
COOLDOWN_SECONDS = 120

_LOCUST_DIR = os.path.dirname(os.path.abspath(__file__))
_BENCHMARK_DIR = os.path.dirname(_LOCUST_DIR)
WORKLOADS_DIR = os.path.join(_BENCHMARK_DIR, "workloads")

# ---------------------------------------------------------------------------
# Build stage list from the concurrency ladder
# ---------------------------------------------------------------------------

def _build_stages():
    if SINGLE_LEVEL:
        users = int(SINGLE_LEVEL)
        return [
            {"duration": WARMUP_SECONDS,                             "users": users, "spawn_rate": users, "phase": "warmup",      "level": users},
            {"duration": WARMUP_SECONDS + MEASUREMENT_SECONDS,       "users": users, "spawn_rate": 1,     "phase": "measurement", "level": users},
            {"duration": WARMUP_SECONDS + MEASUREMENT_SECONDS + COOLDOWN_SECONDS, "users": 0, "spawn_rate": users, "phase": "cooldown", "level": users},
        ]

    stages = []
    elapsed = 0
    for users in CONCURRENCY_LADDER:
        stages.append({"duration": elapsed + WARMUP_SECONDS,       "users": users, "spawn_rate": users, "phase": "warmup",      "level": users})
        elapsed += WARMUP_SECONDS
        stages.append({"duration": elapsed + MEASUREMENT_SECONDS,   "users": users, "spawn_rate": 1,     "phase": "measurement", "level": users})
        elapsed += MEASUREMENT_SECONDS
        stages.append({"duration": elapsed + COOLDOWN_SECONDS,      "users": 0,     "spawn_rate": users, "phase": "cooldown",    "level": users})
        elapsed += COOLDOWN_SECONDS
    return stages


STAGES = _build_stages()

# ---------------------------------------------------------------------------
# Event recording (append-only JSONL)
# ---------------------------------------------------------------------------

_events_lock = threading.Lock()
_events_fh = None


def _write_event(payload: dict) -> None:
    global _events_fh
    if _events_fh is None:
        return
    with _events_lock:
        _events_fh.write(json.dumps(payload) + "\n")
        _events_fh.flush()


@events.init.add_listener
def on_locust_init(environment, **kwargs):
    global _events_fh
    _events_fh = open(EVENTS_FILE, "a")
    _write_event({
        "event": "benchmark_start",
        "workload": WORKLOAD,
        "timestamp": time.time(),
        "events_file": EVENTS_FILE,
    })


@events.quitting.add_listener
def on_locust_quit(environment, **kwargs):
    global _events_fh
    _write_event({"event": "benchmark_end", "timestamp": time.time()})
    if _events_fh:
        _events_fh.close()
        _events_fh = None


# ---------------------------------------------------------------------------
# Load shape: concurrency ladder with warmup / measurement / cooldown
# ---------------------------------------------------------------------------

class BenchmarkShape(LoadTestShape):
    """Drives the concurrency ladder defined in BENCHMARK.md."""

    _last_stage_idx = -1

    def tick(self):
        run_time = self.get_current_run_time()
        for i, stage in enumerate(STAGES):
            if run_time < stage["duration"]:
                if i != BenchmarkShape._last_stage_idx:
                    _write_event({
                        "event": "stage_transition",
                        "stage_index": i,
                        "concurrency_level": stage["level"],
                        "phase": stage["phase"],
                        "user_count": stage["users"],
                        "timestamp": time.time(),
                        "run_time_seconds": run_time,
                    })
                    BenchmarkShape._last_stage_idx = i
                return (stage["users"], stage["spawn_rate"])
        return None


# ---------------------------------------------------------------------------
# User behaviour
# ---------------------------------------------------------------------------

class JobRunnerUser(HttpUser):
    """
    Each user continuously submits a job and polls until completion.
    No idle wait between iterations — each user is always busy, which
    means user_count directly controls peak concurrency pressure.
    """

    wait_time = constant(0)

    def on_start(self):
        self._job_py_path = os.path.join(WORKLOADS_DIR, WORKLOAD, "job.py")
        if not os.path.exists(self._job_py_path):
            raise FileNotFoundError(f"Workload not found: {self._job_py_path}")

    @task
    def submit_and_poll(self):
        submit_start = time.time()

        # --- submit ---
        with open(self._job_py_path, "rb") as f:
            resp = self.client.post(
                "/jobs",
                files={"files": ("job.py", f, "text/plain")},
                name="POST /jobs",
                catch_response=True,
            )

        accepted_at = time.time()

        if resp.status_code != 202:
            resp.failure(f"Expected 202, got {resp.status_code}: {resp.text[:200]}")
            _write_event({
                "event": "submit_failed",
                "workload": WORKLOAD,
                "status_code": resp.status_code,
                "submit_start": submit_start,
                "accepted_at": accepted_at,
                "submission_latency": accepted_at - submit_start,
            })
            return

        resp.success()
        job_id = resp.json().get("job_id")

        if not job_id:
            _write_event({
                "event": "submit_no_job_id",
                "workload": WORKLOAD,
                "submit_start": submit_start,
                "accepted_at": accepted_at,
            })
            return

        # --- poll until terminal ---
        running_at = None
        completed_at = None
        terminal_status = None

        while True:
            poll_resp = self.client.get(
                f"/jobs/{job_id}/status",
                name="GET /jobs/{job_id}/status",
                catch_response=True,
            )

            if poll_resp.status_code != 200:
                poll_resp.failure(f"Status poll failed: {poll_resp.status_code}")
                break

            poll_resp.success()
            data = poll_resp.json()
            current_status = data.get("status")

            if running_at is None and current_status in ("STARTING", "RUNNING"):
                running_at = time.time()

            if current_status in TERMINAL_STATES:
                completed_at = time.time()
                terminal_status = current_status
                break

            time.sleep(POLL_INTERVAL)

        _write_event({
            "event": "job_completed",
            "job_id": job_id,
            "workload": WORKLOAD,
            "submit_start": submit_start,
            "accepted_at": accepted_at,
            "running_at": running_at,
            "completed_at": completed_at,
            "terminal_status": terminal_status,
            "submission_latency": accepted_at - submit_start,
            "completion_latency": (completed_at - submit_start) if completed_at else None,
        })
