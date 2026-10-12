"""Episode builder tests on synthetic raw runs (no Docker)."""

import json
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from logsentinel.episodes.builder import build_episode, write_manifest

T0 = 1_791_780_000.0
STEP = 15
SERVICES = ("gateway", "orders", "inventory")


def _iso(ts):
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="milliseconds")


def make_raw_run(tmp_path, *, name="run-test-S5-s0", bad_post=0.0, target=20.0, load_factor=1.0,
                 drop_steps=(), versions_after="1.2.0", restart_at=None, pre_bad=0.002,
                 pre_min=30, horizon_min=30):  # fmt: skip
    run = tmp_path / "raw" / name
    (run / "logs").mkdir(parents=True)
    rng = np.random.default_rng(0)
    rows = []
    ts_grid = T0 + np.arange(-pre_min * 60, horizon_min * 60 + STEP, STEP)
    for ts in ts_grid:
        t = ts - T0
        if int(t) in drop_steps:
            continue
        req = target * STEP * load_factor
        bad = req * (bad_post if t >= 300 else pre_bad)
        rows += [
            (ts, "requests", None, "{}", req),
            (ts, "bad", None, "{}", bad),
            (ts, "errors_5xx", None, "{}", 0.0),
            (ts, "target_rps", None, "{}", target),
            (ts, "gw_latency_p95_ms", None, "{}", 50 + rng.normal(0, 1)),
        ]
        for svc in SERVICES:
            rows.append((ts, "svc_cpu", svc, "{}", 0.1 + rng.normal(0, 0.005)))
            start = 1000.0 if restart_at is None or t < restart_at else 2000.0
            rows.append((ts, "process_start", svc, "{}", start if svc == "orders" else 1000.0))
            version = versions_after if (svc == "orders" and t >= 0) else "1.0.0"
            rows.append((ts, "version", svc, json.dumps({"version": version}), 1.0))
    pd.DataFrame(rows, columns=["ts", "name", "service", "labels", "value"]).to_parquet(
        run / "metrics.parquet", index=False
    )
    lines = []
    for t in range(-pre_min * 60, horizon_min * 60, 10):
        lines.append({"ts": _iso(T0 + t), "service": "orders", "level": "INFO",
                      "msg": f"GET /orders/{{order_id}} -> 200 in {5 + t % 7}.3 ms"})  # fmt: skip
        if t >= 120 and t % 60 == 0:
            lines.append({"ts": _iso(T0 + t), "service": "orders", "level": "ERROR",
                          "msg": f"DatabaseTimeout: database query order_by_customer timed out {t}",
                          "exc_type": "DatabaseTimeout"})  # fmt: skip
    (run / "logs" / "orders.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    change = {"change_id": "S5-abc", "run_id": name, "t0": T0, "service": "orders",
              "change_type": "deploy", "from_version": "1.0.0", "to_version": "1.2.0"}  # fmt: skip
    scenario = {"scenario": "S5", "name": "inefficient_query_deploy", "seed": 0, "variant_index": 0,
                "change": {"change_type": "deploy"}, "workload": {"rps": target, "profile": "steady"},
                "oracle": {"intended_effects": {}, "change_causal": True},
                "pre_min": pre_min, "horizon_min": horizon_min, "host_context": {"before": [], "after": []},
                "latency_slo_ms": 50.0}  # fmt: skip
    (run / "change.json").write_text(json.dumps(change))
    (run / "scenario.json").write_text(json.dumps(scenario))
    return run


@pytest.fixture
def out(tmp_path):
    return tmp_path / "episodes"


def test_clean_regression_episode(tmp_path, out):
    meta = build_episode(make_raw_run(tmp_path, bad_post=0.05), out)
    assert meta["status"] == "ok" and meta["flags"] == []
    assert (meta["label"], meta["label_rule"]) == ("AR", "material_persistent")
    s = pd.read_parquet(out / meta["episode_id"] / "series.parquet")
    assert (
        s["t"].iloc[0] == -1800
        and s["t"].iloc[-1] == 1800
        and (s["t"].diff().dropna() == STEP).all()
    )
    assert {"svc_cpu__orders", "svc_cpu", "gw_error_rate"} <= set(s.columns)


