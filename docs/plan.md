# LogSentinel — Build Plan (from scratch)

## Context
`C:\PFold\LogSentinel` contains only `context.txt`, the research brief. LogSentinel is meant to **qualify post-change anomalies** into BA / TA / AR / CR and recommend No action / Observe / Investigate / Escalate. The goal is to show, with honest experiments, whether this cuts unnecessary actions without missing harmful regressions, compared with context-blind detectors, persistence filters, and SCWarn- and MELODY-style baselines. This plan takes it from an empty folder to a working, evaluated prototype, including the testbed app, the ML, the dashboard and an optional cloud runner.

**Environment checked:** Windows 11 with Docker Desktop 29.8 (WSL2; the engine is currently stopped), kubectl, Git 2.52, Node 24, Python 3.12 (msys) and 3.14 (py launcher). No cloud CLIs or Terraform. **7.4 GB RAM, i5-8265U, no GPU.** So the design is CPU-only and uses tree models and statistics, no deep learning first. The local stack must stay small (about 4 GB budget), and long batches of episodes may go to a cloud VM later.

**RAM-first rule (user directive, 2026-10-12):** when there is a trade-off, choose the option that uses less RAM. Docker Desktop/WSL runs only while the testbed is collecting data. Modelling, evaluation and the dashboard run natively with Docker stopped. Testbed target: ≤ 1 GB of containers, with the WSL VM capped through `.wslconfig`.

**Defaults chosen; each can be changed later without rework:**
- Local Docker Compose first.
- Cloud is optional and provider-agnostic: a single VM, set up with Terraform in Milestone 7.
- Dashboard: Grafana for telemetry plus Streamlit for episodes, decisions and results.

---

