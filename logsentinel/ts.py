"""Time-series helpers shared by the labeler and decision-time features.

Labels and features compute impact the same way (same windows, same run logic), so that differences
between them come only from the information available (horizon vs. cutoff), not from implementation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Return [start, end) index pairs of consecutive True values."""
    mask = np.asarray(mask, dtype=bool)
    if not mask.any():
        return []
    padded = np.concatenate([[False], mask, [False]])
    edges = np.flatnonzero(np.diff(padded.astype(np.int8)))
    return list(zip(edges[0::2], edges[1::2], strict=True))


def merge_runs(spans: list[tuple[int, int]], max_gap: int) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start - merged[-1][1] < max_gap:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def sustained(mask: np.ndarray, min_len: int) -> np.ndarray:
    out = np.zeros(len(mask), dtype=bool)
    for start, end in runs(mask):
        if end - start >= min_len:
            out[start:end] = True
    return out


def trailing_ratio(num: pd.Series, den: pd.Series, window: int) -> np.ndarray:
    """Trailing-window ratio of sums (request-weighted); NaN where the window has no traffic."""
    n = num.rolling(window, min_periods=1).sum()
    d = den.rolling(window, min_periods=1).sum()
    return (n / d.replace(0, np.nan)).to_numpy()
