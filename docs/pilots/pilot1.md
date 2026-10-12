# Pilot 1 — 2026-10-12 (GitHub Actions run 38239997109)

**Setup.** 9 episodes, one per scenario (seed 0), standard windows (30 min baseline, 30 min horizon;
45 min for S6). Each episode ran on its own GitHub-hosted runner: `ubuntu-24.04`, 4 vCPU, 16 GB. The runner
CPUs were heterogeneous: AMD EPYC 7763 (×3), EPYC 9V45 (×2), EPYC 9V74 (×2), Intel Xeon 6973P-C,
Xeon Platinum 8370C. All 9 jobs succeeded. Latency SLO at collection time: 300 ms (the original draft).

**Purpose.** Calibration only. None of these episodes may be used for training or evaluation. They are
all rejected (`slo_mismatch`) under the calibrated label config.

## Observations

| Scenario | Intended | Label (SLO 300 ms) | Key measurement |
|---|---|---|---|
| S1 harmless deploy | BA | BA (1.5 min "unexplained") | CPU wiggle +0.01–0.02 cores on a ~0.05-core baseline |
| S2 intentional change | BA | BA (5 min unexplained) | gw p95 rose more than the declared +50% envelope |
| S3 workload ×1.5 | BA | BA, explained | requests 224 → 330 per step |
| S4 transient DB slowdown | TA | **CR** | +DB latency queued requests: gw p95 max 4,683 ms |
| S5 inefficient query | AR | **BA** | gw p95 9.6 → 22.4 ms (2.3×), DB p95 4.8 → 9.6 ms: real, but far below SLO |
| S6 memory leak (8 KB/req) | AR/CR | **BA** | inventory RSS 58 → 152 MB in 45 min (2.2 MB/min at 4.3 req/s); no OOM |
| S7 unrelated infra failure | AR/CR | CR | |
| S8 new log template | BA | BA, explained | |
| S9 severe regression | CR | CR | |

**Healthy baseline on runners:** gw p95 8.8–18.3 ms, p99 ≤ 24.7 ms. The laptop showed p95 ≈ 50 ms.

## Defects found
1. Labelled Prometheus counters do not exist until first incremented. Episodes with zero bad requests had
   `bad = NaN` instead of 0 (the labels were correct only by accident).
2. A 300 ms SLO is 20–30× the healthy p95 on the runners, so regressions of 2–3× were invisible to labels.

## Changes for pilot 2
| Change | Rationale |
|---|---|
| SLO 300 → **50 ms** (≈ 2× worst healthy p99, ≈ 3× typical p95) | spec rule "≈ 3× healthy p95 from pilots" |
| One source of truth: the orchestrator passes the label-config SLO to the load generator. Runs record it, and the builder rejects mismatches | "bad" is decided at collection time |
| Pre-create load-generator counters; the builder fills true zeros where traffic was observed | defect 1 |
| Absolute minimum deviations (5 ms, 0.05 cores, 10 MB, 0.5 pp errors, 1 count) in labels and features | S1 noise |
| S2 envelope: gw p95 max +150%, CPU max +200% | S2 measurement |
| S4: +40–200 ms for 0.5–1.25 min (was +120–400 ms for 1–3 min) | intended TA |
| S5: no `LIMIT`, so a full scan over 600k rows (was LIMIT 1 over 200k) | effect too small |
| S6: 20 / 30 / 45 KB/req (was 8 / 12 / 18) | OOM at ≈ 26 / 17 / 12 min instead of ≈ 62 min |

**Threats noted:** runner CPU heterogeneity (record per episode; check results by CPU model) and a
different SLO from the laptop (never mix environments in one dataset).
