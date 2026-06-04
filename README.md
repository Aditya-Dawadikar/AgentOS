# AgentOS

AgentOS is a lightweight job execution platform for AI agents. It accepts job bundles over HTTP, stores them on disk, runs each job inside an isolated Docker container, and exposes status, logs, and artifacts through a small FastAPI service.

The current repository contains an implementation-focused prototype in `JobRunner/`. The higher-level product intent is described in `PROJECT.md`.

## What It Does

- Accepts multipart job submissions through FastAPI.
- Requires each submitted bundle to include `job.py`.
- Creates one Docker container per job using `python:3-alpine`.
- Mounts a per-job `/data` directory for logs and artifacts.
- Tracks job state in SQLite.
- Records container lifecycle events, stdout/stderr, and terminal exit state.
- Supports external termination through a file-based kill switch.

## Current Architecture

The implemented flow is:

1. `POST /jobs` receives uploaded files.
2. Files are written to `JobRunner/jobs/<job_id>/`.
3. `core.runner.run_job()` creates a container and starts a detached monitor process.
4. `core.container_monitor` streams logs, watches Docker events, handles `kill_switch.txt`, removes the container, and marks completion in SQLite.
5. API endpoints expose job metadata, status, and generated artifacts.

## Repository Layout

```text
.
├── PROJECT.md               Product vision and high-level architecture
├── README.md                Repository overview and local usage
├── AGENTS.md                Working guidance for coding agents
└── JobRunner/
    ├── main.py              FastAPI app entry point
    ├── api/                 Job endpoints and response models
    ├── core/                Container runner and detached monitor
    ├── db.py                SQLite persistence layer
    ├── jobs/                Uploaded job source bundles
    ├── data/                Per-job logs, lifecycle records, and artifacts
    ├── requirements.txt     Python dependencies
    └── tests/               Pytest suite
```

## API Surface

The FastAPI app currently exposes:

- `GET /health`
- `POST /jobs`
- `GET /jobs`
- `GET /jobs/{job_id}`
- `GET /jobs/{job_id}/status`
- `GET /jobs/{job_id}/artifacts`

Static job artifacts are mounted at `/artifacts` and served from `JobRunner/data/`.

## Prerequisites

- Python 3.10+ recommended
- Docker Desktop or another local Docker daemon
- Network access to pull `python:3-alpine` on the first run if the image is not cached

## Local Setup

From the `JobRunner/` directory:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Running The Service

From `JobRunner/`:

```powershell
python main.py
```

The app starts on `http://0.0.0.0:8000`.

You can also run it with Uvicorn directly:

```powershell
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Submitting A Job

Each submission must include a `job.py` file. Example:

```powershell
curl -X POST http://localhost:8000/jobs \
  -F "files=@job.py"
```

Example `job.py`:

```python
from pathlib import Path

Path('/data/output.txt').write_text('hello from AgentOS', encoding='utf-8')
print('job complete', flush=True)
```

After submission:

- container logs are written to `JobRunner/data/<job_id>/container_logs.txt`
- Docker lifecycle events are written to `JobRunner/data/<job_id>/container_lifecycle.txt`
- metadata is written to `JobRunner/data/<job_id>/container_desc.txt`
- artifacts created under `/data` appear in `JobRunner/data/<job_id>/`

## Execution Constraints

The current runner applies fixed limits when creating containers:

- `nano_cpus=500_000_000`
- `mem_limit=128m`
- `memswap_limit=128m`
- `pids_limit=64`
- writable `/data` mount only for job outputs

These are enforced in `JobRunner/core/runner.py`.

## Testing

From `JobRunner/`:

```powershell
pytest
```

Notes:

- Most tests are Docker-backed integration tests.
- A running Docker daemon is required for the full suite.
- The first run may be slower if `python:3-alpine` must be pulled.
- The monitor orchestration tests in `tests/test_monitor_main.py` are fully mocked and can run without Docker.

Useful targeted runs:

```powershell
pytest tests/test_monitor_main.py -q
pytest tests/test_container_lifecycle.py -q
pytest tests/test_kill_switch.py -q
```

## Current Status

This repository is a focused prototype rather than a full production control plane. It already demonstrates the core execution loop:

- upload source files
- launch isolated execution
- collect logs and artifacts
- persist terminal state
- expose results over HTTP

`PROJECT.md` describes the broader intended platform direction beyond the current implementation.