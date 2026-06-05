"""
Top-level benchmark orchestrator for AgentOS JobRunner.

Implements the standard benchmark flow from BENCHMARK.md:
  1. Capture environment metadata
  2. Start host-side probe on the target host
  3. Run the Locust load generator (concurrency ladder or single level)
  4. Stop host-side probe
    5. Persist per-run summary artifacts

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

Output files land in --output-dir (default: results/) inside a per-run timestamped folder.
    <run_dir>/env.json        environment metadata
    <run_dir>/summary.json    benchmark summary from the load generator
    <run_dir>/probe.jsonl     raw host probe samples
    <run_dir>/profile_summary.json    request-path timing summary from JobRunner
    <run_dir>/profile_report.txt      human-readable request-path timing report
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
_DEFAULT_PROFILE_EVENTS_FILE = os.path.abspath(
    os.path.join(_HERE, '..', 'JobRunner', 'data', 'request_profile_events.jsonl')
)

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


def run_locust(host: str,
               workload: str,
               summary_file: str,
               benchmark_run_id: str,
               single_level: int = None) -> int:
    locustfile = os.path.join(_HERE, "load_generator", "locustfile.py")
    cmd = [
        sys.executable, "-m", "locust",
        "-f", locustfile,
        "--host", host,
        "--headless",
    ]

    env = os.environ.copy()
    env["BENCHMARK_WORKLOAD"] = workload
    env["BENCHMARK_SUMMARY_FILE"] = summary_file
    env["BENCHMARK_RUN_ID"] = benchmark_run_id
    if single_level is not None:
        env["BENCHMARK_SINGLE_LEVEL"] = str(single_level)

    print(f"  Locust command: {' '.join(cmd)}")
    proc = subprocess.run(cmd, env=env)
    return proc.returncode


def compute_profile_summary(profile_events_file: str, run_id: str, output_file: str) -> int:
    script = os.path.join(_HERE, 'analysis', 'compute_profile_metrics.py')
    cmd = [
        sys.executable,
        script,
        profile_events_file,
        '--run-id', run_id,
        '--output', output_file,
    ]
    print(f"  Profile summary command: {' '.join(cmd)}")
    proc = subprocess.run(cmd)
    return proc.returncode


def render_profile_report(summary_file: str, output_file: str) -> int:
    script = os.path.join(_HERE, 'analysis', 'profile_report.py')
    cmd = [
        sys.executable,
        script,
        summary_file,
        '--output', output_file,
    ]
    print(f"  Profile report command: {' '.join(cmd)}")
    proc = subprocess.run(cmd)
    return proc.returncode


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
    parser.add_argument("--output-dir", default="results",
                        help="Base directory for output files (default: results/)")
    parser.add_argument("--instance-type", default=None,
                        help="EC2 instance type label (optional, auto-detected on EC2)")
    parser.add_argument("--image-pre-pulled", action="store_true",
                        help="Flag that Docker image was pre-pulled before the run")
    parser.add_argument("--single-level", type=int, default=None,
                        help="Test a single concurrency level instead of the full ladder")
    parser.add_argument("--probe-interval", type=float, default=1.0,
                        help="Host probe sample interval in seconds (default: 1.0)")
    parser.add_argument("--profile-events-file", default=_DEFAULT_PROFILE_EVENTS_FILE,
                        help="Path to JobRunner request profile JSONL (default: JobRunner/data/request_profile_events.jsonl)")
    args = parser.parse_args()

    run_ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    run_dir = os.path.join(args.output_dir, run_ts)
    os.makedirs(run_dir, exist_ok=True)

    # 1. Environment metadata
    print("Step 1/6: Capturing environment metadata...")
    env = capture_environment(args)
    env["profile_events_file"] = args.profile_events_file
    env_file = os.path.join(run_dir, "env.json")
    with open(env_file, "w") as fh:
        json.dump(env, fh, indent=2)
    print(f"  Run ID  : {env['run_id']}")
    print(f"  Git SHA : {env['git_commit_sha']}")
    print(f"  Host    : {env['target_host']}")
    print(f"  Workload: {env['workload']}")
    print(f"  Mode    : {env['benchmark_mode']}")

    # 2. Start host probe
    probe_file = os.path.join(run_dir, "probe.jsonl")
    print(f"\nStep 2/6: Starting host-side probe -> {probe_file}")
    probe_proc = start_probe(args.db, probe_file, interval=args.probe_interval)

    try:
        # 3. Run load generator
        summary_file = os.path.join(run_dir, "summary.json")
        print(f"\nStep 3/6: Running load generator -> {summary_file}")
        if args.single_level:
            print(f"  Mode: single level (concurrency={args.single_level})")
        else:
            print("  Mode: full concurrency ladder [1,4,16,64,256]")

        rc = run_locust(
            host=args.host,
            workload=args.workload,
            summary_file=summary_file,
            benchmark_run_id=env['run_id'],
            single_level=args.single_level,
        )
        if rc != 0:
            print(f"  WARNING: Locust exited with code {rc}")

    finally:
        # 4. Stop probe
        print("\nStep 4/6: Stopping host-side probe...")
        probe_proc.terminate()
        try:
            probe_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            probe_proc.kill()

    profile_summary_file = os.path.join(run_dir, 'profile_summary.json')
    profile_report_file = os.path.join(run_dir, 'profile_report.txt')
    profile_summary_created = False
    profile_report_created = False

    # 5. Compute request-path profile summary
    print("\nStep 5/6: Computing request-path profile summary...")
    if os.path.isfile(args.profile_events_file):
        profile_rc = compute_profile_summary(args.profile_events_file, env['run_id'], profile_summary_file)
        if profile_rc == 0 and os.path.isfile(profile_summary_file):
            profile_summary_created = True
            report_rc = render_profile_report(profile_summary_file, profile_report_file)
            if report_rc == 0 and os.path.isfile(profile_report_file):
                profile_report_created = True
            else:
                print(f"  WARNING: Profile report generation failed with code {report_rc}")
        else:
            print(f"  WARNING: Profile summary generation failed with code {profile_rc}")
    else:
        print(f"  WARNING: profile events file not found: {args.profile_events_file}")

    # 6. Persist run artifacts
    print("\nStep 6/6: Finalizing summary artifacts...")

    print("\n" + "=" * 60)
    print("Benchmark complete.")
    print(f"  Output directory : {run_dir}/")
    print(f"  Environment      : {env_file}")
    print(f"  Summary          : {summary_file}")
    print(f"  Probe samples    : {probe_file}")
    if profile_summary_created:
        print(f"  Profile summary  : {profile_summary_file}")
    if profile_report_created:
        print(f"  Profile report   : {profile_report_file}")
    print("=" * 60)


if __name__ == "__main__":
    main()
