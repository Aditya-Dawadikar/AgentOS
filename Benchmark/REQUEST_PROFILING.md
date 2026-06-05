# Request Profiling

This document explains how to run and interpret the request-path profiling flow for AgentOS JobRunner.

The profiling flow measures where time is spent inside `POST /jobs` for each submitted job. It is meant to answer questions like:

- Is the bottleneck in request file handling or validation?
- Is the bottleneck in archive creation?
- Is Docker container creation or startup dominating request time?
- Which phase grows first as concurrency increases?

## What Gets Captured

Each `POST /jobs` submission appends one JSONL record to:

`../JobRunner/data/request_profile_events.jsonl`

Each record contains request metadata plus phase timings, including:

- `job_dir_prepare_ms`
- `file_persist_ms`
- `job_bundle_validation_ms`
- `data_dir_prepare_ms`
- `db_mark_starting_ms`
- `archive_build_ms`
- `docker_create_ms`
- `put_archive_ms`
- `monitor_launch_ms`
- `container_start_ms`
- `container_desc_write_ms`
- `db_mark_running_ms`
- `db_mark_failed_ms` when relevant
- `runner_total_ms`
- `request_total_ms`

When a benchmark run is launched through the benchmark harness, Locust attaches a benchmark run id and stage metadata to each request so the profiling records can be filtered back to that exact run.

## Generated Artifacts

For orchestrated benchmark runs, the profiling flow produces these files under:

`results/<timestamp>/`

- `profile_summary.json`: machine-readable request-path timing summary
- `profile_report.txt`: human-readable timing report

The standard benchmark summary files are still produced alongside them:

- `env.json`
- `summary.json`
- `probe.jsonl`

## Recommended Workflow

Use this order:

1. Run a single low-concurrency or single-level benchmark to establish a clean baseline.
2. Inspect `profile_report.txt` to identify the dominant phase.
3. Run the stepped concurrency ladder.
4. Compare `per_concurrency_level` in `profile_summary.json` to see which phase expands first.

Do not start with stress alone if the goal is diagnosis. Baseline first gives you the uncontended cost of one request; stress later tells you which phase scales poorly.

## Option 1: Run With The Orchestrator

This is the default and recommended path because it runs Locust, captures the host probe, and generates the profiling summary and report automatically.

### Windows PowerShell

```powershell
Set-Location "d:\AgentOS\Benchmark"
.\venv\Scripts\python.exe run_benchmark.py `
    --host http://localhost:8000 `
    --db ..\JobRunner\data\jobs.sqlite3 `
    --workload sleep_short
```

### Windows `cmd`

```cmd
cd /d d:\AgentOS\Benchmark
venv\Scripts\python.exe run_benchmark.py ^
  --host http://localhost:8000 ^
  --db ..\JobRunner\data\jobs.sqlite3 ^
  --workload sleep_short
```

### Single-Level Baseline Run

Use this first when you want a low-noise baseline.

```powershell
Set-Location "d:\AgentOS\Benchmark"
.\venv\Scripts\python.exe run_benchmark.py `
    --host http://localhost:8000 `
    --db ..\JobRunner\data\jobs.sqlite3 `
    --workload sleep_fixed `
    --single-level 1
```

### Full Ladder Run

Use this after the baseline to study how the dominant phase changes with concurrency.

```powershell
Set-Location "d:\AgentOS\Benchmark"
.\venv\Scripts\python.exe run_benchmark.py `
    --host http://localhost:8000 `
    --db ..\JobRunner\data\jobs.sqlite3 `
    --workload sleep_short
```

### Custom Profile Event File

By default the orchestrator reads profiling records from:

`../JobRunner/data/request_profile_events.jsonl`

If your service writes that file elsewhere, override it:

```powershell
Set-Location "d:\AgentOS\Benchmark"
.\venv\Scripts\python.exe run_benchmark.py `
    --host http://localhost:8000 `
    --db ..\JobRunner\data\jobs.sqlite3 `
    --profile-events-file C:\temp\request_profile_events.jsonl
```

