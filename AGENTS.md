# AGENTS.md

This file gives coding agents the minimum repo-specific guidance needed to work safely in this workspace.

## Scope

Most active code lives under `JobRunner/`. Treat the repository root as documentation and project framing; treat `JobRunner/` as the executable service.

## Architecture Snapshot

- `JobRunner/main.py` creates the FastAPI app, mounts `/artifacts`, and registers the jobs router.
- `JobRunner/api/jobs.py` handles upload validation, job submission, listing, detail lookup, status lookup, and artifact enumeration.
- `JobRunner/core/runner.py` creates per-job Docker containers and launches the detached monitor process.
- `JobRunner/core/container_monitor.py` streams stdout/stderr, records Docker lifecycle events, watches `kill_switch.txt`, removes the container, and marks terminal job state.
- `JobRunner/db.py` persists job metadata in SQLite at `JobRunner/data/jobs.sqlite3`.

## Key Runtime Assumptions

- Jobs are uploaded into `JobRunner/jobs/<job_id>/`.
- Every valid job bundle must include `job.py`.
- Each job gets its own host data directory at `JobRunner/data/<job_id>/`, mounted into the container as `/data`.
- The runner currently uses the `python:3-alpine` image and fixed resource limits.
- The monitor process is responsible for the final DB transition to `SUCCEEDED` or `FAILED`.

## Working Rules

- Keep changes local to the behavior being modified. This repo is small enough that broad refactors are rarely justified.
- Preserve the current execution contract unless the task explicitly changes it: API writes files, runner starts the container, monitor closes the loop.
- Do not remove or repurpose lifecycle artifacts such as `container_logs.txt`, `container_lifecycle.txt`, `container_desc.txt`, or `kill_switch.txt` without updating tests and API behavior together.
- Be careful around `JobRunner/data/` and `JobRunner/jobs/`. They may contain user-created or prior test artifacts. Only remove artifacts you created for the current task.
- Avoid changing persisted field names in SQLite responses unless the task requires an API contract change.

## Validation Guidance

Run commands from `JobRunner/` unless the task is root-doc-only.

- For changes in `core/container_monitor.py`, start with `pytest tests/test_monitor_main.py -q`.
- For changes in `core/runner.py`, prefer targeted tests such as `pytest tests/test_build_job_archive.py -q` and then the most relevant Docker-backed integration test.
- For kill-switch behavior, use `pytest tests/test_kill_switch.py -q` or `pytest tests/test_watch_kill_switch.py -q`.
- For lifecycle or terminal-state behavior, use `pytest tests/test_container_lifecycle.py -q` and the closest related failure-mode test.
- For artifact or mount behavior, use `pytest tests/test_volume_and_artifacts.py -q`.
- If you only change Markdown docs, a diff review is enough.

## Environment Notes

- Most of the test suite requires a running Docker daemon.
- A cold machine may need to pull `python:3-alpine` before integration tests can pass.
- The project uses direct imports like `from api import ...` and `from core.runner import ...`, so running commands from the `JobRunner/` directory is the safest default.
- Logging is intentionally simple and configured in `JobRunner/app_logging.py`.

## When Adding Tests

- Follow the existing pattern: concise docstring at module and test level, explicit explanation of why the test matters, and targeted assertions.
- Reuse helpers from `JobRunner/tests/conftest.py` instead of duplicating polling or artifact parsing logic.
- Prefer mocked unit tests for monitor orchestration details and Docker-backed integration tests for behavior that depends on real container lifecycle semantics.

## When Updating Docs

- Keep `README.md` user-facing.
- Keep `AGENTS.md` implementation-facing.
- If behavior changes materially, update both when the change affects both humans and coding agents.