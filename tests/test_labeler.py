from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from logsentinel.episodes.labeler import LabelConfig, Oracle, label_episode

CFG = LabelConfig.load(Path(__file__).parents[1] / "experiments/configs/labels.yaml")
STEP = CFG.windows.step_s
REQ = 40  # requests per 15 s step


def make_series(seed: int = 0, bad_frac: float = 0.002, latency: float = 100.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    t = np.arange(-30 * 60, 30 * 60, STEP)
    n = len(t)
    return pd.DataFrame(
        {
            "t": t,
            "requests": np.full(n, REQ),
            "bad": rng.binomial(REQ, bad_frac, n),
            "errors_5xx": np.zeros(n, dtype=int),
            "gw_latency_p95_ms": latency + rng.normal(0, 3, n),
        }
    )


def window(df: pd.DataFrame, start_min: float, end_min: float) -> np.ndarray:
    return ((df["t"] >= start_min * 60) & (df["t"] < end_min * 60)).to_numpy()


def test_quiet_episode_is_ba():
    res = label_episode(make_series(), Oracle(), CFG)
    assert res.label == "BA"
    assert res.explained
    assert res.impact_onset_s is None


def test_declared_latency_shift_is_explained_ba():
    df = make_series()
    df.loc[df["t"] >= 0, "gw_latency_p95_ms"] *= 1.30
    res = label_episode(df, Oracle(intended_effects={"gw_latency_p95_ms": 0.30}), CFG)
    assert (res.label, res.explained) == ("BA", True)


def test_declared_upper_bound_effect_is_explained_ba():
    df = make_series()
    df.loc[df["t"] >= 0, "gw_latency_p95_ms"] *= 1.30
    res = label_episode(df, Oracle(intended_effects={"gw_latency_p95_ms": {"max_rel": 0.5}}), CFG)
    assert (res.label, res.explained) == ("BA", True)


def test_deviation_beyond_declared_upper_bound_is_unexplained():
    df = make_series()
    df.loc[df["t"] >= 0, "gw_latency_p95_ms"] *= 1.80
    res = label_episode(df, Oracle(intended_effects={"gw_latency_p95_ms": {"max_rel": 0.5}}), CFG)
    assert res.explained is False


def test_undeclared_persistent_immaterial_shift_is_unexplained_ba():
    df = make_series()
    df.loc[df["t"] >= 0, "gw_latency_p95_ms"] *= 1.30
    res = label_episode(df, Oracle(), CFG)
    assert (res.label, res.rule, res.explained) == (
        "BA",
        "unexplained_persistent_immaterial",
        False,
    )


def test_short_unexpected_deviation_that_recovers_is_ta():
    df = make_series()
    df.loc[window(df, 3, 6), "gw_latency_p95_ms"] *= 1.8
    res = label_episode(df, Oracle(), CFG)
    assert (res.label, res.rule) == ("TA", "unexpected_recovered")


def test_short_material_burst_is_ta():
    df = make_series()
    df.loc[window(df, 4, 5), "bad"] = (
        REQ // 4
    )  # 25% bad for 1 min -> short-window burn ~25x, but < 2 min
    res = label_episode(df, Oracle(), CFG)
    assert res.label == "TA"
    assert res.impact_onset_s == pytest.approx(4 * 60)


def test_sustained_material_degradation_is_ar():
    df = make_series()
    df.loc[window(df, 5, 30), "bad"] = int(REQ * 0.05)  # 5% bad -> burn ~5, below crit
    res = label_episode(df, Oracle(change_causal=True), CFG)
    assert (res.label, res.rule) == ("AR", "material_persistent")
    assert res.change_causal is True


def test_recurrent_material_bursts_are_ar():
    df = make_series()
    for start in (2, 11, 20):
        df.loc[window(df, start, start + 1), "bad"] = REQ // 5  # 1-min bursts, 20% bad
    res = label_episode(df, Oracle(), CFG)
    assert (res.label, res.rule) == ("AR", "material_recurrent")


def test_severe_error_rate_is_cr():
    df = make_series()
    df.loc[window(df, 8, 14), ["bad", "errors_5xx"]] = int(REQ * 0.2)
    res = label_episode(df, Oracle(), CFG)
    assert (res.label, res.rule) == ("CR", "severe_sustained")
    assert res.slo_violation_s is not None


def test_severe_spike_shorter_than_d_crit_is_not_cr():
    df = make_series()
    df.loc[window(df, 8, 8.25), ["bad", "errors_5xx"]] = REQ  # 15 s total outage
    res = label_episode(df, Oracle(), CFG)
    assert res.label != "CR"


def test_impact_already_in_baseline_does_not_count_as_regression():
    df = make_series(bad_frac=0.03)  # noisy service: burn ~3 throughout, also before t0
    res = label_episode(df, Oracle(), CFG)
    assert res.label in ("BA", "TA")


def test_late_onset_flags_horizon_truncation_as_ambiguous():
    df = make_series()
    df.loc[window(df, 28, 30), "bad"] = REQ // 4
    res = label_episode(df, Oracle(), CFG)
    assert res.ambiguous


def test_labeler_is_deterministic():
    df = make_series(seed=7)
    df.loc[window(df, 5, 30), "bad"] = int(REQ * 0.04)
    assert label_episode(df, Oracle(), CFG).to_dict() == label_episode(df, Oracle(), CFG).to_dict()


def test_tiny_absolute_cpu_wiggle_is_not_a_deviation():
    df = make_series()
    df["svc_cpu"] = 0.05
    df.loc[window(df, 2, 5), "svc_cpu"] = 0.07  # +40% relative, but only +0.02 cores
    res = label_episode(df, Oracle(), CFG)
    assert res.explained and res.label == "BA"


def _crash_then(df, tail_rss):
    """Short material burst at 20-21 min (crash), with RSS behaviour afterwards given by tail_rss."""
    df["svc_rss_mb"] = 60.0
    df.loc[window(df, 0, 20), "svc_rss_mb"] = 60 + 7 * df.loc[window(df, 0, 20), "t"] / 60  # leak
    df.loc[window(df, 20, 21), "bad"] = REQ // 4  # OOM restart burst
    df.loc[window(df, 21, 30), "svc_rss_mb"] = tail_rss(df.loc[window(df, 21, 30), "t"])
    return df


def test_crash_with_cause_still_growing_is_ar():
    df = _crash_then(make_series(), lambda t: 60 + 7 * (t - 21 * 60) / 60)  # leaks again
    res = label_episode(df, Oracle(), CFG)
    assert (res.label, res.rule) == ("AR", "material_with_unresolved_deviation")


def test_crash_with_clean_recovery_is_ta():
    df = _crash_then(make_series(), lambda t: 60.0 + 0 * t)  # back to baseline
    res = label_episode(df, Oracle(), CFG)
    assert (res.label, res.rule) == ("TA", "material_short_lived")
