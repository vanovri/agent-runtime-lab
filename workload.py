"""Shared synthetic workload definition; no performance assertions."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Protocol, TypeVar

logger = logging.getLogger(__name__)

_TOOL_COUNT = 15
_SLOT_MODULUS = 20
_NESTED_VALUE_COUNT = 12

T_Tool_co = TypeVar("T_Tool_co", covariant=True)


class ToolFactory(Protocol[T_Tool_co]):
    """Callable factory matching ToolDefinition(name, description, parameters)."""

    def __call__(
        self, name: str, description: str, parameters: dict[str, Any]
    ) -> T_Tool_co: ...


def _validate_non_negative_int(value: int, field_name: str) -> None:
    """Reject booleans, non-integers, and negative values."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer, got {value!r}")


def _validate_nested_flag(nested: bool) -> None:
    """Reject non-boolean values for the nested payload flag."""
    if not isinstance(nested, bool):
        raise ValueError(f"nested must be a boolean, got {nested!r}")


def _default_tool_parameters() -> dict[str, Any]:
    """Build fresh JSON-Schema-like parameter dictionary for a synthetic tool."""
    return {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "filters": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["query"],
    }


def tools(tool_class: ToolFactory[T_Tool_co]) -> list[T_Tool_co]:
    """Instantiate 15 deterministic synthetic search tool definitions."""
    return [
        tool_class(f"search_{i}", f"Synthetic tool {i}", _default_tool_parameters())
        for i in range(_TOOL_COUNT)
    ]


def resolve_session_class(module: Any) -> Any:
    """Resolve OptimizedAgentSession or LegacyAgentSession from a runtime module."""
    session_cls = getattr(module, "OptimizedAgentSession", None) or getattr(
        module, "LegacyAgentSession", None
    )
    if session_cls is None:
        raise ValueError(f"Module {module!r} exposes no supported AgentSession class")
    return session_cls


def exercise(module: Any, turns: int, nested: bool = True) -> dict[str, Any]:
    """Run a synthetic session for `turns` iterations and return final payload."""
    _validate_non_negative_int(turns, "turns")
    _validate_nested_flag(nested)
    session_cls = resolve_session_class(module)
    session = session_cls("synthetic-01", tools(module.ToolDefinition))
    result: dict[str, Any] = session.prepare_session_payload()
    for turn in range(turns):
        session.append_turn(*turn_payload(turn, nested))
        result = session.prepare_session_payload()
    return result


def turn_payload(turn: int, nested: bool) -> tuple[str, str, dict[str, Any]]:
    """Construct deterministic (role, content, state_delta) tuple for a turn."""
    _validate_non_negative_int(turn, "turn")
    _validate_nested_flag(nested)
    value: dict[str, Any] | int = (
        {"values": list(range(_NESTED_VALUE_COUNT)), "labels": ["synthetic", str(turn)]}
        if nested
        else turn
    )
    role = "user" if turn % 2 == 0 else "assistant"
    return (
        role,
        f"Message {turn}",
        {f"slot_{turn % _SLOT_MODULUS}": value, "cursor": turn},
    )


def digest(value: Any) -> str:
    """Compute canonical SHA-256 hex digest over JSON-serialized payload."""
    serialized = json.dumps(value, sort_keys=True, allow_nan=False).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()
