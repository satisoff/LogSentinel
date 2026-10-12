"""Expected model, decision-time features (incl. leakage), rule qualifier and action policy."""

import numpy as np
import pandas as pd
import pytest
from synthetic import SERVICES, make_run, post

from logsentinel.episodes.builder import build_episode
from logsentinel.episodes.store import load_view
from logsentinel.expected.baseline import ShiftPrior, WorkloadBaseline, conformal_quantile
from logsentinel.pipeline import decide, load_policy
from logsentinel.qualify.policy import choose_action

POLICY = load_policy()


def built(tmp_path, **kw):
    run = make_run(tmp_path, **kw)
    return build_episode(run, tmp_path / "episodes")["episode_id"], tmp_path / "episodes"


def decision(tmp_path, cutoff_min=15, **kw):
    ep, root = built(tmp_path, **kw)
    return decide(ep, cutoff_min * 60, root=root, return_features=True)


# --- expected model -----------------------------------------------------------------------------
def test_conformal_quantile_finite_sample_level():
    r = np.arange(1, 11, dtype=float)  # n=10, alpha=0.1 -> ceil(11*0.9)/10 = 1.0 -> max
    assert conformal_quantile(r, 0.1) == 10


def test_interval_coverage_on_stationary_noise():
    rng = np.random.default_rng(1)
    rate = 20 * (1 + 0.2 * np.sin(np.arange(240) / 20))
    y = 50 * (rate / 20) ** 0.3 * np.exp(rng.normal(0, 0.05, 240))
    m = WorkloadBaseline(alpha=0.1).fit(rate[:120], y[:120], "gw_latency_p95_ms")
    band = m.predict(rate[120:])
    covered = ((y[120:] >= band["lo"]) & (y[120:] <= band["hi"])).mean()
    assert 0.8 <= covered <= 1.0


def test_steady_baseline_uses_family_prior_for_load_scaling():
    rate = np.full(120, 20.0)
    cpu = np.full(120, 0.1) * np.exp(np.random.default_rng(0).normal(0, 0.02, 120))
    m = WorkloadBaseline().fit(rate, cpu, "svc_cpu")
    assert m.b == pytest.approx(1.0)  # no load variance -> prior elasticity
    assert m.predict(np.array([40.0]))["expected"].iloc[0] == pytest.approx(0.2, rel=0.1)


def test_shift_prior_fit_and_lookup():
    rows = pd.DataFrame({"change_type": ["feature_flag"] * 4, "service": ["inventory"] * 4,
                         "metric": ["svc_cpu"] * 4, "log_shift": [0.4, 0.5, 0.45, 0.55]})  # fmt: skip
    prior = ShiftPrior.fit(rows)
    shift, spread = prior.get("feature_flag", "inventory", "svc_cpu")
    assert shift == pytest.approx(0.475) and spread > 0
    assert prior.get("deploy", "orders", "svc_cpu") == (0.0, 0.0)


# --- leakage ------------------------------------------------------------------------------------
def test_features_do_not_depend_on_data_after_cutoff(tmp_path):
    ep, root = built(tmp_path)
    a = decide(ep, 600, root=root, return_features=True)[1].features
    s = pd.read_parquet(root / ep / "series.parquet")
    future = s["t"] > 600
    for col in s.columns:
        if col != "t":
            s.loc[future, col] = s.loc[future, col] * 50 + 999
    s.to_parquet(root / ep / "series.parquet", index=False)
    b = decide(ep, 600, root=root, return_features=True)[1].features
    assert a == b
    assert load_view(ep, 600, root).series["t"].max() == 600


# --- qualifier on scenario-shaped episodes --------------------------------------------------------
def test_quiet_deploy_is_ba_no_action(tmp_path):
    d, _ = decision(tmp_path)
    assert (d.label, d.action) == ("BA", "N")


def test_workload_increase_is_explained_by_load(tmp_path):
    d, fr = decision(tmp_path, rps_post=36.0, change={"change_type": "workload", "service": None})
    assert d.label == "BA", d.summary
    assert fr.features["wl_rate_ratio"] == pytest.approx(1.8)


def test_persistent_intended_step_without_impact_is_ba(tmp_path):
    def step_up(df):
        df.loc[post(df), "svc_cpu__orders"] *= 1.6  # deliberate extra work, flat afterwards

    d, fr = decision(tmp_path, mutate=step_up)
    assert fr.features["dev_chg_svc_cpu_frac_out"] > 0.8
    assert (d.label, d.rule) == ("BA", "persistent_deviation_no_impact")


def test_short_latency_spike_that_recovers_is_ta(tmp_path):
    def spike(df):
        df.loc[post(df, 3, 6), "gw_latency_p95_ms"] *= 2.5

    d, _ = decision(tmp_path, mutate=spike)
    assert (d.label, d.action) == ("TA", "O"), d.summary


def test_sustained_bad_requests_is_ar_investigate(tmp_path):
    def degrade(df):
        df.loc[post(df, 2), "bad"] = df.loc[post(df, 2), "requests"] * 0.05

    d, _ = decision(tmp_path, cutoff_min=10, mutate=degrade)
    assert (d.label, d.action) == ("AR", "I"), d.summary


def test_error_storm_is_cr_escalate(tmp_path):
    def storm(df):
        m = post(df, 1)
        df.loc[m, "errors_5xx"] = df.loc[m, "requests"] * 0.2
        df.loc[m, "bad"] = df.loc[m, "errors_5xx"]

    d, _ = decision(tmp_path, cutoff_min=5, mutate=storm)
    assert (d.label, d.action) == ("CR", "E"), d.summary


def test_memory_leak_trend_is_ar_before_impact(tmp_path):
    def leak(df):
        m = post(df)
        df.loc[m, "svc_rss_mb__orders"] = 60 + 4 * df.loc[m, "t"] / 60  # +4 MB/min

    d, fr = decision(tmp_path, cutoff_min=15, mutate=leak)
    assert fr.features["imp_material_min"] == 0
    assert d.label == "AR" and d.rule.startswith("deviation_worsening"), d.summary


def test_new_error_template_is_reported_in_evidence(tmp_path):
    logs = [{"t": 60 + 30 * i, "level": "ERROR", "msg": "DatabaseTimeout: query order_by_customer timed out",
             "exc_type": "DatabaseTimeout"} for i in range(5)]  # fmt: skip
    d, fr = decision(tmp_path, extra_logs=logs)
    assert fr.features["log_new_error_templates"] == 1
    assert "DatabaseTimeout" in d.summary


# --- action policy ------------------------------------------------------------------------------
def test_cost_policy_trades_off_mixed_beliefs():
    action, costs = choose_action({"BA": 0.7, "AR": 0.3}, POLICY)
    assert action == "I" and costs["I"] == pytest.approx(0.7)
    assert choose_action({"BA": 0.97, "AR": 0.03}, POLICY)[0] == "O"
    assert choose_action({"BA": 1.0}, POLICY)[0] == "N"


def test_all_services_present_in_features(tmp_path):
    _, fr = decision(tmp_path)
    assert {f"chg_svc_{s}" for s in SERVICES} <= set(fr.features)
