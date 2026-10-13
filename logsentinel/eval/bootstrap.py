"""Two-stage cluster bootstrap over change identities (evaluation.md section 5, amendment v1.1).

Replicates of one change (seeds, workloads) are correlated, so the primary resampling unit is the
change_id. Stage 1 draws change_ids with replacement, stratified by scenario; scenarios with one
change in the evaluated split are pooled into one stratum, because a one-cluster stratum would
always redraw the same cluster and contribute zero variance (pilot integration test, 2026-10-13: the
main split has one test change per scenario). Stage 2 draws episodes with replacement within each
drawn change. Paired comparisons use the same resample for both systems.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd


def strata(index: pd.DataFrame) -> list[np.ndarray]:
    """change_id strata: one per scenario with >= 2 changes, plus one pooled stratum of the rest."""
    out, pooled = [], []
    for _, g in index.groupby("scenario"):
        changes = g["change_id"].unique()
        (out.append(changes) if len(changes) >= 2 else pooled.extend(changes))
    if pooled:
        out.append(np.array(pooled))
    return out


def _resample(
    o: pd.DataFrame, groups: list[np.ndarray], rng: np.random.Generator
) -> list[np.ndarray]:
    """Stage 1 + 2: list of episode-row positions (into o) for one bootstrap replicate."""
    rows_by_change = {
        c: np.flatnonzero(o["change_id"].to_numpy() == c) for c in o["change_id"].unique()
    }
    picks = []
    for stratum in groups:
        for c in rng.choice(stratum, size=len(stratum), replace=True):
            rows = rows_by_change.get(c)
            if rows is not None and len(rows):
                picks.append(rng.choice(rows, size=len(rows), replace=True))
    return picks


def cluster_bootstrap(
    per_system: dict[str, pd.DataFrame],
    stat: Callable[[pd.DataFrame], dict[str, float]],
    n: int = 2000,
    seed: int = 0,
    reference: str | None = None,
    alpha: float = 0.05,
) -> pd.DataFrame:
    """per_system: name -> per-episode outcomes (with change_id, scenario). Returns rows
    (system, metric, estimate, lo, hi) plus, if `reference` is given, paired differences
    (system - reference) as metric 'diff:<metric>'."""
    first = next(iter(per_system.values()))
    order = first["episode_id"].tolist()
    aligned = {
        name: o.set_index("episode_id").loc[order].reset_index() for name, o in per_system.items()
    }
    groups = strata(first[["change_id", "scenario"]].drop_duplicates())
    rng = np.random.default_rng(seed)
    samples: dict[str, list[dict[str, float]]] = {name: [] for name in per_system}
    for _ in range(n):
        rows = np.concatenate(_resample(first, groups, rng))
        for name, o in aligned.items():  # paired: identical episode draws for every system
            samples[name].append(stat(o.iloc[rows].reset_index(drop=True)))
    rows = []
    for name, o in per_system.items():
        point = stat(o)
        boot = pd.DataFrame(samples[name])
        for metric, est in point.items():
            lo, hi = (
                np.nanquantile(boot[metric], [alpha / 2, 1 - alpha / 2])
                if boot[metric].notna().any()
                else (np.nan, np.nan)
            )
            rows.append({"system": name, "metric": metric, "estimate": est, "lo": lo, "hi": hi})
        if reference and name != reference:
            ref_boot = pd.DataFrame(samples[reference])
            ref_point = stat(per_system[reference])
            for metric in point:
                diff = boot[metric] - ref_boot[metric]
                lo, hi = (
                    np.nanquantile(diff, [alpha / 2, 1 - alpha / 2])
                    if diff.notna().any()
                    else (np.nan, np.nan)
                )
                rows.append(
                    {
                        "system": name,
                        "metric": f"diff:{metric}",
                        "estimate": point[metric] - ref_point[metric],
                        "lo": lo,
                        "hi": hi,
                    }
                )
    return pd.DataFrame(rows)
