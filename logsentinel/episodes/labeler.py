"""Deterministic ground-truth labeler for post-change episodes.

Implements docs/spec/labels.md. Unlike the systems under test, the labeler may use the scenario
oracle (declared intended effects) and data over the full label horizon H.

Input series contract (one row per aligned step):
    t          seconds relative to the change time t0 (negative = baseline window)
    requests   client requests in the step (including timeouts)
    bad        bad requests in the step (5xx, timeout, or latency > SLO)
    errors_5xx 5xx responses in the step
    <metric>   any metric listed in deviation_oracle.metrics (missing columns are skipped)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml
from pydantic import BaseModel

from logsentinel.ts import merge_runs, runs, sustained, trailing_ratio

LABELS = ("BA", "TA", "AR", "CR")


class Windows(BaseModel):
    step_s: int
    t_pre_min: float
    t_horizon_min: float


class Slo(BaseModel):
    latency_slo_ms: float
    target: float

    @property
    def budget(self) -> float:
        return 1.0 - self.target


class Impact(BaseModel):
    short_window_min: float
    burn_window_min: float
    b_crit: float
    err_window_min: float
    e_crit: float
    d_crit_min: float
    b_mat: float
    merge_gap_min: float


class Rules(BaseModel):
    p_mat_min: float
    k_rec: int
    k_rec_total_min: float
    tail_min: float
    tail_total_min: float
    recovery_min: float


class DeviationOracle(BaseModel):
    metrics: list[str]
    k_mad: float
    r_min: float
    min_sustain_min: float
    intended_tolerance: float


class LabelConfig(BaseModel):
    version: str
    frozen: bool
    windows: Windows
    slo: Slo
    impact: Impact
    rules: Rules
    deviation_oracle: DeviationOracle
    ambiguity_margin: float

    @classmethod
    def load(cls, path: str | Path) -> LabelConfig:
        with open(path, encoding="utf-8") as f:
            return cls.model_validate(yaml.safe_load(f))

    def steps(self, minutes: float) -> int:
        return max(1, int(round(minutes * 60 / self.windows.step_s)))


@dataclass
class Oracle:
    """Scenario ground truth; never visible to systems under test."""

    # metric -> relative shift r (exact, accepted within +-tolerance*|r|),
    #       or {"max_rel": x} (any deviation up to x*|baseline median| is intended)
    intended_effects: dict[str, float | dict[str, float]] = field(default_factory=dict)
    change_causal: bool | None = None


@dataclass
class LabelResult:
    label: str
    rule: str
    change_causal: bool | None
    explained: bool
    ambiguous: bool
    impact_onset_s: float | None
    slo_violation_s: float | None
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _near(value: float, threshold: float, margin: float) -> bool:
    return threshold != 0 and abs(value - threshold) <= margin * abs(threshold)


def label_episode(series: pd.DataFrame, oracle: Oracle, cfg: LabelConfig) -> LabelResult:
    df = series.sort_values("t").reset_index(drop=True)
    step = cfg.windows.step_s
    beta = cfg.slo.budget
    pre = (df["t"] < 0).to_numpy()
    horizon = ((df["t"] >= 0) & (df["t"] < cfg.windows.t_horizon_min * 60)).to_numpy()
    if not pre.any() or not horizon.any():
        raise ValueError("series must contain both baseline (t<0) and horizon (t>=0) steps")

    # --- Impact states (§3) ---------------------------------------------------------------
    # Multi-window, as in SRE burn-rate alerting: the long windows set severity, and the short
    # window requires the impact to still be happening, so a brief spike is not smeared over the
    # long window.
    short = cfg.steps(cfg.impact.short_window_min)
    burn = trailing_ratio(df["bad"], df["requests"], cfg.steps(cfg.impact.burn_window_min)) / beta
    err = trailing_ratio(df["errors_5xx"], df["requests"], cfg.steps(cfg.impact.err_window_min))
    burn_s = trailing_ratio(df["bad"], df["requests"], short) / beta
    err_s = trailing_ratio(df["errors_5xx"], df["requests"], short)
    frac_pre = df.loc[pre, "bad"].sum() / max(df.loc[pre, "requests"].sum(), 1)
    burn_pre = frac_pre / beta
    excess = burn_s - burn_pre

    long_hot = (np.nan_to_num(burn) >= cfg.impact.b_crit) | (
        np.nan_to_num(err) >= cfg.impact.e_crit
    )
    short_hot = (np.nan_to_num(burn_s) >= cfg.impact.b_crit) | (
        np.nan_to_num(err_s) >= cfg.impact.e_crit
    )
    severe = sustained(horizon & long_hot & short_hot, cfg.steps(cfg.impact.d_crit_min))
    material = horizon & (np.nan_to_num(excess) >= cfg.impact.b_mat)
    h_idx = np.flatnonzero(horizon)
    h0, h1 = h_idx[0], h_idx[-1] + 1
    intervals = merge_runs(runs(material[h0:h1]), cfg.steps(cfg.impact.merge_gap_min))
    material_steps = int(material.sum())
    material_min = material_steps * step / 60

    # --- Deviation oracle (§4) ------------------------------------------------------------
    dev = cfg.deviation_oracle
    unexplained = np.zeros(len(df), dtype=bool)
    explained_dev = np.zeros(len(df), dtype=bool)
    per_metric: dict[str, Any] = {}
    for metric in dev.metrics:
        if metric not in df.columns:
            continue
        values = df[metric].to_numpy(dtype=float)
        base = values[pre]
        if not np.isfinite(base).any():
            continue
        med = float(np.nanmedian(base))
        mad = float(np.nanmedian(np.abs(base - med)))
        thr = max(dev.k_mad * mad, dev.r_min * abs(med))
        # one-sided: all oracle metrics are higher-is-worse (an improvement is not a regression)
        raw_dev = horizon & (values - med > thr)
        effect = oracle.intended_effects.get(metric)
        if isinstance(effect, dict):
            covered = np.abs(values - med) <= max(thr, effect["max_rel"] * abs(med))
        elif effect is not None:
            center = med * (1 + effect)
            tol = max(thr, dev.intended_tolerance * abs(effect * med))
            covered = np.abs(values - center) <= tol
        else:
            covered = np.zeros(len(df), dtype=bool)
        deviant = sustained(raw_dev & ~covered, cfg.steps(dev.min_sustain_min))
        unexplained |= deviant
        explained_dev |= raw_dev & covered
        per_metric[metric] = {
            "median_pre": med,
            "mad_pre": mad,
            "threshold": thr,
            "unexplained_min": float(deviant.sum() * step / 60),
        }

    # --- Rules (§5) -----------------------------------------------------------------------
    r = cfg.rules
    severe_min = float(severe.sum() * step / 60)
    tail = horizon & (df["t"] >= (cfg.windows.t_horizon_min - r.tail_min) * 60).to_numpy()
    recovery = horizon & (df["t"] >= (cfg.windows.t_horizon_min - r.recovery_min) * 60).to_numpy()
    if severe.any():
        label, rule = "CR", "severe_sustained"
    elif material_min >= r.p_mat_min:
        label, rule = "AR", "material_persistent"
    elif len(intervals) >= r.k_rec and material_min >= r.k_rec_total_min:
        label, rule = "AR", "material_recurrent"
    elif (material & tail).any() and material_min > r.tail_total_min:
        label, rule = "AR", "material_unrecovered"
    elif material.any():
        label, rule = "TA", "material_short_lived"
    elif unexplained.any() and not (unexplained & recovery).any():
        label, rule = "TA", "unexpected_recovered"
    elif unexplained.any():
        label, rule = "BA", "unexplained_persistent_immaterial"
    else:
        label, rule = "BA", "expected_or_no_deviation"

    # --- Secondary attributes (§6) --------------------------------------------------------
    impact_steps = np.flatnonzero(material | severe)
    impact_onset = None
    if impact_steps.size:
        # Trailing windows lag the raw signal; walk back to the first raw degraded step.
        first = impact_steps[0]
        step_excess = (df["bad"] / df["requests"].replace(0, np.nan) - frac_pre) / beta
        lookback = np.arange(max(h0, first - short + 1), first + 1)
        raw_hits = lookback[np.nan_to_num(step_excess.to_numpy()[lookback]) >= cfg.impact.b_mat]
        impact_onset = float(df["t"].iloc[raw_hits[0] if raw_hits.size else first])
    burn_1h = trailing_ratio(df["bad"], df["requests"], cfg.steps(60)) / beta
    slo_steps = np.flatnonzero(horizon & (np.nan_to_num(burn_1h) >= 1.0))
    slo_violation = float(df["t"].iloc[slo_steps[0]]) if slo_steps.size else None

    m = cfg.ambiguity_margin
    max_burn = float(np.nanmax(np.where(horizon, burn, np.nan)))
    max_err = float(np.nanmax(np.where(horizon, err, np.nan)))
    max_excess = float(np.nanmax(np.where(horizon, excess, np.nan)))
    ambiguous = any(
        [
            _near(max_burn, cfg.impact.b_crit, m),
            _near(max_err, cfg.impact.e_crit, m),
            _near(max_excess, cfg.impact.b_mat, m),
            material_steps > 0 and _near(material_min, r.p_mat_min, m),
            label in ("BA", "TA")
            and bool(((material | unexplained) & tail).any()),  # truncated by H
        ]
    )

    evidence = {
        "config_version": cfg.version,
        "burn_pre": float(burn_pre),
        "max_burn_5m": max_burn,
        "max_burn_short": float(np.nanmax(np.where(horizon, burn_s, np.nan))),
        "max_err_2m": max_err,
        "max_excess_burn": max_excess,
        "severe_min": severe_min,
        "material_min": material_min,
        "material_intervals_s": [
            (float(df["t"].iloc[h0 + a]), float(df["t"].iloc[h0 + b - 1]) + step)
            for a, b in intervals
        ],
        "unexplained_deviation_min": float(unexplained.sum() * step / 60),
        "explained_deviation_min": float(explained_dev.sum() * step / 60),
        "metrics": per_metric,
    }
    return LabelResult(
        label=label,
        rule=rule,
        change_causal=oracle.change_causal,
        explained=not unexplained.any(),
        ambiguous=ambiguous,
        impact_onset_s=impact_onset,
        slo_violation_s=slo_violation,
        evidence=evidence,
    )
