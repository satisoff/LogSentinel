"""Run one controlled change episode end to end and write the raw capture.

Timeline:  reset -> warm-up -> baseline (pre_min) -> t0: apply change -> faults at offsets
           -> observe (horizon_min) -> export [t0 - pre_min, t0 + horizon_min] -> done.
All times are taken from the Docker VM clock via Prometheus, so they align with telemetry.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from logsentinel.collect.logs import slice_logs
from logsentinel.collect.prometheus import export_window
from orchestrator import testbed as tb
from orchestrator.scenarios import EpisodePlan

log = logging.getLogger("orchestrator")
RAW_DIR = tb.ROOT / "data" / "raw"
LIVE_LOGS = RAW_DIR / "_live_logs"
EXPORT_LAG_S = 30  # wait for the last scrapes to land before exporting


def _sleep(seconds: float, label: str) -> None:
    log.info("%s: waiting %.1f min", label, seconds / 60)
    time.sleep(max(0.0, seconds))


def apply_change(change: dict[str, Any]) -> dict[str, Any]:
    if change["change_type"] == "workload":
        load = tb.get_load()
        load["rps"] = load["rps"] * change["load"]["rps_factor"]
        load["ramp_s"] = change["load"].get("ramp_s", 0)
        tb.set_load(**load)
        return {"from_version": None, "to_version": None}
    service = change["service"]
    from_version = tb.current_version(service)
    profile = {"version": change["to_version"], **change.get("profile", {})}
    tb.deploy(service, profile, change.get("rollout_pct", 100.0))
    return {"from_version": from_version, "to_version": change["to_version"]}


def _fault_actions(fault: dict[str, Any]):
    kind, params = fault["type"], fault.get("params", {})
    if kind == "db_latency":
        return (lambda: tb.db_latency(**params)), tb.toxiproxy_reset
    if kind == "db_down":
        return (lambda: tb.db_enable(False)), (lambda: tb.db_enable(True))
    if kind == "pause":
        svc = params["service"]
        return (lambda: tb.pause(svc, True)), (lambda: tb.pause(svc, False))
    raise ValueError(f"unknown fault type {kind!r}")


def schedule_faults(
    faults: list[dict[str, Any]], record: list[dict[str, Any]]
) -> list[threading.Timer]:
    timers = []
    for fault in faults:
        start, stop = _fault_actions(fault)
        entry = {"type": fault["type"], "params": fault.get("params", {})}
        record.append(entry)

        def begin(entry=entry, start=start):
            start()
            entry["start_ts"] = tb.prom_time()
            log.info("fault started: %s", entry["type"])

        def end(entry=entry, stop=stop):
            stop()
            entry["end_ts"] = tb.prom_time()
            log.info("fault ended: %s", entry["type"])

        timers.append(threading.Timer(fault["at_min"] * 60, begin))
        timers.append(threading.Timer((fault["at_min"] + fault["duration_min"]) * 60, end))
    for t in timers:
        t.daemon = True
        t.start()
    return timers


def run_episode(plan: EpisodePlan, warmup_min: float = 3.0, out_root: Path = RAW_DIR) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    run_id = f"run-{stamp}-{plan.scenario}-s{plan.seed}"
    out = out_root / run_id
    out.mkdir(parents=True)
    log.info("episode %s (%s, change %s)", run_id, plan.name, plan.change_id)

    tb.wait_healthy()
    tb.reset_all()
    host_before = tb.host_context()
    w = plan.workload
    tb.set_load(rps=w["rps"], profile=w["profile"], seed=plan.seed, ramp_s=0, running=True)
    _sleep(warmup_min * 60, "warm-up")
    _sleep(plan.pre_min * 60, "baseline")

    versions = apply_change(plan.change)
    t0 = tb.prom_time()
    log.info("change applied at t0=%.1f", t0)
    fault_record: list[dict[str, Any]] = []
    timers = schedule_faults(plan.faults, fault_record)
    try:
        _sleep(plan.horizon_min * 60 + EXPORT_LAG_S, "observation")
    finally:
        for t in timers:
            t.cancel()
        tb.toxiproxy_reset()
        for svc in tb.APP_SERVICES:
            tb.compose("unpause", svc, check=False)

    host_after = tb.host_context()
    start, end = t0 - plan.pre_min * 60, t0 + plan.horizon_min * 60
    metrics = export_window(tb.PROMETHEUS_URL, start, end)
    metrics.to_parquet(out / "metrics.parquet", index=False)
    log_counts = slice_logs(LIVE_LOGS, out / "logs", start, end)

    change_event = {  # visible to systems under test
        "change_id": plan.change_id,
        "run_id": run_id,
        "t0": t0,
        "t0_iso": datetime.fromtimestamp(t0, UTC).isoformat(),
        "service": plan.change.get("service"),
        "change_type": plan.change["change_type"],
        "scope": plan.change.get("scope", "single_service"),
        "rollout_pct": plan.change.get("rollout_pct", 100.0),
        "diff_summary": plan.change.get("diff_summary", {}),
        **versions,
    }
    scenario_record = {  # oracle: labeler/evaluator only
        **plan.to_dict(),
        "run_id": run_id,
        "faults_applied": fault_record,
        "window": {"start": start, "end": end, "t0": t0},
        "warmup_min": warmup_min,
        # data-quality context: other containers competing for the laptop's CPU/RAM
        "host_context": {"before": host_before, "after": host_after},
        "log_lines": log_counts,
        "metric_rows": len(metrics),
    }
    (out / "change.json").write_text(json.dumps(change_event, indent=2))
    (out / "scenario.json").write_text(json.dumps(scenario_record, indent=2))
    log.info("episode written to %s (%d metric rows, logs %s)", out, len(metrics), log_counts)
    return out
