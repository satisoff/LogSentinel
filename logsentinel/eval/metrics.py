"""Evaluation metrics, frozen definitions v1 (docs/spec/evaluation.md).

A *system* yields one action per (episode, decision time) under the online protocol. Per episode:
  first alert   = earliest t_d with action Investigate or Escalate
  final action  = action at the first alert, else the action at the last decision time
  detected      = harmful (AR/CR) and first alert <= deadline
                  (AR: end of horizon; CR: impact onset + 10 min, or end of horizon without onset)
  false alert   = BA/TA episode with any alert
  unnecessary action   = final action stronger than the label's recommended action
  unnecessary rollback = final action E and (label != CR or change_causal is False)
  cost          = C[label, final action] (+2 if E and change_causal is False)
"""

from __future__ import annotations

import numpy as np
import pandas as pd

ACTION_RANK = {"N": 0, "O": 1, "I": 2, "E": 3}
RECOMMENDED = {"BA": "N", "TA": "O", "AR": "I", "CR": "E"}
HARMFUL = ("AR", "CR")
CR_DEADLINE_MIN = 10.0
INNOCENT_ROLLBACK_PENALTY = 2.0


def outcomes(
    episodes: pd.DataFrame, decisions: pd.DataFrame, costs: dict, actions: list[str]
) -> pd.DataFrame:
    """episodes: episode_id, change_id, scenario, label, change_causal, impact_onset_s,
    slo_violation_s, horizon_min. decisions: episode_id, cutoff_min, action."""
    d = decisions.sort_values(["episode_id", "cutoff_min"])
    col = {a: i for i, a in enumerate(actions)}
    rows = []
    for ep in episodes.itertuples(index=False):
        seq = d[(d["episode_id"] == ep.episode_id) & (d["cutoff_min"] <= ep.horizon_min)]
        alerts = seq[seq["action"].isin(["I", "E"])]
        first = float(alerts["cutoff_min"].iloc[0]) if len(alerts) else np.nan
        final = (
            alerts["action"].iloc[0]
            if len(alerts)
            else (seq["action"].iloc[-1] if len(seq) else "N")
        )
        onset = ep.impact_onset_s / 60 if pd.notna(ep.impact_onset_s) else np.nan
        harmful = ep.label in HARMFUL
        if ep.label == "CR" and np.isfinite(onset):
            deadline = onset + CR_DEADLINE_MIN
        else:
            deadline = ep.horizon_min
        detected = bool(harmful and np.isfinite(first) and first <= deadline)
        innocent = ep.change_causal is False or ep.change_causal == 0
        cost = costs[ep.label][col[final]] + (
            INNOCENT_ROLLBACK_PENALTY if final == "E" and innocent else 0.0
        )
        slo = ep.slo_violation_s / 60 if pd.notna(ep.slo_violation_s) else np.nan
        rows.append({
            "episode_id": ep.episode_id, "change_id": ep.change_id, "scenario": ep.scenario,
            "label": ep.label, "harmful": harmful, "alerted": bool(np.isfinite(first)),
            "first_alert_min": first, "final_action": final, "detected": detected,
            "missed": harmful and not detected,
            "false_alert": (not harmful) and bool(np.isfinite(first)),
            "unnecessary_action": ACTION_RANK[final] > ACTION_RANK[RECOMMENDED[ep.label]],
            "unnecessary_rollback": final == "E" and (ep.label != "CR" or innocent),
            "delay_min": first - onset if detected and np.isfinite(onset) else np.nan,
            "lead_min": slo - first if detected and np.isfinite(slo) else np.nan,
            "cost": cost,
        })  # fmt: skip
    return pd.DataFrame(rows)


