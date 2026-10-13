# Pilot 4 — 2026-10-13 (GitHub Actions run 38370903633)

**Setup.** S6 seeds 0–3 with the pilot-3 change (15 s application warm-up after any restart), no swap,
and leak sizes 25 / 35 / 50 KB/req. All 4 jobs succeeded and all episodes passed the quality checks.
Calibration data only.

## Result
| seed | leak | rps | OOM kills at (min) | peak 1-min burn | material (min) | label |
|---|---|---|---|---|---|---|
| 0 | 25 KB | 15 | 24.2 | 10.2 | 1.25 | AR (unresolved deviation) |
| 1 | 35 KB | 15 | 16.8, 33.5 | 8.6 | 2.25 | AR (unresolved deviation) |
| 2 | 50 KB | 15 | 12.2, 24.0, 34.8 | 10.9 | 3.5 | AR (unresolved deviation) |
| 3 | 25 KB | 25 | 14.5, 28.2, 42.5 | 10.5 | 3.75 | AR (unrecovered) |

Each crash now costs users ≈ 15 s of failed requests to inventory (peak short-window burn 8.6–10.9× budget,
well above B_mat = 2). Impact onset coincides with the first OOM. S6 now expresses its design intent.

## Finding: the ambiguity test was naive
All four episodes were flagged ambiguous because the 2-min 5xx rate peaked at 4.2–5.6%, "near" E_crit = 5%.
No severe period was ever sustained, so a CR label was never at stake (the same happened to two S4
episodes with the 5-min burn near B_crit). **Change:** an episode is ambiguous iff its label changes when
B_crit, E_crit, B_mat and P_mat are scaled by 0.8 or 1.2, or the horizon truncates ongoing impact or
deviation. Under this test all pilot-4 S6 labels are stable (AR at ±20%). Labels for perturbed thresholds
are stored in the evidence record. Two tests cover both directions.

## Status
Every scenario now produces its intended label class under the current config:
S1/S2/S3/S8 → BA, S4 → TA, S5 → AR/CR (one variant BA), S6 → AR, S7/S9 → CR. Ready to freeze v1.
