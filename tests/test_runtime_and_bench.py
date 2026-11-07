"""Comprehensive offline unit and characterization tests for agent-runtime-lab."""

from __future__ import annotations

import gc
import importlib.util
import io
import json
import statistics
import subprocess
import tempfile
import tracemalloc
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

import adk_runtime_legacy as legacy
import adk_runtime_optimized as optimized
import referee_adk_bench as bench
from workload import digest, exercise, resolve_session_class, tools, turn_payload


@dataclass(eq=True)
class _CustomPayloadBox:
    """Custom container exercising the copy.deepcopy fallback in _clone."""

    label: str
    items: list[int]


class RuntimeIsolationAndCloneTests(unittest.TestCase):
    """Verify deep mutation isolation, container cloning, type contracts, and runtime parity."""

    def test_clone_atomic_values_returned_directly(self) -> None:
        atomics: tuple[Any, ...] = (None, True, False, 0, 42, -7, 3.14, "text", b"bytes")
        for item in atomics:
            with self.subTest(item=item):
                cloned = optimized._clone(item)
                self.assertEqual(cloned, item)
                self.assertIs(type(cloned), type(item))

    def test_clone_preserves_dict_and_list_cycles_without_aliasing_input(self) -> None:
        cyclic_dict: dict[str, Any] = {"name": "root", "children": []}
        cyclic_dict["self"] = cyclic_dict
        cyclic_dict["children"].append(cyclic_dict)

        cloned_dict = optimized._clone(cyclic_dict)
        self.assertIsNot(cloned_dict, cyclic_dict)
        self.assertIs(cloned_dict["self"], cloned_dict)
        self.assertIs(cloned_dict["children"][0], cloned_dict)

        cloned_dict["children"].append("mutated")
        self.assertEqual(len(cyclic_dict["children"]), 1)

        cyclic_list: list[Any] = ["head"]
        cyclic_list.append(cyclic_list)
        cloned_list = optimized._clone(cyclic_list)
        self.assertIsNot(cloned_list, cyclic_list)
        self.assertIs(cloned_list[1], cloned_list)

    def test_clone_falls_back_to_deepcopy_for_tuples_sets_and_custom_objects(self) -> None:
        box = _CustomPayloadBox(label="box", items=[1, 2, 3])
        payload: dict[Any, Any] = {
            ("tuple", "key"): ({1, 2}, frozenset({"x"}), box),
        }
        cloned = optimized._clone(payload)
        self.assertEqual(cloned, payload)
        cloned_set, cloned_frozenset, cloned_box = cloned[("tuple", "key")]
        self.assertIsInstance(cloned_set, set)
        self.assertIsInstance(cloned_frozenset, frozenset)
        self.assertIsNot(cloned_box, box)
        cloned_box.items.append(4)
        self.assertEqual(box.items, [1, 2, 3])

    def test_empty_session_and_deep_nested_mutation_isolation(self) -> None:
        legacy_session = legacy.LegacyAgentSession("empty-01", [])
        opt_session = optimized.OptimizedAgentSession("empty-01", [])
        self.assertEqual(
            legacy_session.prepare_session_payload(),
            opt_session.prepare_session_payload(),
        )
        self.assertEqual(
            opt_session.prepare_session_payload(),
            {
                "session_id": "empty-01",
                "turn_count": 0,
                "state": {},
                "messages": [],
                "tools": [],
            },
        )

        deep_delta: dict[str, Any] = {
            "level1": {"level2": {"level3": [{"tags": ["a", "b"]}]}},
        }
        legacy_session.append_turn("user", {"parts": [{"text": "hello"}]}, deep_delta)
        opt_session.append_turn("user", {"parts": [{"text": "hello"}]}, deep_delta)

        deep_delta["level1"]["level2"]["level3"][0]["tags"].append("external-edit")
        legacy_out = legacy_session.prepare_session_payload()
        opt_out = opt_session.prepare_session_payload()
        self.assertEqual(legacy_out, opt_out)
        self.assertEqual(
            opt_out["state"]["level1"]["level2"]["level3"][0]["tags"],
            ["a", "b"],
        )

        opt_out["state"]["level1"]["level2"]["level3"][0]["tags"].append("output-edit")
        self.assertEqual(
            opt_session.prepare_session_payload()["state"]["level1"]["level2"]["level3"][0][
                "tags"
            ],
            ["a", "b"],
        )

    def test_tool_and_session_type_contracts_reject_invalid_arguments(self) -> None:
        modules: tuple[Any, Any] = (legacy, optimized)
        for mod in modules:
            with self.assertRaises(TypeError):
                mod.ToolDefinition(123, "desc", {})
            with self.assertRaises(TypeError):
                mod.ToolDefinition("name", None, {})
            with self.assertRaises(TypeError):
                mod.ToolDefinition("name", "desc", ["not", "a", "dict"])

            session_cls = resolve_session_class(mod)
            with self.assertRaises(TypeError):
                session_cls(999, [])
            with self.assertRaises(TypeError):
                session_cls("sess", ("not", "a", "list"))

            session = session_cls("sess", [])
            with self.assertRaises(TypeError):
                session.append_turn(123, "msg", {})
            with self.assertRaises(TypeError):
                session.append_turn("user", "msg", ["not-dict"])

    def test_validator_registry_patterns_match_valid_ids_and_reject_invalid(self) -> None:
        modules: tuple[Any, Any] = (legacy, optimized)
        for mod in modules:
            reg = mod.GLOBAL_VALIDATOR_REGISTRY
            self.assertEqual(len(reg), 400)
            self.assertIsNotNone(reg["rule_0"].match("agent_id_0_abcdefgh"))
            self.assertIsNotNone(reg["rule_399"].match("session_id_399_A1_b2-C3d4"))
            self.assertIsNone(reg["rule_0"].match("agent_id_0_short"))
            self.assertIsNone(reg["rule_0"].match("agent_id_1_abcdefgh"))

    def test_negative_control_fixture_exhibits_schema_cache_leak_and_lazy_attr(self) -> None:
        fixture_path = Path(__file__).parent / "fixtures" / "supplied_optimized.py"
        spec = importlib.util.spec_from_file_location("supplied_neg_ctrl", fixture_path)
        assert spec is not None and spec.loader is not None
        supplied = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(supplied)

        tool = supplied.ToolDefinition("t", "desc", {"required": ["q"]})
        first = tool.to_schema()
        first["parameters"]["required"].append("leaked")
        self.assertIn("leaked", tool.to_schema()["parameters"]["required"])

        with mock.patch("time.sleep"):
            reg = supplied.GLOBAL_VALIDATOR_REGISTRY
            self.assertEqual(len(reg), 400)
            self.assertIs(reg, supplied.GLOBAL_VALIDATOR_REGISTRY)
        with self.assertRaises(AttributeError):
            _ = supplied.NON_EXISTENT_ATTR


