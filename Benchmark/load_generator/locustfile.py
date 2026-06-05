"""
Locust load generator for AgentOS JobRunner benchmark (Part 1).

Implements the submission-and-poll pattern required by BENCHMARK.md:
  - POST /jobs with the configured workload
  - poll GET /jobs/{job_id}/status until terminal state
    - write per-run summary metrics to a JSON file

The BenchmarkShape class drives the default concurrency ladder defined
in BENCHMARK.md: 1, 4, 16, 64, 256 users with warmup /
measurement / cooldown stages per level.

Environment variables:
  BENCHMARK_WORKLOAD     workload name (default: sleep_short)
    BENCHMARK_SUMMARY_FILE path for summary JSON output (default: benchmark_summary.json)
  BENCHMARK_POLL_INTERVAL seconds between status polls (default: 0.5)
  BENCHMARK_SINGLE_LEVEL single concurrency level to test; skips ladder
"""

import json
import os
import re
import threading
import time
from uuid import uuid4

from locust import HttpUser, constant, events, task, LoadTestShape

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WORKLOAD = os.environ.get("BENCHMARK_WORKLOAD", "sleep_short")
SUMMARY_FILE = os.environ.get("BENCHMARK_SUMMARY_FILE",
                              os.environ.get("BENCHMARK_EVENTS_FILE", "benchmark_summary.json"))
POLL_INTERVAL = float(os.environ.get("BENCHMARK_POLL_INTERVAL", "0.5"))
SINGLE_LEVEL = os.environ.get("BENCHMARK_SINGLE_LEVEL")
BENCHMARK_RUN_ID = os.environ.get("BENCHMARK_RUN_ID")

TERMINAL_STATES = {"SUCCEEDED", "FAILED"}

# Concurrency ladder from BENCHMARK.md
CONCURRENCY_LADDER = [1, 4, 16, 64, 256]

# Stage timing in seconds (per concurrency level)
WARMUP_SECONDS = 5
MEASUREMENT_SECONDS = 20
COOLDOWN_SECONDS = 5
FINAL_DRAIN_SECONDS = 30

_LOCUST_DIR = os.path.dirname(os.path.abspath(__file__))
_BENCHMARK_DIR = os.path.dirname(_LOCUST_DIR)
_RESULTS_DIR = os.path.join(_BENCHMARK_DIR, "results")
WORKLOADS_DIR = os.path.join(_BENCHMARK_DIR, "workloads")

# ---------------------------------------------------------------------------
# Build stage list from the concurrency ladder
# ---------------------------------------------------------------------------

def _build_stages():
    if SINGLE_LEVEL:
        users = int(SINGLE_LEVEL)
        return [
            {"duration": WARMUP_SECONDS,                       "users": users, "spawn_rate": users, "phase": "warmup",     "level": users},
            {"duration": WARMUP_SECONDS + MEASUREMENT_SECONDS, "users": users, "spawn_rate": 1,     "phase": "measurement", "level": users},
            {"duration": WARMUP_SECONDS + MEASUREMENT_SECONDS + FINAL_DRAIN_SECONDS,
             "users": users, "spawn_rate": 1, "phase": "drain", "level": users},
        ]

    stages = []
    elapsed = 0
    for index, users in enumerate(CONCURRENCY_LADDER):
        is_last_level = index == len(CONCURRENCY_LADDER) - 1
        stages.append({"duration": elapsed + WARMUP_SECONDS, "users": users, "spawn_rate": users, "phase": "warmup", "level": users})
        elapsed += WARMUP_SECONDS
        stages.append({"duration": elapsed + MEASUREMENT_SECONDS, "users": users, "spawn_rate": 1, "phase": "measurement", "level": users})
        elapsed += MEASUREMENT_SECONDS
        if is_last_level:
            stages.append({"duration": elapsed + FINAL_DRAIN_SECONDS, "users": users, "spawn_rate": 1, "phase": "drain", "level": users})
            elapsed += FINAL_DRAIN_SECONDS
        else:
            stages.append({"duration": elapsed + COOLDOWN_SECONDS, "users": 0, "spawn_rate": users, "phase": "cooldown", "level": users})
            elapsed += COOLDOWN_SECONDS
    return stages


STAGES = _build_stages()

# ---------------------------------------------------------------------------
# Summary recording
# ---------------------------------------------------------------------------

