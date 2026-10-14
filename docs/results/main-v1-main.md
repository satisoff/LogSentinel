# Results — campaign `main-v1`, split `main` (pre-registered, evaluated 2026-10-14)

Frozen definitions: evaluation.md v1.1, labels v1. Thresholds and the strongest baseline were chosen on
validation, and the test split (41 episodes, 9 change identities, labels BA 17 / TA 5 / AR 13 / CR 6) was
evaluated once. Full tables: `main-v1-main-summary.md`; raw outputs: `experiments/reports/main-v1-main/`.

## Headline (H1: fewer false alerts at matched harmful recall, without worse delay)

| system | false alerts / episode | harmful recall | utility | unnecessary rollbacks / episode |
|---|---|---|---|---|
| B1 rolling z (strongest on validation) | 0.12 [0, 0.35] | **0.89** [0.60, 1] | −2.07 [−4.5, −0.33] | 0 |
| B5 persistence filter | 0.22 [0, 0.51] | 1.0 | −1.71 | 0 |
| LogSentinel rules (tuned on val) | 0.17 [0, 0.40] | 1.0 | −1.05 [−2.83, −0.13] | 0.07 |
| LogSentinel model (recall-matched) | 0.20 [0, 0.44] | 1.0 | **−0.61** [−1.17, −0.08] | 0.17 |

95% two-stage cluster-bootstrap CIs over change_id.

**H1 is not supported on this split.** At the validation-chosen operating point, the strongest baseline (B1)
raised fewer false alerts than LogSentinel (paired difference for the LogSentinel model +0.07 [0, +0.26];
for tuned rules +0.05 [0, +0.16]). B1 also missed 2 of 19 harmful episodes, so recall was not actually
matched on test. Among systems with full test recall, LogSentinel (rules 0.17, model 0.20) had the fewest
false alerts, ahead of B5 (0.22), B6 (0.39) and B2/B3 (0.54). These differences are within the CIs.

**Utility:** the recall-matched LogSentinel model had the best action-weighted utility, and it ranked first
in all 12 cost-sensitivity settings. The paired difference to B1 (+1.46 [−0.23, +3.84]) has a CI that
includes 0. Detection delay was similar for all systems (median ≈ 1.75 min).

Power is limited: the test set has 9 change identities (one per scenario).

## Failure analysis
1. **Transient anomalies (S4, TA) were not distinguished from regressions by any system.** All 5 TA test
   episodes triggered false alerts in B1, the LogSentinel model and the LogSentinel rules. The S4 TA/AR
   boundary depends on how long a slowdown lasts, which is unknowable at early decision times. Only B5
   (persistence) avoided them, at the price of 9 false alerts on persistent-but-benign deviations (S2, S3).
2. **Shortcut learning on change metadata.** The learned model alerted at t0 + 2 min on harmless S1
   deploys, and on S4 episodes before their fault had started, with no deviation evidence. In this testbed,
   change type and service largely identify the scenario (e.g. all S4/S9 changes are `config`), so the
   `chg_` features act as a scenario identifier. The ablation agrees: dropping change context lowered
   false alerts (0.15 vs 0.20). This is a testbed artefact that a real system could not exploit.
3. **No causal attribution.** LogSentinel escalated all 3 S7 critical incidents, whose cause was an
   unrelated infrastructure failure, which counts as an *unnecessary rollback* of an innocent change.
   Neither the rules nor the model have a signal for "the change did not cause this".
4. **Ablations:** SLO-impact features matter most (without them false alerts 0.34 and utility −1.76).
   Logs and persistence features added nothing measurable on this split. The cost-based policy beat argmax
   on utility (−0.61 vs −1.44).
5. The cost-policy variant (`LS_model_cost`) alerts liberally (false alerts 0.54), as the cost matrix
   implies (missing an AR costs 10× a false alert).

## What this does and does not show
- It does **not** show that LogSentinel reduces false alerts at matched recall. On this split it did not.
- It suggests that impact-aware qualification improves action-weighted utility, but this is not significant.
- Any fix to the failures above is **post hoc**. It must be evaluated on fresh data (a new campaign), not on
  this test split.
