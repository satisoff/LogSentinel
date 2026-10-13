# Problem Statement, Scope and Hypothesis — v0

## 1. Problem

Anomaly detectors flag behaviour that deviates from a learned baseline. After a change, such as a
deployment, config, feature flag, dependency, infrastructure or workload change, many deviations are
expected, transient or immaterial. Treating every deviation as alert-worthy creates unnecessary
operational actions. Suppressing alerts near changes would hide exactly the regressions that changes
cause. **Detection** ("is this unusual?") and **qualification** ("does this warrant action, and which?")
are different decision tasks.

## 2. Task definition

Given a change event `c`, pre-change telemetry `W_pre`, and post-change telemetry up to decision time
`t_d`, output:
1. a class `y ∈ {BA, TA, AR, CR}` with calibrated probabilities;
2. an action `a ∈ {N, O, I, E}` chosen by a cost-sensitive policy;
3. an evidence record (deviations vs. expectation, persistence, impact) supporting the decision.

Ground truth is defined in `labels.md`; evaluation in `evaluation.md`.

## 3. System boundaries

**In scope:** a controlled testbed (gateway + 2 services + PostgreSQL), metrics and logs, single
changes per episode, recommendation of actions.

**Out of scope (v1):** automatic remediation/rollback, distributed traces, root-cause localisation
beyond the changed service, overlapping concurrent changes, production data.

## 4. Hypothesis (what we test, not what we claim)

H1. On held-out changes, a qualifier that jointly uses (i) change-conditioned deviation with
uncertainty, (ii) temporal persistence/recurrence/recovery and (iii) measured service impact produces
**fewer false alerts per episode at matched harmful-regression recall** than: context-blind detectors,
a change-aware detector, a persistence filter, and SCWarn-/MELODY-inspired baselines. It does so
**without materially worse detection delay**: the median detection delay is at most 5 min (one decision
step) worse than the best baseline's, paired on the same episodes (evaluation.md v1).

H2. Each of the three elements contributes incrementally (ablations).

H3. The advantage holds under leave-one-scenario-out and held-out-workload splits.

**Negative results are first-class outcomes.** If H1 fails, the paper reports that.

## 5. Candidate contribution (hedged)

We do **not** claim to be the first to use change context, multimodal telemetry, deployment-level
detection, persistence, SLO-aware alerting or rollback decisions; prior work covers each (see
`literature.md`). The candidate contribution is narrower:

> An explicit formulation and evaluation of *post-change operational qualification* as a joint,
> cost-sensitive decision over change-conditioned deviation, persistence and measured service impact.
> It comes with a labelled, reproducible benchmark of controlled change episodes, where ground truth
> is separated from the decision-time information.

Whether this formulation adds value beyond existing change-aware detectors plus persistence filters
is an empirical question. The contribution statement will be revised in M8 to match the evidence.

## 6. Key assumptions

- The SLO and impact thresholds in `labels.md` are a reasonable proxy for "operationally significant".
- A small controlled testbed exercises the decision problem meaningfully. Realism is a stated limitation.
- Scenario ground truth (declared intended effects, injected faults) is known to the experimenter only.

## 7. Research risks

| Risk | Consequence | Mitigation |
|---|---|---|
| Circular labels (labels computed from the same SLO signals the model sees) | Inflated results | Decision time < label horizon; headline at t_d ≤ 10 min; scenario-based oracle for "unexpected"; report the t_d sweep |
| Testbed too easy / unrealistic | Results don't transfer | Workload variety and noise, unrelated faults, canary variants, LOSO split; discuss threats to validity |
| Small n (100–200) | Wide CIs, unstable ranking | Paired bootstrap, pre-specified headline comparison, honest CIs |
| Baseline reproduction fidelity | Unfair comparison | "Inspired by" labelling, documented deviations, tune all baselines with the same budget on validation |
| Label threshold choices drive conclusions | Fragile claims | Sensitivity on thresholds, windows and costs |
| Collection time (≈ 63 min/episode) | Schedule slip | Pilot shorter windows, optional parallel cloud stacks |
| Laptop RAM (7.4 GB) | Instability during collection | RAM-first stack (≤ 1 GB containers), WSL memory cap, Docker only during collection |