_summary_lock = threading.Lock()
_run_started_at = None
_resolved_summary_file = None
_summary = {
    "stages": [],
    "submission_records": [],
    "job_records": [],
}
_current_stage = {
    "concurrency_level": None,
    "phase": None,
}


def _resolve_summary_file(configured_path: str) -> str:
    directory, filename = os.path.split(configured_path)
    stem, suffix = os.path.splitext(filename)
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    timestamp_pattern = re.compile(r"(^|_)(\d{8}T\d{6}Z?)(_|$)")

    if not directory:
        run_dir = os.path.join(_RESULTS_DIR, timestamp)
        os.makedirs(run_dir, exist_ok=True)
        return os.path.join(run_dir, f"{stem}{suffix or '.json'}")

    if timestamp_pattern.search(stem):
        resolved_filename = filename
    else:
        resolved_filename = f"{timestamp}_{stem}{suffix or '.json'}"

    resolved_path = os.path.join(directory, resolved_filename) if directory else resolved_filename
    resolved_dir = os.path.dirname(resolved_path)
    if resolved_dir:
        os.makedirs(resolved_dir, exist_ok=True)
    return resolved_path


def _percentile(values: list[float], percentile: float):
    if not values:
        return None
    sorted_values = sorted(values)
    index = (len(sorted_values) - 1) * percentile / 100.0
    lower = int(index)
    upper = lower + 1
    if upper >= len(sorted_values):
        return sorted_values[lower]
    return sorted_values[lower] * (1.0 - (index - lower)) + sorted_values[upper] * (index - lower)


def _stage_snapshot() -> dict:
    with _summary_lock:
        return {
            "concurrency_level": _current_stage["concurrency_level"],
            "phase": _current_stage["phase"],
        }


def _request_headers(stage_snapshot: dict) -> dict[str, str]:
    headers = {
        "X-Benchmark-Request-Id": uuid4().hex,
        "X-Benchmark-Workload": WORKLOAD,
    }
    if BENCHMARK_RUN_ID:
        headers["X-Benchmark-Run-Id"] = BENCHMARK_RUN_ID
    if stage_snapshot.get("concurrency_level") is not None:
        headers["X-Benchmark-Concurrency-Level"] = str(stage_snapshot["concurrency_level"])
    if stage_snapshot.get("phase"):
        headers["X-Benchmark-Phase"] = str(stage_snapshot["phase"])
    return headers


def _metrics_from_records(submission_records: list[dict], job_records: list[dict]) -> dict:
    submission_latencies = [record["submission_latency"] for record in submission_records]
    completion_latencies = [record["completion_latency"] for record in job_records if record.get("completion_latency") is not None]
    submit_failures = sum(1 for record in submission_records if not record.get("accepted"))
    succeeded = sum(1 for record in job_records if record.get("terminal_status") == "SUCCEEDED")
    failed = sum(1 for record in job_records if record.get("terminal_status") == "FAILED")
    no_terminal_status = sum(1 for record in job_records if record.get("terminal_status") not in TERMINAL_STATES)
    total_submissions = len(submission_records)

    return {
        "total_submissions": total_submissions,
        "submit_failures": submit_failures,
        "completed_jobs": len(job_records),
        "succeeded": succeeded,
        "failed": failed,
        "no_terminal_status": no_terminal_status,
        "error_rate": ((submit_failures + failed) / total_submissions) if total_submissions else None,
        "submission_latency_s": {
            "p50": _percentile(submission_latencies, 50),
            "p95": _percentile(submission_latencies, 95),
            "p99": _percentile(submission_latencies, 99),
            "mean": (sum(submission_latencies) / len(submission_latencies)) if submission_latencies else None,
            "max": max(submission_latencies) if submission_latencies else None,
        },
        "completion_latency_s": {
            "p50": _percentile(completion_latencies, 50),
            "p95": _percentile(completion_latencies, 95),
            "p99": _percentile(completion_latencies, 99),
            "mean": (sum(completion_latencies) / len(completion_latencies)) if completion_latencies else None,
            "max": max(completion_latencies) if completion_latencies else None,
        } if completion_latencies else None,
    }


