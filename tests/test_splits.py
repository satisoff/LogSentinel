import json

import pandas as pd
import pytest

from logsentinel.episodes.splits import (
    SplitError,
    check_no_leak,
    freeze,
    make_heldout_workload,
    make_loso,
    make_main,
    verify,
)


@pytest.fixture
def manifest(tmp_path):
    rows = []
    for s, n_changes in {"S1": 4, "S5": 4, "S9": 5, "S6": 3}.items():
        for c in range(n_changes):
            for seed, prof in enumerate(["steady", "diurnal", "bursty"]):
                rows.append({"episode_id": f"run-{s}-c{c}-s{seed}", "scenario": s,
                             "change_id": f"{s}-c{c}", "workload_profile": prof})  # fmt: skip
    df = pd.DataFrame(rows)
    path = tmp_path / "episodes.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return df, path


def _cids(df, eps):
    return set(df.set_index("episode_id").loc[eps, "change_id"])


def test_main_split_is_grouped_and_covers_every_scenario(manifest):
    df, _ = manifest
    parts = make_main(df, seed=0)
    check_no_leak(df, parts)
    assert sum(len(v) for v in parts.values()) == len(df)
    for part in ("train", "val", "test"):
        assert {s for s in df.set_index("episode_id").loc[parts[part], "scenario"]} == {
            "S1",
            "S5",
            "S9",
            "S6",
        }
    assert parts == make_main(df, seed=0)  # deterministic
    assert parts != make_main(df, seed=1)


def test_loso_holds_out_whole_scenario(manifest):
    df, _ = manifest
    parts = make_loso(df, "S5")
    assert set(df.set_index("episode_id").loc[parts["test"], "scenario"]) == {"S5"}
    assert not _cids(df, parts["test"]) & _cids(df, parts["train"] + parts["val"])


def test_heldout_workload_has_unseen_profile_and_unseen_changes(manifest):
    df, _ = manifest
    parts = make_heldout_workload(df, "bursty")
    by = df.set_index("episode_id")
    assert set(by.loc[parts["test"], "workload_profile"]) == {"bursty"}
    assert "bursty" not in set(by.loc[parts["train"] + parts["val"], "workload_profile"])
    check_no_leak(df, parts)


def test_leak_detection(manifest):
    df, _ = manifest
    parts = make_main(df)
    parts["test"].append(parts["train"][0])
    with pytest.raises(SplitError):
        check_no_leak(df, parts)


def test_freeze_and_verify_detect_manifest_change(manifest, tmp_path):
    df, path = manifest
    out = freeze("main", make_main(df), df, seed=0, manifest=path, out_dir=tmp_path / "splits")
    assert verify(out, df, manifest=path)["counts"]["test"] > 0
    with pytest.raises(SplitError):
        freeze("main", make_main(df), df, seed=0, manifest=path, out_dir=tmp_path / "splits")
    path.write_text(path.read_text() + "\n")
    with pytest.raises(SplitError, match="manifest changed"):
        verify(out, df, manifest=path)
