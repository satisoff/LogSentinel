# Dataset Specification — FROZEN v1 (2026-10-13)

## 1. Storage layout

```
data/
  raw/<run_id>/                 raw capture per episode run
    change.json                 structured change event (schema §2)
    scenario.json               scenario manifest incl. oracle fields (never given to systems)
    metrics.parquet             Prometheus range-query export, long format (schema §3)
    requests.parquet            per-request client-side records from the load generator
    logs/<service>.jsonl        structured service logs
  episodes/
    episodes.jsonl              manifest: one line per episode (schema §4), rebuilt from meta.json files
    <episode_id>/series.parquet aligned 15 s wide table (pre + post); columns <metric>__<service>,
                                client SLI columns, and service-agnostic aggregates for the labeler
    <episode_id>/logs.parquet   parsed logs; Drain3 template per line, mined online per service;
                                new_template = first seen after t0 within the episode
    <episode_id>/change.json    change event (visible fields only are exposed by store.load_view)
    <episode_id>/label.json     label, secondary attributes, evidence record (evaluator only)
    <episode_id>/meta.json      manifest row: identifiers, label summary, quality flags and details
  splits/<split_name>.json      frozen episode-id lists + hash of episodes.jsonl
```

## 2. Change event (`change.json`) — visible to systems

| Field | Type | Example |
|---|---|---|
| `change_id` | str | `chg-0042` (identity of the change; replicates of the same change share it) |
| `run_id` | str | `run-20261020-0042-s3` |
| `t0` | ISO-8601 UTC | change applied (end of rollout command) |
| `service` | enum | `orders` |
| `change_type` | enum | `deploy` · `config` · `feature_flag` · `dependency` · `infra` · `workload` |
| `from_version` / `to_version` | str | `1.4.0` → `1.5.0` |
| `scope` | enum | `single_service` · `multi_service` |
| `rollout_pct` | float | 100 (canary variants: 10/50) |
| `diff_summary` | dict | `{"config_keys": ["db_pool_size"], "flags": {}, "deps": {}}` |
| `declared_risk` | enum? | optional operator-declared risk, if we simulate it |

## 3. Telemetry

**Metrics (long format):** `ts, service, metric, labels(json), value`. Initial metric set:

| Metric | Source |
|---|---|
| request rate, error rate (4xx/5xx), latency histogram → p50/p95/p99 | service `prometheus_client` |
| CPU seconds, RSS memory, open fds | cAdvisor-free: process collector in each service |
| DB query latency histogram, pool in-use / waiting | instrumented DB client |
| retries, timeouts, exceptions (by type) | service counters |
| client-side request outcome (status, latency, timeout) | load generator → `requests.parquet` |

**Logs:** JSON lines `{ts, service, level, logger, msg, exc_type?, trace_id?, ...}`.
Drain3 mines templates per service. A template is **new** if it was not seen in `W_pre` or in the
template store built from the training split only (store per split, to avoid leakage).

## 4. Episode manifest (`episodes.jsonl`)

`episode_id, change_id, run_id, scenario, scenario_variant, seed, workload_profile, service, t0,
T_pre, T_post, T_H, label, label_auto, label_adjudicated, change_causal, explained, ambiguous,
impact_onset, slo_violation_time, data_quality_flags[]`

## 5. Data-quality checks (episode rejected or flagged)

Implemented in `logsentinel/episodes/builder.py`; thresholds in `experiments/configs/episodes.yaml`.
**Rejecting** flags: `missing_data`, `gap_near_t0`, `load_unstable`, `baseline_contaminated`,
`change_not_applied`. **Informational** flags: `restarts` (expected in S6), `host_noise`,
`nonstandard_windows` (smoke/pilot runs). Original checklist:

- Missing scrape steps > 5% in any window, or any gap > 1 min around t0.
- Load generator deviating from its target rate by > 10% in `W_pre` (the baseline is unstable).
- Clock skew: not checked separately. All telemetry and t0 are stamped on the same Docker VM clock
  (t0 is read from Prometheus `time()`), so there is no cross-host skew by construction.
- Baseline contamination: severe/material impact already present in `W_pre`.
- Scenario failed to apply (change controller error, container not healthy).

Rejected episodes are retained with flags and counted in the dataset statistics. They are never
silently dropped.

## 6. Independence and splits

- Each episode is a **fresh run**. The stack is reset (DB restored from snapshot, containers restarted)
  before `W_pre`, so one episode's post-window never contaminates the next one's baseline.
- **Grouping key = `change_id`.** All replicates (seeds, workloads) of the same change go to the same
  partition.
- `main`: 60/20/20 train/val/test, grouped by `change_id`, stratified by scenario × label as far as
  grouping allows.
- `loso`: leave-one-scenario-out (one fold per scenario) for unseen change types.
- `heldout_workload`: one workload profile is held out entirely for test.
- Each split file stores the SHA-256 of `episodes.jsonl` at freeze time. Evaluation refuses to run if
  the hash differs.
- An automated test asserts that no `change_id` appears in more than one partition.

## 7. Main dataset v1 (campaign `main-v1`)

- **152 episodes:** S4 seeds 0–23 (S4 is the only TA-producing scenario), all other scenarios seeds 0–15.
  Expected mix from the pilots: roughly 40% BA, 15% TA, 25% AR, 20% CR (measured, not assumed).
- **Environment:** GitHub-hosted `ubuntu-24.04` runners (4 vCPU / 16 GB), one isolated stack per runner,
  workflow `.github/workflows/collect.yml`. Runner CPU models vary and are recorded per shard.
- **Campaign tag:** each run records `campaign` (workflow input, `main-v1`). Splits and evaluation use only
  that campaign's episodes.
- **Pilot data** (pilots 1–4, smoke runs) lives in `data/pilots/` and is never used for training,
  tuning or evaluation.
- **Config fingerprint:** each episode stores the SHA-256 of the label config it was labelled with.

## 8. Reporting

Dataset statistics are reported in the paper: episodes per scenario × label, rejected episodes by
reason, class imbalance, label agreement (κ), and window/threshold sensitivity of the label distribution.
