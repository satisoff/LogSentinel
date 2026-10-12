# Label Specification — v0 (DRAFT, not frozen)

Status: **draft**. Numeric thresholds marked ⟨pilot⟩ are initial values. They are set from pilot runs (M4),
then frozen in `experiments/configs/labels.yaml` **before** any model is trained on the main dataset.
Every change after freezing must be recorded in the changelog at the bottom.

## 1. Unit of analysis

An **episode** `e` = one controlled change `c` applied to service `s` at time `t0`, together with:

| Window | Definition | Initial value |
|---|---|---|
| Baseline `W_pre` | `[t0 − T_pre, t0)` | T_pre = 30 min ⟨pilot⟩ |
| Observation `W_post` | `[t0, t0 + T_post]` | T_post = 30 min ⟨pilot⟩ |
| Label horizon `H` | `[t0, t0 + T_H]`, T_H ≥ T_post | T_H = 30 min ⟨pilot⟩. Extend for delayed-onset scenarios (memory leak) |
| Decision times `t_d` | When a system must emit a decision | {2, 5, 10, 15, 20, 30} min after t0 |
| Resolution | Aligned step for all series | 15 s |

**Decision: every post-change episode is labelled, whether or not any detector fired.**
The four classes describe the episode's *operational status*, not a detector's output. BA therefore
naturally covers (a) no deviation, (b) expected deviations, and (c) persistent deviations without
material impact. Detector outputs are features or baselines, never the population filter. Results on
the "detector-flagged" subset are reported separately so we can compare with "qualify detected
anomalies" formulations.

**Decision: labels use information up to `t0 + T_H`; systems may only use information up to `t_d`.**
This keeps the label from being a deterministic function of the classifier's own inputs. At
`t_d = T_H` the two coincide; that point is reported but is not the headline operating point.

## 2. Service-level indicator (SLI) and SLO

All impact criteria are measured at the **user-facing gateway**. Per-request outcomes come from the
load generator, which acts as the client.

- A request is **bad** if `status ≥ 500`, or it timed out, or `latency > L_slo`.
- `L_slo` = 300 ms ⟨pilot⟩. Set to roughly 3× the healthy baseline p95 measured in pilots.
- SLO target = 99% good requests ⟨pilot⟩, so the error budget `β` = 0.01.
- `bad_frac(t)` = bad requests / total requests in step `t`.
- **Burn rate** `b_w(t)` = mean bad fraction over the trailing window `w`, divided by `β`.
  - At burn rate 1 the budget is consumed exactly at the SLO rate.
- **Excess burn** `Δb_w(t) = b_w(t) − b̄_pre`, where `b̄_pre` is the mean burn rate over `W_pre`.
  This makes impact relative to the pre-change state, so a noisy baseline is not counted as regression.
- **Affected-request fraction** `f(t)` = `max(0, bad_frac(t) − bad_frac_pre)`.

## 3. Impact states (computed per 15 s step over H)

Two window lengths are used, following the multi-window convention of SRE burn-rate alerting. Long
windows establish severity. A short window (`w_s` = 1 min) requires the impact to still be present,
so a brief spike is not smeared across the long window.

| State | Condition | Initial thresholds |
|---|---|---|
| **severe** | (`b_5m ≥ B_crit` OR 5xx-rate over 2 min ≥ `E_crit`) AND (`b_1m ≥ B_crit` OR 5xx-rate over 1 min ≥ `E_crit`), sustained ≥ `D_crit` | B_crit = 14.4 (Google SRE workbook fast-burn convention), E_crit = 5% ⟨pilot⟩, D_crit = 2 min |
| **material** | excess short-window burn `b_1m − b̄_pre ≥ B_mat` | B_mat = 2.0 ⟨pilot⟩ |
| **none** | otherwise | — |

Intervals: maximal runs of consecutive material steps; gaps < 1 min are merged.
`impact_onset` is the first raw step (not the trailing-window step) whose own excess burn reaches B_mat.
This avoids biasing detection-delay measurements by the window lag.

## 4. Deviation oracle (for BA vs TA)

TA requires an *unexpected* deviation. To keep ground truth independent of any model under test,
"unexpected" is decided from **scenario ground truth** plus a fixed robust check, not from LogSentinel's
expected-behaviour model.

