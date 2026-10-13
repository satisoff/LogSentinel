"""Learned qualifier: calibrated class probabilities P(BA, TA, AR, CR | decision-time features).

Training rows are (episode, decision time) pairs: the model learns to forecast an episode's label
from what is observable at t_d. All rows of an episode share its change_id, so the calibration folds
are grouped by change_id (no leakage of a change across folds).

Model: LightGBM multiclass with small trees, because the data is small (~100 training episodes x
6 decision times). Calibration: per-class sigmoid (Platt) fitted on out-of-fold predictions of a
GroupKFold, then renormalised.
Ablations drop feature groups by prefix (chg_, dev_, per_, imp_, log_, wl_).
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.calibration import CalibratedClassifierCV
from sklearn.model_selection import GroupKFold

from logsentinel.qualify.rules import LABELS

DEFAULT_PARAMS = {
    "n_estimators": 200,
    "learning_rate": 0.05,
    "num_leaves": 7,
    "max_depth": 3,
    "min_child_samples": 5,
    "subsample": 0.8,
    "subsample_freq": 1,
    "colsample_bytree": 0.8,
    "class_weight": "balanced",
    "verbose": -1,
}


def select_features(X: pd.DataFrame, drop_prefixes: tuple[str, ...] = ()) -> pd.DataFrame:
    keep = (
        [c for c in X.columns if not c.startswith(tuple(drop_prefixes))]
        if drop_prefixes
        else list(X.columns)
    )
    return X[keep]


@dataclass
class QualifierModel:
    drop_prefixes: tuple[str, ...] = ()
    seed: int = 0
    n_folds: int = 5
    params: dict = field(default_factory=lambda: dict(DEFAULT_PARAMS))
    columns_: list[str] = field(default_factory=list)
    model_: CalibratedClassifierCV | None = None
    classes_: list[str] = field(default_factory=list)

    def fit(
        self, X: pd.DataFrame, y: list[str] | pd.Series, groups: list[str] | pd.Series
    ) -> QualifierModel:
        Xs = select_features(X, self.drop_prefixes)
        self.columns_ = list(Xs.columns)
        y = pd.Series(list(y))
        groups = pd.Series(list(groups))
        n_groups = groups.nunique()
        folds = min(self.n_folds, n_groups)
        if folds < 2:
            raise ValueError("need at least two change identities to calibrate")
        base = LGBMClassifier(random_state=self.seed, **self.params)
        cv = list(GroupKFold(n_splits=folds).split(Xs, y, groups))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self.model_ = CalibratedClassifierCV(base, method="sigmoid", cv=cv).fit(Xs, y)
        self.classes_ = list(self.model_.classes_)
        return self

    def predict_proba(self, X: pd.DataFrame) -> pd.DataFrame:
        if self.model_ is None:
            raise RuntimeError("fit() first")
        Xs = X.reindex(columns=self.columns_)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            p = self.model_.predict_proba(Xs)
        out = pd.DataFrame(p, columns=self.classes_, index=X.index)
        for lab in LABELS:  # classes absent from training get probability 0
            if lab not in out:
                out[lab] = 0.0
        out = out[list(LABELS)]
        return out.div(out.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)
