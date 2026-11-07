"""Correctness-gated paired benchmark of a synthetic educational agent runtime."""

from __future__ import annotations

import argparse
import gc
import hashlib
import html
import importlib
import json
import logging
import math
import platform
import subprocess
import sys
import time
import tracemalloc
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

from workload import digest, exercise, resolve_session_class, tools, turn_payload

logger = logging.getLogger(__name__)

ROOT: Path = Path(__file__).resolve().parent
MODULES: tuple[str, str] = ("adk_runtime_legacy", "adk_runtime_optimized")
IMPORT_METRIC_KEYS: tuple[str, str, str] = (
    "import_ms",
    "first_registry_access_ms",
    "import_plus_registry_ms",
)
BENCHMARK_TURNS: tuple[int, int] = (50, 150)
IMPORT_REDUCTION_TARGET_PERCENT: float = 20.0
PREPARE_REDUCTION_TARGET_PERCENT: float = 40.0


class SummaryStats(TypedDict):
    """Descriptive statistics summary for a sample series."""

    samples: list[int | float]
    median: int | float
    min: int | float
    max: int | float
    p10: int | float
    p90: int | float


@dataclass(frozen=True)
class CliArgs:
    """Validated command-line options for referee_adk_bench."""

    repeats: int
    report: Path
    html: Path | None
    enforce_targets: bool


