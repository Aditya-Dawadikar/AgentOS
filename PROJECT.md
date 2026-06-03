# AgentOS

A lightweight execution platform that allows AI Agents to safely submit, execute, monitor, and manage computational jobs inside isolated Docker containers.

AgentOS acts as the operating system layer between autonomous agents and compute infrastructure. Agents interact through MCP tools, while AgentOS handles scheduling, execution, monitoring, logging, artifact management, and lifecycle control.

---

# Vision

Modern AI agents can reason, plan, and generate code.

However, most agents still lack a reliable execution environment.

AgentOS provides:

* Job submission APIs
* Queue-based execution
* Docker container isolation
* Resource restrictions
* Structured logging
* Artifact management
* Monitoring
* MCP integration

The goal is to allow any agent to execute code safely without needing direct infrastructure access.

---

# High Level Architecture

```text
                         ┌────────────────────┐
                         │      AI Agent      │
                         └─────────┬──────────┘
                                   │
                                   │ MCP Tool Call
                                   ▼
                         ┌────────────────────┐
                         │ AgentOS MCP Layer  │
                         └─────────┬──────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────┐
│                 FastAPI Job Server                       │
└───────────────┬───────────────────────────┬──────────────┘
                │                           │
                ▼                           ▼
        ┌──────────────┐            ┌────────────────┐
        │ Postgres     │            │ Redis Queue    │
        │ jobs table   │            │ job_queue      │
        └──────────────┘            └───────┬────────┘
                                            │
                                            ▼
                                   ┌────────────────┐
                                   │ Worker Pool    │
                                   └───────┬────────┘
                                           │
                                           ▼
                              ┌────────────────────────┐
                              │ Docker Container / Job │
                              └───────────┬────────────┘
                                          │
                                          ▼
                              ┌────────────────────────┐
                              │ Local Logger Service   │
                              └───────────┬────────────┘
                                          │
                                          ▼
                              ┌────────────────────────┐
                              │ Redis Queue            │
                              │ job_event_queue        │
                              └───────────┬────────────┘
                                          │
                                          ▼
                              ┌────────────────────────┐
                              │ Event Writer Worker    │
                              └───────────┬────────────┘
                                          │
                                          ▼
                              ┌────────────────────────┐
                              │ Postgres job_logs      │
                              └────────────────────────┘
```

---

# Core Components

## Job Server

Responsible for:

* Job creation
* Job updates
* Job deletion
* Job retrieval
* Queue submission

Technology:

* FastAPI
* Postgres
* Redis

Endpoints:

```text
POST   /jobs
GET    /jobs
GET    /jobs/{job_id}
DELETE /jobs/{job_id}
GET    /jobs/{job_id}/status
```

---

## Redis Job Queue

Purpose:

* Decouple API layer from execution layer
* Support worker scaling
* Buffer bursts of requests

Queue Name:

```text
job_queue
```

Job Lifecycle:

```text
CREATED
QUEUED
RUNNING
SUCCEEDED
FAILED
TIMEOUT
CANCELLED
```

---

## Worker Pool

Responsible for:

* Reading jobs from queue
* Creating containers
* Monitoring execution
* Collecting logs
* Uploading artifacts
* Updating status

Worker instances can be scaled independently.

---

## Docker Execution Environment

Every submitted job executes inside its own container.

Example restrictions:

```text
CPU Limit
Memory Limit
Execution Timeout
Read Only Filesystem
PID Limits
Network Restrictions
```

Example:

```bash
docker run \
  --memory=512m \
  --cpus=1 \
  --read-only \
  --rm
```

---

## Local Logger Service

Runs outside containers.

Purpose:

* Receive execution events
* Receive logs
* Receive metrics
* Forward events to queue

The execution environment never receives direct database credentials.

---

## Event Queue

Queue Name:

```text
job_event_queue
```

Purpose:

