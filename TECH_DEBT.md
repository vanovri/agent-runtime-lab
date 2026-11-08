# Technical Debt & Performance Ledger: agent-runtime-lab

Crosschecked 2026-11-08 WIB. All code diffs below are proposal-only and have not changed production source. Source references and removed patch lines were checked against this working tree. Runtime speedups remain unmeasured unless evidence is explicitly cited.

Reviewed on 2026-11-08 WIB against the current working tree on disk. All line numbers are 1-based and verified directly against the current production files. Every diff below is a non-destructive engineering proposal, not an applied production edit.

## TD-01 — Use `_clone(state_delta)` fast path instead of `copy.deepcopy` in `OptimizedAgentSession.append_turn`

- **Location**: `adk_runtime_optimized.py:147-154`
- **Analysis**: `OptimizedAgentSession` defines `_clone` specifically to avoid `copy.deepcopy` dispatch overhead on plain JSON-like `dict`/`list`/primitive trees, yet `append_turn` still invokes `copy.deepcopy(state_delta)` on every turn.

```diff
diff --git a/adk_runtime_optimized.py b/adk_runtime_optimized.py
--- a/adk_runtime_optimized.py
+++ b/adk_runtime_optimized.py
@@ -149,6 +149,6 @@
                 "turn": len(self.events),
                 "role": role,
                 "content": content,
-                "state_delta": copy.deepcopy(state_delta),
+                "state_delta": _clone(state_delta),
             }
         )
```

- **Validation**: Run `C:\Python314\python.exe -m pytest -q` and `referee_adk_bench.py --repeats 3` to verify mutation-contract and digest parity.

## TD-02 — Extend `_clone` fast path to `tuple` containers before falling back to `copy.deepcopy`

- **Location**: `adk_runtime_optimized.py:60-67`
- **Analysis**: All-atomic plain tuples miss `_ATOMIC` and fall back to generic `copy.deepcopy`. An atomic-only shortcut can return the same immutable tuple. Tuples containing mutable values must keep the existing fallback because tuple/list cycles and shared aliases need deepcopy's reentrant handling. This is a small constant-factor proposal, not an established benchmark improvement.

```diff
diff --git a/adk_runtime_optimized.py b/adk_runtime_optimized.py
--- a/adk_runtime_optimized.py
+++ b/adk_runtime_optimized.py
@@ -64,6 +64,8 @@
             item if type(item) in _ATOMIC else _clone(item, memo) for item in value
         )
         return cloned_list
+    if kind is tuple and all(type(item) in _ATOMIC for item in value):
+        return value
     return copy.deepcopy(value, memo)
 
 
```

- **Validation**: Test atomic tuples, tuples containing mutable dicts/lists, and shared tuple aliases via `C:\Python314\python.exe -m pytest -q`.
