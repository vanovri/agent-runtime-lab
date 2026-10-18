"""Mutation-safe optimization of the educational reference, not Google ADK."""
import copy
import hashlib
import json

_ATOMIC = {str, int, float, bool, bytes, type(None)}


def _clone(value, memo=None):
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
        result = {}; memo[identity] = result
        for key,item in value.items():
            result[_clone(key,memo)] = _clone(item,memo)
        return result
    if kind is list:
        result = []; memo[identity] = result
        result.extend(_clone(item,memo) for item in value)
        return result
    return copy.deepcopy(value,memo)


def __getattr__(name):
    if name != 'GLOBAL_VALIDATOR_REGISTRY':
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import re
    registry = {f"rule_{i}": re.compile(rf"^(?:agent|tool|session)_id_{i}_[a-zA-Z0-9_\-]{{8,64}}$")
                for i in range(400)}
    globals()[name] = registry
    return registry


class ToolDefinition:
    def __init__(self, name, description, parameters):
        self.name, self.description, self.parameters = name, description, parameters
        self._last_parameters = None
        self._last_version = None

    def to_schema(self):
        # Recompute the fingerprint to preserve nested public parameter mutations.
        # Cache only the digest, never a caller-mutable output dictionary.
        raw = json.dumps(self.parameters, sort_keys=True)
        if raw != self._last_parameters:
            self._last_version = hashlib.sha256(raw.encode()).hexdigest()[:12]
            self._last_parameters = raw
        return {"name": self.name, "description": self.description,
                "parameters": _clone(self.parameters), "schema_version": self._last_version}


class OptimizedAgentSession:
    def __init__(self, session_id, tools):
        self.session_id, self.tools, self.events = session_id, tools, []

    def append_turn(self, role, content, state_delta):
        self.events.append({"turn": len(self.events), "role": role, "content": content,
                            "state_delta": copy.deepcopy(state_delta)})

    def prepare_session_payload(self):
        # Public events can be edited/reordered by callers. Fold their current
        # values each time, but copy only the final merged state and transcript.
        # Returning a full history still takes O(history length).
        state, messages, message_memo = {}, [], {}
        for event in self.events:
            state.update(event['state_delta'])
            messages.append({"role": _clone(event['role'],message_memo),
                             "content": _clone(event['content'],message_memo)})
        return {"session_id": self.session_id, "turn_count": len(self.events),
                "state": {key:_clone(value) for key,value in state.items()},
                "messages": messages,
                "tools": [tool.to_schema() for tool in self.tools]}