def _build_summary_payload() -> dict:
    with _summary_lock:
        stages = list(_summary["stages"])
        submission_records = list(_summary["submission_records"])
        job_records = list(_summary["job_records"])

    overall_metrics = _metrics_from_records(submission_records, job_records)
    per_level_metrics = {}
    levels = sorted({stage.get("concurrency_level") for stage in stages if stage.get("concurrency_level") is not None}
                    | {record.get("concurrency_level") for record in submission_records if record.get("concurrency_level") is not None}
                    | {record.get("concurrency_level") for record in job_records if record.get("concurrency_level") is not None})

    for level in levels:
        level_submissions = [record for record in submission_records if record.get("concurrency_level") == level]
        level_jobs = [record for record in job_records if record.get("concurrency_level") == level]
        measurement_submissions = [record for record in level_submissions if record.get("phase") == "measurement"]
        measurement_jobs = [record for record in level_jobs if record.get("phase") == "measurement"]
        per_level_metrics[str(level)] = {
            "all_phases": _metrics_from_records(level_submissions, level_jobs),
            "measurement_phase": _metrics_from_records(measurement_submissions, measurement_jobs),
        }

    return {
        "run_started_at": _run_started_at,
        "run_finished_at": time.time(),
        "benchmark_run_id": BENCHMARK_RUN_ID,
        "workload": WORKLOAD,
        "summary_file": _resolved_summary_file,
        **overall_metrics,
        "stages": stages,
        "max_concurrency_level_reached": max((stage["concurrency_level"] for stage in stages), default=0),
        "per_concurrency_level": per_level_metrics,
    }


def _write_summary_file() -> None:
    if not _resolved_summary_file:
        return
    summary_payload = _build_summary_payload()
    with open(_resolved_summary_file, "w", encoding="utf-8") as summary_file:
        json.dump(summary_payload, summary_file, indent=2)
    print(f"Benchmark summary file: {_resolved_summary_file}")


@events.init.add_listener
def on_locust_init(environment, **kwargs):
    global _resolved_summary_file, _run_started_at
    _resolved_summary_file = _resolve_summary_file(SUMMARY_FILE)
    _run_started_at = time.time()
    print(f"Benchmark summary file: {_resolved_summary_file}")


@events.quitting.add_listener
def on_locust_quit(environment, **kwargs):
    _write_summary_file()


# ---------------------------------------------------------------------------
# Load shape: concurrency ladder with warmup / measurement / cooldown
# ---------------------------------------------------------------------------

class BenchmarkShape(LoadTestShape):
    """Drives the concurrency ladder defined in BENCHMARK.md."""

    _last_stage_idx = -1

    def tick(self):
        run_time = self.get_run_time()
        for i, stage in enumerate(STAGES):
            if run_time < stage["duration"]:
                if i != BenchmarkShape._last_stage_idx:
                    with _summary_lock:
                        _current_stage["concurrency_level"] = stage["level"]
                        _current_stage["phase"] = stage["phase"]
                        _summary["stages"].append({
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
        accepted_at = None
        stage_snapshot = _stage_snapshot()

        if stage_snapshot["phase"] == "drain":
            time.sleep(POLL_INTERVAL)
            return

        # --- submit ---
        with open(self._job_py_path, "rb") as f:
            with self.client.post(
                "/jobs",
                files={"files": ("job.py", f, "text/plain")},
                headers=_request_headers(stage_snapshot),
                name="POST /jobs",
                catch_response=True,
            ) as resp:
                accepted_at = time.time()
                with _summary_lock:
                    _summary["submission_records"].append({
                        "concurrency_level": stage_snapshot["concurrency_level"],
                        "phase": stage_snapshot["phase"],
                        "submit_start": submit_start,
                        "accepted_at": accepted_at,
                        "submission_latency": accepted_at - submit_start,
                        "accepted": resp.status_code == 202,
                        "status_code": resp.status_code,
                    })

                if resp.status_code != 202:
                    resp.failure(f"Expected 202, got {resp.status_code}: {resp.text[:200]}")
                    return

                resp.success()
                job_id = resp.json().get("job_id")

        if not job_id:
            return

        # --- poll until terminal ---
        running_at = None
        completed_at = None
        terminal_status = None

        while True:
            with self.client.get(
                f"/jobs/{job_id}/status",
                name="GET /jobs/{job_id}/status",
                catch_response=True,
            ) as poll_resp:
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

        with _summary_lock:
            _summary["job_records"].append({
                "job_id": job_id,
                "concurrency_level": stage_snapshot["concurrency_level"],
                "phase": stage_snapshot["phase"],
                "submit_start": submit_start,
                "accepted_at": accepted_at,
                "completed_at": completed_at,
                "terminal_status": terminal_status,
                "submission_latency": accepted_at - submit_start,
                "completion_latency": (completed_at - submit_start) if completed_at else None,
            })
