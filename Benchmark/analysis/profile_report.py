"""
Human-readable report generator for JobRunner request-path profiling.

Usage:
  python profile_report.py results/profile_summary.json
  python profile_report.py results/profile_summary.json --output results/profile_report.txt
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def _fmt(value: Any, fmt_str: str = '.2f', suffix: str = '') -> str:
    if value is None:
        return 'N/A'
    try:
        return format(value, fmt_str) + suffix
    except (TypeError, ValueError):
        return str(value)


def _phase_cell(summary: dict[str, Any]) -> str:
    phase = summary.get('dominant_phase_by_mean_ms')
    phase_mean = summary.get('dominant_phase_mean_ms')
    if not phase:
        return 'N/A'
    return f'{phase} ({_fmt(phase_mean, ".2f", "ms")})'


def render_report(summary: dict[str, Any]) -> str:
    if summary.get('error'):
        return json.dumps(summary, indent=2)

    lines: list[str] = []
    width = 88
    overall = summary.get('overall', {})
    overall_request = (overall.get('phase_breakdown_ms') or {}).get('request_total_ms') or {}
    overall_runner = (overall.get('phase_breakdown_ms') or {}).get('runner_total_ms') or {}

    lines.append('=' * width)
    lines.append('AgentOS JobRunner - Request Path Profile Report')
    lines.append('=' * width)
    lines.append(f"Benchmark run id : {summary.get('benchmark_run_id') or 'N/A'}")
    lines.append(f"Profile records  : {summary.get('record_count', 0)}")
    lines.append(f"Accepted/failed  : {overall.get('accepted_count', 0)} / {overall.get('failed_count', 0)}")
    lines.append(f"Request total    : p50={_fmt(overall_request.get('p50'), '.2f', 'ms')}  p95={_fmt(overall_request.get('p95'), '.2f', 'ms')}  p99={_fmt(overall_request.get('p99'), '.2f', 'ms')}")
    lines.append(f"Runner total     : p50={_fmt(overall_runner.get('p50'), '.2f', 'ms')}  p95={_fmt(overall_runner.get('p95'), '.2f', 'ms')}  p99={_fmt(overall_runner.get('p99'), '.2f', 'ms')}")
    lines.append(f"Dominant phase   : {_phase_cell(overall)}")

    share = overall.get('mean_phase_share_of_request_pct') or {}
    if share:
        lines.append('')
        lines.append('Mean Phase Share Of Request')
        lines.append('-' * 40)
        for phase_name, pct in sorted(share.items(), key=lambda item: item[1], reverse=True):
            lines.append(f'  {phase_name:<28} {_fmt(pct, ".2f", "%")}')

    lines.append('')
    lines.append('Per-Concurrency Summary')
    lines.append('-' * width)
    lines.append(f"{'Level':>8}  {'Req p95':>10}  {'Runner p95':>12}  {'Dominant phase':<42}  {'Failures':>8}")
    lines.append('-' * width)
    for level, level_summary in sorted((summary.get('per_concurrency_level') or {}).items(), key=lambda item: int(item[0])):
        measurement = level_summary.get('measurement_phase') or {}
        request_total = (measurement.get('phase_breakdown_ms') or {}).get('request_total_ms') or {}
        runner_total = (measurement.get('phase_breakdown_ms') or {}).get('runner_total_ms') or {}
        lines.append(
            f"{level:>8}  {_fmt(request_total.get('p95'), '.2f', 'ms'):>10}  {_fmt(runner_total.get('p95'), '.2f', 'ms'):>12}  {_phase_cell(measurement):<42}  {measurement.get('failed_count', 0):>8}"
        )

    error_examples = overall.get('error_examples') or []
    if error_examples:
        lines.append('')
        lines.append('Error Examples')
        lines.append('-' * 40)
        for example in error_examples:
            lines.append(
                f"  job_id={example.get('job_id')} status={example.get('status_code')} error={example.get('error_message')}"
            )

    lines.append('')
    lines.append('=' * width)
    return '\n'.join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description='Render a request-path profile report')
    parser.add_argument('summary_file', help='JSON file from compute_profile_metrics.py')
    parser.add_argument('--output', help='Write report text to this file')
    args = parser.parse_args()

    with open(args.summary_file, encoding='utf-8') as handle:
        summary = json.load(handle)

    report = render_report(summary)
    print(report)

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as handle:
            handle.write(report)
        print(f'Profile report written to {args.output}', file=sys.stderr)

    return 0


if __name__ == '__main__':
    raise SystemExit(main())