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
infra/         optional cloud runner (Terraform, single VM)
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

## Milestones

See `docs/plan.md`. Summary: M0 bootstrap → M1 specification → M2 testbed → M3 episode builder →
M4 minimal end-to-end → M5 data generation → M6 training & evaluation → M7 optional cloud runner →
M8 refinement & documentation.
