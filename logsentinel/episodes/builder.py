"""Build episodes from raw runs: aligned series, parsed logs, quality checks, label, manifest.

Input:  data/raw/<run_id>/{change.json, scenario.json, metrics.parquet, logs/<service>.jsonl}
Output: data/episodes/<run_id>/{series.parquet, logs.parquet, change.json, label.json, meta.json}
        data/episodes/episodes.jsonl (rebuilt from all episode directories)

series.parquet holds the full window [t0 - pre, t0 + horizon]. Systems under test must slice
it with a decision-time cutoff (logsentinel.features); only the labeler sees the full horizon.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml
from drain3 import TemplateMiner
from drain3.masking import MaskingInstruction
from drain3.template_miner_config import TemplateMinerConfig

from logsentinel.episodes.labeler import LabelConfig, Oracle, label_episode

log = logging.getLogger(__name__)
logging.getLogger("drain3").setLevel(logging.WARNING)
ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = ROOT / "data" / "raw"
EPISODE_DIR = ROOT / "data" / "episodes"
CONFIG_DIR = ROOT / "experiments" / "configs"

# Client-side SLI series (one value per step, no service label).
CLIENT_COLUMNS = [
    "requests",
    "bad",
    "errors_5xx",
    "client_timeouts",
    "target_rps",
    "gw_latency_p50_ms",
    "gw_latency_p95_ms",
    "gw_latency_p99_ms",
]
COUNT_COLUMNS = {"requests", "bad", "errors_5xx", "client_timeouts"}


@dataclass
class BuildConfig:
    raw: dict[str, Any]

    @classmethod
    def load(cls, path: Path = CONFIG_DIR / "episodes.yaml") -> BuildConfig:
        with open(path, encoding="utf-8") as f:
            return cls(yaml.safe_load(f))

    def __getattr__(self, key: str) -> Any:
        try:
            return self.raw[key]
        except KeyError as exc:
            raise AttributeError(key) from exc


# --- Series alignment --------------------------------------------------------------------------
def align_series(
    metrics: pd.DataFrame, t0: float, pre_min: float, horizon_min: float, step: int, services
) -> pd.DataFrame:
    """Wide table on an exact 15 s grid relative to t0. Missing steps stay NaN."""
    grid = np.arange(-int(pre_min * 60), int(horizon_min * 60) + step, step)
    m = metrics.copy()
    m["t"] = ((m["ts"] - t0) / step).round().astype(int) * step

    client = m[m["name"].isin(CLIENT_COLUMNS)].pivot_table(
        index="t", columns="name", values="value", aggfunc="sum"
    )
    per_svc = m[~m["name"].isin(CLIENT_COLUMNS) & m["service"].isin(services)]
    # exceptions/version carry extra labels; aggregate over them per service
    numeric = per_svc[per_svc["name"] != "version"]
    wide_svc = numeric.pivot_table(
        index="t", columns=["name", "service"], values="value", aggfunc="sum"
    )
    wide_svc.columns = [f"{n}__{s}" for n, s in wide_svc.columns]

    df = pd.DataFrame(index=pd.Index(grid, name="t"))
    df = df.join(client).join(wide_svc)
    for col in CLIENT_COLUMNS:
        if col not in df:
            df[col] = np.nan
    # Prometheus has no series for a labelled counter that never incremented. Where traffic was
    # observed, an absent count is a true zero, not missing data (pilot 1: episodes without any
    # bad request had bad=NaN).
    observed = df["requests"].notna()
    for col in COUNT_COLUMNS - {"requests"}:
        df[col] = df[col].where(df[col].notna() | ~observed, 0.0)
    for svc in services:
        svc_obs = df.get(f"svc_rps__{svc}", pd.Series(np.nan, index=df.index)).notna()
        for name in ("retries", "timeouts", "exceptions"):
            col = f"{name}__{svc}"
            df[col] = (
                df[col].where(df[col].notna() | ~svc_obs, 0.0)
                if col in df
                else np.where(svc_obs, 0.0, np.nan)
            )

    # Service-agnostic aggregates used by the labeler's deviation oracle.
    df["gw_error_rate"] = df["errors_5xx"] / df["requests"].replace(0, np.nan)
    agg = {"svc_cpu": "sum", "svc_rss_mb": "max", "db_latency_p95_ms": "max",
           "retries": "sum", "timeouts": "sum"}  # fmt: skip
    for name, how in agg.items():
        cols = [c for c in df.columns if c.startswith(f"{name}__")]
        if cols:
            frame = df[cols]
            df[name] = frame.sum(axis=1, min_count=1) if how == "sum" else frame.max(axis=1)
        else:
            df[name] = np.nan
    return df.reset_index()


# --- Logs + templates ----------------------------------------------------------------------------
def _miner(cfg: BuildConfig) -> TemplateMiner:
    c = TemplateMinerConfig()
    c.drain_sim_th = cfg.drain3["sim_th"]
    c.drain_depth = cfg.drain3["depth"]
    c.masking_instructions = [
        MaskingInstruction(m["pattern"], m["name"]) for m in cfg.drain3["masks"]
    ]
    return TemplateMiner(config=c)


def parse_logs(log_dir: Path, t0: float, cfg: BuildConfig) -> pd.DataFrame:
    """Parse JSON logs and mine templates online, per service, in time order.

    `new_template` marks lines whose template was first seen after t0 within this episode. Only
    the episode's own past is used, so the flag is available at decision time and cannot leak
    across episodes. A cross-episode template store (fitted on the training split only) is a
    feature-time concern.
    """
    cols = ["ts", "t", "service", "level", "msg", "exc_type", "event", "version"]
    rows = []
    for path in sorted(log_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            ts = pd.Timestamp(d["ts"]).timestamp()
            rows.append([ts, ts - t0, d.get("service", path.stem), d.get("level"), d.get("msg", ""),
                         d.get("exc_type"), d.get("event"), d.get("version")])  # fmt: skip
    df = pd.DataFrame(rows, columns=cols).sort_values("ts", kind="stable").reset_index(drop=True)
    df["template_id"] = pd.Series([None] * len(df), dtype="object")
    df["template"] = pd.Series([None] * len(df), dtype="object")
    df["new_template"] = False
    for service, idx in df.groupby("service").groups.items():
        miner = _miner(cfg)
        seen_pre: set[int] = set()
        first_seen: dict[int, float] = {}
        ids = []
        for i in idx:
            cid = miner.add_log_message(df.at[i, "msg"])["cluster_id"]
            ids.append(cid)
            first_seen.setdefault(cid, df.at[i, "t"])
            if df.at[i, "t"] < 0:
                seen_pre.add(cid)
        templates = {c.cluster_id: c.get_template() for c in miner.drain.clusters}
        df.loc[idx, "template_id"] = [f"{service}:{c}" for c in ids]
        df.loc[idx, "template"] = [templates[c] for c in ids]
        df.loc[idx, "new_template"] = [c not in seen_pre and first_seen[c] >= 0 for c in ids]
    return df.astype({"template_id": "string", "template": "string", "new_template": bool})


# --- Quality checks ------------------------------------------------------------------------------
def quality_checks(
    series: pd.DataFrame,
    metrics: pd.DataFrame,
    change: dict[str, Any],
    scenario: dict[str, Any],
    burn_pre: float,
    label_cfg: LabelConfig,
    cfg: BuildConfig,
) -> tuple[list[str], dict[str, Any]]:
    q = cfg.quality
    flags: list[str] = []
    details: dict[str, Any] = {}
    pre, post = series[series["t"] < 0], series[series["t"] >= 0]

    missing = {
        w: float(part["requests"].isna().mean()) for w, part in (("pre", pre), ("post", post))
    }
    details["missing_frac"] = missing
    if max(missing.values()) > q["max_missing_frac"]:
        flags.append("missing_data")

    near = series[series["t"].abs() <= 300]
    gap, longest = 0, 0
    for present in near["requests"].notna():
        gap = 0 if present else gap + cfg.step_s
        longest = max(longest, gap)
    details["longest_gap_near_t0_s"] = longest
    if longest > q["max_gap_near_t0_s"]:
        flags.append("gap_near_t0")

    expected = (pre["target_rps"] * cfg.step_s).mean()
    observed = pre["requests"].mean()
    load_ratio = float(observed / expected) if expected else float("nan")
    details["pre_load_ratio"] = load_ratio
    if not np.isfinite(load_ratio) or abs(load_ratio - 1) > q["load_tolerance"]:
        flags.append("load_unstable")

    details["burn_pre"] = burn_pre
    if burn_pre >= label_cfg.impact.b_mat:
        flags.append("baseline_contaminated")

    applied = _change_applied(series, metrics, change, scenario, cfg)
    details["change_applied"] = applied
    if not applied:
        flags.append("change_not_applied")

    restarts = {}
    for svc in cfg.app_services:
        col = f"process_start__{svc}"
        if col in series:
            restarts[svc] = int(series[col].dropna().diff().fillna(0).gt(0).sum())
    details["restarts"] = restarts
    if any(restarts.values()):
        flags.append("restarts")

    host = scenario.get("host_context") or {}
    others = {c["name"] for part in host.values() for c in part if "name" in c}
    details["other_containers"] = sorted(others)
    if others:
        flags.append("host_noise")

    # "bad" is decided by the load generator at collection time; labels must use the same SLO.
    run_slo = float(scenario.get("latency_slo_ms", cfg.legacy_latency_slo_ms))
    details["latency_slo_ms"] = {"run": run_slo, "labels": label_cfg.slo.latency_slo_ms}
    if run_slo != label_cfg.slo.latency_slo_ms:
        flags.append("slo_mismatch")

    std = cfg.standard_windows
    if scenario["pre_min"] != std["pre_min"] or scenario["horizon_min"] not in std["horizon_min"]:
        flags.append("nonstandard_windows")
    return flags, details


def _change_applied(series, metrics, change, scenario, cfg) -> bool:
    if change["change_type"] == "workload":
        factor = scenario["change"]["load"]["rps_factor"]
        pre = series.loc[series["t"] < 0, "target_rps"].median()
        post = series.loc[series["t"] >= 0, "target_rps"].max()
        return bool(post >= (1 - cfg.quality["workload_change_tolerance"]) * factor * pre)
    v = metrics[(metrics["name"] == "version") & (metrics["service"] == change["service"])]
    if v.empty:
        return False
    versions = v.assign(version=v["labels"].map(lambda s: json.loads(s).get("version")))
    after = versions[versions["ts"] >= change["t0"]]
    return bool((after["version"] == change["to_version"]).any())


# --- Build ---------------------------------------------------------------------------------------
def build_episode(
    run_dir: Path, out_root: Path = EPISODE_DIR, force: bool = False
) -> dict[str, Any]:
    cfg = BuildConfig.load()
    label_cfg = LabelConfig.load(CONFIG_DIR / "labels.yaml")
    change = json.loads((run_dir / "change.json").read_text())
    scenario = json.loads((run_dir / "scenario.json").read_text())
    out = out_root / run_dir.name
    if (out / "meta.json").exists() and not force:
        return json.loads((out / "meta.json").read_text())

    metrics = pd.read_parquet(run_dir / "metrics.parquet")
    series = align_series(
        metrics, change["t0"], scenario["pre_min"], scenario["horizon_min"], cfg.step_s,
        cfg.app_services,
    )  # fmt: skip
    logs = parse_logs(run_dir / "logs", change["t0"], cfg)

    label_cfg.windows.t_horizon_min = scenario["horizon_min"]
    oracle = Oracle(
        intended_effects=scenario["oracle"].get("intended_effects", {}),
        change_causal=scenario["oracle"].get("change_causal"),
    )
    label = label_episode(series, oracle, label_cfg)
    flags, details = quality_checks(
        series, metrics, change, scenario, label.evidence["burn_pre"], label_cfg, cfg
    )
    rejected = sorted(set(flags) & set(cfg.reject_flags))

    out.mkdir(parents=True, exist_ok=True)
    series.to_parquet(out / "series.parquet", index=False)
    logs.to_parquet(out / "logs.parquet", index=False)
    (out / "change.json").write_text(json.dumps(change, indent=2))
    (out / "label.json").write_text(json.dumps(label.to_dict(), indent=2))
    meta = {
        "episode_id": run_dir.name,
        "change_id": change["change_id"],
        "run_id": change["run_id"],
        "scenario": scenario["scenario"],
        "scenario_name": scenario["name"],
        "variant_index": scenario["variant_index"],
        "seed": scenario["seed"],
        "workload_profile": scenario["workload"]["profile"],
        "workload_rps": scenario["workload"]["rps"],
        "service": change.get("service"),
        "change_type": change["change_type"],
        "t0": change["t0"],
        "pre_min": scenario["pre_min"],
        "horizon_min": scenario["horizon_min"],
        "label": label.label,
        "label_auto": label.label,
        "label_adjudicated": None,
        "label_rule": label.rule,
        "change_causal": label.change_causal,
        "explained": label.explained,
        "ambiguous": label.ambiguous,
        "impact_onset_s": label.impact_onset_s,
        "slo_violation_s": label.slo_violation_s,
        "flags": flags,
        "status": "rejected" if rejected else "ok",
        "reject_reasons": rejected,
        "quality": details,
        "n_log_lines": len(logs),
        "n_new_templates": int(logs.loc[logs["new_template"], "template_id"].nunique()),
        "label_config": label_cfg.version,
        "label_config_sha256": hashlib.sha256(
            (CONFIG_DIR / "labels.yaml").read_bytes()
        ).hexdigest(),
        "campaign": scenario.get("campaign", "pilot"),
        "build_config": cfg.version,
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    log.info("built %s: %s (%s) flags=%s", run_dir.name, label.label, label.rule, flags)
    return meta


def write_manifest(out_root: Path = EPISODE_DIR) -> Path:
    metas = [json.loads(p.read_text()) for p in sorted(out_root.glob("*/meta.json"))]
    path = out_root / "episodes.jsonl"
    path.write_text("".join(json.dumps(m) + "\n" for m in metas), encoding="utf-8")
    return path


def build_all(raw_root: Path = RAW_DIR, out_root: Path = EPISODE_DIR, force: bool = False) -> list:
    metas = []
    for run_dir in sorted(raw_root.glob("run-*")):
        if not (run_dir / "metrics.parquet").exists():
            continue
        try:
            metas.append(build_episode(run_dir, out_root, force=force))
        except Exception:  # noqa: BLE001 - one broken run must not stop the batch
            log.exception("failed to build %s", run_dir.name)
    write_manifest(out_root)
    return metas