class WorkloadTests(unittest.TestCase):
    """Test synthetic workload builders, digest canonicalization, and input contracts."""

    def test_tools_builds_fifteen_independent_definitions(self) -> None:
        built = tools(optimized.ToolDefinition)
        self.assertEqual(len(built), 15)
        self.assertEqual(built[0].name, "search_0")
        self.assertEqual(built[-1].name, "search_14")
        built[0].parameters["required"].append("extra")
        self.assertEqual(built[1].parameters["required"], ["query"])

    def test_resolve_session_class_rejects_unsupported_module(self) -> None:
        with self.assertRaises(ValueError):
            resolve_session_class(object())

    def test_turn_payload_alternates_roles_and_wraps_slots(self) -> None:
        role0, msg0, delta0 = turn_payload(0, False)
        self.assertEqual((role0, msg0), ("user", "Message 0"))
        self.assertEqual(delta0, {"slot_0": 0, "cursor": 0})

        role21, msg21, delta21 = turn_payload(21, True)
        self.assertEqual((role21, msg21), ("assistant", "Message 21"))
        self.assertIn("slot_1", delta21)
        self.assertEqual(delta21["slot_1"]["labels"], ["synthetic", "21"])

    def test_turn_payload_and_exercise_reject_invalid_inputs(self) -> None:
        bad_turns: tuple[Any, ...] = (-1, True, False, 1.5, "2")
        for bad_turn in bad_turns:
            with self.subTest(bad_turn=bad_turn):
                with self.assertRaises(ValueError):
                    turn_payload(bad_turn, False)
                with self.assertRaises(ValueError):
                    exercise(optimized, bad_turn, False)

        bad_nesteds: tuple[Any, ...] = (1, 0, "true", None)
        for bad_nested in bad_nesteds:
            with self.subTest(bad_nested=bad_nested):
                with self.assertRaises(ValueError):
                    turn_payload(0, bad_nested)
                with self.assertRaises(ValueError):
                    exercise(optimized, 1, bad_nested)

    def test_digest_is_key_order_independent_and_rejects_non_finite_floats(self) -> None:
        d1 = digest({"b": 1, "a": [2, 3]})
        d2 = digest({"a": [2, 3], "b": 1})
        self.assertEqual(d1, d2)
        self.assertEqual(len(d1), 64)

        for bad_float in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad_float=bad_float):
                with self.assertRaises(ValueError):
                    digest({"metric": bad_float})


