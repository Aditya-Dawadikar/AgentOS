# Benchmark Criteria

This document defines the fixed benchmark contract for AgentOS JobRunner.

The benchmark criteria in this file are intended to remain stable across implementation changes. Future code changes may improve or regress the measured results, but they must not silently change the benchmark goals, workload definitions, measurement rules, or acceptance criteria documented here.

## Purpose

The benchmark program has two phases.

### Part 1: Concurrency Benchmark

Measure how many jobs the current JobRunner can sustain concurrently while maintaining acceptable latency and failure behavior.

This phase answers:

- How many jobs can be active at the same time?
- What submission latency does the system impose under load?
- What throughput can the system sustain?
- At what point do failures, backlog growth, or tail latencies become unacceptable?

### Part 2: Execution Profiling

Measure the latency of each major step in the job lifecycle so bottlenecks can be identified precisely.

This phase answers:

- Where time is spent inside the request and execution path
- Whether the dominant cost is API work, Docker orchestration, monitoring, or persistence
- Which steps improve or regress after internal code changes

## Scope

These criteria apply to the current execution path implemented in the repository:

- request submission through `POST /jobs`
- file persistence into `JobRunner/jobs/<job_id>/`
- Docker container creation and startup in `core.runner.run_job()`
- detached monitoring and terminal state updates in `core.container_monitor`
- state persistence in SQLite

If the internal architecture changes later, including introduction of a queue, asynchronous workers, or a different persistence layer, the benchmark criteria remain the same unless this document is intentionally versioned.

## Stability Rule

The following items are benchmark invariants and must not be changed casually:

- benchmark workloads
- benchmark environment rules
- load generation pattern
- concurrency definitions
- reported metrics
- success and failure thresholds
- steady-state interpretation rules

If a benchmark invariant must change, the change must be explicit and versioned in this file. Old and new benchmark results must not be compared as if they were equivalent.

## Benchmark Phases

### Phase A: Warm Benchmark

This is the default comparison benchmark.

Conditions:

- service is already running
- Docker daemon is healthy
- `python:3-alpine` is already present locally
- host is otherwise idle
- benchmark starts only after warmup completes

Warm benchmark results are the primary basis for comparing code changes over time.

### Phase B: Cold Benchmark

This measures startup sensitivity and cold-path overhead.

Conditions:

- service may be freshly started
- caches may be cold
- image pulls or initialization overhead may still apply

Cold results are useful for operational understanding, but warm results are the primary regression baseline.

## Fixed Workloads

### Primary Workload: `sleep-short`

This is the canonical concurrency benchmark workload.

Behavior:

- single-file job bundle
- `job.py` sleeps for a random duration between 2 and 5 seconds
- prints a completion line to stdout
- exits with status code 0

Purpose:

- keep user workload simple
- isolate orchestration overhead from heavy user computation
- allow overlapping jobs so active concurrency can be observed clearly

Reference job:

```python
import random
import time

time.sleep(random.uniform(2, 5))
print('done', flush=True)
```

### Secondary Workload: `sleep-fixed`

Behavior:

- single-file job bundle
- `job.py` sleeps for exactly 3 seconds
- prints a completion line to stdout
- exits with status code 0

Purpose:

- reduce runtime variance
- support clean step-to-step comparisons

Reference job:

```python
import time

time.sleep(3)
print('done', flush=True)
```

### Secondary Workload: `tiny-fast-exit`

Behavior:

- single-file job bundle
- prints a line and exits immediately

Purpose:

- expose pure orchestration overhead
- show the lower bound of request-to-completion overhead when job runtime is negligible

Reference job:

```python
print('done', flush=True)
```

## Environment Rules

Each benchmark report must capture the environment used for the run.

Required environment fields:

- date and time of run
- git commit SHA
- EC2 instance type
- operating system and kernel
- Python version
- Docker version
- AgentOS image used by the runner
- benchmark mode: warm or cold
- whether Docker image was pre-pulled

Comparison rules:

- Compare benchmark runs only when the environment is materially equivalent.
- If the instance type changes, treat the results as a new hardware baseline.
- If the Docker version changes, note it explicitly in the report.
- Keep the host idle except for benchmark-related processes.

## Load Generation Rules

The benchmark load generator may be Locust or an equivalent tool, but the request pattern must remain stable.

### Submission Behavior

Each virtual user or equivalent load unit must:

1. submit a valid job bundle to `POST /jobs`
2. record the returned `job_id`
3. poll `GET /jobs/{job_id}/status` until a terminal state is observed
4. record completion outcome and timing
5. repeat for the duration of the test stage

### Concurrency Levels

The default concurrency ladder is:

- 1
- 2
- 4
- 8
- 16
- 32
- 64
- 128

Additional higher levels may be tested, but the default ladder must always be included so results stay comparable over time.

### Stage Timing

Default timing per concurrency level:

- warmup: 1 minute
- measurement window: 5 minutes
- cooldown: 2 minutes

Rules:

- Do not include warmup data in the reported steady-state metrics.
- Do not start the next level until the previous level has cooled down and no backlog remains.
- If backlog does not drain during cooldown, mark the previous level as unstable.

## Lifecycle Timestamps

The benchmark should capture the following timestamps per job when possible:

- `submit_start`
- `accepted_at`
- `running_at`
- `completed_at`

Definitions:

- `submit_start`: time the client starts the `POST /jobs` request
- `accepted_at`: time the API responds to the submission request
- `running_at`: first observed time the job enters `STARTING` or `RUNNING`
- `completed_at`: first observed time the job enters `SUCCEEDED` or `FAILED`

