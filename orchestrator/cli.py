"""ls-change: testbed and change-episode control.

Examples:
  ls-change up                         start the testbed (builds images on first run)
  ls-change scenarios                  list scenario manifests
  ls-change plan S5 --seed 3           show the resolved plan without running it
  ls-change run S1 --seed 1            run one full episode (~65 min with default windows)
  ls-change run S1 --seed 1 --pre-min 2 --horizon-min 3 --warmup-min 1   quick smoke run
  ls-change batch S1 S4 --seeds 0-4    run several episodes back to back
  ls-change down                       stop the testbed (frees RAM)
"""

from __future__ import annotations

import json
import logging
import sys

import typer

from orchestrator import testbed as tb
from orchestrator.scenarios import list_scenarios, load_spec, resolve

app = typer.Typer(add_completion=False, help=__doc__)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )


@app.command()
def up(grafana: bool = False) -> None:
    """Start the testbed and wait until all services are healthy."""
    _setup_logging()
    tb.LIVE_LOGS.mkdir(parents=True, exist_ok=True)
    args = ["--profile", "grafana"] if grafana else []
    result = tb.compose(*args, "up", "-d", "--build", check=False)
    if result.returncode != 0:
        typer.echo(result.stderr, err=True)
        raise typer.Exit(1)
    tb.wait_healthy()
    typer.echo(
        "testbed healthy: gateway :8080, prometheus :9090" + (", grafana :3000" if grafana else "")
    )


@app.command()
def down(volumes: bool = typer.Option(False, help="Also delete DB/Prometheus volumes")) -> None:
    """Stop the testbed."""
    args = ["down", "-v"] if volumes else ["down"]
    result = tb.compose("--profile", "grafana", *args, check=False)
    typer.echo(result.stderr.strip() or "stopped")


@app.command()
def scenarios() -> None:
    """List scenario manifests."""
    for sid in list_scenarios():
        spec = load_spec(tb.ROOT / "orchestrator" / "scenarios" / f"{sid}.yaml")
        typer.echo(
            f"{spec['id']:4} {spec['name']:38} variants={len(spec.get('change_variants') or [{}])}"
        )


@app.command()
def plan(scenario: str, seed: int = 0) -> None:
    """Print the resolved episode plan for a scenario and seed."""
    typer.echo(json.dumps(resolve(load_spec(scenario), seed).to_dict(), indent=2))


def _parse_seeds(seeds: str) -> list[int]:
    out: list[int] = []
    for part in seeds.split(","):
        lo, _, hi = part.partition("-")
        out.extend(range(int(lo), int(hi or lo) + 1))
    return out


@app.command()
def run(
    scenario: str,
    seed: int = 0,
    pre_min: float | None = typer.Option(None, help="Override baseline window (minutes)"),
    horizon_min: float | None = typer.Option(None, help="Override observation window (minutes)"),
    warmup_min: float = 3.0,
) -> None:
    """Run one change episode and write data/raw/<run_id>/."""
    from orchestrator.runner import run_episode

    _setup_logging()
    p = resolve(load_spec(scenario), seed)
    if pre_min is not None:
        p.pre_min = pre_min
    if horizon_min is not None:
        p.horizon_min = horizon_min
    out = run_episode(p, warmup_min=warmup_min)
    typer.echo(str(out))


@app.command()
def batch(
    scenario_ids: list[str],
    seeds: str = typer.Option("0", help="e.g. '0-4' or '0,2,5'"),
    pre_min: float | None = None,
    horizon_min: float | None = None,
    warmup_min: float = 3.0,
    shard: int = typer.Option(0, help="This worker's shard index (0-based)"),
    shards: int = typer.Option(1, help="Total number of shards (parallel stacks)"),
) -> None:
    """Run episodes for each scenario x seed. Failures are logged and skipped.

    With --shards N, the scenario x seed job list is split round-robin and this process runs
    only the jobs with index % N == shard (one shard per parallel stack, LS_STACK=shard).
    """
    from orchestrator.runner import run_episode

    _setup_logging()
    log = logging.getLogger("orchestrator")
    jobs = [(sid, seed) for sid in scenario_ids for seed in _parse_seeds(seeds)]
    mine = [job for i, job in enumerate(jobs) if i % shards == shard]
    log.info("stack %s: %d of %d jobs", tb.PROJECT, len(mine), len(jobs))
    for sid, seed in mine:
        p = resolve(load_spec(sid), seed)
        if pre_min is not None:
            p.pre_min = pre_min
        if horizon_min is not None:
            p.horizon_min = horizon_min
        try:
            run_episode(p, warmup_min=warmup_min)
        except Exception:  # noqa: BLE001 - keep the batch going; the failure is logged
            log.exception("episode %s seed %d failed", sid, seed)
    tb.set_load(**{**tb.get_load(), "running": False})


if __name__ == "__main__":
    app()
