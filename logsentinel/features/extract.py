"""Decision-time features for one episode view (data <= t0 + cutoff only).

Feature groups (ablations remove a group by prefix):
  chg_   change context        dev_   change-conditioned deviation vs. expected interval
  per_   persistence/trend      imp_   service impact (SLO burn)
  log_   log evidence           wl_    workload

Every value is computed from `EpisodeView`, which is already truncated at the cutoff, so features
cannot see the future. tests/test_features.py checks this by perturbing post-cutoff data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from logsentinel.episodes.labeler import LabelConfig
from logsentinel.episodes.store import EpisodeView
from logsentinel.expected.baseline import ShiftPrior, expected_bands
from logsentinel.ts import merge_runs, runs, sustained, trailing_ratio

CHANGE_TYPES = ("deploy", "config", "feature_flag", "workload")
SERVICES = ("gateway", "orders", "inventory")
GROUPS = ("chg_", "dev_", "per_", "imp_", "log_", "wl_")


@dataclass
class FeatureResult:
    features: dict[str, float]
    bands: dict[str, pd.DataFrame] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)


def _slope_per_min(t: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """OLS slope per minute and its t-statistic (0, 0 when undefined)."""
    m = np.isfinite(y)
    if m.sum() < 4:
        return 0.0, 0.0
    tt, yy = t[m] / 60, y[m]
    tc = tt - tt.mean()
    sxx = float((tc**2).sum())
    if sxx == 0:
        return 0.0, 0.0
    slope = float((tc * (yy - yy.mean())).sum() / sxx)
    resid = yy - yy.mean() - slope * tc
    se = float(np.sqrt((resid**2).sum() / (len(yy) - 2) / sxx))
    return slope, (slope / se if se > 0 else 0.0)


def _deviation(band: pd.DataFrame, cfg: dict, step: int) -> dict[str, float]:
    post = band[band["t"] >= 0]
    if post.empty or post["expected"].isna().all():
        return {}
    obs, lo, hi, exp = (post[c].to_numpy(float) for c in ("observed", "lo", "hi", "expected"))
    half = np.maximum((hi - lo) / 2, 1e-9)
    z = (obs - exp) / half
    # One-sided: every key metric is higher-is-worse, so only upward deviations count.
    out = np.nan_to_num(obs > hi).astype(bool)
    out = sustained(out, max(1, int(cfg["min_out_min"] * 60 / step)))
    spans = merge_runs(runs(out), int(60 / step))
    recent = int(cfg["recent_min"] * 60 / step)
    t = post["t"].to_numpy(float)
    slope_win = t >= t[-1] - cfg["slope_min"] * 60
    slope, slope_t = _slope_per_min(t[slope_win], z[slope_win])
    return {
        "frac_out": float(out.mean()),
        "z_mean": float(np.nanmean(z)) if np.isfinite(z).any() else 0.0,
        "z_now": float(np.nanmean(z[-recent:])) if np.isfinite(z[-recent:]).any() else 0.0,
        "longest_out_min": max((e - s for s, e in spans), default=0) * step / 60,
        "n_excursions": float(len(spans)),
        "out_now": float(out[-recent:].any()),
        "recovered": float(bool(spans) and not out[-recent:].any()),
        "slope": slope,
        "slope_t": slope_t,
    }


def _hot(values: np.ndarray, threshold: float) -> np.ndarray:
    return np.nan_to_num(values) >= threshold


def _impact(series: pd.DataFrame, lcfg: LabelConfig, recent: int) -> dict[str, float]:
    step, beta = lcfg.windows.step_s, lcfg.slo.budget
    pre = (series["t"] < 0).to_numpy()
    post = ~pre
    short = lcfg.steps(lcfg.impact.short_window_min)
    burn5 = (
        trailing_ratio(series["bad"], series["requests"], lcfg.steps(lcfg.impact.burn_window_min))
        / beta
    )
    burn_s = trailing_ratio(series["bad"], series["requests"], short) / beta
    err2 = trailing_ratio(
        series["errors_5xx"], series["requests"], lcfg.steps(lcfg.impact.err_window_min)
    )
    err_s = trailing_ratio(series["errors_5xx"], series["requests"], short)
    frac_pre = series.loc[pre, "bad"].sum() / max(series.loc[pre, "requests"].sum(), 1)
    excess = burn_s - frac_pre / beta
    long_hot = _hot(burn5, lcfg.impact.b_crit) | _hot(err2, lcfg.impact.e_crit)
    short_hot = _hot(burn_s, lcfg.impact.b_crit) | _hot(err_s, lcfg.impact.e_crit)
    severe = post & long_hot & short_hot
    severe = sustained(severe, lcfg.steps(lcfg.impact.d_crit_min))
    material = post & (np.nan_to_num(excess) >= lcfg.impact.b_mat)
    spans = merge_runs(runs(material), lcfg.steps(lcfg.impact.merge_gap_min))
    p = series.loc[post]
    excess_bad = np.clip(p["bad"] - frac_pre * p["requests"], 0, None).sum()
    t_post = series["t"].to_numpy(float)[post]
    exc_post = np.nan_to_num(excess[post])
    last_material = t_post[material[post]].max() if material[post].any() else np.nan
    return {
        "burn_pre": float(frac_pre / beta),
        "burn_short_now": float(np.nan_to_num(burn_s[-1])),
        "burn_5m_now": float(np.nan_to_num(burn5[-1])),
        "burn_short_max": float(np.nanmax(np.where(post, burn_s, np.nan))) if post.any() else 0.0,
        "err_short_now": float(np.nan_to_num(err_s[-1])),
        "err_2m_max": float(np.nanmax(np.where(post, err2, np.nan))) if post.any() else 0.0,
        "excess_burn_now": float(np.nan_to_num(excess[-1])),
        "excess_burn_slope": _slope_per_min(t_post[-lcfg.steps(5) :], exc_post[-lcfg.steps(5) :])[
            0
        ],
        "material_min": float(material.sum() * step / 60),
        "severe_min": float(severe.sum() * step / 60),
        "material_intervals": float(len(spans)),
        "material_now": float(material[-recent:].any()),
        "min_since_material": float((t_post[-1] - last_material) / 60)
        if np.isfinite(last_material)
        else -1.0,
        "budget_frac": float(excess_bad / max(beta * p["requests"].sum(), 1e-9)),
        "affected_frac_now": float(np.nan_to_num(excess[-1]) * beta),
    }


def _logs(logs: pd.DataFrame, cutoff_s: float, pre_s: float) -> dict[str, float]:
    if logs.empty:
        return {k: 0.0 for k in ("err_ratio", "warn_ratio", "new_templates", "new_error_templates",
                                 "new_exc_types", "err_per_min")}  # fmt: skip
    pre, post = logs[logs["t"] < 0], logs[logs["t"] >= 0]
    post_min, pre_min = max(cutoff_s / 60, 0.25), max(pre_s / 60, 0.25)

    def rate(df, level, minutes):
        return (df["level"] == level).sum() / minutes

    new = post[post["new_template"]]
    pre_exc = set(pre["exc_type"].dropna())
    return {
        "err_ratio": float(
            (rate(post, "ERROR", post_min) + 0.1) / (rate(pre, "ERROR", pre_min) + 0.1)
        ),
        "warn_ratio": float(
            (rate(post, "WARNING", post_min) + 0.1) / (rate(pre, "WARNING", pre_min) + 0.1)
        ),
        "err_per_min": float(rate(post, "ERROR", post_min)),
        "new_templates": float(new["template_id"].nunique()),
        "new_error_templates": float(
            new.loc[new["level"].isin(["ERROR", "WARNING"]), "template_id"].nunique()
        ),
        "new_exc_types": float(len(set(post["exc_type"].dropna()) - pre_exc)),
    }


def extract(
    view: EpisodeView,
    policy: dict,
    label_cfg: LabelConfig,
    prior: ShiftPrior | None = None,
) -> FeatureResult:
    fcfg, step = policy["features"], label_cfg.windows.step_s
    s, change = view.series, view.change
    svc, ctype = change.get("service"), change.get("change_type")
    feats: dict[str, float] = {}

    # change context
    for ct in CHANGE_TYPES:
        feats[f"chg_type_{ct}"] = float(ctype == ct)
    for name in SERVICES:
        feats[f"chg_svc_{name}"] = float(svc == name)
    feats["chg_rollout_pct"] = float(change.get("rollout_pct") or 100.0)
    feats["chg_minutes_since"] = float(view.cutoff_s / 60)

    # deviation vs. expected interval (+ persistence/trend of the deviation)
    metrics = list(fcfg["key_metrics"])
    alias = {}
    if svc:
        for m in fcfg["changed_service_metrics"]:
            col = f"{m}__{svc}"
            if col in s:
                metrics.append(col)
                alias[col] = f"chg_{m}"
    bands = expected_bands(s, metrics, ctype, svc, prior, **policy["expected"])
    dev_summary = {}
    for metric, band in bands.items():
        d = _deviation(band, fcfg, step)
        if not d:
            continue
        key = alias.get(metric, metric)
        dev_summary[key] = d
        for k in ("frac_out", "z_mean", "z_now", "out_now"):
            feats[f"dev_{key}_{k}"] = d[k]
        for k in ("longest_out_min", "n_excursions", "recovered", "slope", "slope_t"):
            feats[f"per_{key}_{k}"] = d[k]
    if dev_summary:
        feats["dev_max_frac_out"] = max(d["frac_out"] for d in dev_summary.values())
        feats["dev_n_metrics_out"] = float(
            sum(d["longest_out_min"] > 0 for d in dev_summary.values())
        )
        feats["per_max_longest_out_min"] = max(d["longest_out_min"] for d in dev_summary.values())
        feats["per_max_slope"] = max(d["slope"] for d in dev_summary.values())

    # service impact
    recent = int(fcfg["recent_min"] * 60 / step)
    for k, v in _impact(s, label_cfg, recent).items():
        feats[f"imp_{k}"] = v

    # logs
    pre_s = float(-s["t"].min()) if len(s) else 0.0
    for k, v in _logs(view.logs, view.cutoff_s, pre_s).items():
        feats[f"log_{k}"] = v

    # workload
    pre_rate = s.loc[s["t"] < 0, "target_rps"].median()
    post_rate = s.loc[s["t"] >= 0, "target_rps"].median()
    feats["wl_rate_ratio"] = float(post_rate / pre_rate) if pre_rate else 1.0

    new_templates = []
    if not view.logs.empty:
        new = view.logs[view.logs["new_template"]]
        new_templates = (
            new.groupby("template")
            .agg(level=("level", "first"), n=("msg", "size"), first_t=("t", "min"))
            .sort_values("first_t").reset_index().head(5).to_dict("records")
        )  # fmt: skip
    evidence = {"deviations": dev_summary, "new_templates": new_templates}
    return FeatureResult(features=feats, bands=bands, evidence=evidence)