class SummaryStatisticsTests(unittest.TestCase):
    """Verify summary() quantiles, extrema, immutability, and strict validation."""

    def test_summary_singleton_sample(self) -> None:
        samples = [42.25]
        res = bench.summary(samples)
        self.assertEqual(res["samples"], [42.25])
        self.assertEqual(res["median"], 42.25)
        self.assertEqual(res["min"], 42.25)
        self.assertEqual(res["max"], 42.25)
        self.assertEqual(res["p10"], 42.25)
        self.assertEqual(res["p90"], 42.25)

    def test_summary_unsorted_odd_and_even_samples_without_mutating_caller(self) -> None:
        odd = [9.0, 1.0, 5.0, 3.0, 7.0]
        odd_res = bench.summary(odd)
        self.assertEqual(odd, [9.0, 1.0, 5.0, 3.0, 7.0])
        self.assertEqual(odd_res["median"], statistics.median(odd))
        self.assertEqual(
            (odd_res["median"], odd_res["min"], odd_res["max"], odd_res["p10"], odd_res["p90"]),
            (5.0, 1.0, 9.0, 1.0, 7.0),
        )

        even = [40.0, 10.0, 30.0, 20.0]
        even_res = bench.summary(even)
        self.assertEqual(even_res["median"], statistics.median(even))
        self.assertEqual(
            (
                even_res["median"],
                even_res["min"],
                even_res["max"],
                even_res["p10"],
                even_res["p90"],
            ),
            (25.0, 10.0, 40.0, 10.0, 30.0),
        )

    def test_summary_tied_and_negative_values(self) -> None:
        tied = (-2.0, -2.0, 0.0, 5.0, 5.0)
        res = bench.summary(tied)
        self.assertEqual(res["min"], -2.0)
        self.assertEqual(res["max"], 5.0)
        self.assertEqual(res["median"], 0.0)
        self.assertEqual(res["samples"], [-2.0, -2.0, 0.0, 5.0, 5.0])

    def test_summary_rejects_empty_and_invalid_numeric_elements(self) -> None:
        with self.assertRaises(ValueError):
            bench.summary([])

        invalid_inputs: list[Any] = [
            [True, 1.0],
            [False],
            [float("nan"), 1.0],
            [float("inf"), 1.0],
            [float("-inf"), 1.0],
            ["1.0"],
            "123",
            b"123",
            None,
            123,
        ]
        for bad in invalid_inputs:
            with self.subTest(bad=bad):
                with self.assertRaises((ValueError, TypeError)):
                    bench.summary(bad)


