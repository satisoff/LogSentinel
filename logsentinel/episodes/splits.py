"""Leak-free dataset splits, grouped by change identity (docs/spec/dataset.md section 6).

Replicates of the same change (other seeds or workloads) share a change_id and always land in the
same partition. Splits are frozen to data/splits/<name>.json together with the SHA-256 of the
manifest they were made from. `verify` refuses a split whose manifest has changed or whose
partitions share a change_id.

  main              per scenario, its change_ids go 60/20/20 to train/val/test (at least 1 each
                    when the scenario has >= 3 change_ids)
  loso_<S>          leave-one-scenario-out: test = scenario S; val = grouped 20% of the others
  heldout_<profile> test = test-partition change_ids of `main`, restricted to one workload
                    profile; train/val = the main train/val change_ids without that profile
"""

from __future__ import annotations

import hashlib
import json
import random
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from logsentinel.episodes.builder import EPISODE_DIR, ROOT

SPLIT_DIR = ROOT / "data" / "splits"
PARTS = ("train", "val", "test")


class SplitError(RuntimeError):
    pass


def manifest_hash(path: Path = EPISODE_DIR / "episodes.jsonl") -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _allocate(change_ids: list[str], rng: random.Random, frac=(0.6, 0.2, 0.2)) -> dict[str, str]:
    ids = sorted(change_ids)
    rng.shuffle(ids)
    n = len(ids)
    if n >= 3:
        n_test = max(1, round(frac[2] * n))
        n_val = max(1, round(frac[1] * n))
    else:  # too few identities for three partitions: keep them for training
        n_test = n_val = 0
    out = {}
    for i, cid in enumerate(ids):
        out[cid] = "test" if i < n_test else "val" if i < n_test + n_val else "train"
    return out


def _partitions(df: pd.DataFrame, assign: dict[str, str]) -> dict[str, list[str]]:
    parts = {p: [] for p in PARTS}
    for ep, cid in zip(df["episode_id"], df["change_id"], strict=True):
        if cid in assign:
            parts[assign[cid]].append(ep)
    return {p: sorted(v) for p, v in parts.items()}


def make_main(df: pd.DataFrame, seed: int = 0) -> dict[str, list[str]]:
    assign: dict[str, str] = {}
    for scenario, g in df.groupby("scenario"):
        rng = random.Random(f"main:{seed}:{scenario}")
        assign.update(_allocate(g["change_id"].unique().tolist(), rng))
    return _partitions(df, assign)


def make_loso(df: pd.DataFrame, scenario: str, seed: int = 0) -> dict[str, list[str]]:
    rest = df[df["scenario"] != scenario]
    rng = random.Random(f"loso:{seed}:{scenario}")
    ids = sorted(rest["change_id"].unique())
    rng.shuffle(ids)
    n_val = max(1, round(0.2 * len(ids))) if len(ids) >= 2 else 0
    assign = {cid: ("val" if i < n_val else "train") for i, cid in enumerate(ids)}
    assign.update({cid: "test" for cid in df.loc[df["scenario"] == scenario, "change_id"]})
    return _partitions(df, assign)


def make_heldout_workload(df: pd.DataFrame, profile: str, seed: int = 0) -> dict[str, list[str]]:
    main = make_main(df, seed)
    by_ep = df.set_index("episode_id")
    out = {}
    for part in PARTS:
        eps = by_ep.loc[main[part]] if main[part] else by_ep.iloc[0:0]
        keep = (
            eps["workload_profile"] == profile
            if part == "test"
            else eps["workload_profile"] != profile
        )
        out[part] = sorted(eps.index[keep])
    return out


def check_no_leak(df: pd.DataFrame, parts: dict[str, list[str]]) -> None:
    cid = df.set_index("episode_id")["change_id"]
    seen: dict[str, str] = {}
    for part, eps in parts.items():
        for c in set(cid.loc[eps]) if eps else set():
            if c in seen and seen[c] != part:
                raise SplitError(f"change_id {c} appears in both {seen[c]} and {part}")
            seen[c] = part


def freeze(
    name: str,
    parts: dict[str, list[str]],
    df: pd.DataFrame,
    seed: int,
    manifest: Path = EPISODE_DIR / "episodes.jsonl",
    out_dir: Path = SPLIT_DIR,
) -> Path:
    check_no_leak(df, parts)
    path = out_dir / f"{name}.json"
    if path.exists():
        raise SplitError(
            f"{path} already exists; splits are frozen (delete it deliberately to remake)"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = {
        "name": name,
        "seed": seed,
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "manifest_sha256": manifest_hash(manifest),
        "counts": {p: len(v) for p, v in parts.items()},
        "change_ids": {
            p: sorted(set(df.set_index("episode_id").loc[v, "change_id"])) if v else []
            for p, v in parts.items()
        },  # fmt: skip
        "episodes": parts,
    }
    path.write_text(json.dumps(doc, indent=2))
    return path


def verify(path: Path, df: pd.DataFrame, manifest: Path = EPISODE_DIR / "episodes.jsonl") -> dict:
    doc = json.loads(path.read_text())
    if doc["manifest_sha256"] != manifest_hash(manifest):
        raise SplitError(f"{path.name}: manifest changed since the split was frozen")
    check_no_leak(df, doc["episodes"])
    return doc
