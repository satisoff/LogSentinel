# Evaluation Definitions — FROZEN v1 (2026-10-13)

These definitions must be frozen, with a dated changelog entry, **before** any system is run on the
test split.

## 1. Actions and ordering

Every system emits, per episode and decision time `t_d`, one action from the ordered set:

`NoAction (N) < Observe (O) < Investigate (I) < Escalate (E)`

The recommended action per true label is BA→N, TA→O, AR→I, CR→E.

Mapping for baselines that only output a binary/score alarm: alarm ⇒ **I**, no alarm ⇒ **N**.
Optionally, if a baseline exposes a severity score, a second threshold maps to **E**. Each such
threshold is tuned on validation, the same way as for LogSentinel.

## 2. Core definitions

| Term | Definition |
|---|---|
| **Alert** | Action ∈ {I, E}. O is *not* an alert (non-paging watch), but it is counted and costed separately. |
| **Harmful episode** | True label ∈ {AR, CR}. |
| **Detected harmful episode** | Harmful, and an alert is issued at some `t_d ≤ deadline`. Deadline: end of H for AR; `impact_onset + 10 min` for CR. |
| **Missed regression** | Harmful episode that is not detected by its deadline. |
| **False alert** | Alert on a BA or TA episode. |
| **Unnecessary action** | Action strictly stronger than the recommended action for the true label. |
| **Unnecessary rollback** | Action E where the true label ≠ CR **or** `change_causal = false`. |
| **Detection delay** | `t_alert − impact_onset` for detected harmful episodes. It can be negative (alert before impact). |
| **Lead time before SLO violation** | `slo_violation_time − t_alert`, when both exist. |

**Online protocol.** Systems are evaluated at each `t_d` in the grid using data ≤ `t_d` only. The
"first alert time" is the earliest `t_d` at which the action is ≥ I. Unless stated otherwise, headline
numbers use the **online protocol**: the action at the earliest alerting `t_d`, or the action at the
final `t_d` if no alert occurs.

## 3. Metrics (priority order)

1. **False alerts per episode at fixed harmful recall.** For each system, choose the alert threshold
   on validation as the threshold achieving harmful recall ≥ 0.95 (0.90 reported as well), then apply
   it unchanged to test. Report test false-alert rate and the achieved test recall.
2. **Harmful recall at a fixed alert budget:** budget = harmful prevalence of the validation split (alerts per
   episode that a perfect system would spend). Report false alerts per episode and harmful recall at that budget.
3. Harmful-regression precision and recall (AR ∪ CR), and CR recall on its own.
4. Detection delay (median, IQR) and lead time before SLO violation.
5. Unnecessary-rollback rate (per episode, and per E action).
6. Probability calibration of the qualifier: per-class Brier score, ECE (10 bins), reliability plots.
7. **Action-weighted operational utility**: `U = −mean_e C[label_e, action_e]` using the cost matrix below.
8. Generalisation: all of the above on the leave-one-scenario-out and held-out-workload splits.
9. Error-budget burn avoided: reported **only** under an explicit counterfactual assumption
   (an action at `t_alert` stops further excess burn after a fixed mitigation time). It is labelled
   as model-based and is not a headline metric.

Accuracy and macro-F1 are reported for completeness only.

## 4. Cost matrix (default; subject to sensitivity analysis)

Rows are the true label; columns are the action. Units are arbitrary "operator cost".

| | N | O | I | E |
|---|---|---|---|---|
| BA | 0 | 0.1 | 1 | 3 |
| TA | 0.2 | 0 | 1 | 3 |
| AR | 10 | 6 | 0 | 1.5 |
| CR | 50 | 40 | 10 | 0 |

The cost is +2 when the action is E and `change_causal = false` (unnecessary rollback of an innocent change).

**Sensitivity analysis:** scale the missed-regression costs (lower-left block) by {0.5, 1, 2, 5}
and the false-alert costs (upper-right block) by {0.5, 1, 2}. Report the utility ranking of all
systems under each setting. A ranking that flips under plausible costs must be reported as such.

## 5. Uncertainty and statistics

- **Cluster bootstrap over `change_id`**, stratified by scenario, 2,000 resamples, 95% percentile CIs.
  Replicates of the same change (seeds, workloads) are correlated, so resampling individual
  episodes would understate uncertainty. Episode-level bootstrap CIs are reported alongside, for reference only.
- **Paired** bootstrap for differences between systems on the same test episodes. McNemar's test for
  alert/no-alert disagreement on harmful episodes.
- Training randomness: 5 seeds per learned model. Report the mean, and the CI across seeds and episodes.
- **No telemetry-row-level statistics** are used for any claim.
- Multiple comparisons: the headline comparison (LogSentinel vs. the strongest baseline) is
  pre-specified. All other comparisons are exploratory and labelled as such.

## 6. Systems compared

1. Context-blind statistical detector (rolling z-score / EWMA on key metrics).
2. Context-blind ML detector (IsolationForest on windowed multivariate features).
3. Change-aware detector (pre/post comparison conditioned on change metadata and workload).
4. SCWarn-inspired multimodal bad-change detector (metrics + log-template features). We disclose
   every deviation from the original.
5. Detector #1 or #2 + fixed persistence suppression (alert only if the alarm holds ≥ N min).
6. MELODY-inspired: semi-supervised deep one-class on deployment-level features, CPU-sized.
   **Not a faithful reproduction**: MELODY's data and code are not available to us. Reported as
   "inspired by" with the differences listed.
7. LogSentinel: rule-based qualifier.
8. LogSentinel: learned qualifier + cost-sensitive policy.

## 7. Ablations (each removes one component of system 8)

Change context · log features · metric features · log–metric fusion (late concatenation replaced by
a single modality) · persistence features · service-impact/SLO features · adaptive policy (replaced by
argmax class → action).

## 8. Sensitivity analyses

Window lengths T_pre/T_post ∈ {15, 30, 45} min · label thresholds P_mat and B_mat at ±50% · decision time
grid · cost matrix (above).

## Changelog
- v0 (2026-10-12): initial draft. Bootstrap changed to cluster bootstrap over change_id.
- **v1 (2026-10-13): frozen.** CR deadline 10 min, recall target 0.95, alert budget = validation harmful
  prevalence, delay margin (H1) = median detection delay at most 5 min worse than the best baseline, paired
  on the same episodes.
