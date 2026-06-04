# AgentOS JobRunner — Benchmark

This directory contains all benchmark tooling for the AgentOS JobRunner, implementing the contract defined in [`BENCHMARK.md`](../BENCHMARK.md).

## Overview

The benchmark is split into two roles:

| Role | Responsibility |
|---|---|
| **Load generator** | Submits jobs, polls status, records client-side latency events |
| **Host probe** | Samples DB active-job count, Docker container count, CPU, memory — 1 Hz |

After the run, both datasets are merged offline by `analysis/compute_metrics.py` and rendered by `analysis/report.py`.

## Directory Structure

```
Benchmark/
├── workloads/
│   ├── sleep_short/job.py      # primary: random 2–5 s sleep
│   ├── sleep_fixed/job.py      # fixed 3 s sleep
│   └── tiny_fast_exit/job.py   # instant exit (pure orchestration overhead)
├── load_generator/
│   ├── locustfile.py           # Locust user + concurrency ladder shape
│   └── requirements.txt
├── host_probes/
│   ├── probe.py                # 1 Hz server-side sampler
│   └── requirements.txt
├── analysis/
│   ├── compute_metrics.py      # offline metric computation
│   └── report.py               # human-readable summary table
├── run_benchmark.py            # end-to-end orchestrator
└── requirements.txt            # all benchmark dependencies
```

## Prerequisites

- Python 3.9+
- Docker daemon running and accessible
- `python:3-alpine` image pulled (warm benchmark mode):
  ```
  docker pull python:3-alpine
  ```

---

## Setting 1 — Local (JobRunner + Locust on the same machine)

Use this setting for development, quick iteration, and smoke-testing on a local workstation.

> **Note:** Because the load generator and the service share the same CPU, results will underestimate production capacity. Use this setting for regression comparison only — not for capacity planning.

### Step 1 — Install dependencies

```bash
# JobRunner service
cd JobRunner
pip install -r requirements.txt

# Benchmark tooling
cd ../Benchmark
pip install -r requirements.txt
```

### Step 2 — Start JobRunner

From the repo root:

```bash
cd JobRunner
uvicorn main:app --host 0.0.0.0 --port 8000
```

Verify it is healthy:

```bash
curl http://localhost:8000/health
```

### Step 3 — Start the host-side probe

Open a second terminal. The probe must run on the same machine as JobRunner because it reads the SQLite database directly.

```bash
cd Benchmark
python host_probes/probe.py \
    --db   ../JobRunner/data/jobs.sqlite3 \
    --output probe_samples.jsonl \
    --interval 1.0
```

Leave this running for the duration of the benchmark. Stop it with `Ctrl-C` when the load generator finishes.

### Step 4 — Run the load generator

Option A — **Full concurrency ladder** (1 → 2 → 4 → 8 → 16 → 32 → 64 → 128, ~64 minutes total):

```bash
cd Benchmark
BENCHMARK_WORKLOAD=sleep_short \
BENCHMARK_EVENTS_FILE=events.jsonl \
python -m locust \
    -f load_generator/locustfile.py \
    --host http://localhost:8000 \
    --headless
```

Option B — **Single concurrency level** (quick smoke test, ~8 minutes):

```bash
cd Benchmark
BENCHMARK_WORKLOAD=sleep_short \
BENCHMARK_EVENTS_FILE=events.jsonl \
BENCHMARK_SINGLE_LEVEL=8 \
python -m locust \
    -f load_generator/locustfile.py \
    --host http://localhost:8000 \
    --headless
```

### Step 5 — Stop the probe

Switch to the probe terminal and press `Ctrl-C`. It writes a `probe_end` marker and exits cleanly.

### Step 6 — Compute metrics (offline)

Replace `--concurrency` with the level you tested (or `0` if you ran the full ladder without stage tracking):

```bash
cd Benchmark
python analysis/compute_metrics.py events.jsonl \
    --probe-file  probe_samples.jsonl \
    --concurrency 8 \
    --output      metrics_c8.json
```

For the full ladder, pass `--measurement-start` and `--measurement-end` (epoch seconds) per level — the stage transition timestamps are recorded in `events.jsonl` and can be read directly, or use the orchestrator in step 6b.

### Step 6b — Or use the orchestrator (runs steps 3–6 automatically)

The orchestrator handles probe startup/shutdown, Locust, and offline analysis in one command:

```bash
cd Benchmark
python run_benchmark.py \
    --host      http://localhost:8000 \
    --db        ../JobRunner/data/jobs.sqlite3 \
    --workload  sleep_short \
    --mode      warm \
    --output-dir benchmark_results/
```

All output files land in `benchmark_results/` with a UTC timestamp prefix.

### Step 7 — Generate the report

```bash
cd Benchmark
python analysis/report.py metrics_c8.json

# Multiple levels:
python analysis/report.py benchmark_results/*_metrics_c*.json \
    --env benchmark_results/*_env.json \
    --output benchmark_results/report.txt
```

---

## Setting 2 — Remote JobRunner (EC2) + Local Locust

Use this setting for realistic capacity measurements. JobRunner runs on a dedicated EC2 instance; the load generator runs on your local machine or a CI runner.

> The host probe **must run on the EC2 instance** because it reads the SQLite database and Docker daemon state that live there.

### Step 1 — Prepare the EC2 instance

SSH into the EC2 instance and set up the service:

