# Bounded optimization prompt template

This file is a reusable template, not a record of an unattended model run.

```text
Optimize adk_runtime_optimized.py against the documented reference contract.
Before editing, run the contract suite and save a nine-pair benchmark baseline.
Freeze tests/, workload.py, adk_runtime_legacy.py, referee_adk_bench.py, and targets.

Permitted edits: adk_runtime_optimized.py only. Preserve signatures, returned field
names, nested input snapshots, fresh output containers, public event/tool mutation,
container types, and alias behavior. Keep all original source data untouched.

Do not insert delays into the reference, change workloads, remove failing tests,
fabricate samples, install dependencies, use network services, or publish anything.
Run contracts after every edit and the paired benchmark after contracts pass.
Targets: >=40% preparation reduction on all four workloads, >=20% cold-import
reduction. Record first-registry-access cost separately from import time.

At most five edit/verify attempts and twenty minutes. Stop with an incomplete
result if targets cannot be reached without breaking the contract. Report source
hashes, environment, every sample, median/spread, memory, and limitations. Never
claim Google ADK production results or O(1) full-history serialization.
```