* Buffer logs
* Buffer execution events
* Decouple logging from execution

Example Events:

```json
{
  "job_id": "123",
  "event_type": "INFO",
  "message": "Processing file",
  "timestamp": "..."
}
```

---

## Event Writer

Responsible for:

* Reading from job_event_queue
* Batch persistence
* Retry handling

Writes events into:

```text
job_logs
```

table.

---

# Database Design

## jobs

```text
id
agent_id
status
image
command
created_at
started_at
completed_at
cpu_limit
memory_limit
timeout_seconds
artifact_location
```

---

## job_logs

Partitioned by time.

```text
id
job_id
event_type
message
metadata
timestamp
```

Possible event types:

```text
INFO
DEBUG
WARNING
ERROR
METRIC
SYSTEM
```

---

# Artifact Storage

Generated files are uploaded after job completion.

Examples:

```text
csv
json
txt
html
pdf
png
```

Future:

```text
S3
MinIO
```

Example structure:

```text
artifacts/

└── job_123
    ├── output.csv
    ├── report.pdf
    └── logs.txt
```

---

# MCP Integration

AgentOS exposes MCP tools.

Examples:

```text
submit_job
get_job_status
get_job_logs
list_jobs
cancel_job
```

This allows:

```text
Claude
Cursor
OpenAI Agents
LangGraph Agents
Custom MCP Clients
```

to execute workloads through AgentOS.

---

# Development Roadmap

## Stage 1

### Job Server

Goal:

Build and deploy locally.

Deliverables:

* FastAPI server
* Job CRUD APIs
* Postgres jobs table
* Redis job queue
* Queue submission

Validation:

```text
Create job
List jobs
Delete jobs
Push jobs to queue
```

---

## Stage 2

### Worker Pool

Goal:

Build execution layer.

Deliverables:

* Queue consumers
* Docker execution
* Status updates
* Resource limits
* Timeout handling

Testing:

* Locust load testing
* Multiple concurrent jobs

Validation:

```text
Job executes successfully
Job failures handled
Job timeout handled
```

---

## Stage 3

### Logging Pipeline

Goal:

Full event flow.

Deliverables:

* Local Logger Service
* job_event_queue
* Event Writer
* job_logs persistence

Validation:

```text
Logs visible in database
Execution events recorded
Container failures captured
```

---

## Stage 4

### Dashboard

Goal:

Visualize system state.

Features:

* Job list
* Job details
* Job logs
* Execution history
* Search and filtering

Validation:

```text
Real-time status visibility
```

---

## Stage 5

### MCP Integration

Goal:

Allow external agents to execute jobs.

Deliverables:

* MCP Server
* MCP Tool Definitions
* Authentication Layer

Validation:

```text
Agent submits job
Agent receives result
Agent reads logs
```

---

## Stage 6

### Production Deployment

Goal:

Cloud deployment.

Infrastructure:

```text
Railway
EC2
Docker
Postgres
Redis
```

Future Enhancements:

```text
Kubernetes
Autoscaling Workers
S3 Artifacts
OpenTelemetry
Grafana
Prometheus
Loki
```

Validation:

```text
Production workload execution
Persistent storage
Horizontal scaling
```

---

# Future Ideas

## Resource Scheduling

```text
Priority Queues
Fair Scheduling
Rate Limiting
Per-Agent Quotas
```

## Observability

```text
Prometheus
Grafana
OpenTelemetry
Distributed Tracing
```

## Security

```text
Image Allow Lists
Sandboxing
Network Isolation
Secrets Management
```

## Execution

```text
Python
NodeJS
Go
Rust
Custom Images
```

---

# Long Term Goal

AgentOS should become the execution runtime for autonomous agents.

Agents focus on reasoning.

AgentOS handles:

* Compute
* Isolation
* Monitoring
* Logging
* Artifacts
* Scheduling
* Execution Lifecycle

allowing agents to safely execute work at scale.
