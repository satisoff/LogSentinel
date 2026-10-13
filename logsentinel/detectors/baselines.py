"""Baseline detectors (docs/spec/evaluation.md section 6).

Every detector maps an EpisodeView (data <= t0 + cutoff only) to a scalar anomaly score; higher
means more anomalous. An alarm is `score >= threshold`, with the threshold chosen per detector on
the validation split (evaluation.md section 3, metric 1). Baselines only raise alarms (action
Investigate); they never escalate, so they cannot cause unnecessary rollbacks.

Where a baseline is "inspired by" a published system, the docstring lists what differs. These are
not faithful reproductions and must be reported as such.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.stats import mannwhitneyu
from sklearn.ensemble import IsolationForest
from sklearn.neural_network import MLPRegressor
from sklearn.preprocessing import StandardScaler

from logsentinel.episodes.store import EpisodeView
from logsentinel.expected.baseline import family

STEP_S = 15
# Gateway-level and service-aggregate metrics: available without knowing what changed.
BLIND_METRICS = (
    "gw_latency_p95_ms",
    "gw_latency_p99_ms",
    "gw_error_rate",
    "svc_cpu",
    "svc_rss_mb",
    "db_latency_p95_ms",
    "retries",
    "timeouts",
)
# Per-service metrics used when the changed service is known.
SERVICE_METRICS = (
    "svc_latency_p95_ms",
    "svc_cpu",
    "svc_rss_mb",
    "db_latency_p95_ms",
    "svc_5xx_rps",
    "exceptions",
)
ABS_FLOOR = {"latency": 5.0, "cpu": 0.05, "rss": 10.0, "error": 0.005, "count": 1.0, "rps": 1.0}


def robust_scale(base: np.ndarray, metric: str) -> tuple[float, float]:
    """Median and a floored robust spread, so near-constant baselines do not explode z-scores."""
    base = base[np.isfinite(base)]
    if base.size == 0:
        return np.nan, np.nan
    med = float(np.median(base))
    mad = 1.4826 * float(np.median(np.abs(base - med)))
    return med, max(mad, 0.05 * abs(med), ABS_FLOOR.get(family(metric), 1e-6) / 4)


def changed_service_columns(view: EpisodeView) -> list[str]:
    svc = view.change.get("service")
    return [f"{m}__{svc}" for m in SERVICE_METRICS if svc and f"{m}__{svc}" in view.series]


def _rolling_max(step_scores: np.ndarray, t: np.ndarray, smooth: int = 2) -> float:
    post = t >= 0
    if not post.any():
        return 0.0
    s = (
        pd.Series(np.where(np.isfinite(step_scores), step_scores, 0.0))
        .rolling(smooth, min_periods=1)
        .mean()
    )
    return float(np.max(s.to_numpy()[post]))


class Detector:
    name = "detector"
    needs_training = False

    def fit(self, views: list[EpisodeView], labels: list[str]) -> Detector:
        return self

    def score(self, view: EpisodeView) -> float:
        raise NotImplementedError


# --- B1: context-blind statistical detector -------------------------------------------------------
@dataclass
class RollingZ(Detector):
    """Rolling robust z-score on gateway/aggregate metrics against the trailing `window_min` minutes
    (ending `gap_min` before each step). It does not know about the change."""

    window_min: float = 20
    gap_min: float = 1
    metrics: tuple[str, ...] = BLIND_METRICS
    name: str = "B1_rolling_z"

    def step_scores(self, s: pd.DataFrame) -> np.ndarray:
        w, g = int(self.window_min * 60 / STEP_S), int(self.gap_min * 60 / STEP_S)
        t = s["t"].to_numpy()
        out = np.zeros(len(s))
        cols = [m for m in self.metrics if m in s]
        values = {m: s[m].to_numpy(float) for m in cols}
        for i in np.flatnonzero(t >= 0):
            lo, hi = max(0, i - g - w), max(0, i - g)
            best = 0.0
            for m in cols:
                med, scale = robust_scale(values[m][lo:hi], m)
                x = values[m][i]
                if np.isfinite(x) and np.isfinite(med):
                    best = max(best, (x - med) / scale)  # one-sided: higher is worse
            out[i] = best
        return out

    def score(self, view: EpisodeView) -> float:
        return _rolling_max(self.step_scores(view.series), view.series["t"].to_numpy())


# --- B5: B1 + fixed persistence suppression -------------------------------------------------------
@dataclass
class PersistenceFiltered(Detector):
    """B1 alarms that only count once the step score stays >= z_threshold for a while. Score =
    longest such run in minutes (the alarm threshold is then a minimum duration)."""

    z_threshold: float = 4.0
    base: RollingZ = field(default_factory=RollingZ)
    name: str = "B5_persistence"

    def score(self, view: EpisodeView) -> float:
        s = view.series
        hot = (self.base.step_scores(s) >= self.z_threshold) & (s["t"].to_numpy() >= 0)
        longest = run = 0
        for h in hot:
            run = run + 1 if h else 0
            longest = max(longest, run)
        return longest * STEP_S / 60


# --- B2: context-blind ML detector ----------------------------------------------------------------
@dataclass
class IsolationForestDetector(Detector):
    """IsolationForest fitted per episode on the pre-change window (recent normal history), applied
    to post-change steps. Multivariate, unsupervised, blind to the change."""

    metrics: tuple[str, ...] = BLIND_METRICS
    seed: int = 0
    name: str = "B2_isolation_forest"

    def score(self, view: EpisodeView) -> float:
        s = view.series
        cols = [m for m in self.metrics if m in s and s.loc[s["t"] < 0, m].notna().any()]
        pre = (s["t"] < 0).to_numpy()
        X = s[cols].to_numpy(float)
        med = np.nanmedian(X[pre], axis=0)
        X = np.where(np.isfinite(X), X, med)
        scaler = StandardScaler().fit(X[pre])
        forest = IsolationForest(n_estimators=100, random_state=self.seed).fit(
            scaler.transform(X[pre])
        )
        anomaly = -forest.score_samples(scaler.transform(X))
        ref = np.quantile(anomaly[pre], 0.99)
        return _rolling_max(anomaly - ref, s["t"].to_numpy(), smooth=4)


# --- B3: change-aware detector --------------------------------------------------------------------
@dataclass
class ChangeAwareTest(Detector):
    """Pre/post comparison anchored at the change: one-sided Mann-Whitney U per metric (gateway
    metrics plus the changed service's metrics), load-normalised for rate-like metrics, with a
    minimum effect size. Score = max -log10(p) over metrics with a meaningful shift."""

    min_post_steps: int = 4
    name: str = "B3_change_aware"

    def score(self, view: EpisodeView) -> float:
        s = view.series
        pre, post = s[s["t"] < 0], s[s["t"] >= 0]
        if len(post) < self.min_post_steps:
            return 0.0
        load = post["target_rps"].median() / max(pre["target_rps"].median(), 1e-9)
        best = 0.0
        for col in [m for m in BLIND_METRICS if m in s] + changed_service_columns(view):
            fam = family(col)
            a, b = pre[col].dropna().to_numpy(float), post[col].dropna().to_numpy(float)
            if len(a) < 8 or len(b) < self.min_post_steps:
                continue
            if fam in ("cpu", "rps", "count"):
                b = b / load  # expected to scale with load
            if np.median(b) - np.median(a) < ABS_FLOOR.get(fam, 0.0):
                continue
            p = mannwhitneyu(b, a, alternative="greater").pvalue
            best = max(best, -np.log10(max(p, 1e-300)))
        return float(best)


# --- B4: SCWarn-inspired multimodal detector ------------------------------------------------------
@dataclass
class SCWarnInspired(Detector):
    """Inspired by SCWarn (Zhao et al., FSE 2021): multimodal (metrics + logs) anomaly detection
    triggered by a software change, alerting on persistent post-change anomalies.

    Differences from the original (disclosed): per-feature AR(3) predictors fitted on the pre-change
    window replace SCWarn's multimodal LSTM; log modality = per-step counts of ERROR and WARNING
    lines, lines with templates first seen after t0, and total lines (SCWarn also counts unmatched
    templates); no cross-modal attention or fusion weights; no interpretation module."""

    order: int = 3
    persist_steps: int = 4
    name: str = "B4_scwarn_inspired"

    @staticmethod
    def log_features(view: EpisodeView) -> pd.DataFrame:
        t = view.series["t"].to_numpy()
        lg = view.logs
        out = pd.DataFrame({"t": t})
        if lg.empty:
            for c in ("log_error", "log_warning", "log_new", "log_total"):
                out[c] = 0.0
            return out
        bucket = (np.floor(lg["t"] / STEP_S) * STEP_S).astype(int)
        grouped = lg.assign(step=bucket.to_numpy())

        def count(mask: pd.Series) -> pd.Series:
            return grouped[mask].groupby("step").size()

        for name, mask in (
            ("log_error", grouped["level"] == "ERROR"),
            ("log_warning", grouped["level"] == "WARNING"),
            ("log_new", grouped["new_template"]),
            ("log_total", pd.Series(True, index=grouped.index)),
        ):
            out[name] = out["t"].map(count(mask)).fillna(0.0).to_numpy()
        return out

    def score(self, view: EpisodeView) -> float:
        s = view.series
        cols = [m for m in ("gw_latency_p95_ms", "gw_error_rate", "requests") if m in s]
        cols += changed_service_columns(view)
        frame = s[["t", *cols]].merge(self.log_features(view), on="t", how="left")
        t = frame["t"].to_numpy()
        pre = t < 0
        step = np.zeros(len(frame))
        p = self.order
        for col in [c for c in frame.columns if c != "t"]:
            y = frame[col].to_numpy(float)
            y = np.where(
                np.isfinite(y), y, np.nanmedian(y[pre]) if np.isfinite(y[pre]).any() else 0.0
            )
            if pre.sum() <= p + 5:
                continue
            X = np.column_stack(
                [y[p - k - 1 : len(y) - k - 1] for k in range(p)] + [np.ones(len(y) - p)]
            )
            target = y[p:]
            fit_rows = pre[p:]
            coef, *_ = np.linalg.lstsq(X[fit_rows], target[fit_rows], rcond=None)
            resid = target - X @ coef
            sigma = max(np.std(resid[fit_rows]), 1e-6, 0.05 * abs(np.median(target[fit_rows])))
            step[p:] = np.maximum(step[p:], resid / sigma)  # one-sided
        return _rolling_max(step, t, smooth=self.persist_steps)


# --- B6: MELODY-inspired semi-supervised detector -------------------------------------------------
def episode_summary(view: EpisodeView) -> np.ndarray:
    """Deployment-level feature vector at the cutoff (shared feature space across episodes)."""
    s = view.series
    pre, post = s[s["t"] < 0], s[s["t"] >= 0]
    feats = []
    for m in BLIND_METRICS:
        if m not in s:
            feats += [0.0, 0.0, 0.0]
            continue
        med, scale = robust_scale(pre[m].to_numpy(float), m)
        x = post[m].to_numpy(float)
        x = x[np.isfinite(x)]
        if x.size == 0 or not np.isfinite(med):
            feats += [0.0, 0.0, 0.0]
            continue
        z = (x - med) / scale
        feats += [float(np.mean(z)), float(np.max(z)), float(np.mean(z > 3))]
    return np.clip(np.nan_to_num(np.array(feats)), -50, 50)


@dataclass
class MelodyInspired(Detector):
    """Inspired by MELODY (arXiv:2401.10338): deployment-level detection with an online feature
    extractor mapping each deployment's multivariate series into a shared feature space, and a
    semi-supervised one-class model trained on deployments known to be normal.

    Differences from the original (disclosed): hand-crafted summary features (robust z
    mean/max/exceedance per metric) replace the learned online extractor; a small MLP autoencoder
    (sklearn) fitted on BA training episodes replaces the deep one-class model; no human relabelling
    loop; no rollback service."""

    hidden: tuple[int, ...] = (16, 4, 16)
    seed: int = 0
    name: str = "B6_melody_inspired"
    needs_training: bool = True
    _scaler: StandardScaler | None = None
    _model: MLPRegressor | None = None

    def fit(self, views: list[EpisodeView], labels: list[str]) -> MelodyInspired:
        X = np.array([episode_summary(v) for v, y in zip(views, labels, strict=True) if y == "BA"])
        if len(X) < 5:
            raise ValueError("MelodyInspired needs at least 5 normal (BA) training views")
        self._scaler = StandardScaler().fit(X)
        Xs = self._scaler.transform(X)
        self._model = MLPRegressor(
            hidden_layer_sizes=self.hidden, max_iter=2000, random_state=self.seed
        ).fit(Xs, Xs)
        return self

    def score(self, view: EpisodeView) -> float:
        if self._model is None:
            raise RuntimeError("fit() first")
        x = self._scaler.transform(episode_summary(view)[None, :])
        return float(np.mean((self._model.predict(x) - x) ** 2))


def all_baselines(seed: int = 0) -> list[Detector]:
    return [
        RollingZ(),
        IsolationForestDetector(seed=seed),
        ChangeAwareTest(),
        SCWarnInspired(),
        PersistenceFiltered(),
        MelodyInspired(seed=seed),
    ]
