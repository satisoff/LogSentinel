# Pilot 3 — 2026-10-12 (GitHub Actions run 38270762759)

**Setup.** S6 (delayed memory leak) seeds 0–3 after the pilot-2 fixes: swap disabled per container
(`memswap_limit = mem_limit`), leak sizes 25 / 35 / 50 KB per request. All 4 jobs succeeded and all
episodes passed the quality checks. Calibration data only.

## Result
| seed | leak | rps | OOM kills at (min) | label |
|---|---|---|---|---|
| 0 | 25 KB | 15 | 24.2 | BA (unexplained, immaterial) |
| 1 | 35 KB | 15 | 16.5, 33.2 | BA |
| 2 | 50 KB | 15 | 12.0, 23.2, 34.0 | BA |
| 3 | 25 KB | 25 | 14.2, 27.8, 41.8 | BA |

- **Swap fix confirmed:** OOM timing now matches the prediction from the measured leak rate (≈ 12–25 min).
- **But crash loops had no user-visible impact.** An OOM-killed service restarted in ~1 s, and the gateway
  retried failed GETs, so no material SLO impact was recorded. By the label taxonomy (impact-based) BA is
  then *correct*. It also shows that the pilot-2 "impact" for S6 came from swapping, not from the crash.
- **Testbed realism gap:** real services take seconds to become ready after a restart (connection pools,
  caches, warm-up), and requests fail meanwhile. That is what makes crash loops hurt in production.

## Change for pilot 4
Application services wait `APP_STARTUP_DELAY_S = 15` s before accepting connections after any (re)start
(`testbed/services/app/common.py`; uvicorn binds its socket only after lifespan startup). This affects only
restarts: baselines and in-place deploys are unchanged. The load generator uses 0.

## Housekeeping
The laptop pilot stopped on 2026-10-12 had left 8 empty run folders: its batch process outlived the
killed shell and kept failing against the stopped testbed. They were removed, and the runner now creates a
run folder only after data has been exported.