```bash
ssh ec2-user@<EC2_PUBLIC_IP>

# Clone the repo and install
git clone <repo-url> AgentOS
cd AgentOS/JobRunner
pip install -r requirements.txt

# Pull the benchmark image
docker pull python:3-alpine
```

### Step 2 — Start JobRunner on EC2

```bash
# On the EC2 instance
cd AgentOS/JobRunner
uvicorn main:app --host 0.0.0.0 --port 8000
```

Ensure port 8000 is open in the EC2 security group for your local IP (or the load-generator IP).

### Step 3 — Start the host-side probe on EC2

In a second SSH session on the EC2 instance:

```bash
# On the EC2 instance
cd AgentOS/Benchmark
pip install -r requirements.txt

python host_probes/probe.py \
    --db      ../JobRunner/data/jobs.sqlite3 \
    --output  /tmp/probe_samples.jsonl \
    --interval 1.0
```

Use `nohup` or `tmux` to keep the probe alive if your SSH session might drop:

```bash
tmux new-session -d -s probe \
    "python AgentOS/Benchmark/host_probes/probe.py \
        --db AgentOS/JobRunner/data/jobs.sqlite3 \
        --output /tmp/probe_samples.jsonl"
```

### Step 4 — Install benchmark dependencies locally

```bash
# On your local machine
cd Benchmark
pip install -r requirements.txt
```

### Step 5 — Run the load generator locally

```bash
# On your local machine
cd Benchmark
BENCHMARK_WORKLOAD=sleep_short \
BENCHMARK_EVENTS_FILE=events.jsonl \
python -m locust \
    -f load_generator/locustfile.py \
    --host http://<EC2_PUBLIC_IP>:8000 \
    --headless
```

For a single level:

```bash
BENCHMARK_WORKLOAD=sleep_short \
BENCHMARK_EVENTS_FILE=events.jsonl \
BENCHMARK_SINGLE_LEVEL=16 \
python -m locust \
    -f load_generator/locustfile.py \
    --host http://<EC2_PUBLIC_IP>:8000 \
    --headless
```

### Step 6 — Stop the probe on EC2

```bash
# On the EC2 instance — if using tmux:
tmux send-keys -t probe C-c
# Or kill by name:
pkill -f probe.py
```

### Step 7 — Copy probe data to your local machine

```bash
# On your local machine
scp ec2-user@<EC2_PUBLIC_IP>:/tmp/probe_samples.jsonl ./probe_samples.jsonl
```

### Step 8 — Compute metrics and generate the report locally

```bash
cd Benchmark

# Per level (example: concurrency=16)
python analysis/compute_metrics.py events.jsonl \
    --probe-file  probe_samples.jsonl \
    --concurrency 16 \
    --output      metrics_c16.json

# Report
python analysis/report.py metrics_c16.json

# Capture environment metadata for the report header
python -c "
import json, subprocess, platform, datetime
env = {
    'date_utc': datetime.datetime.utcnow().isoformat() + 'Z',
    'ec2_instance_type': '<INSTANCE_TYPE>',
    'git_commit_sha': subprocess.check_output(['git','rev-parse','HEAD']).decode().strip(),
    'python_version': platform.python_version(),
    'benchmark_mode': 'warm',
    'workload': 'sleep_short',
}
print(json.dumps(env, indent=2))
" > env.json

python analysis/report.py metrics_c*.json --env env.json --output report.txt
cat report.txt
```

### Environment fields required per BENCHMARK.md

Every report must record the following. Fill these in when running manually:

| Field | Source |
|---|---|
| `date_utc` | auto-captured |
| `git_commit_sha` | `git rev-parse HEAD` |
| `ec2_instance_type` | EC2 metadata or `--instance-type` flag |
| `os_platform` | auto-captured |
| `python_version` | auto-captured |
| `docker_version` | `docker version --format {{.Server.Version}}` |
| `benchmark_mode` | `warm` or `cold` |
| `workload` | `sleep_short`, `sleep_fixed`, or `tiny_fast_exit` |
| `image_pre_pulled` | `true` / `false` |

---

## Setting 3 — Remote EC2 JobRunner + Remote Locust Runner

TBD

---

## Environment Variables Reference

| Variable | Default | Description |
|---|---|---|
| `BENCHMARK_WORKLOAD` | `sleep_short` | Workload name (`sleep_short`, `sleep_fixed`, `tiny_fast_exit`) |
| `BENCHMARK_EVENTS_FILE` | `benchmark_events.jsonl` | Output path for load-generator JSONL events |
| `BENCHMARK_POLL_INTERVAL` | `0.5` | Seconds between `GET /jobs/{id}/status` polls |
| `BENCHMARK_SINGLE_LEVEL` | _(unset)_ | Run only this concurrency level instead of the full ladder |

## Acceptance Thresholds (from BENCHMARK.md)

A concurrency level is **stable** when all of the following hold:

| Metric | Threshold |
|---|---|
| Error rate | < 1% |
| `POST /jobs` latency p95 | < 2 s |
| Completion latency p95 (`sleep-short`) | < 10 s |
| Stuck jobs after cooldown | 0 |
| Backlog | must drain during cooldown |

## Output Files

| File | Contents |
|---|---|
| `<ts>_env.json` | Environment metadata snapshot |
| `<ts>_events.jsonl` | Per-job lifecycle events from the load generator |
| `<ts>_probe.jsonl` | Per-second host samples (DB, Docker, CPU, mem) |
| `<ts>_metrics_cN.json` | Computed metrics for concurrency level N |
| `<ts>_report.txt` | Human-readable summary table and verdict |
