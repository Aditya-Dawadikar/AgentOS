"""
Offline metric computation for AgentOS JobRunner benchmark.

Reads the JSONL events file from the load generator and optionally
a JSONL probe file from the host-side probe, then computes all
Part 1 metrics defined in BENCHMARK.md.

Usage:
  python compute_metrics.py events.jsonl \\
      --probe-file probe.jsonl \\
      --concurrency 16 \\
      --output metrics_c16.json

  # Restrict to a specific measurement window by epoch seconds:
  python compute_metrics.py events.jsonl \\
      --measurement-start 1700000100 \\
      --measurement-end   1700000400 \\
      --concurrency 16 \\
      --output metrics_c16.json
"""

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Any


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class JobEvent:
    job_id: Optional[str]
    workload: str
    submit_start: float
    accepted_at: float
    running_at: Optional[float]
    completed_at: Optional[float]
    terminal_status: Optional[str]
    submission_latency: float
    completion_latency: Optional[float]


@dataclass
class StageTransition:
    stage_index: int
    concurrency_level: int
    phase: str
    timestamp: float
    run_time_seconds: float


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_events(path: str):
    job_events: List[JobEvent] = []
    stage_transitions: List[StageTransition] = []

    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = rec.get("event")

            if event_type == "job_completed":
                job_events.append(JobEvent(
                    job_id=rec.get("job_id"),
                    workload=rec.get("workload", "unknown"),
                    submit_start=rec["submit_start"],
                    accepted_at=rec["accepted_at"],
                    running_at=rec.get("running_at"),
                    completed_at=rec.get("completed_at"),
                    terminal_status=rec.get("terminal_status"),
                    submission_latency=rec["submission_latency"],
                    completion_latency=rec.get("completion_latency"),
                ))

            elif event_type == "stage_transition":
                stage_transitions.append(StageTransition(
                    stage_index=rec.get("stage_index", 0),
                    concurrency_level=rec.get("concurrency_level", 0),
                    phase=rec.get("phase", "unknown"),
                    timestamp=rec["timestamp"],
                    run_time_seconds=rec.get("run_time_seconds", 0.0),
                ))

    return job_events, stage_transitions


def load_probe_samples(path: str) -> List[Dict[str, Any]]:
    samples = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event") in ("probe_start", "probe_end"):
                continue
            if "timestamp" in rec:
                samples.append(rec)
    return samples


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------

def percentile(values: List[float], p: float) -> Optional[float]:
    if not values:
        return None
    sv = sorted(values)
    idx = (len(sv) - 1) * p / 100.0
    lo = int(idx)
    hi = lo + 1
    if hi >= len(sv):
        return sv[lo]
    return sv[lo] * (1.0 - (idx - lo)) + sv[hi] * (idx - lo)


def safe_max(values):
    return max(values) if values else None


def safe_mean(values):
    return sum(values) / len(values) if values else None


# ---------------------------------------------------------------------------
# Active-job time series: C(t)
# ---------------------------------------------------------------------------

def compute_active_timeseries(events: List[JobEvent], resolution: float = 0.5) -> List[dict]:
    """
    C(t) = |{j | running_at_j <= t < completed_at_j}|

    Only counts jobs where both running_at and completed_at are known.
    """
    eligible = [e for e in events if e.running_at is not None and e.completed_at is not None]
    if not eligible:
        return []

    t_start = min(e.running_at for e in eligible)
    t_end = max(e.completed_at for e in eligible)

    timeline = []
    t = t_start
    while t <= t_end + resolution:
        active = sum(1 for e in eligible if e.running_at <= t < e.completed_at)
        timeline.append({"t": t, "t_relative": round(t - t_start, 3), "active": active})
        t += resolution

    return timeline


# ---------------------------------------------------------------------------
# Core metric computation
# ---------------------------------------------------------------------------

def compute_metrics(
    events: List[JobEvent],
    measurement_start: Optional[float] = None,
    measurement_end: Optional[float] = None,
) -> dict:
    if measurement_start is not None and measurement_end is not None:
        events = [
            e for e in events
            if measurement_start <= e.submit_start <= measurement_end
        ]

    total = len(events)
    if total == 0:
        return {"error": "no events in window", "total_jobs": 0}

    succeeded = [e for e in events if e.terminal_status == "SUCCEEDED"]
    failed = [e for e in events if e.terminal_status == "FAILED"]
    no_completion = [e for e in events if e.completed_at is None]

    sub_latencies = [e.submission_latency for e in events]
    comp_latencies = [e.completion_latency for e in events if e.completion_latency is not None]

    # Throughput over measurement window (or full span if no window given)
    if measurement_start is not None and measurement_end is not None:
        time_span = measurement_end - measurement_start
    else:
        completed_times = [e.completed_at for e in events if e.completed_at]
        start_times = [e.submit_start for e in events]
        if completed_times and start_times:
            time_span = max(completed_times) - min(start_times)
        else:
            time_span = None

    throughput = len(succeeded) / time_span if (time_span and time_span > 0) else None

    # Active-job time series for concurrency metrics
    timeseries = compute_active_timeseries(events)
    peak_concurrency = safe_max([pt["active"] for pt in timeseries]) or 0
    avg_concurrency = safe_mean([pt["active"] for pt in timeseries]) or 0

    # Little's Law estimate: L = lambda * W
    avg_completion = safe_mean(comp_latencies)
    little_law_estimate = (throughput * avg_completion) if (throughput and avg_completion) else None

    error_rate = len(failed) / total

    return {
        "total_jobs": total,
        "succeeded": len(succeeded),
        "failed": len(failed),
        "no_completion": len(no_completion),
        "error_rate": error_rate,
        "error_rate_pct": round(error_rate * 100, 4),
        "submission_latency_s": {
            "p50": percentile(sub_latencies, 50),
            "p95": percentile(sub_latencies, 95),
            "p99": percentile(sub_latencies, 99),
            "mean": safe_mean(sub_latencies),
            "max": safe_max(sub_latencies),
        },
        "completion_latency_s": {
            "p50": percentile(comp_latencies, 50),
            "p95": percentile(comp_latencies, 95),
            "p99": percentile(comp_latencies, 99),
            "mean": safe_mean(comp_latencies),
            "max": safe_max(comp_latencies),
        } if comp_latencies else None,
        "throughput_jobs_per_sec": throughput,
        "peak_concurrency": peak_concurrency,
        "avg_concurrency": round(avg_concurrency, 2),
        "little_law_concurrency_estimate": round(little_law_estimate, 2) if little_law_estimate else None,
        "time_span_seconds": round(time_span, 2) if time_span else None,
        "active_timeseries_sample_count": len(timeseries),
    }