class MeasurementAndResilienceTests(unittest.TestCase):
    """Verify session_measure() and peak_memory() validation and process state cleanup."""

    def test_session_measure_and_peak_memory_valid_workloads(self) -> None:
        expected = digest(exercise(legacy, 5, True))
        for mod in (legacy, optimized):
            elapsed_ms = bench.session_measure(mod, 5, True, expected)
            self.assertIsInstance(elapsed_ms, float)
            self.assertGreaterEqual(elapsed_ms, 0.0)

            peak = bench.peak_memory(mod, 5, True, expected)
            self.assertIsInstance(peak, int)
            self.assertGreater(peak, 0)
            self.assertFalse(tracemalloc.is_tracing())

    def test_session_measure_rejects_invalid_turns_nested_and_expected(self) -> None:
        expected = digest(exercise(legacy, 1, False))
        bad_turns_cases: tuple[Any, ...] = (0, -1, -50, True, False, 1.5, "5", None)
        for bad_turns in bad_turns_cases:
            with self.subTest(bad_turns=bad_turns):
                with self.assertRaises(ValueError):
                    bench.session_measure(optimized, bad_turns, False, expected)

        bad_nested_cases: tuple[Any, ...] = (1, 0, "true", None)
        for bad_nested in bad_nested_cases:
            with self.subTest(bad_nested=bad_nested):
                with self.assertRaises(ValueError):
                    bench.session_measure(optimized, 2, bad_nested, expected)

        bad_expected_cases: tuple[Any, ...] = ("", "   ", None, 123)
        for bad_expected in bad_expected_cases:
            with self.subTest(bad_expected=bad_expected):
                with self.assertRaises(ValueError):
                    bench.session_measure(optimized, 2, False, bad_expected)

        with self.assertRaisesRegex(AssertionError, "differs"):
            bench.session_measure(optimized, 2, False, "0" * 64)

    def test_gc_state_restored_when_session_measure_raises_or_succeeds(self) -> None:
        class _ExplodingModule:
            ToolDefinition = optimized.ToolDefinition

            class OptimizedAgentSession:
                def __init__(self, session_id: str, session_tools: list[Any]) -> None:
                    self.session_id = session_id
                    self.tools = session_tools

                def append_turn(self, role: str, content: Any, delta: dict[str, Any]) -> None:
                    pass

                def prepare_session_payload(self) -> dict[str, Any]:
                    raise RuntimeError("simulated prepare failure")

        expected = digest(exercise(optimized, 2, False))
        was_enabled = gc.isenabled()
        try:
            gc.enable()
            with self.assertRaisesRegex(RuntimeError, "simulated prepare failure"):
                bench.session_measure(_ExplodingModule, 3, False, "a" * 64)
            self.assertTrue(gc.isenabled())

            gc.disable()
            bench.session_measure(optimized, 2, False, expected)
            self.assertFalse(gc.isenabled())
        finally:
            if was_enabled:
                gc.enable()
            else:
                gc.disable()

    def test_peak_memory_preserves_or_stops_tracemalloc_and_validates_inputs(self) -> None:
        expected = digest(exercise(legacy, 2, False))
        with self.assertRaises(ValueError):
            bench.peak_memory(optimized, 0, False, expected)
        with self.assertRaises(ValueError):
            bench.peak_memory(optimized, True, False, expected)
        with self.assertRaisesRegex(AssertionError, "differs"):
            bench.peak_memory(optimized, 2, False, "f" * 64)

        with (
            mock.patch.object(bench, "exercise", side_effect=RuntimeError("boom")),
            self.assertRaisesRegex(RuntimeError, "boom"),
        ):
            bench.peak_memory(optimized, 2, False, expected)
        self.assertFalse(tracemalloc.is_tracing())

        tracemalloc.start()
        try:
            peak = bench.peak_memory(optimized, 2, False, expected)
            self.assertGreater(peak, 0)
            self.assertTrue(tracemalloc.is_tracing())
        finally:
            tracemalloc.stop()


