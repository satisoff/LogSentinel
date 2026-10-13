"""Learned qualifier on synthetic tabular data."""

import numpy as np
import pandas as pd
import pytest

from logsentinel.qualify.model import QualifierModel
from logsentinel.qualify.rules import LABELS


@pytest.fixture(scope="module")
def data():
    rng = np.random.default_rng(0)
    rows, y, groups = [], [], []
    for g in range(40):  # 40 change identities x 6 decision times
        lab = LABELS[g % 4]
        k = LABELS.index(lab)
        for _ in range(6):
            rows.append({
                "imp_material_min": max(0.0, k * 3 + rng.normal(0, 1)),
                "imp_severe_min": max(0.0, (k == 3) * 4 + rng.normal(0, 0.5)),
                "dev_max_frac_out": min(1.0, max(0.0, 0.2 * k + rng.normal(0, 0.1))),
                "log_err_ratio": 1 + k + rng.normal(0, 0.5),
                "chg_type_deploy": 1.0,
            })  # fmt: skip
            y.append(lab)
            groups.append(f"chg-{g}")
    return pd.DataFrame(rows), pd.Series(y), pd.Series(groups)


def test_probabilities_are_valid_and_informative(data):
    X, y, groups = data
    tr = groups.str[4:].astype(int) < 30
    m = QualifierModel().fit(X[tr], y[tr], groups[tr])
    p = m.predict_proba(X[~tr])
    assert list(p.columns) == list(LABELS)
    assert np.allclose(p.sum(axis=1), 1.0)
    acc = (p.idxmax(axis=1).to_numpy() == y[~tr].to_numpy()).mean()
    assert acc > 0.6  # chance = 0.25


def test_drop_prefixes_removes_feature_groups(data):
    X, y, groups = data
    m = QualifierModel(drop_prefixes=("imp_", "log_")).fit(X, y, groups)
    assert all(not c.startswith(("imp_", "log_")) for c in m.columns_)
    assert m.predict_proba(X).shape == (len(X), 4)


def test_needs_two_groups(data):
    X, y, _ = data
    with pytest.raises(ValueError):
        QualifierModel().fit(X, y, ["one"] * len(X))
