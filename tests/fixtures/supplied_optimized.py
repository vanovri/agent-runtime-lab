import hashlib
import json
import re
import time
from typing import Any, Dict, List, Optional

# [OPTIMIZATION 1] PEP 562 Lazy Module Attribute Loading — 0ms overhead saat cold import
_LAZY_VALIDATOR_REGISTRY: Optional[Dict[str, re.Pattern]] = None


def __getattr__(name: str) -> Any:
  global _LAZY_VALIDATOR_REGISTRY
  if name == "GLOBAL_VALIDATOR_REGISTRY":
    if _LAZY_VALIDATOR_REGISTRY is None:
      _LAZY_VALIDATOR_REGISTRY = {
          f"rule_{i}": re.compile(
              rf"^(?:agent|tool|session)_id_{i}_[a-zA-Z0-9_\-]{{8,64}}$"
          )
          for i in range(400)
      }
      time.sleep(0.015)
    return _LAZY_VALIDATOR_REGISTRY
  raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class ToolDefinition:
  __slots__ = ("name", "description", "parameters", "_cached_schema")

  def __init__(self, name: str, description: str, parameters: Dict[str, Any]):
    self.name = name
    self.description = description
    self.parameters = parameters
    self._cached_schema: Optional[Dict[str, Any]] = None

  def to_schema(self) -> Dict[str, Any]:
    # [OPTIMIZATION 2] Compute once & cache immutable schema payload
    if self._cached_schema is None:
      raw = json.dumps(self.parameters, sort_keys=True).encode("utf-8")
      schema_hash = hashlib.sha256(raw).hexdigest()[:12]
      self._cached_schema = {
          "name": self.name,
          "description": self.description,
          "parameters": self.parameters,
          "schema_version": schema_hash,
      }
    return self._cached_schema


class OptimizedAgentSession:
  __slots__ = (
      "session_id",
      "tools",
      "events",
      "_merged_state",
      "_transcript",
      "_compiled_tools_cache",
  )

  def __init__(self, session_id: str, tools: List[ToolDefinition]):
    self.session_id = session_id
    self.tools = tools
    self.events: List[Dict[str, Any]] = []
    self._merged_state: Dict[str, Any] = {}
    self._transcript: List[Dict[str, str]] = []
    self._compiled_tools_cache: Optional[List[Dict[str, Any]]] = None

  def append_turn(
      self, role: str, content: str, state_delta: Dict[str, Any]
  ) -> None:
    turn_idx = len(self.events)
    self.events.append({
        "turn": turn_idx,
        "role": role,
        "content": content,
        "state_delta": state_delta,
    })
    # [OPTIMIZATION 3] O(1) incremental state & transcript update
    if state_delta:
      self._merged_state.update(state_delta)
    self._transcript.append({"role": role, "content": content})

  def prepare_session_payload(self) -> Dict[str, Any]:
    if self._compiled_tools_cache is None:
      self._compiled_tools_cache = [t.to_schema() for t in self.tools]

    return {
        "session_id": self.session_id,
        "turn_count": len(self.events),
        "state": dict(self._merged_state),
        "messages": list(self._transcript),
        "tools": self._compiled_tools_cache,
    }