Each scenario manifest declares:
- `intended_effects`: expected metric shifts, e.g. `gateway_latency_p95: +15%`, `new_log_templates: true`.
- `workload_change`: declared load change, if any.
- `injected_fault`: type, onset, duration and target, if any.

The oracle check is run for each key metric `m`: gateway p95 latency, 5xx rate, service CPU, service
memory, DB latency, retry and timeout counts. A step is **deviant** when:

```
|m(t) − median_pre(m)| > max(k_mad · MAD_pre(m), r_min · |median_pre(m)|)
```
with k_mad = 4 and r_min = 0.20 ⟨pilot⟩, sustained ≥ 1 min, and **not covered** by a declared
intended effect (workload changes are declared as intended effects of the workload change).

## 5. Label rules (applied in order; the first match wins)

| # | Label | Rule |
|---|---|---|
| 1 | **CR** | Any step in H is **severe**, sustained ≥ `D_crit` = 2 min. |
| 2 | **AR** | Not CR, and **material** impact that (a) totals ≥ `P_mat` = 10 min in H, OR (b) occurs in ≥ `K_rec` = 3 separate intervals totalling ≥ 5 min, OR (c) is still present in the final 5 min of H and longer than 3 min (worsening/unrecovered). |
| 3 | **TA** | Not CR/AR, and either (i) there is some material impact, which is short-lived because rules 1–2 did not fire, or (ii) there is an unexpected deviation (§4) that has recovered: no deviant steps in the final `R` = 10 min of H. |
| 4 | **BA** | Everything else: no deviation; deviations covered by declared intended effects; or unexpected deviations that persist without material impact (`explained=false`). |

Note: material time is measured on the 1-min short-window burn, so it includes up to 1 min of
smoothing lag after the bad requests stop.

**Resolved ambiguity in the brief.** The brief defines BA as *"expected, **or** negligible impact"* and TA as
*"unexpected, short-lived, immaterial"*. A short-lived, unexpected, immaterial deviation matches both.
We resolve it as follows: **short-lived & unexpected ⇒ TA**, and **persistent & immaterial ⇒ BA**.
BA episodes with unexplained deviations get the secondary attribute `explained=false`, so this subgroup
can be analysed separately.

## 6. Secondary attributes (recorded, not part of the 4-class label)

- `change_causal` (oracle): did the change cause the impact? False for the "unrelated infrastructure
  failure" scenario. **Used to define unnecessary rollback.**
- `explained`: whether all deviations are covered by declared intended effects.
- `impact_onset`: first material/severe step. This is the reference for detection delay and lead time.
- `slo_violation_time`: first step where `b_1h` would exceed 1, i.e. the SLO would be violated at this rate. Used for lead time.
- `ambiguous`: true if any decisive quantity is within ±20% of its threshold, or a BA/TA episode
  still shows material impact or unexplained deviation in the final 5 min of H (truncated by the
  horizon). These episodes are double-reviewed.

## 7. Review and disagreement

The labeler is deterministic code (`logsentinel/episodes/labeler.py`) and writes an evidence record
(states per step, intervals, which rule fired). For `ambiguous` episodes, plus a random 10% sample of
the rest, two reviewers assign labels independently from the evidence plots, without seeing the
automatic label. We report Cohen's κ (reviewer vs reviewer, and reviewer vs automatic labeler). We
keep both the automatic label and the adjudicated label. The primary results use the adjudicated
label, and a sensitivity run uses the automatic label.

## 8. Known limitations of this definition

- Labels reflect *this* testbed's SLO and are not universal.
- The deviation oracle uses declared intended effects. This is experimenter knowledge that a real
  system would not have perfectly; it is what makes the ground truth possible, and it is never
  given to any system under test.
- Window lengths and thresholds influence class balance. Sensitivity analysis over T_post, P_mat
  and B_mat is required (see `evaluation.md`).

## Changelog
- v0 (2026-10-12): initial draft. TA rule split into short-lived material impact vs. recovered unexpected deviation; horizon-truncation ambiguity added. Multi-window impact states: a short 1-min window for material time and severe persistence (the earlier trailing-only windows made a 15 s outage count as 2 min severe); raw-step impact onset.
