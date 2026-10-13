"""ls-episodes: build episodes from raw runs and summarise the dataset.

ls-episodes build                 build all new raw runs, then rebuild the manifest
ls-episodes build --force         rebuild everything (e.g. after a label-config change)
ls-episodes build --run RUN_ID    build one run
ls-episodes summary               scenario x label table, rejected episodes, flags
"""

from __future__ import annotations

import json
import logging
from collections import Counter

import pandas as pd
import typer

from logsentinel.episodes.builder import (
    EPISODE_DIR,
    RAW_DIR,
    build_all,
    build_episode,
    write_manifest,
)

app = typer.Typer(add_completion=False, help=__doc__)


@app.command()
def build(run: str | None = None, force: bool = False) -> None:
    """Build episodes from data/raw into data/episodes."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if run:
        meta = build_episode(RAW_DIR / run, force=force)
        write_manifest()
        typer.echo(json.dumps({k: meta[k] for k in ("episode_id", "label", "label_rule", "flags")}))
    else:
        metas = build_all(force=force)
        typer.echo(f"{len(metas)} episodes in {EPISODE_DIR / 'episodes.jsonl'}")


@app.command()
def summary() -> None:
    """Print dataset statistics from the manifest."""
    path = EPISODE_DIR / "episodes.jsonl"
    if not path.exists():
        typer.echo("no manifest yet; run `ls-episodes build`")
        raise typer.Exit(1)
    df = pd.read_json(path, lines=True)
    ok = df[df["status"] == "ok"]
    typer.echo(f"episodes: {len(df)}  ok: {len(ok)}  rejected: {len(df) - len(ok)}  "
               f"change identities: {df['change_id'].nunique()}")  # fmt: skip
    if len(ok):
        table = pd.crosstab(ok["scenario"], ok["label"]).reindex(
            columns=["BA", "TA", "AR", "CR"], fill_value=0
        )
        typer.echo("\nlabels (ok episodes):\n" + table.to_string())
        typer.echo(f"\nambiguous: {int(ok['ambiguous'].sum())}  unexplained BA: "
                   f"{int(((ok['label'] == 'BA') & ~ok['explained']).sum())}")  # fmt: skip
    flags = Counter(f for fl in df["flags"] for f in fl)
    typer.echo("\nflags: " + (", ".join(f"{k}={v}" for k, v in flags.most_common()) or "none"))
    rej = df[df["status"] != "ok"]
    for _, r in rej.iterrows():
        typer.echo(f"  rejected {r['episode_id']}: {', '.join(r['reject_reasons'])}")


@app.command()
def split(campaign: str = "main-v1", seed: int = 0) -> None:
    """Freeze grouped splits for one campaign into data/splits/<campaign>/ (never overwrites)."""
    from logsentinel.episodes.splits import (
        SPLIT_DIR,
        freeze,
        make_heldout_workload,
        make_loso,
        make_main,
    )
    from logsentinel.episodes.store import load_manifest

    df = load_manifest()
    df = df[df.get("campaign", pd.Series("pilot", index=df.index)).fillna("pilot") == campaign]
    if df.empty:
        typer.echo(f"no usable episodes for campaign {campaign!r}")
        raise typer.Exit(1)
    out = SPLIT_DIR / campaign
    made = {"main": make_main(df, seed)}
    made.update({f"loso_{s}": make_loso(df, s, seed) for s in sorted(df["scenario"].unique())})
    made.update(
        {
            f"heldout_{p}": make_heldout_workload(df, p, seed)
            for p in sorted(df["workload_profile"].unique())
        }
    )
    for name, parts in made.items():
        path = freeze(name, parts, df, seed, out_dir=out)
        typer.echo(f"{path.name}: " + ", ".join(f"{k}={len(v)}" for k, v in parts.items()))


if __name__ == "__main__":
    app()
