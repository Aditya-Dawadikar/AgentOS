"""
Offline metric computation for JobRunner request-path profiling.

Reads the structured request profile JSONL emitted by JobRunner and computes
overall plus per-concurrency timing summaries for the POST /jobs hot path.

Usage:
  python compute_profile_metrics.py ../JobRunner/data/request_profile_events.jsonl \
      --run-id abc12345 \
      --output results/profile_summary.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PHASE_ORDER = [
    'job_dir_prepare_ms',
    'file_persist_ms',
    'job_bundle_validation_ms',
    'data_dir_prepare_ms',
    'db_mark_starting_ms',
    'archive_build_ms',
    'docker_create_ms',
    'put_archive_ms',
    'monitor_launch_ms',
    'container_start_ms',
    'container_desc_write_ms',
    'db_mark_running_ms',
    'db_mark_failed_ms',
    'runner_total_ms',
    'request_total_ms',
]


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    sorted_values = sorted(values)
    index = (len(sorted_values) - 1) * p / 100.0
    lower = int(index)
    upper = lower + 1
    if upper >= len(sorted_values):
        return sorted_values[lower]
    weight = index - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def mean(values: list[float]) -> float | None:
    return (sum(values) / len(values)) if values else None


def summarize_values(values: list[float]) -> dict[str, float | int | None]:
    return {
        'count': len(values),
        'min': min(values) if values else None,
        'p50': percentile(values, 50),
        'p95': percentile(values, 95),
        'p99': percentile(values, 99),
        'mean': mean(values),
        'max': max(values) if values else None,
    }


def load_profile_records(path: str, run_id: str | None = None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with open(path, encoding='utf-8') as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped.lstrip('\ufeff'))
            except json.JSONDecodeError:
                continue

            if record.get('event') != 'request_profile':
                continue
            if run_id and record.get('benchmark_run_id') != run_id:
                continue
            records.append(record)
    return records


def _ordered_phase_names(records: list[dict[str, Any]]) -> list[str]:
    discovered = set()
    for record in records:
        discovered.update((record.get('phases_ms') or {}).keys())

    ordered = [name for name in PHASE_ORDER if name in discovered]
    ordered.extend(sorted(discovered - set(ordered)))
    return ordered


def summarize_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    phase_names = _ordered_phase_names(records)
    phase_values: dict[str, list[float]] = {name: [] for name in phase_names}
    workloads = sorted({record.get('workload') for record in records if record.get('workload')})
    failures = [record for record in records if not record.get('accepted')]
    accepted_count = sum(1 for record in records if record.get('accepted'))

    for record in records:
        phases = record.get('phases_ms') or {}
        for phase_name in phase_names:
            value = phases.get(phase_name)
            if isinstance(value, (int, float)):
                phase_values[phase_name].append(float(value))

    phase_breakdown = {
        phase_name: summarize_values(values)
        for phase_name, values in phase_values.items()
    }

    request_total_mean = (phase_breakdown.get('request_total_ms') or {}).get('mean')
    internal_phase_names = [
        phase_name for phase_name in phase_names
        if phase_name not in {'request_total_ms', 'runner_total_ms'}
    ]
    dominant_phase = None
    dominant_mean = None
    for phase_name in internal_phase_names:
        candidate_mean = (phase_breakdown.get(phase_name) or {}).get('mean')
        if candidate_mean is None:
            continue
        if dominant_mean is None or candidate_mean > dominant_mean:
            dominant_phase = phase_name
            dominant_mean = candidate_mean

    mean_share = {}
    if request_total_mean:
        for phase_name in internal_phase_names:
            candidate_mean = (phase_breakdown.get(phase_name) or {}).get('mean')
            if candidate_mean is not None:
                mean_share[phase_name] = round((candidate_mean / request_total_mean) * 100.0, 2)

    return {
        'record_count': len(records),
        'accepted_count': accepted_count,
        'failed_count': len(failures),
        'workloads': workloads,
        'phase_breakdown_ms': phase_breakdown,
        'dominant_phase_by_mean_ms': dominant_phase,
        'dominant_phase_mean_ms': dominant_mean,
        'mean_phase_share_of_request_pct': mean_share,
        'error_examples': [
            {
                'job_id': record.get('job_id'),
                'status_code': record.get('status_code'),
                'error_message': record.get('error_message'),
            }
            for record in failures[:5]
        ],
    }


def build_summary(records: list[dict[str, Any]], source_file: str, run_id: str | None = None) -> dict[str, Any]:
    levels = sorted({record.get('concurrency_level') for record in records if record.get('concurrency_level') is not None})
    per_level = {}

    for level in levels:
        level_records = [record for record in records if record.get('concurrency_level') == level]
        measurement_records = [record for record in level_records if record.get('phase') == 'measurement']
        per_level[str(level)] = {
            'all_phases': summarize_records(level_records),
            'measurement_phase': summarize_records(measurement_records),
        }

    recorded_at_values = [record.get('recorded_at') for record in records if isinstance(record.get('recorded_at'), (int, float))]
    return {
        'benchmark_run_id': run_id,
        'source_file': source_file,
        'record_count': len(records),
        'time_window': {
            'first_recorded_at': min(recorded_at_values) if recorded_at_values else None,
            'last_recorded_at': max(recorded_at_values) if recorded_at_values else None,
        },
        'overall': summarize_records(records),
        'per_concurrency_level': per_level,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='Compute offline request-path profile metrics')
    parser.add_argument('profile_events_file', help='Path to request_profile_events.jsonl')
    parser.add_argument('--run-id', help='Filter to a specific BENCHMARK_RUN_ID')
    parser.add_argument('--output', help='Write summary JSON to this file')
    args = parser.parse_args()

    records = load_profile_records(args.profile_events_file, run_id=args.run_id)
    if not records:
        result = {
            'error': 'no matching request profile records',
            'benchmark_run_id': args.run_id,
            'source_file': args.profile_events_file,
            'record_count': 0,
        }
        if args.output:
            with open(args.output, 'w', encoding='utf-8') as handle:
                json.dump(result, handle, indent=2)
        print(json.dumps(result, indent=2))
        return 1

    summary = build_summary(records, args.profile_events_file, run_id=args.run_id)
    print(json.dumps(summary, indent=2))

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open('w', encoding='utf-8') as handle:
            json.dump(summary, handle, indent=2)
        print(f'Profile summary written to {args.output}', file=sys.stderr)

    return 0


if __name__ == '__main__':
    raise SystemExit(main())