# Scenario Catalogue — v0

Each scenario is implemented in `orchestrator/scenarios/<id>.yaml`; full sheets are in `sheets.md`. The **oracle fields**
(`intended_effects`, `injected_fault`, `change_causal`) are written to `scenario.json` and are visible only to
the labeler and the evaluator. The "expected label" column is the design intent. The **actual label is
always computed by the labeler from measured outcomes**, so a scenario may yield different labels
across seeds and workloads (e.g. an inefficient query is TA under low load and AR under high load).
That variation is intended.

| ID | Scenario | Change | Injected behaviour | Intended label(s) | change_causal |
|---|---|---|---|---|---|
| S1 | Harmless deploy / refactor | `orders` 1.0→1.1, no functional change | none | BA | n/a |
| S2 | Intentional behaviour change | `inventory` adds a validation step (+~15% latency, declared) | declared latency shift; new INFO template | BA | yes (benign) |
| S3 | Workload increase, no fault | no code change; load profile ×1.5–2 | declared `workload_change` | BA, or AR if capacity is exceeded | no |
| S4 | Short-lived DB slowdown | config change on `orders` | Toxiproxy +200 ms DB latency for 2–4 min, then removed | TA | no (coincident) |
| S5 | Inefficient query deploy | `orders` 1.2 with missing index / N+1 query | sustained DB time ↑, scaling with load | AR (or CR at high load) | yes |
| S6 | Delayed memory leak | `inventory` 1.3 leaks per request | RSS grows; GC pauses and latency rise after ~10–20 min; OOM-restart possible | AR / CR (requires extended H) | yes |
| S7 | Unrelated infra failure after deploy | harmless `orders` deploy | at t0+5–10 min: DB connection-limit squeeze or container pause on a dependency | AR / CR | **no** |
| S8 | Legitimate new log template | `orders` 1.4 logs a new structured INFO/WARN event | new templates, no impact | BA | yes (benign) |
| S9 | Severe error/latency regression | `gateway` 2.0 bad config: wrong timeout / failing route for a fraction of requests | 5xx ≥ 5% or p99 ≫ SLO | CR | yes |

Variants (multiply episodes, not change identities): seed, workload profile {steady, diurnal-ramp,
bursty}, intensity level (e.g. leak rate, fault fraction), rollout_pct {100, canary 25} where supported.

**Per-scenario sheet** (to be filled in during M2, one file per scenario): purpose · setup steps ·
parameters and ranges · expected behaviour · impact criteria that should or should not trigger ·
labeling notes and known ambiguities.
