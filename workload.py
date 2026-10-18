"""Shared synthetic workload definition; no performance assertions."""
import hashlib
import json


def tools(tool_class):
    return [tool_class(f'search_{i}', f'Synthetic tool {i}', {
        'type':'object','properties':{'query':{'type':'string'},
        'filters':{'type':'array','items':{'type':'string'}}}, 'required':['query']}) for i in range(15)]


def exercise(module, turns, nested=True):
    cls = getattr(module, 'OptimizedAgentSession', None) or module.LegacyAgentSession
    session = cls('synthetic-01', tools(module.ToolDefinition))
    result = session.prepare_session_payload()
    for turn in range(turns):
        session.append_turn(*turn_payload(turn,nested))
        result = session.prepare_session_payload()
    return result


def turn_payload(turn, nested):
    value = {'values':list(range(12)), 'labels':['synthetic',str(turn)]} if nested else turn
    return ('user' if turn % 2 == 0 else 'assistant', f'Message {turn}',
            {f'slot_{turn % 20}':value, 'cursor':turn})


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()
