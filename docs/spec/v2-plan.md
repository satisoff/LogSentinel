# LogSentinel v2 — pre-registered plan (2026-10-14, written before any `main-v2` data was seen)

## Why v2
The pre-registered evaluation on `main-v1` (`docs/results/main-v1-main.md`) did **not** support H1. Its
failure analysis found three problems. v2 addresses each. Because these fixes were informed by the
`main-v1` test split, they are evaluated only on a **fresh campaign, `main-v2`**, and its test split
is used once.

| main-v1 finding | v2 change |
|---|---|
| Shortcut learning: change type/service one-hots identify the scenario; the model alerted on harmless deploys at t0+2 min with no evidence | **Drop change-identity features** (`chg_type_*`, `chg_svc_*`) from the learned model. Change conditioning remains through the train-fitted shift prior of the expected-behaviour model and `chg_rollout_pct` / `chg_minutes_since`. |
| Transient anomalies (S4 TA) alerted by every system | **Patience gate** for non-severe alerts: an Investigate requires persistence evidence (material impact ≥ G min, or ≥ 2 material intervals, or a statistically clear worsening deviation trend). Otherwise the action is Observe. Severe impact is never gated. G ∈ {0, 1, 2, 3, 5} is chosen on validation, subject to the 0.95 harmful-recall target. |
| No causal attribution: unrelated infrastructure failures (S7) escalated with a rollback | **Attribution features** (`att_*`) and a **rollback flag** on Escalate: rollback is recommended only when the impact is attributable to the changed service (it is the most-deviating service, and there is no shared-dependency signal across services). |

## Evaluation amendment v2 (applies to `main-v2` only, to all systems)
The action Escalate gains a flag `rollback` (True/False).
- *Unnecessary rollback* = Escalate **with rollback** where label ≠ CR or `change_causal = false`.
- The +2 innocent-change penalty applies only to Escalate **with rollback**.
- Baselines never recommend rollback (as in v1). v1 LogSentinel variants always set rollback on Escalate
  (as in v1), so their v1 numbers are reproduced exactly.
All other v1.1 definitions are unchanged: labels v1, cost matrix, CR deadline, recall target 0.95,
alert budget, delay margin, two-stage cluster bootstrap.

## Data
Campaign `main-v2`: S4 seeds 24–47, all other scenarios seeds 16–31 (152 episodes), on the same GitHub
runners and frozen scenario manifests. **Limitation:** the change *configurations* are the same 34 as in
`main-v1` (seeds pick change variants deterministically). Episodes are new (new workloads, fault draws
and runner hardware), but change identities are shared with main-v1. Generalisation to unseen change
types is assessed by the leave-one-scenario-out splits.

## Pre-specified hypotheses on main-v2 (main split, test used once)
- **H1′** The v2 learned qualifier (recall-matched on validation) raises fewer false alerts per episode
  than the strongest validation baseline, with the paired 95% CI of the difference below 0, at test
  harmful recall ≥ 0.95, and median delay at most 5 min worse.
- **H2′** v2 raises fewer false alerts on TA episodes than the v1 learned qualifier (paired).
- **H3′** v2 recommends fewer unnecessary rollbacks per episode than the v1 learned qualifier (paired).
All other comparisons are exploratory. Negative results will be reported as such.
