"""
Top-level benchmark orchestrator for AgentOS JobRunner.

Implements the standard benchmark flow from BENCHMARK.md:
  1. Capture environment metadata
  2. Start host-side probe on the target host
  3. Run the Locust load generator (concurrency ladder or single level)
  4. Stop host-side probe
  5. Compute metrics offline and print a summary report

Usage (full ladder, warm mode):
  python run_benchmark.py \\
      --host http://localhost:8000 \\
      --db   ../JobRunner/data/jobs.sqlite3

Usage (single concurrency level, quick test):
  python run_benchmark.py \\
      --host http://localhost:8000 \\
      --db   ../JobRunner/data/jobs.sqlite3 \\
      --single-level 8 \\
      --workload sleep_fixed

Output files land in --output-dir (default: benchmark_results/).
  <ts>_env.json        environment metadata
  <ts>_events.jsonl    raw job events from load generator
  <ts>_probe.jsonl     raw host probe samples
  <ts>_metrics_cN.json computed metrics per concurrency level (or combined)
  <ts>_report.txt      human-readable summary
"""

import argparse
import datetime
import json
import os
import platform
import subprocess
import sys
import time
import urllib.request
import uuid


_HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Environment capture
# ---------------------------------------------------------------------------

def capture_environment(args: argparse.Namespace) -> dict:
    env = {
        "run_id": str(uuid.uuid4())[:8],
        "date_utc": datetime.datetime.utcnow().isoformat() + "Z",
        "python_version": platform.python_version(),
        "os_platform": platform.platform(),
        "benchmark_mode": args.mode,
        "workload": args.workload,
        "image_pre_pulled": args.image_pre_pulled,
        "target_host": args.host,
    }

    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, cwd=_HERE,
        ).decode().strip()
        env["git_commit_sha"] = sha
    except Exception:
        env["git_commit_sha"] = "unknown"

    try:
        docker_ver = subprocess.check_output(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        env["docker_version"] = docker_ver
    except Exception:
        env["docker_version"] = "unknown"

    # EC2 instance type — best-effort, silently skipped on non-EC2 hosts
    try:
        req = urllib.request.Request(
            "http://169.254.169.254/latest/meta-data/instance-type",
        )
        with urllib.request.urlopen(req, timeout=1) as resp:
            env["ec2_instance_type"] = resp.read().decode().strip()
    except Exception:
        env["ec2_instance_type"] = args.instance_type or "local"

    return env


# ---------------------------------------------------------------------------
# Sub-process launchers
# ---------------------------------------------------------------------------

def start_probe(db_path: str, output_file: str, interval: float = 1.0) -> subprocess.Popen:
    probe_script = os.path.join(_HERE, "host_probes", "probe.py")
    return subprocess.Popen(
        [sys.executable, probe_script,
         "--db", db_path,
         "--output", output_file,
         "--interval", str(interval)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def run_locust(
    host: str,
    workload: str,
    events_file: str,
    single_level: int = None,
) -> int:
    locustfile = os.path.join(_HERE, "load_generator", "locustfile.py")
    cmd = [
        sys.executable, "-m", "locust",
        "-f", locustfile,
        "--host", host,
        "--headless",
    ]

    env = os.environ.copy()
    env["BENCHMARK_WORKLOAD"] = workload
    env["BENCHMARK_EVENTS_FILE"] = events_file
    if single_level is not None:
        env["BENCHMARK_SINGLE_LEVEL"] = str(single_level)

    print(f"  Locust command: {' '.join(cmd)}")
    proc = subprocess.run(cmd, env=env)
    return proc.returncode


def compute_metrics(
    events_file: str,
    probe_file: str,
    concurrency: int,
    output_file: str,
    measurement_start: float = None,
    measurement_end: float = None,
) -> dict:
    script = os.path.join(_HERE, "analysis", "compute_metrics.py")
    cmd = [
        sys.executable, script,
        events_file,
        "--probe-file", probe_file,
        "--concurrency", str(concurrency),
        "--output", output_file,
    ]
    if measurement_start is not None:
        cmd += ["--measurement-start", str(measurement_start)]
    if measurement_end is not None:
        cmd += ["--measurement-end", str(measurement_end)]

    subprocess.run(cmd, check=True)
    with open(output_file) as fh:
        return json.load(fh)


def generate_report(metrics_files: list, env_file: str, output_file: str) -> None:
    script = os.path.join(_HERE, "analysis", "report.py")
    cmd = [sys.executable, script] + metrics_files + ["--env", env_file, "--output", output_file]
    subprocess.run(cmd, check=True)


# ---------------------------------------------------------------------------
# Determine measurement windows from stage transitions in events JSONL
# ---------------------------------------------------------------------------

def extract_measurement_windows(events_file: str) -> list:
    """
    Reads stage_transition events and returns a list of
    (concurrency_level, phase_start_ts, phase_end_ts) for 'measurement' phases.
    """
    transitions = []
    with open(events_file) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event") == "stage_transition":
                transitions.append(rec)

    windows = []
    for i, t in enumerate(transitions):
        if t.get("phase") == "measurement":
            level = t.get("concurrency_level")
            t_start = t["timestamp"]
            # End is the start of the next transition (cooldown)
            t_end = transitions[i + 1]["timestamp"] if i + 1 < len(transitions) else None
            windows.append({
                "concurrency_level": level,
                "measurement_start": t_start,
                "measurement_end": t_end,
            })

    return windows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Run AgentOS JobRunner benchmark",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--host", default="http://localhost:8000", help="JobRunner base URL")
    parser.add_argument("--db", required=True, help="Path to jobs.sqlite3")
    parser.add_argument(
        "--workload", default="sleep_short",
        choices=["sleep_short", "sleep_fixed", "tiny_fast_exit"],
        help="Benchmark workload (default: sleep_short)",
    )
    parser.add_argument("--mode", default="warm", choices=["warm", "cold"],
                        help="Benchmark phase: warm (default) or cold")
    parser.add_argument("--output-dir", default="benchmark_results",
                        help="Directory for output files (default: benchmark_results/)")
    parser.add_argument("--instance-type", default=None,
                        help="EC2 instance type label (optional, auto-detected on EC2)")
    parser.add_argument("--image-pre-pulled", action="store_true",
                        help="Flag that Docker image was pre-pulled before the run")
    parser.add_argument("--single-level", type=int, default=None,
                        help="Test a single concurrency level instead of the full ladder")
    parser.add_argument("--probe-interval", type=float, default=1.0,
                        help="Host probe sample interval in seconds (default: 1.0)")
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    run_ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S")

    # 1. Environment metadata
    print("Step 1/5: Capturing environment metadata...")
    env = capture_environment(args)
    env_file = os.path.join(args.output_dir, f"{run_ts}_env.json")
    with open(env_file, "w") as fh:
        json.dump(env, fh, indent=2)
    print(f"  Run ID  : {env['run_id']}")
    print(f"  Git SHA : {env['git_commit_sha']}")
    print(f"  Host    : {env['target_host']}")
    print(f"  Workload: {env['workload']}")
    print(f"  Mode    : {env['benchmark_mode']}")

    # 2. Start host probe
    probe_file = os.path.join(args.output_dir, f"{run_ts}_probe.jsonl")
    print(f"\nStep 2/5: Starting host-side probe -> {probe_file}")
    probe_proc = start_probe(args.db, probe_file, interval=args.probe_interval)

    try:
        # 3. Run load generator
        events_file = os.path.join(args.output_dir, f"{run_ts}_events.jsonl")
        print(f"\nStep 3/5: Running load generator -> {events_file}")
        if args.single_level:
            print(f"  Mode: single level (concurrency={args.single_level})")
        else:
            print("  Mode: full concurrency ladder [1,2,4,8,16,32,64,128]")

        rc = run_locust(
            host=args.host,
            workload=args.workload,
            events_file=events_file,
            single_level=args.single_level,
        )
        if rc != 0:
            print(f"  WARNING: Locust exited with code {rc}")

    finally:
        # 4. Stop probe
        print("\nStep 4/5: Stopping host-side probe...")
        probe_proc.terminate()
        try:
            probe_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            probe_proc.kill()

    # 5. Offline metric computation
    print("\nStep 5/5: Computing metrics offline...")
    metrics_files = []

    if args.single_level:
        out_path = os.path.join(args.output_dir, f"{run_ts}_metrics_c{args.single_level}.json")
        compute_metrics(events_file, probe_file, args.single_level, out_path)
        metrics_files.append(out_path)
    else:
        # Extract per-level measurement windows from stage transitions
        windows = extract_measurement_windows(events_file)
        if windows:
            for w in windows:
                level = w["concurrency_level"]
                out_path = os.path.join(args.output_dir, f"{run_ts}_metrics_c{level}.json")
                compute_metrics(
                    events_file, probe_file, level, out_path,
                    measurement_start=w["measurement_start"],
                    measurement_end=w["measurement_end"],
                )
                metrics_files.append(out_path)
        else:
            # Fallback: compute across all events if no stage transitions recorded
            print("  WARNING: no stage transitions found — computing metrics across full run")
            out_path = os.path.join(args.output_dir, f"{run_ts}_metrics_all.json")
            compute_metrics(events_file, probe_file, 0, out_path)
            metrics_files.append(out_path)

    report_file = os.path.join(args.output_dir, f"{run_ts}_report.txt")
    generate_report(metrics_files, env_file, report_file)

    print("\n" + "=" * 60)
    print("Benchmark complete.")
    print(f"  Output directory : {args.output_dir}/")
    print(f"  Environment      : {env_file}")
    print(f"  Events           : {events_file}")
    print(f"  Probe samples    : {probe_file}")
    for mf in metrics_files:
        print(f"  Metrics          : {mf}")
    print(f"  Report           : {report_file}")
    print("=" * 60)


if __name__ == "__main__":
    main()