## Option 2: Manual Locust Run + Offline Profiling Summary

Use this if you want to drive Locust yourself and generate the profiling report later.

### Step 1: Run Locust With A Benchmark Run Id

The `BENCHMARK_RUN_ID` is how the offline summary script filters the JSONL profile records for one specific run.

### Windows PowerShell

```powershell
Set-Location "d:\AgentOS\Benchmark"
$env:BENCHMARK_RUN_ID = "profile-baseline-001"
$env:BENCHMARK_WORKLOAD = "sleep_fixed"
$env:BENCHMARK_SUMMARY_FILE = "summary.json"
$env:BENCHMARK_SINGLE_LEVEL = "1"
.\venv\Scripts\python.exe -m locust `
    -f load_generator\locustfile.py `
    --host http://localhost:8000 `
    --headless
```

### Windows `cmd`

```cmd
cd /d d:\AgentOS\Benchmark
set BENCHMARK_RUN_ID=profile-baseline-001
set BENCHMARK_WORKLOAD=sleep_fixed
set BENCHMARK_SUMMARY_FILE=summary.json
set BENCHMARK_SINGLE_LEVEL=1
venv\Scripts\python.exe -m locust ^
  -f load_generator\locustfile.py ^
  --host http://localhost:8000 ^
  --headless
```

### Step 2: Build The Machine-Readable Profile Summary

```powershell
Set-Location "d:\AgentOS\Benchmark"
.\venv\Scripts\python.exe analysis\compute_profile_metrics.py `
    ..\JobRunner\data\request_profile_events.jsonl `
    --run-id profile-baseline-001 `
    --output results\manual-profile-summary.json
```

### Step 3: Render The Human-Readable Report

```powershell
Set-Location "d:\AgentOS\Benchmark"
.\venv\Scripts\python.exe analysis\profile_report.py `
    results\manual-profile-summary.json `
    --output results\manual-profile-report.txt
```

## How To Read The Results

### `profile_report.txt`

This file is the fastest way to inspect a run. It shows:

- request total p50, p95, p99
- runner total p50, p95, p99
- dominant phase by mean time
- mean share of total request time by phase
- per-concurrency summaries for measurement windows
- example failures when requests were rejected or errored

### `profile_summary.json`

Use this file when you want to compare runs programmatically or inspect the full breakdown. The key sections are:

- `overall.phase_breakdown_ms`
- `overall.dominant_phase_by_mean_ms`
- `overall.mean_phase_share_of_request_pct`
- `per_concurrency_level.<level>.measurement_phase.phase_breakdown_ms`

## Interpreting Bottlenecks

Typical interpretations:

- `file_persist_ms` grows first: request upload or disk write pressure
- `archive_build_ms` grows first: Python-side packaging overhead
- `docker_create_ms` grows first: Docker daemon/container creation bottleneck
- `put_archive_ms` grows first: archive transfer into the container is expensive
- `container_start_ms` grows first: image/container startup overhead or daemon scheduling pressure

If only higher concurrency levels degrade while the baseline stays clean, that points to a scaling bottleneck rather than a fundamental single-request cost.

## Operational Notes

- The profiling JSONL file is append-only. Old runs remain in the same file until removed.
- The offline scripts rely on `BENCHMARK_RUN_ID` to isolate one run from the accumulated JSONL stream.
- If you forget to set `BENCHMARK_RUN_ID` during a manual Locust run, the offline summary cannot reliably isolate that run.
- If `profile_summary.json` is missing after an orchestrated run, check whether the configured `--profile-events-file` exists and whether the service was restarted with the profiling code deployed.

## Minimal Baseline Then Stress Sequence

If the goal is diagnosis rather than throughput reporting, use this sequence:

1. Single level `1` with `sleep_fixed`
2. Single level `4` with `sleep_fixed`
3. Full ladder with `sleep_short`

That gives you:

- uncontended request cost
- early scaling signal
- full stress behavior