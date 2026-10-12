"""Transparent rule-based qualifier: BA / TA / AR / CR at decision time.

The rules mirror the label taxonomy but use only decision-time evidence: impact so far,
change-conditioned deviations, and their persistence and trend. Thresholds live in
experiments/configs/policy.yaml and are tuned on validation only.
"""

from __future__ import annotations

from dataclasses import dataclass

LABELS = ("BA", "TA", "AR", "CR")


@dataclass
class RuleDecision:
    label: str
    rule: str
    probs: dict[str, float]


def _decide(label: str, rule: str) -> RuleDecision:
    return RuleDecision(label, rule, {lab: float(lab == label) for lab in LABELS})


def _deviation_metrics(f: dict[str, float]) -> list[str]:
    return [
        k[len("dev_") : -len("_frac_out")]
        for k in f
        if k.startswith("dev_") and k.endswith("_frac_out")
    ]


def qualify(f: dict[str, float], rules: dict) -> RuleDecision:
    material_min = f.get("imp_material_min", 0.0)
    if f.get("imp_severe_min", 0.0) >= rules["cr_severe_min"]:
        return _decide("CR", "severe_impact_sustained")
    if material_min >= rules["ar_material_min"]:
        return _decide("AR", "material_impact_persistent")
    if f.get("imp_material_intervals", 0.0) >= rules["ar_recurrent_intervals"]:
        return _decide("AR", "material_impact_recurrent")
    worsening = f.get("imp_excess_burn_slope", 0.0) > 0
    if f.get("imp_material_now", 0.0) and worsening and material_min >= 1:
        return _decide("AR", "material_impact_worsening")

    # Deviation trend before any impact (e.g. a leak): out of range, persistent, and worsening.
    metrics = _deviation_metrics(f)
    trend = sorted(
        m
        for m in metrics
        if f[f"dev_{m}_frac_out"] >= rules["ar_trend_out_frac"]
        and f.get(f"per_{m}_slope", 0.0) >= rules["ar_trend_slope"]
        and f.get(f"per_{m}_slope_t", 0.0) >= rules["ar_trend_t"]
        and f.get(f"per_{m}_longest_out_min", 0.0) >= rules["ar_trend_longest_min"]
    )
    if trend:
        return _decide("AR", "deviation_worsening:" + ",".join(trend))

    deviating = f.get("per_max_longest_out_min", 0.0) > 0
    if not deviating and material_min == 0:
        return _decide("BA", "no_unexpected_deviation")
    if material_min > 0:
        return _decide("TA", "short_material_impact")
    if not any(f.get(f"dev_{m}_out_now", 0.0) for m in metrics):
        return _decide("TA", "deviation_recovered")
    rising = [
        m
        for m in metrics
        if f.get(f"dev_{m}_out_now", 0.0)
        and f.get(f"per_{m}_slope", 0.0) > rules["ba_flat_slope"]
        and f.get(f"per_{m}_slope_t", 0.0) >= rules["ba_trend_t"]
    ]
    if not rising:
        return _decide("BA", "persistent_deviation_no_impact")
    return _decide("TA", "deviation_ongoing_watch")
