# Literature Comparison — v0 (in progress)

**Verification status.** Entries marked ✔ were checked against the paper's abstract or landing page on
2026-10-12. Entries marked ◻ are summarised from memory and **must be checked against the full text**
before any statement is used in the paper.

## 1. Primary competitors

### MELODY ✔ (abstract level)
*MELODY: Robust Semi-Supervised Hybrid Model for Entity-Level Online Anomaly Detection with
Multivariate Time Series.* arXiv:2401.10338 (AWS AI Labs).

- **Task:** detect anomalous *deployments* (entities) from multivariate telemetry.
- **Method:** an online feature extractor maps the multivariate time series of different entities into
  a shared feature space, followed by a semi-supervised deep one-class model.
- **Labels:** rule-based auto-labelling marks deployments that passed safety checks as normal. Humans
  score the rest on a −1..3 scale (−1 = unsure).
- **Operational loop:** anomaly score + threshold + safety rules feed a rollback decision service.
- **Evaluation:** cloud-service data with more than 1.2M time series. Reports relative F1 gains of
  7.6–56.5% over prior methods.
- **Data/code:** not publicly available to us ◻ (verify).

### SCWarn ✔ (abstract level)
Zhao, Chen, et al. *Identifying Bad Software Changes via Multimodal Anomaly Detection for Online
Service Systems.* ESEC/FSE 2021. DOI 10.1145/3468264.3468543.

- **Task:** binary bad-change identification after a software change.
- **Method:** multimodal learning over heterogeneous change-related data (metrics and logs), with
  interpretable alerts.
- **Evaluation:** two datasets. Reports average F1 0.95 and a 20.4–60.7% reduction in mean time to detect.
- **Known critique:** counts unmatched new log templates, which can miss log semantics (noted by
  arXiv:2507.09315).
- **Code:** check for a public implementation ◻. If one exists, prefer running it over reimplementing.

## 2. Related work to cover ◻ (verify all)

| Work | Relevance |
|---|---|
| Gandalf (Microsoft Azure, NSDI 2020) | Safe deployment: correlates failure signals with rollouts across space and time to stop bad rollouts. Closest on "is this change to blame" |
| Lumos (Microsoft, KDD 2020) | Diagnosing metric regressions in web-scale applications with pre/post comparison controlling for population shifts. Change-conditioned expectation |
| FUNNEL (IWQoS 2015; extended in IEEE TSC) | Assessing software changes' impact on KPIs, using a difference-in-differences style comparison against control groups |
| HADES (arXiv:2207.02918 ✔ title) | Heterogeneous (log + metric) anomaly detection for software systems via attentive multimodal learning; compares against SCWarn |
| Google SRE Workbook, "Alerting on SLOs" | Multi-window multi-burn-rate alerting (e.g. 14.4× fast burn). Our SLO impact features and severe threshold adopt this convention |
| Persistence / `for:`-style suppression (e.g. Prometheus alert `for` clause) | Persistence-based alert suppression baseline |
| Cost-sensitive / alert-fatigue work in AIOps (to survey) | Action-cost framing |

## 3. Method-level comparison (to complete after full-text reading)

| Dimension | MELODY | SCWarn | Persistence filter | LogSentinel (proposed) |
|---|---|---|---|---|
| Output | anomaly score → rollback decision | bad/not-bad change | alert / suppressed | 4-class qualification + action + evidence |
| Change context | deployment = entity | change-triggered | no | change metadata conditions the expected behaviour |
| Expected-behaviour model with uncertainty | ◻ | ◻ | no | quantile/conformal intervals |
| Persistence / recovery | ◻ (online windows) | ◻ | yes (fixed) | learned features (duration, recurrence, slope, recovery) |
| Service impact (SLO/burn) | ◻ safety rules | ◻ | no | explicit features + label definition |
| Cost-sensitive action policy | threshold + rules | ◻ | no | explicit cost matrix, calibrated thresholds |
| Evaluation metric | F1 | F1, MTTD | — | false alerts at fixed harmful recall, delay, utility |

## 4. What is genuinely different (provisional)

1. **Output space:** graded operational classes with distinct actions, versus a binary bad/anomalous
   decision. Need to confirm neither paper does graded severity.
2. **Evaluation target:** unnecessary actions at matched recall plus cost sensitivity, versus F1.
3. **Ground-truth separation:** labels defined over a horizon beyond the decision time.

**Already covered by prior work, so not claimable:** using change context; multimodal fusion; deployment-level
detection; persistence filtering; SLO burn alerting; rollback decision services.

**Evidence needed to justify the contribution:** H1–H3 in `problem.md` must hold with paired CIs
excluding zero against the strongest baseline. Otherwise the contribution reduces to the benchmark
and the negative or neutral finding.

## Sources checked
- https://arxiv.org/abs/2401.10338
- https://2021.esec-fse.org/details/fse-2021-papers/97/Identifying-Bad-Software-Changes-via-Multimodal-Anomaly-Detection-for-Online-Service-
- https://arxiv.org/pdf/2207.02918
- https://arxiv.org/pdf/2507.09315
