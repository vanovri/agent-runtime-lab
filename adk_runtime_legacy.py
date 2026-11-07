"""Educational reference runtime. Deliberately eager, with no artificial sleeps."""

from __future__ import annotations

import copy
import hashlib
import json
import re

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any, TypedDict

    class ToolSchemaDict(TypedDict):
        name: str
        description: str
        parameters: dict[str, Any]
        schema_version: str

    class TurnEventDict(TypedDict):
        turn: int
        role: str
        content: Any
        state_delta: dict[str, Any]

    class MessageDict(TypedDict):
        role: Any
        content: Any

    class SessionPayloadDict(TypedDict):
        session_id: str
        turn_count: int
        state: dict[str, Any]
        messages: list[MessageDict]
        tools: list[ToolSchemaDict]


_VALIDATOR_COUNT = 400


def _init_heavy_telemetry_and_validators() -> dict[str, re.Pattern[str]]:
    """Eagerly compile all synthetic identifier validation rules."""
    return {
        f"rule_{i}": re.compile(rf"^(?:agent|tool|session)_id_{i}_[a-zA-Z0-9_\-]{{8,64}}$")
        for i in range(_VALIDATOR_COUNT)
    }


GLOBAL_VALIDATOR_REGISTRY: dict[str, re.Pattern[str]] = _init_heavy_telemetry_and_validators()


def _compute_schema_version(parameters: dict[str, Any]) -> str:
    """Compute deterministic 12-hex-character SHA-256 schema version digest."""
    serialized = json.dumps(parameters, sort_keys=True).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()[:12]


class ToolDefinition:
    """Reference tool metadata definition that deep-copies parameters on every export."""

    def __init__(self, name: str, description: str, parameters: dict[str, Any]) -> None:
        if not isinstance(name, str):
            raise TypeError("name must be a str")
        if not isinstance(description, str):
            raise TypeError("description must be a str")
        if not isinstance(parameters, dict):
            raise TypeError("parameters must be a dict")
        self.name: str = name
        self.description: str = description
        self.parameters: dict[str, Any] = parameters

    def to_schema(self) -> ToolSchemaDict:
        """Export isolated tool schema dictionary with freshly computed version hash."""
        params = copy.deepcopy(self.parameters)
        version = _compute_schema_version(params)
        return {
            "name": self.name,
            "description": self.description,
            "parameters": params,
            "schema_version": version,
        }


class LegacyAgentSession:
    """Reference session container preserving full deep-copy semantics."""

    def __init__(self, session_id: str, tools: list[ToolDefinition]) -> None:
        if not isinstance(session_id, str):
            raise TypeError("session_id must be a str")
        if not isinstance(tools, list):
            raise TypeError("tools must be a list")
        self.session_id: str = session_id
        self.tools: list[ToolDefinition] = tools
        self.events: list[TurnEventDict] = []

    def append_turn(self, role: str, content: Any, state_delta: dict[str, Any]) -> None:
        """Append a turn event with an eager deep copy of state_delta."""
        if not isinstance(role, str):
            raise TypeError("role must be a str")
        if not isinstance(state_delta, dict):
            raise TypeError("state_delta must be a dict")
        self.events.append(
            {
                "turn": len(self.events),
                "role": role,
                "content": content,
                "state_delta": copy.deepcopy(state_delta),
            }
        )

    def _fold_snapshot(
        self, snapshot: list[TurnEventDict]
    ) -> tuple[dict[str, Any], list[MessageDict]]:
        """Fold snapshotted event deltas and build message transcript."""
        state: dict[str, Any] = {}
        messages: list[MessageDict] = []
        for event in snapshot:
            for key, value in event["state_delta"].items():
                state[key] = copy.deepcopy(value)
            messages.append({"role": event["role"], "content": event["content"]})
        return state, messages

    def prepare_session_payload(self) -> SessionPayloadDict:
        """Prepare complete session payload from a deep snapshot of current history."""
        snapshot = copy.deepcopy(self.events)
        state, messages = self._fold_snapshot(snapshot)
        return {
            "session_id": self.session_id,
            "turn_count": len(snapshot),
            "state": state,
            "messages": messages,
            "tools": [tool.to_schema() for tool in self.tools],
        }
