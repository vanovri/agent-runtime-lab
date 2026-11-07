# Technical Debt & Performance Ledger: agent-runtime-lab

Reviewed on 2026-11-07 against the current working tree on disk. All line numbers are 1-based and verified directly against the current production files. Every diff below is a non-destructive engineering proposal, not an applied production edit.

## TD-01 — Use `_clone(state_delta)` fast path instead of `copy.deepcopy` in `OptimizedAgentSession.append_turn`

- **Location**: `adk_runtime_optimized.py:147-154`
- **Analysis**: `OptimizedAgentSession` defines `_clone` specifically to avoid `copy.deepcopy` dispatch overhead on plain JSON-like `dict`/`list`/primitive trees, yet `append_turn` still invokes `copy.deepcopy(state_delta)` on every turn.

```diff
--- a/adk_runtime_optimized.py
+++ b/adk_runtime_optimized.py
@@ -149,7 +149,7 @@
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
- **Analysis**: Plain `tuple` values in `content` or `parameters` miss `_ATOMIC` and fall back to `copy.deepcopy(value, memo)`, incurring generic dispatch overhead even when all tuple elements are atomic.

```diff
--- a/adk_runtime_optimized.py
+++ b/adk_runtime_optimized.py
@@ -64,6 +64,14 @@
             item if type(item) in _ATOMIC else _clone(item, memo) for item in value
         )
         return cloned_list
+    if kind is tuple:
+        if all(type(item) in _ATOMIC for item in value):
+            return value
+        cloned_tuple = tuple(
+            item if type(item) in _ATOMIC else _clone(item, memo) for item in value
+        )
+        memo[identity] = cloned_tuple
+        return cloned_tuple
     return copy.deepcopy(value, memo)
```

- **Validation**: Test atomic tuples, tuples containing mutable dicts/lists, and shared tuple aliases via `C:\Python314\python.exe -m pytest -q`.
