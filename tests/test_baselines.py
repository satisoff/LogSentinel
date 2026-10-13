"""Baseline detectors on synthetic episodes: they must separate quiet from degraded, and respect cutoffs."""

import pandas as pd
import pytest
from synthetic import make_run, post

from logsentinel.detectors.baselines import (
    ChangeAwareTest,
    IsolationForestDetector,
    MelodyInspired,
    PersistenceFiltered,
    RollingZ,
    SCWarnInspired,
    all_baselines,
)
from logsentinel.episodes.builder import build_episode
from logsentinel.episodes.store import load_view


def degrade(df):
    m = post(df, 2)
    df.loc[m, "gw_latency_p95_ms"] *= 3
    df.loc[m, "svc_latency_p95_ms__orders"] *= 3
    df.loc[m, "db_latency_p95_ms__orders"] *= 4


@pytest.fixture(scope="module")
def episodes(tmp_path_factory):
    root = tmp_path_factory.mktemp("bl")
    out = root / "episodes"
    quiet = build_episode(make_run(root, "run-syn-S1-s0", seed=1), out)["episode_id"]
    bad = build_episode(make_run(root, "run-syn-S5-s0", seed=2, mutate=degrade), out)["episode_id"]
    normals = [
        build_episode(make_run(root, f"run-syn-S1-s{i}", seed=10 + i), out)["episode_id"]
        for i in range(1, 7)
    ]
    return out, quiet, bad, normals


@pytest.mark.parametrize(
    "det",
    [
        RollingZ(),
        IsolationForestDetector(),
        ChangeAwareTest(),
        SCWarnInspired(),
        PersistenceFiltered(),
    ],
)
def test_unsupervised_baselines_rank_degraded_above_quiet(episodes, det):
    root, quiet, bad, _ = episodes
    q = det.score(load_view(quiet, 600, root))
    b = det.score(load_view(bad, 600, root))
    assert b > q, (det.name, q, b)


def test_melody_inspired_learns_from_normal_episodes(episodes):
    root, quiet, bad, normals = episodes
    det = MelodyInspired().fit([load_view(e, 600, root) for e in normals], ["BA"] * len(normals))
    assert det.score(load_view(bad, 600, root)) > det.score(load_view(quiet, 600, root))


def test_scores_ignore_data_after_cutoff(episodes):
    root, _, bad, _ = episodes
    before = {d.name: d.score(load_view(bad, 300, root)) for d in all_baselines()[:5]}
    s = pd.read_parquet(root / bad / "series.parquet")
    s.loc[s["t"] > 300, "gw_latency_p95_ms"] = 1e6
    s.to_parquet(root / bad / "series.parquet", index=False)
    after = {d.name: d.score(load_view(bad, 300, root)) for d in all_baselines()[:5]}
    assert before == after


def test_baseline_names_are_unique():
    names = [d.name for d in all_baselines()]
    assert len(names) == len(set(names)) == 6