def _validate_positive_int(value: int, field_name: str) -> None:
    """Ensure `value` is a strict positive integer (>= 1), rejecting bool."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")


def _validate_measurement_inputs(turns: int, nested: bool, expected: str) -> None:
    """Validate direct measurement workload arguments."""
    _validate_positive_int(turns, "turns")
    if not isinstance(nested, bool):
        raise ValueError("nested must be a boolean")
    if not isinstance(expected, str) or not expected.strip():
        raise ValueError("expected must be a non-empty digest string")


def _validate_numeric_sample(item: Any) -> int | float:
    """Reject booleans, non-numbers, NaN, and infinities in sample lists."""
    if isinstance(item, bool) or not isinstance(item, (int, float)):
        raise ValueError(f"Sample must be a finite int or float, got {item!r}")
    if not math.isfinite(item):
        raise ValueError(f"Sample must be finite, got {item!r}")
    return item


def summary(values: Iterable[int | float]) -> SummaryStats:
    """Compute min, max, median, p10, and p90 from a single sort pass."""
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise TypeError("values must be a non-string iterable of numbers")
    samples = [_validate_numeric_sample(item) for item in values]
    if not samples:
        raise ValueError("values must contain at least one sample")
    ordered = sorted(samples)
    count = len(ordered)
    mid = count // 2
    median_val: int | float = (
        ordered[mid] if count % 2 == 1 else (ordered[mid - 1] + ordered[mid]) / 2
    )
    return {
        "samples": samples,
        "median": median_val,
        "min": ordered[0],
        "max": ordered[-1],
        "p10": ordered[int((count - 1) * 0.1)],
        "p90": ordered[int((count - 1) * 0.9)],
    }


def cold(module: str) -> list[float]:
    """Measure fresh-process import, first validator access, and combined milliseconds."""
    if not isinstance(module, str) or not module.strip():
        raise ValueError("module must be a non-empty module name")
    script = (
        "import time,importlib,json; "
        "t=time.perf_counter_ns(); "
        f"m=importlib.import_module({module!r}); "
        "a=time.perf_counter_ns(); "
        "r=m.GLOBAL_VALIDATOR_REGISTRY; "
        "b=time.perf_counter_ns(); "
        "assert len(r)==400; "
        "print(json.dumps([(a-t)/1e6,(b-a)/1e6,(b-t)/1e6]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logger.error("Malformed JSON from cold import probe for %s: %r", module, result.stdout)
        raise ValueError(f"Malformed JSON output from cold import probe for {module}") from exc
    if not isinstance(parsed, list) or len(parsed) != 3:
        raise ValueError(f"Expected 3 timing floats from cold probe, got {parsed!r}")
    return [float(_validate_numeric_sample(item)) for item in parsed]


def session_measure(module: Any, turns: int, nested: bool, expected: str) -> float:
    """Measure cumulative `prepare_session_payload` duration in ms with GC restored."""
    _validate_measurement_inputs(turns, nested, expected)
    session_cls = resolve_session_class(module)
    session = session_cls("synthetic-01", tools(module.ToolDefinition))
    was_gc_enabled = gc.isenabled()
    gc.disable()
    total_ns = 0
    result: dict[str, Any] | None = None
    try:
        for turn in range(turns):
            session.append_turn(*turn_payload(turn, nested))
            start_ns = time.perf_counter_ns()
            result = session.prepare_session_payload()
            total_ns += time.perf_counter_ns() - start_ns
    finally:
        if was_gc_enabled:
            gc.enable()
    if result is None or digest(result) != expected:
        logger.error("Timed payload digest mismatch for module %r", module)
        raise AssertionError("Timed output differs from reference")
    return total_ns / 1e6


def peak_memory(module: Any, turns: int, nested: bool, expected: str) -> int:
    """Measure peak traced bytes over a full session exercise with tracemalloc cleanup."""
    _validate_measurement_inputs(turns, nested, expected)
    gc.collect()
    was_tracing = tracemalloc.is_tracing()
    if was_tracing:
        tracemalloc.reset_peak()
    else:
        tracemalloc.start()
    try:
        value = exercise(module, turns, nested)
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        if not was_tracing:
            tracemalloc.stop()
    if digest(value) != expected:
        logger.error("Memory-run payload digest mismatch for module %r", module)
        raise AssertionError("Memory-run output differs")
    return peak_bytes


def _percentage_reduction(baseline: float, candidate_value: float) -> float:
    """Compute percentage reduction safely even when baseline is zero."""
    if baseline == 0.0:
        return 0.0
    return 100.0 * (1.0 - candidate_value / baseline)


def _run_contract_gate() -> str:
    """Execute unittest discovery gate and return combined output log."""
    gate = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )
    combined_log = gate.stdout + gate.stderr
    if gate.returncode != 0:
        logger.error("Contract test gate failed with exit code %d", gate.returncode)
        raise AssertionError(combined_log)
    return combined_log


def _measure_cold_imports(
    repeats: int,
) -> tuple[dict[str, dict[str, SummaryStats]], float]:
    """Warm up and measure alternating fresh-process cold import timings."""
    raw_imports: dict[str, list[list[float]]] = {name: [] for name in MODULES}
    for name in MODULES:
        cold(name)
    for repeat in range(repeats):
        pair_order = MODULES if repeat % 2 == 0 else (MODULES[1], MODULES[0])
        for name in pair_order:
            raw_imports[name].append(cold(name))
    import_report: dict[str, dict[str, SummaryStats]] = {
        name: {
            metric_key: summary([sample[idx] for sample in raw_imports[name]])
            for idx, metric_key in enumerate(IMPORT_METRIC_KEYS)
        }
        for name in MODULES
    }
    reduction = _percentage_reduction(
        float(import_report[MODULES[0]]["import_ms"]["median"]),
        float(import_report[MODULES[1]]["import_ms"]["median"]),
    )
    return import_report, reduction


def _measure_workload_case(
    modules: Sequence[Any], turns: int, nested: bool, repeats: int
) -> dict[str, Any]:
    """Run parity check, warmup, alternating timing pairs, and memory probe for one workload."""
    expected = digest(exercise(modules[0], turns, nested))
    if digest(exercise(modules[1], turns, nested)) != expected:
        logger.error("Pre-timing parity check failed for turns=%d nested=%s", turns, nested)
        raise AssertionError("Pre-timing parity failed")
    for module in modules:
        session_measure(module, turns, nested, expected)
    samples: dict[str, list[float]] = {name: [] for name in MODULES}
    for repeat in range(repeats):
        indices = (0, 1) if repeat % 2 == 0 else (1, 0)
        for index in indices:
            samples[MODULES[index]].append(
                session_measure(modules[index], turns, nested, expected)
            )
    measurements = {name: summary(vals) for name, vals in samples.items()}
    saving = _percentage_reduction(
        float(measurements[MODULES[0]]["median"]),
        float(measurements[MODULES[1]]["median"]),
    )
    peak_bytes = {
        name: peak_memory(module, turns, nested, expected)
        for name, module in zip(MODULES, modules, strict=True)
    }
    return {
        "turns": turns,
        "nested": nested,
        "payload_sha256": expected,
        "prepare_total_ms": measurements,
        "median_reduction_percent": saving,
        "whole_session_peak_bytes": peak_bytes,
    }


def _source_hashes() -> dict[str, str]:
    """Compute SHA-256 digests of core lab Python files."""
    tracked_paths = (
        ROOT / "adk_runtime_legacy.py",
        ROOT / "adk_runtime_optimized.py",
        Path(__file__).resolve(),
        ROOT / "workload.py",
    )
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in tracked_paths
    }


def benchmark(repeats: int) -> dict[str, Any]:
    """Run correctness gate, cold-import probes, and paired session benchmarks."""
    _validate_positive_int(repeats, "repeats")
    unit_test_log = _run_contract_gate()
    modules = [importlib.import_module(name) for name in MODULES]
    import_report, import_reduction = _measure_cold_imports(repeats)
    cases = [
        _measure_workload_case(modules, turns, nested, repeats)
        for turns in BENCHMARK_TURNS
        for nested in (False, True)
    ]
    targets_met = import_reduction >= IMPORT_REDUCTION_TARGET_PERCENT and all(
        case["median_reduction_percent"] >= PREPARE_REDUCTION_TARGET_PERCENT
        for case in cases
    )
    return {
        "schema_version": 1,
        "status": "pass",
        "data": "synthetic",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "timer": "perf_counter_ns",
        },
        "method": {
            "repeats": repeats,
            "order": "alternating pairs",
            "warmups": 1,
            "gc_during_timing": "disabled/restored",
            "timed_region": "sum of prepare_session_payload calls, append/construction excluded",
            "memory": "separate tracemalloc whole-session run, not used for timing",
            "import": (
                "fresh processes, module span excludes interpreter startup; "
                "first registry access shown separately"
            ),
        },
        "unit_test_log": unit_test_log,
        "sources": _source_hashes(),
        "imports": import_report,
        "import_median_reduction_percent": import_reduction,
        "workloads": cases,
        "requested_targets": {
            "import_reduction_percent": int(IMPORT_REDUCTION_TARGET_PERCENT),
            "prepare_reduction_percent": int(PREPARE_REDUCTION_TARGET_PERCENT),
        },
        "performance_targets_met": targets_met,
    }


def _render_workload_rows(workloads: Sequence[Mapping[str, Any]]) -> str:
    """Render HTML table rows for preparation-time benchmark workloads."""
    rows: list[str] = []
    for case in workloads:
        ref_ms, cand_ms = (
            float(case["prepare_total_ms"][name]["median"]) for name in MODULES
        )
        kind_label = "nested" if case["nested"] else "scalar"
        reduction = float(case["median_reduction_percent"])
        rows.append(
            f"<tr><td>{int(case['turns'])} / {kind_label}</td>"
            f"<td>{ref_ms:.3f}</td><td>{cand_ms:.3f}</td>"
            f"<td>{reduction:.1f}%</td></tr>"
        )
    return "".join(rows)


def _render_import_rows(imports: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> str:
    """Render HTML table rows for fresh-process cold import timings."""
    rows: list[str] = []
    for name in MODULES:
        values = imports[name]
        cells = "".join(
            f"<td>{float(values[key]['median']):.3f}</td>" for key in IMPORT_METRIC_KEYS
        )
        rows.append(f"<tr><td>{html.escape(name)}</td>{cells}</tr>")
    return "".join(rows)


_HTML_STYLE = (
    "body{margin:0;background:#f4f3ef;color:#24322c;font:16px/1.6 system-ui,sans-serif}"
    "main{max-width:1040px;margin:auto;padding:56px 24px}"
    "h1{font-size:clamp(32px,5vw,58px);line-height:1.08;letter-spacing:-2px;max-width:760px}"
    "h2{font-size:25px;margin-top:45px}"
    "small{font:12px monospace}"
    "table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums;background:#fff}"
    "th,td{text-align:left;padding:14px;border-bottom:1px solid #d6ded8}"
    "th{background:#e5ebe4}"
    "p{max-width:850px}"
    ".table{overflow:auto}"
    "a{color:inherit}"
    ".badge{display:inline-block;padding:6px 12px;background:#dce8dc;border-radius:4px}"
    "details{margin-top:30px;background:#fff;padding:18px}"
    "pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}"
)


def render(report: Mapping[str, Any]) -> str:
    """Render self-contained HTML report from benchmark JSON payload."""
    workload_rows = _render_workload_rows(report["workloads"])
    import_rows = _render_import_rows(report["imports"])
    escaped_json = html.escape(json.dumps(report, indent=2))
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Agent Runtime Lab | Measured report</title><style>\n{_HTML_STYLE}</style>"
        "<main><small>SYNTHETIC / REPRODUCIBLE / PYTHON</small>"
        "<h1>Faster session preparation.<br>Keep the mutation contract.</h1>"
        '<span class="badge">Correctness gates passed</span>'
        "<p>This measured educational runtime preserves nested input snapshots, fresh output "
        "containers, mutable public events and tool changes. No artificial sleeps. This is not "
        "an upstream Google ADK benchmark.</p>"
        "<h2>Preparation time</h2>"
        "<p>Median total milliseconds spent in prepare calls. Construction and append are "
        "excluded. Alternating paired repeats; raw samples remain in JSON.</p>"
        '<div class="table"><table><tr><th>Turns / payload</th><th>Reference ms</th>'
        f"<th>Candidate ms</th><th>Reduction</th></tr>{workload_rows}</table></div>"
        "<h2>Cold import and deferred work</h2>"
        "<p>Fresh Python processes. A lazy import defers validator compilation rather than "
        "removing its cost. Both the first-use cost and total are shown.</p>"
        '<div class="table"><table><tr><th>Module</th><th>Import ms</th>'
        f"<th>First access ms</th><th>Combined ms</th></tr>{import_rows}</table></div>"
        "<p>Full-history payloads still require O(history length) work. The candidate folds "
        "current public history and copies the final merged state instead of copying every "
        "superseded event delta. Schema digest caching invalidates on nested parameter changes. "
        "Results depend on workload and machine.</p>"
        "<details><summary>Method, source hashes and exact measurements</summary>"
        f"<pre>{escaped_json}</pre></details>"
        '<p><a href="https://github.com/vanovri/agent-runtime-lab">Source and contract tests</a>'
        " · Developed with Codex assistance.</p></main></html>"
    )


def _parse_cli_args(argv: Sequence[str] | None = None) -> CliArgs:
    """Parse and validate CLI arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=9)
    parser.add_argument("--report", type=Path, default=ROOT / "reports" / "local.json")
    parser.add_argument("--html", type=Path)
    parser.add_argument("--enforce-targets", action="store_true")
    parsed = parser.parse_args(argv)
    if not 3 <= parsed.repeats <= 31:
        parser.error("repeats must be 3..31")
    return CliArgs(
        repeats=parsed.repeats,
        report=parsed.report,
        html=parsed.html,
        enforce_targets=bool(parsed.enforce_targets),
    )


