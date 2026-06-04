"""
Report generator for AgentOS JobRunner benchmark.

Reads one or more JSON metrics files produced by compute_metrics.py
and renders a human-readable summary table plus a final verdict.

Usage:
  # Single run
  python report.py metrics_c16.json

  # Full concurrency ladder (pass all metrics files)
  python report.py results/metrics_c*.json --env results/env.json

  # Write to file
  python report.py results/metrics_c*.json --output report.txt
"""

import argparse
import json
import sys
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt(value, fmt_str: str = ".3f", suffix: str = "") -> str:
    if value is None:
        return "N/A"
    try:
        return format(value, fmt_str) + suffix
    except (TypeError, ValueError):
        return str(value)


def _latency_row(latency_dict: Optional[dict]) -> str:
    if not latency_dict:
        return "N/A"
    p50 = _fmt(latency_dict.get("p50"), ".3f", "s")
    p95 = _fmt(latency_dict.get("p95"), ".3f", "s")
    p99 = _fmt(latency_dict.get("p99"), ".3f", "s")
    return f"p50={p50}  p95={p95}  p99={p99}"


# ---------------------------------------------------------------------------
# Table rendering
# ---------------------------------------------------------------------------

COL_W = [12, 10, 10, 10, 10, 8, 10]
HEADERS = ["Concurrency", "Throughput", "Sub p95", "Sub p99", "Comp p95", "Error%", "Verdict"]


def _row(*cells) -> str:
    parts = []
    for cell, w in zip(cells, COL_W):
        parts.append(str(cell).rjust(w))
    return "  ".join(parts)


def _separator() -> str:
    return "  ".join("-" * w for w in COL_W)


# ---------------------------------------------------------------------------
# Main render
# ---------------------------------------------------------------------------

def render_report(results: List[Dict[str, Any]], env: Optional[dict] = None) -> str:
    lines: List[str] = []
    width = 80

    lines.append("=" * width)
    lines.append("AgentOS JobRunner — Benchmark Report (Part 1: Concurrency)")
    lines.append("=" * width)

    if env:
        lines.append("")
        lines.append("Environment")
        lines.append("-" * 40)
        for k, v in env.items():
            lines.append(f"  {k:<26} {v}")

    lines.append("")
    lines.append("Concurrency Ladder Results")
    lines.append("-" * width)
    lines.append(_row(*HEADERS))
    lines.append(_separator())

    # Track summary stats
    sustainable_concurrency: Optional[int] = None
    first_unstable: Optional[int] = None
    peak_overall = 0
    all_stable = True

    for r in results:
        level = r.get("concurrency_level")
        m = r.get("metrics", {})
        tc = r.get("threshold_check", {})
        stable = tc.get("stable", False)

        if not stable:
            all_stable = False
            if first_unstable is None:
                first_unstable = level
        else:
            sustainable_concurrency = level

        peak = m.get("peak_concurrency", 0) or 0
        if peak > peak_overall:
            peak_overall = peak

        throughput = m.get("throughput_jobs_per_sec")
        sub_p95 = (m.get("submission_latency_s") or {}).get("p95")
        sub_p99 = (m.get("submission_latency_s") or {}).get("p99")
        comp_p95 = (m.get("completion_latency_s") or {}).get("p95")
        error_pct = m.get("error_rate_pct", 0)

        verdict = "STABLE" if stable else "UNSTABLE"

        lines.append(_row(
            level if level is not None else "?",
            _fmt(throughput, ".3f", "/s"),
            _fmt(sub_p95, ".3f", "s"),
            _fmt(sub_p99, ".3f", "s"),
            _fmt(comp_p95, ".3f", "s"),
            _fmt(error_pct, ".2f", "%"),
            verdict,
        ))

        # Print threshold violations indented under the row
        issues = tc.get("issues", [])
        for issue in issues:
            lines.append(f"    ! {issue}")

    lines.append(_separator())

    lines.append("")
    lines.append("Summary")
    lines.append("-" * 40)
    lines.append(f"  Peak observed concurrency      : {peak_overall}")
    lines.append(f"  Highest sustainable concurrency: {sustainable_concurrency if sustainable_concurrency is not None else 'N/A'}")
    lines.append(f"  First unstable level           : {first_unstable if first_unstable is not None else 'None (all stable)' if all_stable else 'N/A'}")

    # Dominant bottleneck hint — based on which threshold was hit first
    if first_unstable is not None:
        bottleneck_candidates = []
        for r in results:
            if r.get("concurrency_level") == first_unstable:
                issues = r.get("threshold_check", {}).get("issues", [])
                for issue in issues:
                    if "p95 latency" in issue and "POST" in issue:
                        bottleneck_candidates.append("submission latency (request-path bottleneck)")
                    elif "completion p95" in issue:
                        bottleneck_candidates.append("completion latency (Docker or scheduling bottleneck)")
                    elif "error_rate" in issue:
                        bottleneck_candidates.append("error rate (capacity or crash)")
                    elif "stuck" in issue:
                        bottleneck_candidates.append("stuck jobs (monitor or container lifecycle issue)")
        if bottleneck_candidates:
            lines.append(f"  Dominant bottleneck hint       : {'; '.join(set(bottleneck_candidates))}")

    # Per-level latency detail
    lines.append("")
    lines.append("Latency Detail")
    lines.append("-" * 40)
    for r in results:
        level = r.get("concurrency_level", "?")
        m = r.get("metrics", {})
        sub = m.get("submission_latency_s")
        comp = m.get("completion_latency_s")
        lines.append(f"  c={level:<5}  submission: {_latency_row(sub)}")
        lines.append(f"         completion: {_latency_row(comp)}")

    # Host probe summary (if available in any result)
    probe_results = [r for r in results if r.get("host_probes")]
    if probe_results:
        lines.append("")
        lines.append("Host Resource Usage (from probe samples)")
        lines.append("-" * 40)
        for r in probe_results:
            level = r.get("concurrency_level", "?")
            hp = r["host_probes"]
            cpu = hp.get("cpu_percent") or {}
            mem = hp.get("memory_percent") or {}
            lines.append(
                f"  c={level:<5}  CPU p95={_fmt(cpu.get('p95'), '.1f', '%')}  max={_fmt(cpu.get('max'), '.1f', '%')}"
                f"  MEM p95={_fmt(mem.get('p95'), '.1f', '%')}  max={_fmt(mem.get('max'), '.1f', '%')}"
                f"  DB peak active={hp.get('db_active_jobs_peak', 'N/A')}"
                f"  Docker peak={hp.get('docker_containers_peak', 'N/A')}"
            )

    lines.append("")
    lines.append("=" * width)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate human-readable benchmark report")
    parser.add_argument("results_files", nargs="+", help="JSON files from compute_metrics.py")
    parser.add_argument("--env", help="JSON environment file (from run_benchmark.py)")
    parser.add_argument("--output", help="Write report text to this file")
    args = parser.parse_args()

    results = []
    for path in args.results_files:
        with open(path) as fh:
            results.append(json.load(fh))

    results.sort(key=lambda r: r.get("concurrency_level") or 0)

    env = None
    if args.env:
        with open(args.env) as fh:
            env = json.load(fh)

    report = render_report(results, env)
    print(report)

    if args.output:
        with open(args.output, "w") as fh:
            fh.write(report)
        print(f"\nReport written to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