class BenchmarkAndCliTests(unittest.TestCase):
    """Offline mocked tests for cold(), benchmark(), render(), and main()."""

    def test_cold_parses_subprocess_timings_and_rejects_malformed_output(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["python"], returncode=0, stdout="[1.25, 3.5, 4.75]\n", stderr=""
        )
        with mock.patch("subprocess.run", return_value=completed) as run_mock:
            timings = bench.cold("adk_runtime_optimized")
        self.assertEqual(timings, [1.25, 3.5, 4.75])
        run_mock.assert_called_once()

        bad_length = subprocess.CompletedProcess(
            args=["python"], returncode=0, stdout="[1.0, 2.0]\n", stderr=""
        )
        with (
            mock.patch("subprocess.run", return_value=bad_length),
            self.assertRaises(ValueError),
        ):
            bench.cold("adk_runtime_optimized")

        bad_json = subprocess.CompletedProcess(
            args=["python"], returncode=0, stdout="not-json\n", stderr=""
        )
        with (
            mock.patch("subprocess.run", return_value=bad_json),
            self.assertRaises(ValueError),
        ):
            bench.cold("adk_runtime_optimized")

        with (
            mock.patch(
                "subprocess.run",
                side_effect=AssertionError("subprocess must not run on invalid module"),
            ),
            self.assertRaises(ValueError),
        ):
            bench.cold("")

    def test_percentage_reduction_handles_zero_baseline(self) -> None:
        self.assertEqual(bench._percentage_reduction(0.0, 1.0), 0.0)
        self.assertAlmostEqual(bench._percentage_reduction(10.0, 4.0), 60.0)

    def test_benchmark_orchestrates_gate_paired_runs_and_report_schema(self) -> None:
        gate_ok = subprocess.CompletedProcess(
            args=["python"], returncode=0, stdout="Ran 14 tests\nOK\n", stderr=""
        )

        def fake_cold(module_name: str) -> list[float]:
            if module_name == "adk_runtime_legacy":
                return [4.0, 0.01, 4.01]
            return [0.5, 3.5, 4.0]

        with (
            mock.patch("subprocess.run", return_value=gate_ok),
            mock.patch.object(bench, "cold", side_effect=fake_cold),
            mock.patch.object(
                bench,
                "session_measure",
                side_effect=lambda mod, turns, nested, expected: (
                    10.0 if mod is legacy else 4.0
                ),
            ),
            mock.patch.object(bench, "peak_memory", return_value=16384),
        ):
            report = bench.benchmark(3)

        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(len(report["workloads"]), 4)
        self.assertTrue(report["performance_targets_met"])
        self.assertAlmostEqual(report["import_median_reduction_percent"], 87.5)
        for case in report["workloads"]:
            self.assertAlmostEqual(case["median_reduction_percent"], 60.0)
            self.assertEqual(
                case["whole_session_peak_bytes"],
                {"adk_runtime_legacy": 16384, "adk_runtime_optimized": 16384},
            )

    def test_benchmark_raises_on_failed_gate_parity_error_or_bad_repeats(self) -> None:
        bad_repeats_cases: tuple[Any, ...] = (0, -1, True, 2.5)
        with mock.patch(
            "subprocess.run",
            side_effect=AssertionError("subprocess must not run on invalid repeats"),
        ):
            for bad_repeats in bad_repeats_cases:
                with self.subTest(bad_repeats=bad_repeats):
                    with self.assertRaises(ValueError):
                        bench.benchmark(bad_repeats)

        gate_fail = subprocess.CompletedProcess(
            args=["python"], returncode=1, stdout="FAIL: contract", stderr="\ntrace"
        )
        with (
            mock.patch("subprocess.run", return_value=gate_fail),
            self.assertRaisesRegex(AssertionError, "FAIL: contract"),
        ):
            bench.benchmark(3)

        gate_ok = subprocess.CompletedProcess(
            args=["python"], returncode=0, stdout="OK", stderr=""
        )
        with (
            mock.patch("subprocess.run", return_value=gate_ok),
            mock.patch.object(bench, "cold", return_value=[1.0, 1.0, 2.0]),
            mock.patch.object(bench, "digest", side_effect=["hash_a", "hash_b"]),
            self.assertRaisesRegex(AssertionError, "Pre-timing parity failed"),
        ):
            bench.benchmark(3)

    def test_render_escapes_html_and_renders_all_tables(self) -> None:
        sample_report: dict[str, Any] = {
            "schema_version": 1,
            "status": "pass",
            "unit_test_log": "<script>alert(1)</script>",
            "imports": {
                "adk_runtime_legacy": {
                    "import_ms": {"median": 4.0},
                    "first_registry_access_ms": {"median": 0.01},
                    "import_plus_registry_ms": {"median": 4.01},
                },
                "adk_runtime_optimized": {
                    "import_ms": {"median": 0.8},
                    "first_registry_access_ms": {"median": 3.2},
                    "import_plus_registry_ms": {"median": 4.0},
                },
            },
            "workloads": [
                {
                    "turns": 50,
                    "nested": False,
                    "median_reduction_percent": 45.2,
                    "prepare_total_ms": {
                        "adk_runtime_legacy": {"median": 2.0},
                        "adk_runtime_optimized": {"median": 1.096},
                    },
                },
                {
                    "turns": 50,
                    "nested": True,
                    "median_reduction_percent": 55.0,
                    "prepare_total_ms": {
                        "adk_runtime_legacy": {"median": 4.0},
                        "adk_runtime_optimized": {"median": 1.8},
                    },
                },
            ],
        }
        rendered = bench.render(sample_report)
        self.assertIn("<!doctype html>", rendered)
        self.assertIn("50 / scalar", rendered)
        self.assertIn("50 / nested", rendered)
        self.assertNotIn("<script>alert(1)</script>", rendered)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", rendered)

    def test_main_cli_success_enforce_targets_bounds_and_failure_report(self) -> None:
        fake_report: dict[str, Any] = {
            "schema_version": 1,
            "status": "pass",
            "import_median_reduction_percent": 15.0,
            "performance_targets_met": False,
            "imports": {
                name: {
                    k: {"median": 1.0}
                    for k in (
                        "import_ms",
                        "first_registry_access_ms",
                        "import_plus_registry_ms",
                    )
                }
                for name in bench.MODULES
            },
            "workloads": [
                {
                    "turns": 50,
                    "nested": False,
                    "median_reduction_percent": 35.0,
                    "prepare_total_ms": {
                        "adk_runtime_legacy": {"median": 2.0},
                        "adk_runtime_optimized": {"median": 1.3},
                    },
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            json_path = tmp_dir / "nested" / "report.json"
            html_path = tmp_dir / "nested" / "report.html"

            # 1. Passing run without --enforce-targets
            stdout_buf = io.StringIO()
            with (
                mock.patch.object(bench, "benchmark", return_value=dict(fake_report)),
                mock.patch("sys.stdout", stdout_buf),
            ):
                exit_code = bench.main(
                    [
                        "--repeats",
                        "3",
                        "--report",
                        str(json_path),
                        "--html",
                        str(html_path),
                    ]
                )
            self.assertEqual(exit_code, 0)
            saved = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "pass")
            self.assertIn("<!doctype html>", html_path.read_text(encoding="utf-8"))
            summary_line = json.loads(stdout_buf.getvalue().strip())
            self.assertEqual(summary_line["status"], "pass")

            # 2. Non-zero return code with --enforce-targets when targets not met
            with (
                mock.patch.object(bench, "benchmark", return_value=dict(fake_report)),
                mock.patch("sys.stdout", io.StringIO()),
            ):
                enforce_code = bench.main(
                    [
                        "--repeats",
                        "3",
                        "--report",
                        str(json_path),
                        "--html",
                        str(html_path),
                        "--enforce-targets",
                    ]
                )
            self.assertEqual(enforce_code, 1)
            self.assertEqual(
                json.loads(json_path.read_text(encoding="utf-8"))["status"],
                "targets_not_met",
            )

            # 3. Out-of-bounds repeats rejected by argparse
            for out_of_bounds in ("2", "32"):
                with (
                    mock.patch("sys.stderr", io.StringIO()),
                    self.assertRaises(SystemExit) as ctx,
                ):
                    bench.main(["--repeats", out_of_bounds])
                self.assertEqual(ctx.exception.code, 2)

            # 4. Benchmark failure writes failed status to JSON and HTML before re-raising
            with (
                mock.patch.object(
                    bench, "benchmark", side_effect=RuntimeError("<bad & broken>")
                ),
                self.assertRaisesRegex(RuntimeError, "<bad & broken>"),
            ):
                bench.main(
                    [
                        "--repeats",
                        "3",
                        "--report",
                        str(json_path),
                        "--html",
                        str(html_path),
                    ]
                )
            failed_json = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(failed_json["status"], "failed")
            self.assertIn("<bad & broken>", failed_json["error"])
            failed_html = html_path.read_text(encoding="utf-8")
            self.assertIn("Benchmark failed", failed_html)
            self.assertIn("&lt;bad &amp; broken&gt;", failed_html)


if __name__ == "__main__":
    unittest.main()
