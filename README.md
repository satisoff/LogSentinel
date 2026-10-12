# LogSentinel

Research prototype for **actionability-aware, change-conditioned qualification of post-change anomalies**.

After a software or configuration change, LogSentinel assigns each post-change episode one of four
operational classes and recommends a response:

| Class | Meaning | Recommended response |
|---|---|---|
| BA — Benign Adaptation | Deviation expected given the change, or negligible impact | No action |
| TA — Transient Anomaly | Unexpected but short-lived and operationally immaterial | Observe |
| AR — Actionable Regression | Persistent/recurring with material SLO degradation | Investigate |
| CR — Critical Regression | Crosses a predefined severe-impact threshold | Escalate / consider rollback (policy-gated) |

**Research question.** Does explicitly qualifying post-change deviations using change-conditioned
expectations, temporal persistence, and measurable service impact reduce unnecessary operational
actions without sacrificing timely detection of harmful regressions?

> Status: early development. No results have been measured yet. Nothing in this repository should
> be read as evidence that LogSentinel outperforms any existing method until the evaluation
> pipeline (milestone M6) has produced it.

## Repository layout

```
docs/          specification, literature comparison, architecture, scenario sheets
testbed/       controlled distributed application + observability stack (Docker Compose)
orchestrator/  change controller, scenario runner, fault injectors
logsentinel/   core library: collect, episodes, features, expected, detectors, qualify, explain
experiments/   configs, training/evaluation/ablation scripts, reports
dashboard/     Streamlit app (episode explorer, decisions, results)
infra/         cloud collection VM: cloud-init, push/collect/pull scripts (see infra/README.md)
data/          raw telemetry, episodes, frozen splits (gitignored)
tests/         unit tests
```

## Setup (Windows, PowerShell or Git Bash)

Requires Python 3.12 and Docker Desktop (Docker is needed only while the testbed collects data). [uv](https://docs.astral.sh/uv/) manages the interpreter. On Windows, Smart App Control must be off: it blocks the unsigned native wheels that numpy, scipy and others ship.

```bash
py -m pip install --user uv
py -m uv python install 3.12
py -m uv venv --python 3.12 .venv
py -m uv pip install --python .venv/Scripts/python.exe -e ".[dev,dashboard]"
.venv/Scripts/python.exe -m pytest
```

## Testbed quick start

Docker is needed only while collecting data. Stop other Docker stacks first: they compete for RAM
and CPU and add noise to measurements.

```bash
PY=.venv/Scripts/python.exe
$PY -m orchestrator.cli up                     # build + start (~350 MB in use, 1.1 GB cap)
$PY -m orchestrator.cli scenarios              # list S1-S9
$PY -m orchestrator.cli plan S5 --seed 3       # resolved plan, without running it
$PY -m orchestrator.cli run S9 --seed 0 --pre-min 3 --horizon-min 5 --warmup-min 2   # ~11 min smoke run
$PY -m orchestrator.cli run S5 --seed 3        # full episode (~65 min)
$PY -m orchestrator.cli down                   # stop and free RAM (volumes kept)
```

Each run writes `data/raw/<run_id>/`: `change.json` (visible to systems), `scenario.json` (oracle +
run record), `metrics.parquet`, `logs/<service>.jsonl`. Grafana is optional: `up --grafana` → :3000.

Long collections run better on a cloud VM (free tiers work): see `infra/README.md`.

## Episodes, decisions and dashboard

```bash
PY=.venv/Scripts/python.exe
$PY -m logsentinel.episodes.cli build           # raw runs -> episodes (+ labels, quality flags)
$PY -m logsentinel.episodes.cli summary         # scenario x label table, rejected episodes
$PY -c "from logsentinel.pipeline import decide_online as d; [print(x.cutoff_s/60, x.label, x.action, x.summary) for x in d('<episode_id>')]"
$PY -m streamlit run dashboard/app.py           # http://localhost:8501 (Docker not needed)
```

The decision pipeline is: expected-behaviour bands (per-episode workload model, conformal intervals,
optional change-conditioned shift prior) -> decision-time features (`chg_`, `dev_`, `per_`, `imp_`,
`log_`, `wl_` groups) -> rule-based qualifier -> cost-based action -> evidence summary. Configuration
lives in `experiments/configs/policy.yaml`. Nothing is tuned or frozen yet.

## Milestones

See `docs/plan.md`. Summary: M0 bootstrap → M1 specification → M2 testbed → M3 episode builder →
M4 minimal end-to-end → M5 data generation → M6 training & evaluation → M7 optional cloud runner →
M8 refinement & documentation.
