"""Mutation-safe optimization of the educational reference, not Google ADK."""

from __future__ import annotations

import copy
import hashlib
import json

TYPE_CHECKING = False
if TYPE_CHECKING:
    import re
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


_ATOMIC: frozenset[type] = frozenset({str, int, float, bool, bytes, type(None)})
_VALIDATOR_COUNT = 400


def _clone(value: Any, memo: dict[int, Any] | None = None) -> Any:
    """Fast path for plain containers; retain deepcopy's aliases and fallback."""
    kind = type(value)
    if kind in _ATOMIC:
        return value
    if memo is None:
        memo = {}
    identity = id(value)
    if identity in memo:
        return memo[identity]
    if kind is dict:
        cloned_dict: dict[Any, Any] = {}
        memo[identity] = cloned_dict
        for key, item in value.items():
            cloned_key = key if type(key) in _ATOMIC else _clone(key, memo)
            cloned_item = item if type(item) in _ATOMIC else _clone(item, memo)
            cloned_dict[cloned_key] = cloned_item
        return cloned_dict
    if kind is list:
        cloned_list: list[Any] = []
        memo[identity] = cloned_list
        cloned_list.extend(
            item if type(item) in _ATOMIC else _clone(item, memo) for item in value
        )
        return cloned_list
    return copy.deepcopy(value, memo)


def _build_validator_registry() -> dict[str, re.Pattern[str]]:
    """Compile the 400 identifier regular expressions on first demand."""
    import re

    return {
        f"rule_{i}": re.compile(rf"^(?:agent|tool|session)_id_{i}_[a-zA-Z0-9_\-]{{8,64}}$")
        for i in range(_VALIDATOR_COUNT)
    }


def __getattr__(name: str) -> dict[str, re.Pattern[str]]:
    """PEP 562 lazy module attribute loader for GLOBAL_VALIDATOR_REGISTRY."""
    if name != "GLOBAL_VALIDATOR_REGISTRY":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    registry = _build_validator_registry()
    globals()[name] = registry
    return registry


class ToolDefinition:
    """Mutation-safe tool definition caching only the immutable digest string."""

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
        self._last_parameters: str | None = None
        self._last_version: str | None = None

    def _resolve_schema_version(self, raw: str | None = None) -> str:
        """Recompute version hash only when serialized parameters change."""
        serialized = json.dumps(self.parameters, sort_keys=True) if raw is None else raw
        if serialized != self._last_parameters or self._last_version is None:
            self._last_version = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:12]
            self._last_parameters = serialized
        return self._last_version

    def to_schema(self) -> ToolSchemaDict:
        """Return fresh parameter containers paired with cached schema digest."""
        raw = json.dumps(self.parameters, sort_keys=True)
        version = (
            self._last_version
            if raw == self._last_parameters and self._last_version is not None
            else self._resolve_schema_version(raw)
        )
        return {
            "name": self.name,
            "description": self.description,
            "parameters": _clone(self.parameters),
            "schema_version": version,
        }


class OptimizedAgentSession:
    """Mutation-safe session folding current public history without intermediate snapshots."""

    def __init__(self, session_id: str, tools: list[ToolDefinition]) -> None:
        if not isinstance(session_id, str):
            raise TypeError("session_id must be a str")
        if not isinstance(tools, list):
            raise TypeError("tools must be a list")
        self.session_id: str = session_id
        self.tools: list[ToolDefinition] = tools
        self.events: list[TurnEventDict] = []

    def append_turn(self, role: str, content: Any, state_delta: dict[str, Any]) -> None:
        """Append a turn event while isolating caller mutations to state_delta."""
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

    def _fold_current_events(self) -> tuple[dict[str, Any], list[MessageDict]]:
        """Fold current public events and clone message transcript preserving aliases."""
        merged_state: dict[str, Any] = {}
        messages: list[MessageDict] = []
        message_memo: dict[int, Any] = {}
        atomic_types = _ATOMIC
        for event in self.events:
            merged_state.update(event["state_delta"])
            role = event["role"]
            content = event["content"]
            messages.append(
                {
                    "role": role if type(role) in atomic_types else _clone(role, message_memo),
                    "content": (
                        content if type(content) in atomic_types else _clone(content, message_memo)
                    ),
                }
            )
        isolated_state = {
            key: (value if type(value) in atomic_types else _clone(value))
            for key, value in merged_state.items()
        }
        return isolated_state, messages

    def prepare_session_payload(self) -> SessionPayloadDict:
        """Prepare complete session payload while observing direct event/tool edits."""
        state, messages = self._fold_current_events()
        return {
            "session_id": self.session_id,
            "turn_count": len(self.events),
            "state": state,
            "messages": messages,
            "tools": [tool.to_schema() for tool in self.tools],
        }
