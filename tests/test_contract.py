from __future__ import annotations

import importlib.util
import random
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

import adk_runtime_legacy as reference
import adk_runtime_optimized as candidate
from workload import exercise, tools

SessionPair = tuple[reference.LegacyAgentSession, candidate.OptimizedAgentSession]


class ContractTests(unittest.TestCase):
    def pair(self) -> SessionPair:
        return (
            reference.LegacyAgentSession("id", tools(reference.ToolDefinition)),
            candidate.OptimizedAgentSession("id", tools(candidate.ToolDefinition)),
        )

    def equal(self, pair: SessionPair) -> tuple[dict[str, Any], dict[str, Any]]:
        a, b = (s.prepare_session_payload() for s in pair)
        self.assertEqual(a, b)
        return a, b  # type: ignore[return-value]

    def test_payloads_at_required_lengths(self) -> None:
        for n in (0, 1, 50, 150):
            for nested in (False, True):
                with self.subTest(turns=n, nested=nested):
                    self.assertEqual(
                        exercise(reference, n, nested),
                        exercise(candidate, n, nested),
                    )

    def test_input_delta_is_snapshotted(self) -> None:
        pair = self.pair()
        delta: dict[str, Any] = {"nested": {"items": [1, 2]}}
        for s in pair:
            s.append_turn("user", "hello", delta)
        delta["nested"]["items"].append(999)
        self.assertEqual(self.equal(pair)[0]["state"]["nested"]["items"], [1, 2])

    def test_payload_mutation_does_not_leak(self) -> None:
        pair = self.pair()
        for s in pair:
            s.append_turn("user", "hello", {"nested": [1]})
        for output in self.equal(pair):
            output["state"]["nested"].append(2)
            output["messages"][0]["content"] = "changed"
            output["tools"][0]["parameters"]["required"].append("other")
        self.assertEqual(self.equal(pair)[0]["state"]["nested"], [1])
        self.assertEqual(self.equal(pair)[0]["messages"][0]["content"], "hello")

    def test_public_history_mutations_and_reorder_are_observed(self) -> None:
        pair = self.pair()
        for s in pair:
            s.append_turn("user", "first", {"x": [1]})
            s.append_turn("assistant", "second", {"x": [2]})
            s.prepare_session_payload()
            s.events[0]["state_delta"]["x"].append(3)
            s.events.reverse()
            s.events[0]["content"] = {"mutable": ["supported by the reference"]}
        self.equal(pair)
        for p in self.equal(pair):
            p["messages"][0]["content"]["mutable"].append("caller edit")
        self.equal(pair)
        for s in pair:
            s.events.pop()
        self.equal(pair)
        for s in pair:
            s.events.clear()
        self.equal(pair)

    def test_distinct_state_keys_remain_independent_after_export(self) -> None:
        pair = self.pair()
        for session in pair:
            shared = [1]
            session.append_turn("user", "aliases", {"a": shared, "b": shared})
        for payload in self.equal(pair):
            payload["state"]["a"].append(2)
            self.assertEqual(payload["state"]["b"], [1])

    def test_tool_schema_mutations_name_and_order(self) -> None:
        pair = self.pair()
        self.equal(pair)
        for s in pair:
            s.tools[0].parameters["properties"]["query"]["description"] = "new"
            s.tools[0].name = "renamed"
            s.tools[0].description = "revised"
            s.tools.reverse()
        self.equal(pair)

    def test_schema_return_isolation_and_fingerprint_invalidation(self) -> None:
        modules: tuple[Any, Any] = (reference, candidate)
        for module in modules:
            tool = tools(module.ToolDefinition)[0]
            before = tool.to_schema()
            returned = tool.to_schema()
            returned["parameters"]["required"].append("caller mutation")
            self.assertEqual(before, tool.to_schema())
            tool.parameters["required"].append("real change")
            self.assertNotEqual(before["schema_version"], tool.to_schema()["schema_version"])

    def test_seeded_updates_keep_last_write_semantics(self) -> None:
        generator = random.Random(2042)
        pair = self.pair()
        for turn in range(80):
            delta = {f"k{generator.randrange(5)}": {"list": [generator.randrange(20), turn]}}
            for s in pair:
                s.append_turn("user", "日本語 / Bahasa", delta)
            self.equal(pair)

    def test_public_tool_parameters_preserve_python_containers(self) -> None:
        # Hash serialization accepts tuples, but a public result retains tuples.
        modules: tuple[Any, Any] = (reference, candidate)
        for module in modules:
            params: dict[str, Any] = {"enum": ("a", "b")}
            schema = module.ToolDefinition("n", "d", params).to_schema()
            self.assertIsInstance(schema["parameters"]["enum"], tuple)

    def test_aliases_and_python_numeric_types_are_preserved_within_schema(self) -> None:
        modules: tuple[Any, Any] = (reference, candidate)
        for module in modules:
            shared = ["value"]
            params: dict[str, Any] = {"a": shared, "b": shared, "default": 1}
            tool = module.ToolDefinition("n", "d", params)
            payload = tool.to_schema()
            payload["parameters"]["a"].append("test")
            self.assertEqual(payload["parameters"]["b"], ["value", "test"])
            before = tool.to_schema()["schema_version"]
            params["default"] = 1.0
            self.assertNotEqual(before, tool.to_schema()["schema_version"])

    def test_mutable_message_content_aliases_are_preserved(self) -> None:
        pair = self.pair()
        for session in pair:
            session.append_turn("user", "one", {})
            session.append_turn("user", "two", {})
            shared = ["content"]
            session.events[0]["content"] = shared
            session.events[1]["content"] = shared
        for output in self.equal(pair):
            output["messages"][0]["content"].append("edit")
            self.assertEqual(output["messages"][1]["content"], ["content", "edit"])

    def test_lazy_registry_is_equal_and_cached(self) -> None:
        expected = reference.GLOBAL_VALIDATOR_REGISTRY
        actual = candidate.GLOBAL_VALIDATOR_REGISTRY
        self.assertEqual(list(expected), list(actual))
        self.assertTrue(all(expected[k].pattern == actual[k].pattern for k in expected))
        self.assertIs(actual, candidate.GLOBAL_VALIDATOR_REGISTRY)
        with self.assertRaises(AttributeError):
            _ = candidate.not_a_registry

    def test_lazy_registry_not_built_on_fresh_import(self) -> None:
        script = (
            "import adk_runtime_optimized as m; "
            "assert 'GLOBAL_VALIDATOR_REGISTRY' not in m.__dict__"
        )
        subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            cwd=Path(__file__).resolve().parents[1],
        )

    def test_supplied_optimization_fails_mutation_contract(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "supplied_optimized.py"
        spec = importlib.util.spec_from_file_location("supplied_candidate", fixture)
        assert spec is not None and spec.loader is not None
        supplied = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(supplied)
        bad = supplied.OptimizedAgentSession("id", [])
        delta = {"nested": [1]}
        bad.append_turn("user", "hi", delta)
        delta["nested"].append(2)
        good = reference.LegacyAgentSession("id", [])
        good.append_turn("user", "hi", {"nested": [1]})
        self.assertNotEqual(good.prepare_session_payload(), bad.prepare_session_payload())


if __name__ == "__main__":
    unittest.main()
