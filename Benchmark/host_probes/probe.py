"""
Host-side probe for AgentOS JobRunner benchmark.

Runs on the same machine as JobRunner and collects server-side signals
at a fixed interval (default: 1 second). Output is append-only JSONL.

Signals collected per sample:
  - count of jobs in STARTING or RUNNING (SQLite source of truth)
  - count of live Docker containers
  - CPU utilization %
  - memory utilization %
  - Docker daemon health

Usage:
  python probe.py --db /path/to/jobs.sqlite3 --output probe_samples.jsonl

Stop with Ctrl-C or SIGTERM.
"""

import argparse
import json
import os
import signal
import sqlite3
import sys
import threading
import time

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

try:
    import docker as docker_sdk
    _DOCKER = True
except ImportError:
    _DOCKER = False


def _connect_docker():
    if not _DOCKER:
        return None
    try:
        client = docker_sdk.from_env()
        client.ping()
        return client
    except Exception:
        return None


def collect_sample(db_path: str, docker_client) -> dict:
    ts = time.time()
    sample = {
        "timestamp": ts,
        "timestamp_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
    }

    # --- SQLite: active job count ---
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        row = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('STARTING', 'RUNNING')"
        ).fetchone()
        sample["db_active_jobs"] = row[0] if row else 0
        conn.close()
    except Exception as exc:
        sample["db_active_jobs"] = None
        sample["db_error"] = str(exc)

    # --- Docker: live container count + daemon health ---
    if docker_client is not None:
        try:
            containers = docker_client.containers.list(filters={"status": "running"})
            sample["docker_containers"] = len(containers)
            sample["docker_healthy"] = True
        except Exception as exc:
            sample["docker_containers"] = None
            sample["docker_healthy"] = False
            sample["docker_error"] = str(exc)
    else:
        sample["docker_containers"] = None
        sample["docker_healthy"] = None

    # --- psutil: CPU and memory ---
    if _PSUTIL:
        try:
            sample["cpu_percent"] = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory()
            sample["memory_percent"] = mem.percent
            sample["memory_used_mb"] = mem.used // (1024 * 1024)
            sample["memory_available_mb"] = mem.available // (1024 * 1024)
        except Exception as exc:
            sample["psutil_error"] = str(exc)

    return sample


def main():
    parser = argparse.ArgumentParser(description="JobRunner host-side benchmark probe")
    parser.add_argument("--db", required=True, help="Path to jobs.sqlite3")
    parser.add_argument("--output", required=True, help="Output JSONL file path")
    parser.add_argument("--interval", type=float, default=1.0, help="Sample interval in seconds (default: 1.0)")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"WARNING: DB file not found at {args.db} — db_active_jobs will show errors until created", file=sys.stderr)

    docker_client = _connect_docker()
    if docker_client is None:
        print("WARNING: Docker SDK unavailable or daemon unreachable — docker_containers will be null", file=sys.stderr)
    if not _PSUTIL:
        print("WARNING: psutil not installed — CPU/memory metrics will be absent", file=sys.stderr)

    running = True

    def handle_signal(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    print(f"Probe running — interval={args.interval}s  output={args.output}", file=sys.stderr)

    with open(args.output, "a") as fh:
        # Write a start marker so offline analysis can identify probe boundaries
        fh.write(json.dumps({
            "event": "probe_start",
            "timestamp": time.time(),
            "interval_seconds": args.interval,
            "db_path": args.db,
        }) + "\n")
        fh.flush()

        while running:
            sample = collect_sample(args.db, docker_client)
            fh.write(json.dumps(sample) + "\n")
            fh.flush()
            # Sleep in small increments so signal can interrupt promptly
            deadline = time.time() + args.interval
            while running and time.time() < deadline:
                time.sleep(min(0.1, deadline - time.time()))

        fh.write(json.dumps({
            "event": "probe_end",
            "timestamp": time.time(),
        }) + "\n")
        fh.flush()

    print("Probe stopped.", file=sys.stderr)


if __name__ == "__main__":
    main()