# ---------------------------------------------------------------------------
# Acceptance threshold checks (BENCHMARK.md thresholds)
# ---------------------------------------------------------------------------

def check_thresholds(metrics: dict) -> dict:
    """
    Default thresholds from BENCHMARK.md:
      - error rate < 1%
      - no stuck jobs after cooldown
      - POST /jobs p95 < 2s
      - completion p95 < 10s  (sleep-short workload)
    """
    issues = []

    if metrics.get("error_rate", 0) >= 0.01:
        issues.append(
            f"error_rate {metrics['error_rate_pct']:.2f}% >= 1% threshold"
        )

    sub_p95 = (metrics.get("submission_latency_s") or {}).get("p95")
    if sub_p95 is not None and sub_p95 > 2.0:
        issues.append(f"POST /jobs p95 latency {sub_p95:.3f}s > 2.0s threshold")

    comp_p95 = (metrics.get("completion_latency_s") or {}).get("p95")
    if comp_p95 is not None and comp_p95 > 10.0:
        issues.append(f"completion p95 latency {comp_p95:.3f}s > 10.0s threshold")

    stuck = metrics.get("no_completion", 0)
    if stuck > 0:
        issues.append(f"{stuck} jobs have no terminal state (potentially stuck)")

    return {"stable": len(issues) == 0, "issues": issues}


# ---------------------------------------------------------------------------
# Probe aggregation
# ---------------------------------------------------------------------------

def aggregate_probe_samples(samples: List[dict], t_start: float = None, t_end: float = None) -> dict:
    if t_start is not None and t_end is not None:
        samples = [s for s in samples if t_start <= s["timestamp"] <= t_end]

    if not samples:
        return {"sample_count": 0}

    cpu = [s["cpu_percent"] for s in samples if "cpu_percent" in s]
    mem = [s["memory_percent"] for s in samples if "memory_percent" in s]
    db_active = [s["db_active_jobs"] for s in samples if s.get("db_active_jobs") is not None]
    docker_ct = [s["docker_containers"] for s in samples if s.get("docker_containers") is not None]

    return {
        "sample_count": len(samples),
        "cpu_percent": {
            "p50": percentile(cpu, 50),
            "p95": percentile(cpu, 95),
            "max": safe_max(cpu),
            "mean": safe_mean(cpu),
        } if cpu else None,
        "memory_percent": {
            "p50": percentile(mem, 50),
            "p95": percentile(mem, 95),
            "max": safe_max(mem),
        } if mem else None,
        "db_active_jobs_peak": safe_max(db_active),
        "db_active_jobs_mean": round(safe_mean(db_active), 2) if db_active else None,
        "docker_containers_peak": safe_max(docker_ct),
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Compute offline benchmark metrics from load-generator and probe JSONL files",
    )
    parser.add_argument("events_file", help="JSONL events file from locustfile.py")
    parser.add_argument("--probe-file", help="JSONL probe file from probe.py")
    parser.add_argument("--concurrency", type=int, default=None, help="Concurrency level label for this run")
    parser.add_argument("--measurement-start", type=float, default=None, help="Measurement window start (epoch seconds)")
    parser.add_argument("--measurement-end", type=float, default=None, help="Measurement window end (epoch seconds)")
    parser.add_argument("--output", help="Write JSON result to this file")
    args = parser.parse_args()

    job_events, stage_transitions = load_events(args.events_file)
    print(f"Loaded {len(job_events)} job_completed events, {len(stage_transitions)} stage transitions")

    metrics = compute_metrics(
        job_events,
        measurement_start=args.measurement_start,
        measurement_end=args.measurement_end,
    )

    result = {
        "concurrency_level": args.concurrency,
        "measurement_window": {
            "start": args.measurement_start,
            "end": args.measurement_end,
        },
        "metrics": metrics,
        "threshold_check": check_thresholds(metrics),
        "stage_transitions": [
            {
                "index": st.stage_index,
                "level": st.concurrency_level,
                "phase": st.phase,
                "timestamp": st.timestamp,
            }
            for st in stage_transitions
        ],
    }

    if args.probe_file:
        probe_samples = load_probe_samples(args.probe_file)
        print(f"Loaded {len(probe_samples)} probe samples")
        result["host_probes"] = aggregate_probe_samples(
            probe_samples,
            t_start=args.measurement_start,
            t_end=args.measurement_end,
        )

    output_json = json.dumps(result, indent=2)
    print(output_json)

    if args.output:
        with open(args.output, "w") as fh:
            fh.write(output_json)
        print(f"\nMetrics written to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
