"""Cluster bootstrap over change identities, stratified by scenario (evaluation.md section 5).

Replicates of one change (seeds, workloads) are correlated, so the resampling unit is the change_id:
within each scenario, change_ids are drawn with replacement, and all of a drawn change's episodes
enter the resample. Paired comparisons use the same resample for both systems.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd


def _resample_ids(index: pd.DataFrame, rng: np.random.Generator) -> list[str]:
    ids: list[str] = []
    for _, g in index.groupby("scenario"):
        changes = g["change_id"].unique()
        ids.extend(rng.choice(changes, size=len(changes), replace=True))
    return ids


def _take(o: pd.DataFrame, change_ids: list[str]) -> pd.DataFrame:
    by_change = {c: g for c, g in o.groupby("change_id")}
    return pd.concat([by_change[c] for c in change_ids if c in by_change], ignore_index=True)


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
    index = first[["change_id", "scenario"]].drop_duplicates()
    rng = np.random.default_rng(seed)
    samples: dict[str, list[dict[str, float]]] = {name: [] for name in per_system}
    for _ in range(n):
        ids = _resample_ids(index, rng)
        for name, o in per_system.items():
            samples[name].append(stat(_take(o, ids)))
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