def _write_running_placeholders(report_path: Path, html_path: Path | None) -> None:
    """Write initial in-progress status markers before starting benchmark."""
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text('{"status":"running"}\n', encoding="utf-8")
    if html_path is not None:
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(
            "<!doctype html><title>Benchmark running</title>"
            "<p>Benchmark running. No current result yet.</p>",
            encoding="utf-8",
        )


def _write_failure_artifacts(
    report_path: Path, html_path: Path | None, error: BaseException
) -> None:
    """Persist structured JSON and escaped HTML error details when benchmark fails."""
    report_path.write_text(
        json.dumps({"status": "failed", "error": str(error)}, indent=2) + "\n",
        encoding="utf-8",
    )
    if html_path is not None:
        escaped_err = html.escape(str(error))
        html_path.write_text(
            "<!doctype html><title>Benchmark failed</title>"
            f"<h1>Benchmark failed</h1><pre>{escaped_err}</pre>",
            encoding="utf-8",
        )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for running correctness-gated runtime benchmarks."""
    args = _parse_cli_args(argv)
    _write_running_placeholders(args.report, args.html)
    try:
        report = benchmark(args.repeats)
        if args.enforce_targets and not report["performance_targets_met"]:
            logger.warning("Benchmark completed but requested performance targets were not met")
            report["status"] = "targets_not_met"
        args.report.write_text(
            json.dumps(report, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        if args.html is not None:
            args.html.parent.mkdir(parents=True, exist_ok=True)
            args.html.write_text(render(report), encoding="utf-8")
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "import_reduction_percent": report["import_median_reduction_percent"],
                    "prepare_reduction_percent": [
                        case["median_reduction_percent"] for case in report["workloads"]
                    ],
                    "targets_met": report["performance_targets_met"],
                }
            )
        )
        return 0 if report["status"] == "pass" else 1
    except Exception as error:
        logger.error("Benchmark execution failed: %s", error)
        _write_failure_artifacts(args.report, args.html, error)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
