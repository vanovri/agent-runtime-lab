"""Educational reference runtime. Deliberately eager, with no artificial sleeps."""
import copy
import hashlib
import json
import re


def _init_heavy_telemetry_and_validators():
    return {f"rule_{i}": re.compile(rf"^(?:agent|tool|session)_id_{i}_[a-zA-Z0-9_\-]{{8,64}}$")
            for i in range(400)}


GLOBAL_VALIDATOR_REGISTRY = _init_heavy_telemetry_and_validators()


class ToolDefinition:
    def __init__(self, name, description, parameters):
        self.name, self.description, self.parameters = name, description, parameters

    def to_schema(self):
        params = copy.deepcopy(self.parameters)
        version = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:12]
        return {"name": self.name, "description": self.description,
                "parameters": params, "schema_version": version}


class LegacyAgentSession:
    def __init__(self, session_id, tools):
        self.session_id, self.tools, self.events = session_id, tools, []

    def append_turn(self, role, content, state_delta):
        self.events.append({"turn": len(self.events), "role": role, "content": content,
                            "state_delta": copy.deepcopy(state_delta)})

    def prepare_session_payload(self):
        snapshot = copy.deepcopy(self.events)
        state, messages = {}, []
        for event in snapshot:
            for key, value in event['state_delta'].items():
                state[key] = copy.deepcopy(value)
            messages.append({"role": event['role'], "content": event['content']})
        return {"session_id": self.session_id, "turn_count": len(snapshot), "state": state,
                "messages": messages, "tools": [tool.to_schema() for tool in self.tools]}