def summarize(o: pd.DataFrame) -> dict[str, float]:
    harm, nonharm = o[o["harmful"]], o[~o["harmful"]]
    alerted = o[o["alerted"]]
    cr = o[o["label"] == "CR"]
    e_actions = o[o["final_action"] == "E"]
    delay = o["delay_min"].dropna()

    def ratio(a, b):
        return float(a) / b if b else float("nan")

    return {
        "n_episodes": len(o),
        "false_alerts_per_episode": ratio(o["false_alert"].sum(), len(o)),
        "false_alert_rate_nonharmful": ratio(nonharm["false_alert"].sum(), len(nonharm)),
        "alerts_per_episode": ratio(o["alerted"].sum(), len(o)),
        "harmful_recall": ratio(harm["detected"].sum(), len(harm)),
        "harmful_precision": ratio(alerted["harmful"].sum(), len(alerted)),
        "cr_recall": ratio(cr["detected"].sum(), len(cr)),
        "delay_median_min": float(delay.median()) if len(delay) else float("nan"),
        "delay_iqr_min": float(delay.quantile(0.75) - delay.quantile(0.25))
        if len(delay)
        else float("nan"),
        "lead_median_min": float(o["lead_min"].median())
        if o["lead_min"].notna().any()
        else float("nan"),
        "unnecessary_action_rate": ratio(o["unnecessary_action"].sum(), len(o)),
        "unnecessary_rollbacks_per_episode": ratio(o["unnecessary_rollback"].sum(), len(o)),
        "unnecessary_rollback_share_of_E": ratio(
            e_actions["unnecessary_rollback"].sum(), len(e_actions)
        ),
        "utility": -float(o["cost"].mean()) if len(o) else float("nan"),
    }


# --- score-based systems --------------------------------------------------------------------------
def threshold_decisions(scores: pd.DataFrame, threshold: float) -> pd.DataFrame:
    """scores: episode_id, cutoff_min, score -> alarm (I) when score >= threshold, else N."""
    out = scores[["episode_id", "cutoff_min"]].copy()
    out["action"] = np.where(scores["score"].to_numpy() >= threshold, "I", "N")
    return out


def select_threshold(
    scores: pd.DataFrame,
    episodes: pd.DataFrame,
    costs: dict,
    actions: list[str],
    recall: float = 0.95,
) -> tuple[float, bool]:
    """Largest threshold whose harmful recall on these (validation) episodes is >= `recall`.
    Returns (threshold, reached). If the target is unreachable: the threshold with max recall."""
    candidates = np.unique(scores["score"].to_numpy())[::-1]
    best, best_recall = candidates[-1] if len(candidates) else 0.0, -1.0
    for thr in candidates:
        r = summarize(outcomes(episodes, threshold_decisions(scores, thr), costs, actions))[
            "harmful_recall"
        ]
        if r >= recall:
            return float(thr), True
        if r > best_recall:
            best, best_recall = thr, r
    return float(best), False


def budget_threshold(scores: pd.DataFrame, episodes: pd.DataFrame, budget: float) -> float:
    """Smallest threshold whose alerts per episode stay within `budget`."""
    peak = (
        scores.groupby("episode_id")["score"].max().reindex(episodes["episode_id"]).fillna(-np.inf)
    )
    for thr in np.unique(peak.to_numpy()):
        if (peak >= thr).mean() <= budget:
            return float(thr)
    return float("inf")


# --- probability calibration ----------------------------------------------------------------------
def brier(probs: pd.DataFrame, labels: pd.Series) -> float:
    onehot = pd.get_dummies(labels).reindex(columns=probs.columns, fill_value=0).to_numpy(float)
    return float(np.mean(np.sum((probs.to_numpy(float) - onehot) ** 2, axis=1)))


def ece(probs: pd.DataFrame, labels: pd.Series, bins: int = 10) -> float:
    conf = probs.max(axis=1).to_numpy()
    correct = (probs.idxmax(axis=1).to_numpy() == labels.to_numpy()).astype(float)
    edges = np.linspace(0, 1, bins + 1)
    total = 0.0
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        m = (conf > lo) & (conf <= hi)
        if m.any():
            total += m.mean() * abs(correct[m].mean() - conf[m].mean())
    return float(total)
