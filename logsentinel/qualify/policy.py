"""Cost-sensitive action policy: choose the action with the lowest expected operator cost.

    a* = argmin_a  sum_y  p(y) * C[y, a]

With a one-hot (rule-based) distribution this reduces to the recommended action of the class
(BA->N, TA->O, AR->I, CR->E), because the cost matrix diagonal is zero. With calibrated model
probabilities it trades false alerts against missed regressions according to the costs.
Recommendations only: no component executes remediation.
"""

from __future__ import annotations

import numpy as np

from logsentinel.qualify.rules import LABELS

ACTION_NAMES = {
    "N": "no action",
    "O": "observe",
    "I": "investigate",
    "E": "escalate / consider rollback",
}


def expected_costs(probs: dict[str, float], policy: dict) -> dict[str, float]:
    p = np.array([probs.get(lab, 0.0) for lab in LABELS])
    c = np.array([policy["costs"][lab] for lab in LABELS], dtype=float)
    return dict(zip(policy["actions"], (p @ c).tolist(), strict=True))


def choose_action(probs: dict[str, float], policy: dict) -> tuple[str, dict[str, float]]:
    costs = expected_costs(probs, policy)
    order = {a: i for i, a in enumerate(policy["actions"])}
    action = min(costs, key=lambda a: (round(costs[a], 12), order[a]))  # ties -> weaker action
    return action, costs