def test_new_template_only_after_t0(tmp_path, out):
    meta = build_episode(make_raw_run(tmp_path), out)
    logs = pd.read_parquet(out / meta["episode_id"] / "logs.parquet")
    new = logs[logs["new_template"]]
    assert len(new) and (new["t"] >= 0).all() and new["msg"].str.startswith("DatabaseTimeout").all()
    assert not logs.loc[logs["msg"].str.startswith("GET"), "new_template"].any()
    assert meta["n_new_templates"] == 1


def test_missing_steps_are_kept_as_nan_and_flagged(tmp_path, out):
    drops = tuple(range(-600, -300, STEP)) + tuple(range(0, 75, STEP))  # 20 + 5 steps
    meta = build_episode(make_raw_run(tmp_path, drop_steps=drops), out)
    s = pd.read_parquet(out / meta["episode_id"] / "series.parquet")
    assert s["requests"].isna().sum() == len(drops)
    assert {"missing_data", "gap_near_t0"} <= set(meta["flags"]) and meta["status"] == "rejected"


def test_load_mismatch_and_unapplied_change_are_rejected(tmp_path, out):
    meta = build_episode(make_raw_run(tmp_path, load_factor=0.7, versions_after="1.0.0"), out)
    assert {"load_unstable", "change_not_applied"} <= set(meta["reject_reasons"])


def test_contaminated_baseline_is_rejected(tmp_path, out):
    meta = build_episode(make_raw_run(tmp_path, pre_bad=0.05), out)
    assert "baseline_contaminated" in meta["reject_reasons"]


def test_restart_is_counted_but_not_rejected(tmp_path, out):
    meta = build_episode(make_raw_run(tmp_path, restart_at=600), out)
    assert meta["quality"]["restarts"]["orders"] == 1
    assert "restarts" in meta["flags"] and meta["status"] == "ok"


def test_nonstandard_windows_flagged(tmp_path, out):
    meta = build_episode(make_raw_run(tmp_path, pre_min=3, horizon_min=5), out)
    assert "nonstandard_windows" in meta["flags"] and meta["status"] == "ok"


def test_build_is_cached_unless_forced_and_manifest_lists_all(tmp_path, out):
    run = make_raw_run(tmp_path)
    first = build_episode(run, out)
    (out / first["episode_id"] / "meta.json").write_text(json.dumps({**first, "label": "XX"}))
    assert build_episode(run, out)["label"] == "XX"
    assert build_episode(run, out, force=True)["label"] == first["label"]
    build_episode(make_raw_run(tmp_path, name="run-test-S5-s1"), out)
    lines = write_manifest(out).read_text().splitlines()
    assert [json.loads(x)["episode_id"] for x in lines] == ["run-test-S5-s0", "run-test-S5-s1"]


def test_view_enforces_cutoff_and_hides_oracle(tmp_path, out):
    from logsentinel.episodes.store import load_label, load_view

    meta = build_episode(make_raw_run(tmp_path, bad_post=0.05), out)
    view = load_view(meta["episode_id"], cutoff_s=600, root=out)
    assert view.series["t"].max() == 600 and view.logs["t"].max() <= 600
    assert view.series["t"].min() == -1800
    forbidden = {"label", "oracle", "change_causal", "intended_effects", "scenario", "run_id"}
    assert forbidden.isdisjoint(view.change)
    assert load_label(meta["episode_id"], root=out)["label"] == "AR"


def test_absent_counter_series_are_true_zeros_where_traffic_observed(tmp_path, out):
    run = make_raw_run(tmp_path)
    m = pd.read_parquet(run / "metrics.parquet")
    m[m["name"] != "bad"].to_parquet(run / "metrics.parquet", index=False)  # never incremented
    meta = build_episode(run, out)
    s = pd.read_parquet(out / meta["episode_id"] / "series.parquet")
    assert (s["bad"] == 0).all() and meta["status"] == "ok"


def test_run_collected_with_other_slo_is_rejected(tmp_path, out):
    run = make_raw_run(tmp_path)
    sc = json.loads((run / "scenario.json").read_text())
    del sc["latency_slo_ms"]  # legacy run: counted "bad" at the old 300 ms default
    (run / "scenario.json").write_text(json.dumps(sc))
    meta = build_episode(run, out)
    assert "slo_mismatch" in meta["reject_reasons"]
    assert meta["quality"]["latency_slo_ms"] == {"run": 300.0, "labels": 50.0}
