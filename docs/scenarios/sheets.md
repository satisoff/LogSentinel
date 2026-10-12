# Scenario Sheets — v0

Manifests: `orchestrator/scenarios/S*.yaml`. Windows default to 30 min baseline / 30 min horizon,
traffic is 15–25 rps (open loop), and the latency SLO is 300 ms. Labels are always computed from measured
outcomes by `logsentinel/episodes/labeler.py`; "intended label" is the design target only.
Magnitudes marked ⟨pilot⟩ are calibrated in M4 pilots and then frozen.

---

## S1 — Harmless deploy / refactor
- **Purpose:** check that a no-op deployment does not trigger action. This is the main false-alert source for
  change-sensitive detectors.
- **Setup:** redeploy one service with a new version and no or negligible behaviour change (≤ 0.3 ms CPU).
  4 change variants × workload profiles.
- **Expected behaviour:** no SLI change beyond noise.
- **Impact criteria:** none expected. Any material impact is a testbed problem to investigate.
- **Intended label:** BA. **Ambiguity:** bursty workloads may create TA-like noise. If so, it is recorded and analysed.

## S2 — Intentional behaviour change
- **Purpose:** an *expected* deviation. The feature adds validation work and a new log event.
- **Setup:** inventory +3–5 ms CPU per request (one variant also +10 ms wait); one canary variant at 50%.
- **Expected behaviour:** inventory CPU ↑, gateway p95 ↑ modestly, new INFO template.
- **Oracle:** `intended_effects` upper bounds on gw p95 and svc CPU ⟨pilot⟩.
- **Intended label:** BA (explained). It becomes AR only if the added work actually breaches the SLO.

## S3 — Workload increase without a fault
- **Purpose:** distinguish load-driven deviation from software regression.
- **Setup:** load ×1.5 / 1.8 / 2.2 / 2.6, step or ramp (120–300 s).
- **Expected behaviour:** CPU and DB latency ↑. Low factors keep the SLO; high factors may saturate one CPU per service.
- **Oracle:** load-driven upper bounds declared; `change_causal = false`.
- **Intended label:** BA at low factors; AR/CR if capacity is exceeded (as measured). A rollback recommendation is
  always unnecessary here.

## S4 — Short-lived DB slowdown
- **Purpose:** an unexpected but transient deviation coinciding with a harmless config change.
- **Setup:** config change, then Toxiproxy adds +120–400 ms DB latency for 1–3 min starting at t0+3–12 min.
- **Expected behaviour:** latency spike and SLO-bad requests during the fault, then full recovery.
- **Intended label:** TA. Long or strong draws may become AR (e.g. ≥ 3 min near 400 ms) — measured.
- `change_causal = false`.

## S5 — Inefficient-query deployment
- **Purpose:** a sustained, load-dependent regression caused by the change.
- **Setup:** orders filters on the unindexed `customer_ref` column (sequential scan over 200k rows), and/or N+1
  lookups; one canary variant at 25%.
- **Expected behaviour:** DB latency and orders CPU ↑ immediately; gateway p95 ↑, growing with load.
- **Intended label:** AR (CR at high load). `change_causal = true`.

## S6 — Delayed memory leak
- **Purpose:** a regression with delayed onset. It tests persistence/trend features and detection delay.
- **Setup:** inventory retains ~8–18 KB of small objects per request. The container limit is 192 MB, so the
  leak reaches an OOM kill and restart after roughly 15–40 min ⟨pilot⟩. The label horizon is extended to 45 min.
- **Expected behaviour:** RSS grows linearly; GC gen-2 time ↑ → latency creep. Then OOM restart → 5xx burst. The
  restart comes back on the leaking version (persisted state) and the cycle repeats.
- **Intended label:** AR or CR. `change_causal = true`.

## S7 — Unrelated infrastructure failure after a deploy
- **Purpose:** an incident that follows a change but is not caused by it. Investigation is needed; rollback is
  unnecessary.
- **Setup:** harmless deploy, then at t0+5–10 min one of: DB unreachable for 1–3 min, inventory paused for
  1–2.5 min, or +250–600 ms DB latency for 8–15 min.
- **Intended label:** CR (outages) or AR (prolonged latency), as measured. `change_causal = false`.
  An E action counts as an unnecessary rollback.

## S8 — Legitimate new log template
- **Purpose:** new log templates alone must not imply harm. This targets log-novelty detectors (cf. the SCWarn
  critique).
- **Setup:** a new structured INFO event every 3–25 requests on one service.
- **Expected behaviour:** new templates; no SLI change.
- **Intended label:** BA. `change_causal = true` (benign).

## S9 — Severe error/latency regression
- **Purpose:** clear harm that must be escalated quickly.
- **Setup:** gateway answers 10–30% of requests with 500, or has a 60 ms upstream timeout (→ timeouts,
  retries, 504s); one orders variant with 20% errors; one canary at 50%.
- **Expected behaviour:** 5xx rate ≥ 5% within a minute of t0.
- **Intended label:** CR. `change_causal = true`.

---

### Change identity and splits
Each scenario has 3–5 change variants. 34 distinct `change_id`s exist in total. Replicates (seed,
workload) of a variant share its `change_id` and are always placed in the same split. **Implication:** a
60/20/20 grouped split leaves about 7 change identities in test. Statistical claims must account for this
clustering (cluster bootstrap over `change_id`; see evaluation.md). If pilots show this is too coarse,
more change variants are added before data collection, not after.

### Testbed design decisions (with evidence)
- **Postgres durability off** (`fsync`, `synchronous_commit` and `full_page_writes` off; checkpoints every 30 min).
  In the first smoke run (2026-10-12, S9 seed 0), a routine checkpoint coincided with DB p95 latency up to 1.5 s
  and client p95 up to 4.1 s, while the services were idle. On this laptop, flushes to the WSL virtual disk stall
  under memory pressure. That is periodic noise unrelated to any scenario, so it is removed. Limitation: the testbed
  does not model storage-induced latency.
- **Zero-downtime deploys are simulated in-process** (behaviour-profile swap), not by restarting containers.
  Limitation: no cold-start effects. OOM restarts (S6) are real container restarts.
- **Host contention:** other workloads on the laptop (e.g. other Docker stacks) add CPU noise. During data
  collection, other stacks should be stopped and their presence logged in the run record.