These timestamps support both concurrency analysis and end-to-end latency analysis.

## Metrics

### Part 1: Concurrency Metrics

The following metrics must always be reported:

- `POST /jobs` latency p50
- `POST /jobs` latency p95
- `POST /jobs` latency p99
- completion latency p50
- completion latency p95
- completion latency p99
- throughput in completed jobs per second
- active jobs over time
- peak observed concurrency
- average concurrency
- sustainable concurrency
- error rate
- count of stuck jobs after cooldown

### Part 2: Execution Step Metrics

The following step timings must be collected when profiling instrumentation is available:

- request receive to upload complete
- upload complete to `run_job()` entry
- job archive build duration
- Docker container create duration
- `put_archive` duration
- monitor process spawn duration
- container start duration
- time to DB transition to `RUNNING`
- time spent in `RUNNING`
- monitor detection delay after process exit
- terminal DB update duration

If an implementation changes the internal steps, new step timings may be added. Existing step names should be preserved where the meaning remains the same.

## Concurrency Definitions

### Active Job Count Over Time

Let $C(t)$ be the number of active jobs at time $t$.

$$
C(t) = \left|\{j \mid running\_at_j \le t < completed\_at_j\}\right|
$$

In the current implementation, jobs in `STARTING` or `RUNNING` should be considered active for operational sampling.

### Peak Concurrency

Peak observed concurrency is defined as:

$$
C_{peak} = \max_t C(t)
$$

This is the maximum number of simultaneously active jobs observed during the measurement window.

### Average Concurrency

Average concurrency may be estimated by Little's Law:

$$
L = \lambda W
$$

Where:

- $L$ is average concurrent active jobs
- $\lambda$ is throughput in jobs per second
- $W$ is average completion time in seconds

This estimate is useful for summary reporting, but the active job time series remains the source of truth for peak concurrency.

### Sustainable Concurrency

Sustainable concurrency is the highest tested concurrency level that satisfies all benchmark thresholds during the steady-state measurement window.

Do not define sustainability by the largest observed spike. It must satisfy the criteria in the thresholds section below.

## Sampling Rules

The benchmark should collect both client-side and server-side signals.

### Client-Side Signals

- request latency
- completion latency
- terminal status
- per-request success or failure

### Server-Side Signals

- count of jobs in `STARTING` or `RUNNING`
- count of live job containers
- CPU utilization
- memory utilization
- Docker daemon health indicators when available

### Sampling Frequency

Default server-side sample interval:

- once per second

If a different interval is used, it must be recorded in the report.

### Source of Truth Preference

Preferred sources, in order:

1. job table state in SQLite
2. live Docker container count
3. external logs or indirect estimates

If DB active count and Docker active count diverge materially, note the mismatch as a benchmark finding.

## Acceptance Thresholds

The benchmark thresholds below define whether a concurrency level is stable.

Default thresholds:

- error rate must remain below 1 percent
- no jobs may remain stuck in `STARTING` or `RUNNING` after cooldown
- `POST /jobs` latency p95 must remain below 2 seconds
- completion latency p95 for `sleep-short` must remain below 10 seconds
- backlog must not grow monotonically throughout the steady-state window

Interpretation rules:

- If error rate exceeds threshold, the level is unstable.
- If p95 request latency exceeds threshold, the level is unstable.
- If p95 completion latency exceeds threshold, the level is unstable.
- If backlog fails to drain during cooldown, the level is unstable.
- If stuck jobs remain after cooldown, the level is unstable.

These thresholds may be tightened later, but they should not be loosened without explicit justification.

## Reporting Rules

Every benchmark report must include:

- benchmark date
- git commit SHA
- benchmark phase: Part 1 or Part 2
- benchmark mode: warm or cold
- workload name
- EC2 instance type
- concurrency level
- throughput
- latency percentiles
- peak observed concurrency
- average concurrency
- sustainable concurrency conclusion
- error counts and error rate
- stuck job count
- notable operational observations

The final summary for a run should explicitly state:

- highest observed peak concurrency
- highest sustainable concurrency
- first concurrency level that became unstable
- dominant bottleneck if known

## Interpretation Guidance

### What Part 1 Proves

Part 1 measures control-plane concurrency under a simple workload. It tells us how well the system handles:

- upload and file persistence
- request-path orchestration overhead
- Docker create and start throughput
- monitor process fan-out
- state persistence under load

It does not establish capacity for CPU-intensive user workloads.

### What Part 2 Proves

Part 2 identifies which step dominates latency or degrades first under load. It should explain changes observed in Part 1 rather than replace Part 1.

### Sleep Workload Caveat

The sleep workloads are intentionally light. They are good for isolating orchestration and scheduling limits, but they will overestimate capacity compared to heavy real-world compute jobs.

## Change Management

Future implementation changes may alter the internal architecture, including:

- request handling model
- queueing model
- worker topology
- persistence layer
- monitoring strategy

When those changes occur:

- this benchmark document remains the reference contract
- benchmark workloads remain unchanged
- comparison methodology remains unchanged
- only measured results are expected to change

If the benchmark contract itself needs to change, that change must be explicit, reviewed, and recorded in this file.

## Current Default Benchmark Plan

### Part 1: Concurrency First

Run the `sleep-short` workload across the default concurrency ladder and determine:

- peak observed concurrency
- highest sustainable concurrency
- request and completion latency percentiles
- error rate and backlog behavior

### Part 2: Detailed Timing

Instrument the execution path and capture per-step timings for the same benchmark workloads so bottlenecks can be localized.

Part 2 should extend observability without changing the Part 1 benchmark contract.