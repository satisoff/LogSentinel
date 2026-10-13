"""Frozen v1 evaluation definitions on small hand-checkable cases."""

import numpy as np
import pandas as pd
import pytest

from logsentinel.eval.bootstrap import cluster_bootstrap
from logsentinel.eval.metrics import (
    brier,
    budget_threshold,
    ece,
    outcomes,
    select_threshold,
    summarize,
    threshold_decisions,
)
from logsentinel.pipeline import load_policy

POLICY = load_policy()
COSTS, ACTIONS = POLICY["costs"], POLICY["actions"]
GRID = [2, 5, 10, 15, 20, 30]


@pytest.fixture
def episodes():
    return pd.DataFrame([
        {"episode_id": "ba", "change_id": "c1", "scenario": "S1", "label": "BA", "change_causal": None, "impact_onset_s": np.nan, "slo_violation_s": np.nan, "horizon_min": 30},
        {"episode_id": "ta", "change_id": "c2", "scenario": "S4", "label": "TA", "change_causal": False, "impact_onset_s": 360.0, "slo_violation_s": np.nan, "horizon_min": 30},
        {"episode_id": "ar", "change_id": "c3", "scenario": "S5", "label": "AR", "change_causal": True, "impact_onset_s": 60.0, "slo_violation_s": 900.0, "horizon_min": 30},
        {"episode_id": "cr", "change_id": "c4", "scenario": "S7", "label": "CR", "change_causal": False, "impact_onset_s": 420.0, "slo_violation_s": 600.0, "horizon_min": 30},
    ])  # fmt: skip


def plan(per_episode: dict[str, list[str]]) -> pd.DataFrame:
    return pd.DataFrame([{"episode_id": e, "cutoff_min": t, "action": a}
                         for e, acts in per_episode.items() for t, a in zip(GRID, acts, strict=True)])  # fmt: skip


def test_perfect_system(episodes):
    d = plan(
        {
            "ba": ["N"] * 6,
            "ta": ["N", "N", "O", "O", "O", "O"],
            "ar": ["N", "I", "I", "I", "I", "I"],
            "cr": ["N", "N", "E", "E", "E", "E"],
        }
    )
    o = outcomes(episodes, d, COSTS, ACTIONS)
    s = summarize(o)
    assert s["harmful_recall"] == 1 and s["false_alerts_per_episode"] == 0 and s["cr_recall"] == 1
    # CR is change_causal=False: escalating it still counts as an (innocent-change) unnecessary rollback
    assert s["unnecessary_rollbacks_per_episode"] == 0.25
    assert o.set_index("episode_id").loc["ar", "delay_min"] == pytest.approx(
        4.0
    )  # alert 5 - onset 1
    assert o.set_index("episode_id").loc["cr", "lead_min"] == pytest.approx(
        0.0
    )  # SLO violated at 10
    assert s["utility"] == pytest.approx(-2.0 / 4)


def test_always_escalate(episodes):
    o = outcomes(episodes, plan({e: ["E"] * 6 for e in ("ba", "ta", "ar", "cr")}), COSTS, ACTIONS)
    s = summarize(o)
    assert s["false_alerts_per_episode"] == 0.5 and s["harmful_recall"] == 1
    assert s["unnecessary_action_rate"] == 0.75  # BA, TA, AR over-actioned
    assert s["unnecessary_rollbacks_per_episode"] == 1.0  # BA, TA, AR not CR; CR innocent


def test_cr_alert_after_deadline_is_missed(episodes):
    d = plan(
        {"ba": ["N"] * 6, "ta": ["N"] * 6, "ar": ["I"] * 6, "cr": ["N", "N", "N", "N", "N", "E"]}
    )
    o = outcomes(episodes, d, COSTS, ACTIONS).set_index("episode_id")
    assert o.loc["cr", "missed"] and not o.loc["cr", "detected"]  # onset 7 + 10 < alert 30


def test_threshold_selection_reaches_recall_with_fewest_alerts(episodes):
    scores = pd.DataFrame([{"episode_id": e, "cutoff_min": t, "score": s}
                           for e, s in {"ba": 1.0, "ta": 3.0, "ar": 5.0, "cr": 4.0}.items() for t in GRID])  # fmt: skip
    thr, ok = select_threshold(scores, episodes, COSTS, ACTIONS, recall=0.95)
    assert ok and thr == 4.0
    s = summarize(outcomes(episodes, threshold_decisions(scores, thr), COSTS, ACTIONS))
    assert s["false_alerts_per_episode"] == 0
    assert budget_threshold(scores, episodes, budget=0.5) == 4.0


def test_calibration_measures():
    probs = pd.DataFrame({"BA": [1.0, 0.0], "TA": [0.0, 0.0], "AR": [0.0, 1.0], "CR": [0.0, 0.0]})
    assert brier(probs, pd.Series(["BA", "AR"])) == 0.0
    assert ece(probs, pd.Series(["BA", "AR"])) == 0.0
    assert brier(probs, pd.Series(["AR", "BA"])) == 2.0


def test_cluster_bootstrap_ci_and_paired_difference(episodes):
    d = plan({"ba": ["N"] * 6, "ta": ["O"] * 6, "ar": ["I"] * 6, "cr": ["E"] * 6})
    o = outcomes(episodes, d, COSTS, ACTIONS)
    res = cluster_bootstrap({"a": o, "b": o.copy()}, summarize, n=200, seed=1, reference="a")
    row = res[(res.system == "a") & (res.metric == "harmful_recall")].iloc[0]
    assert row.lo <= row.estimate <= row.hi
    diff = res[(res.system == "b") & (res.metric == "diff:utility")].iloc[0]
    assert diff.estimate == 0 and diff.lo == 0 and diff.hi == 0
