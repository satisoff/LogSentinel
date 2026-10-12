"""Read access to built episodes, enforcing the information barrier (docs/architecture.md).

Systems under test (detectors, features, qualifier) must use `load_view(episode_id, cutoff_s)`.
It returns only data observable at decision time t0 + cutoff_s, plus the change event, and no oracle
or label fields. Only the evaluator and labeler use `load_label` / `load_manifest`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from logsentinel.episodes.builder import EPISODE_DIR

# Change-event fields a system may see. Everything else (scenario, oracle, label) is withheld.
VISIBLE_CHANGE_FIELDS = (
    "change_id",
    "t0",
    "service",
    "change_type",
    "scope",
    "rollout_pct",
    "diff_summary",
    "from_version",
    "to_version",
)


@dataclass(frozen=True)
class EpisodeView:
    episode_id: str
    cutoff_s: float
    change: dict[str, Any]
    series: pd.DataFrame  # rows with t <= cutoff_s
    logs: pd.DataFrame  # lines with t <= cutoff_s


def load_view(episode_id: str, cutoff_s: float, root: Path = EPISODE_DIR) -> EpisodeView:
    ep = root / episode_id
    change = json.loads((ep / "change.json").read_text())
    series = pd.read_parquet(ep / "series.parquet")
    logs = pd.read_parquet(ep / "logs.parquet")
    return EpisodeView(
        episode_id=episode_id,
        cutoff_s=cutoff_s,
        change={k: change.get(k) for k in VISIBLE_CHANGE_FIELDS},
        series=series[series["t"] <= cutoff_s].reset_index(drop=True),
        logs=logs[logs["t"] <= cutoff_s].reset_index(drop=True),
    )


def load_label(episode_id: str, root: Path = EPISODE_DIR) -> dict[str, Any]:
    """Ground truth + evidence. Evaluator/labeler only."""
    return json.loads((root / episode_id / "label.json").read_text())


def load_manifest(root: Path = EPISODE_DIR, include_rejected: bool = False) -> pd.DataFrame:
    """Episode manifest (contains labels). Evaluator only."""
    df = pd.read_json(root / "episodes.jsonl", lines=True)
    return df if include_rejected else df[df["status"] == "ok"].reset_index(drop=True)