## Key design decisions (settled in the Milestone 1 spec before any model code)
1. **Unit of analysis = every post-change episode, not only episodes a detector flagged.** Benign Adaptation covers expected deviations whether or not a detector fires. Detector outputs become *features* and *baselines*, not the input filter. Evaluation still reports results on the "detector-flagged" subset separately.
2. **Decision time vs. label horizon (avoids circular labels).** The model decides at time t_d after the change (e.g. +10 min) using only data up to t_d. Ground-truth labels come from (a) the injected scenario and its intended severity, and (b) the outcome over the full window (e.g. +30 min, plus a short extension), measured against frozen SLO rules. Labels therefore cannot simply be read back from the model's own inputs. Detection delay and lead time are measured by sweeping t_d.
3. **Draft label rules** (thresholds to be fixed after pilots, then frozen). The SLO sets a p95 latency target and an error-rate target.
   - CR: fast error-budget burn (e.g. burn rate ≥ 14.4) or error rate ≥ 5% or p99 > 3× target, sustained for ≥ 2 min.
   - AR: not CR, and the deviation is outside the prediction interval for ≥ P min, or recurs ≥ k times, with burn ≥ b₁ or affected-request fraction ≥ f₁.
   - TA: deviation is unexpected but shorter than P, recovered by the end of the window, and burn < b₁.
   - BA: the deviation is within the change-conditioned expectation, or its impact is negligible.
   - Ambiguous episodes are double-reviewed and disagreement is measured (Cohen's κ).
4. **Episode-level splits.** Splits are grouped by change identity and scenario seed; no row-level splitting. A leave-one-scenario-out split tests unseen change types. The test set is frozen before any tuning.
5. **Recommend only. No automatic rollback.** Evidence, decisions and actions are kept as separate records.

## Technology stack
| Concern | Choice |
|---|---|
| Language/env | Python 3.12 in a host venv managed by uv (`.venv`). Runs natively; Smart App Control must be off, because it blocks unsigned numpy/scipy wheels |
| Testbed services | FastAPI: `gateway`, `orders`, `inventory` + PostgreSQL |
| Workload | Small asyncio + httpx load generator (scripted profiles; records per-request latency/status as the user-facing SLI). Uses ~50 MB, versus Locust/gevent |
| Instrumentation (RAM-first) | Services expose `prometheus_client` /metrics, scraped by Prometheus at 5 s with short retention (no OTel Collector). Services write structured JSON logs to bind-mounted files under `data/raw/logs/` (no Loki). Traces are out of scope until RAM allows |
| Fault/change injection | Version tags + env/config/feature flags in the services; Toxiproxy for DB latency; `docker pause/stop` for unrelated infrastructure faults |
| Episode store | Parquet per episode + `episodes.jsonl` manifest; DuckDB for analysis |
| Log parsing | Drain3 templates (new-template counts, severity counts) |
| Expected-behavior model | Quantile regression (LightGBM quantile / statsmodels) on baseline + workload, with conformal calibration of intervals |
| Qualification | Rule-based policy vs. XGBoost/LightGBM classifier with isotonic calibration and a cost-sensitive action policy |
| Baselines | z-score/EWMA; IsolationForest; change-aware pre/post test; SCWarn-inspired multimodal; detector + persistence filter; MELODY-inspired (semi-supervised one-class on deployment features, CPU-sized) |
| Dashboard | Streamlit (episode explorer, telemetry plots, evidence, results). Grafana is an optional Compose profile, started only when needed |
| Experiments | Plain YAML configs + pydantic validation, CSV/JSON run records (MLflow optional extra), pytest |
| Cloud | One Ubuntu VM (free tier: Oracle Always Free Ampere, or GCP $300 trial) via console + cloud-init; parallel stacks; SSH push/pull (no Terraform) |

## Project structure
```
LogSentinel/
  docs/            spec/ (problem, literature, labels, dataset, eval-definitions), architecture.md, scenarios/
  testbed/         services/{gateway,orders,inventory}, db/, loadgen/, observability/ (prometheus, optional grafana), docker-compose.yml
  orchestrator/    change controller (applies changes, logs change events, Grafana annotations), scenario runner, fault injectors
  logsentinel/     collect/ (Prometheus client, log-file reader), episodes/ (builder, schema), features/, expected/, detectors/ (baselines), qualify/ (rules, model, policy), explain/
  experiments/     configs/, train.py, evaluate.py, ablations.py, reports/
  dashboard/       Streamlit app
  infra/           cloud-init + push/collect/pull scripts for a collection VM
  data/            raw/, episodes/, splits/   (gitignored)
  tests/
```

## Milestones (match brief §12 phases; status report after each one)
**M0 — Bootstrap (0.5 day).** `git init`, venv, `pyproject.toml`, pre-commit/ruff, README skeleton.

**M1 — Spec and literature (brief Phase 1–2).** Write `docs/spec/`. Includes the literature comparison: MELODY (arXiv 2401.10338) and SCWarn (FSE '21, DOI 10.1145/3468264.3468543), plus Gandalf (NSDI '20), Lumos (KDD '20), FUNNEL and HADES, compared at the method and evaluation level, with a hedged contribution claim. Also includes label rules, episode/telemetry schema, alert, missed-regression and unnecessary-action definitions, cost matrix, and split strategy. *Deliverable: frozen spec v0.*

**M2 — Testbed (brief Output 4).** Compose stack with services, Postgres, Locust, OTel Collector, Prometheus, Loki and Grafana, under a memory budget. Change controller CLI: `ls-change apply --scenario X --seed N` records structured change metadata. Implement the 8 scenarios from the brief, each with purpose, setup, expected behavior, impact criteria and labeling doc in `docs/scenarios/`.

**M3 — Collection and episode builder (Output 5).** Pull pre (30 min) and post (30 min) windows from the Prometheus and Loki APIs. Align at 15 s resolution and run Drain3. Write episode Parquet and the manifest, then apply the automatic labeler with an evidence record. Data-quality checks cover gaps, clock alignment and missing series.

**M4 — Minimal end-to-end (Phase 3).** Expected-behavior model, deviation features with uncertainty, persistence/recurrence/slope features, SLO and burn features, rule-based qualifier and action policy, explanation record. Streamlit v1 shows the episode timeline, deviations versus intervals, label, action and evidence. Run on about 10 pilot episodes. Use the pilots to choose window lengths and thresholds, then freeze them.

**M5 — Data generation (Phase 4).** Batch runner for about 100–200 episodes across scenarios × seeds × workload profiles. Run it locally overnight or move to the cloud VM in M7. Label audit and κ on ambiguous episodes. Create and freeze the grouped splits.

**M6 — Training and evaluation (Phase 5; Outputs 6–8).** Baselines, supervised qualifier, calibration, and action thresholds tuned on validation only. Metrics: false alerts per episode at fixed recall, harmful-regression precision and recall, detection delay, lead time before SLO violation, unnecessary-rollback rate, ECE/Brier, action-weighted utility with cost sensitivity, and leave-one-scenario-out results. Ablations: change context, logs, metrics, fusion, persistence, SLO, adaptive policy. Bootstrap CIs at episode level. Streamlit results page plus exported tables and plots. Negative results are reported as they come out.

**M7 — Cloud runner (built 2026-10-12, before M5, after a laptop pilot was stopped by low memory).** A provider-agnostic
Ubuntu VM (x86_64 or ARM64) is set up with cloud-init. `push.sh` and `pull.sh` move code up and data back over SSH,
and `collect.sh` runs N isolated stacks in parallel (`LS_STACK`, sharded batch). Terraform was dropped in favour of
console creation plus cloud-init, so free tiers (Oracle Always Free, GCP $300 trial) need no extra tooling.
See `infra/README.md`.

**M8 — Refine and document (Phase 6; Outputs 9–10).** Reproducibility guide, limitations, revised contribution claim backed by measured evidence, and a paper-ready methods and results section.

## Main research risks to track
- Labels end up circular with the features. Mitigated by decision time ≠ label horizon and by scenario-based ground truth.
- Testbed realism. Synthetic services may make the task too easy; mitigate with workload variety, noise and unrelated faults.
- Small n (100–200 episodes). Report wide CIs and avoid overclaiming.
- SCWarn and MELODY reproductions are not faithful copies. Disclose every deviation and make no head-to-head claims.
- Laptop resources. Keep the stack small and fall back to the cloud VM.

## Verification
- `pytest` unit tests: label rules on hand-made series, split leakage check (no shared change_id across splits), feature-window cutoff (no data after t_d).
- `docker compose up` followed by a smoke scenario confirms metrics appear in Prometheus, logs in Loki, and the change annotation in Grafana.
- `python -m orchestrator.run --scenario harmless_deploy --seed 1` produces one episode Parquet plus a label and evidence record, viewable in Streamlit (`streamlit run dashboard/app.py`).
- `python experiments/evaluate.py --config ...` reproduces comparison tables from a fixed seed.
