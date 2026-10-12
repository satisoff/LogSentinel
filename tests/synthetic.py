"""Synthetic raw runs in the exact format the orchestrator writes (no Docker needed)."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

T0 = 1_791_780_000.0
STEP = 15
SERVICES = ("gateway", "orders", "inventory")
CLIENT = ["requests", "bad", "errors_5xx", "target_rps", "gw_latency_p95_ms"]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="milliseconds")


def make_run(
    root: Path,
    name: str = "run-syn-S1-s0",
    *,
    pre_min: int = 30,
    horizon_min: int = 30,
    rps: float = 20.0,
    rps_post: float | None = None,
    bad_frac: float = 0.002,
    mutate: Callable[[pd.DataFrame], None] | None = None,
    change: dict | None = None,
    extra_logs: list[dict] | None = None,
    seed: int = 0,
) -> Path:
    """Wide synthetic telemetry -> long metrics.parquet + logs + change/scenario json.

    `mutate(df)` edits the wide frame in place (column t = seconds from t0) to inject behaviour.
    Load-dependent metrics scale with target_rps the way the testbed does (CPU ~ linear,
    latency weakly), so workload changes are realistic for the expected model.
    """
    rng = np.random.default_rng(seed)
    t = np.arange(-pre_min * 60, horizon_min * 60 + STEP, STEP)
    n = len(t)
    target = np.where(t >= 0, rps_post or rps, rps).astype(float)
    load = target / 20.0
    df = pd.DataFrame({"t": t, "target_rps": target})
    df["requests"] = target * STEP
    df["bad"] = rng.binomial(df["requests"].astype(int), bad_frac).astype(float)
    df["errors_5xx"] = 0.0
    df["gw_latency_p95_ms"] = 50 * load**0.3 * np.exp(rng.normal(0, 0.03, n))
    for svc in SERVICES:
        df[f"svc_cpu__{svc}"] = 0.1 * load * np.exp(rng.normal(0, 0.03, n))
        df[f"svc_rss_mb__{svc}"] = 60 * np.exp(rng.normal(0, 0.005, n))
        df[f"svc_latency_p95_ms__{svc}"] = 20 * load**0.3 * np.exp(rng.normal(0, 0.03, n))
        df[f"db_latency_p95_ms__{svc}"] = 10 * np.exp(rng.normal(0, 0.03, n))
        df[f"process_start__{svc}"] = 1000.0
    if mutate:
        mutate(df)

    ch = {"change_id": "SYN-1", "service": "orders", "change_type": "deploy",
          "from_version": "1.0.0", "to_version": "1.1.0", "rollout_pct": 100.0, **(change or {})}  # fmt: skip
    rows = []
    for _, r in df.iterrows():
        ts = T0 + r["t"]
        rows += [(ts, c, None, "{}", float(r[c])) for c in CLIENT]
        for col in df.columns:
            if "__" in col:
                metric, svc = col.split("__")
                rows.append((ts, metric, svc, "{}", float(r[col])))
        for svc in SERVICES:
            v = ch["to_version"] if svc == ch.get("service") and r["t"] >= 0 else "1.0.0"
            rows.append((ts, "version", svc, json.dumps({"version": v}), 1.0))
    run = root / "raw" / name
    (run / "logs").mkdir(parents=True)
    pd.DataFrame(rows, columns=["ts", "name", "service", "labels", "value"]).to_parquet(
        run / "metrics.parquet", index=False
    )
    lines = [
        {
            "ts": _iso(T0 + s),
            "service": "orders",
            "level": "INFO",
            "msg": f"GET /orders/{{order_id}} -> 200 in {5 + s % 7}.3 ms",
        }  # fmt: skip
        for s in range(-pre_min * 60, horizon_min * 60, 20)
    ]
    for x in extra_logs or []:
        lines.append({"service": "orders", "level": "INFO", **x, "ts": _iso(T0 + x["t"])})
    lines.sort(key=lambda d: d["ts"])
    (run / "logs" / "orders.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    ch.update({"run_id": name, "t0": T0})
    scenario = {
        "scenario": name.split("-")[2], "name": "synthetic", "seed": seed, "variant_index": 0,
        "change": {"change_type": ch["change_type"], "load": {"rps_factor": (rps_post or rps) / rps}},
        "workload": {"rps": rps, "profile": "steady"},
        "oracle": {"intended_effects": {}, "change_causal": True},
        "pre_min": pre_min, "horizon_min": horizon_min, "host_context": {"before": [], "after": []},
        "latency_slo_ms": 50.0,
    }  # fmt: skip
    (run / "change.json").write_text(json.dumps(ch))
    (run / "scenario.json").write_text(json.dumps(scenario))
    return run


def post(df: pd.DataFrame, start_min: float = 0, end_min: float = 1e9) -> pd.Series:
    return (df["t"] >= start_min * 60) & (df["t"] < end_min * 60)
