# Pilot 2 — 2026-10-12 (GitHub Actions runs 38258036071, 38258042794)

**Setup.** 18 episodes: S1–S9 seed 0, plus S4/S5/S6 seeds 1–3 (to see their change variants). Calibrated
config from pilot 1 (SLO 50 ms, absolute deviation minimums, recalibrated S2/S4/S5/S6). All 18 jobs
succeeded and all 18 episodes passed the data-quality checks. Calibration data only: these episodes are
excluded from the training/evaluation dataset.

## Labels vs. design intent

| Scenario | Intent | Pilot 2 |
|---|---|---|
| S1, S2, S3, S8 | BA | BA ×4 (one "unexplained" BA remaining, down from four) |
| S4 transient DB slowdown | TA | **TA ×4** (pilot 1: CR) |
| S5 inefficient query | AR/CR | AR ×2, CR ×1, BA ×1 (the indexed N+1 variant has no measurable impact) |
| S6 memory leak | AR/CR | TA ×3, BA ×1 under the old rules; **AR ×3, BA ×1 after rule 2d** (see below) |
| S7, S9 | CR | CR |

## Findings and changes
1. **Leak crashes were labelled "transient".** The leak OOM-killed the service (1–4 min of material
   impact), memory grew again after the restart, and the label rules called that TA. **Change (label
   rule 2d):** material impact plus an unexplained deviation still present at the end of the horizon → AR.
   S4 (clean recovery) stays TA. Tests cover both.
2. **Swap delayed OOM kills.** Retention matched the configured KB/request (19.6 / 29.5 / 43.3 measured
   for 20 / 30 / 45). The memory limit was reached on time, but CI runners have swap, so the container
   swapped for 15+ min before being killed (OOM at 29–45 min instead of ~14–31). **Change:**
   `memswap_limit = mem_limit` on every container; S6 leak sizes 25 / 35 / 50 KB/req.

## Sanity check of the rule-based pipeline (NOT an evaluation result)
Pilot episodes calibrate the testbed; they are not a test set. Observed behaviour, for orientation only:
- All 8 harmful episodes (S5 ×3, S6 ×3, S7, S9) received an Investigate/Escalate. S5/S9 at 2 min; S6
  leaks at 5 min, 17–29 min before impact onset, from the memory-trend rule.
- No alerts on S1, S3, S5 (indexed N+1) or S8; S2 at most Observe.
- **False alerts:** two S4 episodes (true TA) were escalated to Investigate at 10 min, because the decision
  time fell inside the short fault ("material impact worsening"). This is the central TA-vs-AR trade-off,
  to be tuned on the validation split only.

## Next
Pilot 3: S6 only (after the swap fix) to confirm OOM timing, then freeze label/build/scenario configs (v1)
before the main collection.
