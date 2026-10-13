"""Evaluate one campaign/split end to end (see logsentinel/eval/experiment.py).

.venv/Scripts/python.exe experiments/evaluate.py --campaign main-v1 --split main
.venv/Scripts/python.exe experiments/evaluate.py --campaign main-v1 --split loso_S5 --bootstrap 500
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from logsentinel.episodes.builder import EPISODE_DIR  # noqa: E402
from logsentinel.eval.experiment import main  # noqa: E402


def cli(
    campaign: str = "main-v1",
    split: str = "main",
    root: Path = EPISODE_DIR,
    splits_dir: Path | None = None,
    out: Path | None = None,
    bootstrap: int = 2000,
    seeds: int = 5,
) -> None:
    res = main(campaign, split, root, splits_dir, out, bootstrap, seeds)
    typer.echo(f"strongest baseline on validation: {res['strongest_baseline_on_val']}")
    typer.echo(f"report: {out or Path('experiments/reports') / f'{campaign}-{split}'}")


if __name__ == "__main__":
    typer.run(cli)
