"""LogSentinel decision pipeline: view -> features -> qualification -> action -> evidence."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from logsentinel.episodes.builder import CONFIG_DIR, EPISODE_DIR
from logsentinel.episodes.labeler import LabelConfig
from logsentinel.episodes.store import load_view
from logsentinel.expected.baseline import ShiftPrior
from logsentinel.features.extract import FeatureResult, extract
from logsentinel.qualify.policy import ACTION_NAMES, choose_action
from logsentinel.qualify.rules import qualify


def load_policy(path: Path = CONFIG_DIR / "policy.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


@dataclass
class Decision:
    episode_id: str
    cutoff_s: float
    label: str
    rule: str
    probs: dict[str, float]
    action: str
    action_costs: dict[str, float]
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def summarize(label: str, rule: str, action: str, f: dict[str, float], ev: dict) -> str:
    """One-paragraph, human-readable justification built from the recorded evidence."""
    parts = [f"{label} -> {ACTION_NAMES[action]} (rule: {rule})."]
    if f.get("imp_material_min", 0) > 0 or f.get("imp_severe_min", 0) > 0:
        parts.append(
            f"Impact: material {f['imp_material_min']:.1f} min, "
            f"severe {f['imp_severe_min']:.1f} min, "
            f"burn now {f['imp_burn_short_now']:.1f}x budget, "
            f"5xx max {100 * f['imp_err_2m_max']:.1f}%."
        )
    else:
        parts.append("No material SLO impact so far.")
    devs = sorted(ev.get("deviations", {}).items(), key=lambda kv: -kv[1]["frac_out"])
    devs = [(k, d) for k, d in devs if d["longest_out_min"] > 0][:3]
    if devs:
        items = [
            f"{k} {100 * d['frac_out']:.0f}% of post-change time (trend {d['slope']:+.2f} z/min)"
            for k, d in devs
        ]
        parts.append("Outside expected range: " + "; ".join(items) + ".")
    new = ev.get("new_templates", [])
    if new:
        first = new[0]
        parts.append(
            f"{len(new)} new log template(s), e.g. [{first['level']}] {first['template']}."
        )
    return " ".join(parts)


def decide(
    episode_id: str,
    cutoff_s: float,
    policy: dict | None = None,
    label_cfg: LabelConfig | None = None,
    prior: ShiftPrior | None = None,
    root: Path = EPISODE_DIR,
    return_features: bool = False,
) -> Decision | tuple[Decision, FeatureResult]:
    policy = policy or load_policy()
    label_cfg = label_cfg or LabelConfig.load(CONFIG_DIR / "labels.yaml")
    view = load_view(episode_id, cutoff_s, root)
    fr = extract(view, policy, label_cfg, prior)
    rd = qualify(fr.features, policy["rules"])
    action, costs = choose_action(rd.probs, policy)
    decision = Decision(
        episode_id=episode_id,
        cutoff_s=cutoff_s,
        label=rd.label,
        rule=rd.rule,
        probs=rd.probs,
        action=action,
        action_costs=costs,
        summary=summarize(rd.label, rd.rule, action, fr.features, fr.evidence),
        evidence=fr.evidence,
    )
    return (decision, fr) if return_features else decision


def decide_online(episode_id: str, root: Path = EPISODE_DIR, **kwargs) -> list[Decision]:
    """Decisions at each time in the policy grid (online protocol, evaluation.md section 2)."""
    policy = kwargs.pop("policy", None) or load_policy()
    return [
        decide(episode_id, minutes * 60, policy=policy, root=root, **kwargs)
        for minutes in policy["decision_times_min"]
    ]
