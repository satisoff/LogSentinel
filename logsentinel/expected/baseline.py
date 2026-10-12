"""Expected post-change behaviour with uncertainty.

WorkloadBaseline is fitted per episode on the pre-change window only:

    log(metric + c) = a + b * log(request_rate) + noise

The slope b is ridge-shrunk toward a per-family prior, because a steady baseline cannot identify how
a metric scales with load: CPU and request counts scale roughly linearly, latency weakly, memory not
at all. The offset c keeps zero-heavy metrics stable: 0.5 percentage points for error rates, 1 for
counts, 5% of the baseline median otherwise. The interval half-width is a split-conformal
quantile of absolute residuals: fit on the first 60% of W_pre, calibrate on the last 40%, then
refit on all of W_pre.

Caveat: telemetry residuals are autocorrelated, so nominal coverage (1 - alpha) is optimistic.
Actual coverage is measured on BA episodes in the evaluation, not assumed.

ShiftPrior adds change-conditioning: a typical log-shift per (change_type, service, metric), learned
only from training-split episodes. It moves the expected value and widens the interval, so that
changes known to shift behaviour (e.g. a feature flag adding work) are not treated as unexpected.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# Prior elasticity d log(metric) / d log(load) per metric family.
FAMILY_PRIOR = {"cpu": 1.0, "rps": 1.0, "count": 1.0, "latency": 0.3, "rss": 0.0, "error": 0.0}
# Absolute offsets for zero-heavy families; others use a fraction of the baseline median.
FAMILY_OFFSET = {"error": 0.005, "count": 1.0}
RELATIVE_OFFSET = 0.05


def family(metric: str) -> str:
    m = metric.lower()
    if "error_rate" in m or "5xx" in m:
        return "error"
    if "latency" in m:
        return "latency"
    if "cpu" in m:
        return "cpu"
    if "rss" in m:
        return "rss"
    if "rps" in m:
        return "rps"
    if any(k in m for k in ("retries", "timeouts", "exceptions")):
        return "count"
    return "rss"  # conservative default: no load scaling


def conformal_quantile(abs_resid: np.ndarray, alpha: float) -> float:
    n = len(abs_resid)
    if n == 0:
        return math.inf
    level = min(1.0, math.ceil((n + 1) * (1 - alpha)) / n)
    return float(np.quantile(abs_resid, level, method="higher"))


@dataclass
class ShiftPrior:
    """(change_type, service, metric) -> (log shift, spread). Service '*' matches any."""

    table: dict[tuple[str, str, str], tuple[float, float]] = field(default_factory=dict)

    def get(self, change_type: str | None, service: str | None, metric: str) -> tuple[float, float]:
        for key in ((change_type, service, metric), (change_type, "*", metric)):
            if key in self.table:
                return self.table[key]
        return 0.0, 0.0

    @classmethod
    def fit(cls, rows: pd.DataFrame, min_episodes: int = 3) -> ShiftPrior:
        """rows: change_type, service, metric, log_shift (one row per training episode x metric)."""
        table = {}
        for keys, g in rows.groupby(["change_type", "service", "metric"], dropna=False):
            if len(g) >= min_episodes:
                med = float(g["log_shift"].median())
                mad = float((g["log_shift"] - med).abs().median() * 1.4826)
                table[tuple("*" if pd.isna(k) else k for k in keys)] = (med, mad)
        return cls(table)


@dataclass
class WorkloadBaseline:
    alpha: float = 0.1
    ridge: float = 0.01
    calib_frac: float = 0.4
    min_points: int = 12
    floor: float = 0.03  # minimum half-width in log space (~3%)
    c: float = float("nan")
    a: float = float("nan")
    b: float = float("nan")
    q: float = float("nan")
    ok: bool = False

    def _ridge_fit(self, x: np.ndarray, y: np.ndarray, b0: float) -> tuple[float, float]:
        xm, ym = x.mean(), y.mean()
        n = len(x)
        sxx = float(((x - xm) ** 2).sum())
        sxy = float(((x - xm) * (y - ym)).sum())
        b = (sxy + self.ridge * n * b0) / (sxx + self.ridge * n)
        return float(ym - b * xm), float(b)

    def fit(self, rate: np.ndarray, y: np.ndarray, metric: str) -> WorkloadBaseline:
        rate, y = np.asarray(rate, float), np.asarray(y, float)
        mask = np.isfinite(rate) & np.isfinite(y) & (rate > 0)
        if mask.sum() < self.min_points:
            self.ok = False
            return self
        fam = family(metric)
        yv = np.clip(y[mask], 0, None)
        self.c = FAMILY_OFFSET.get(fam, max(RELATIVE_OFFSET * float(np.median(yv)), 1e-6))
        x, yy = np.log(rate[mask]), np.log(yv + self.c)
        b0 = FAMILY_PRIOR[fam]
        split = int(len(x) * (1 - self.calib_frac))
        a, b = self._ridge_fit(x[:split], yy[:split], b0)
        q = conformal_quantile(np.abs(yy[split:] - (a + b * x[split:])), self.alpha)
        self.a, self.b = self._ridge_fit(x, yy, b0)
        self.q = max(q, self.floor)
        self.ok = True
        return self

    def predict(self, rate: np.ndarray, shift: float = 0.0, spread: float = 0.0) -> pd.DataFrame:
        rate = np.asarray(rate, float)
        if not self.ok:
            nan = np.full(len(rate), np.nan)
            return pd.DataFrame({"expected": nan, "lo": nan, "hi": nan})
        center = self.a + self.b * np.log(np.where(rate > 0, rate, np.nan)) + shift
        half = self.q + spread
        return pd.DataFrame(
            {
                "expected": np.exp(center) - self.c,
                "lo": np.clip(np.exp(center - half) - self.c, 0, None),
                "hi": np.exp(center + half) - self.c,
            }
        )


def load_driver(series: pd.DataFrame, step_s: int = 15) -> np.ndarray:
    """Exogenous load driver: the generator's target rate (open loop), else observed requests."""
    if "target_rps" in series and series["target_rps"].notna().any():
        return series["target_rps"].to_numpy(float)
    return (series["requests"] / step_s).to_numpy(float)


def expected_bands(
    series: pd.DataFrame,
    metrics: list[str],
    change_type: str | None = None,
    service: str | None = None,
    prior: ShiftPrior | None = None,
    **model_kwargs,
) -> dict[str, pd.DataFrame]:
    """Fit on t < 0, predict every row: metric -> DataFrame(t, observed, expected, lo, hi)."""
    rate = load_driver(series)
    pre = (series["t"] < 0).to_numpy()
    out = {}
    for metric in metrics:
        if metric not in series or series.loc[pre, metric].notna().sum() == 0:
            continue
        model = WorkloadBaseline(**model_kwargs).fit(rate[pre], series.loc[pre, metric], metric)
        shift, spread = (prior or ShiftPrior()).get(change_type, service, metric)
        before = model.predict(rate)
        after = model.predict(rate, shift, spread)
        is_post = (series["t"].to_numpy() >= 0)[:, None]
        band = pd.DataFrame(np.where(is_post, after, before), columns=before.columns)
        band.insert(0, "observed", series[metric].to_numpy(float))
        band.insert(0, "t", series["t"].to_numpy())
        out[metric] = band
    return out
