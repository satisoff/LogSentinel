"""End-to-end evaluation of one campaign and split, following evaluation.md v1.

Protocol (no tuning on test):
  1. Train split: fit the change-conditioned shift prior (BA episodes only), the MELODY-inspired
     detector (BA only) and the learned qualifier (all labels, every decision time).
  2. Validation split: choose each score-based system's alert threshold (harmful recall >= 0.95,
     and the alert-budget threshold) and identify the strongest baseline.
  3. Test split: apply everything once. Report metrics with cluster-bootstrap CIs, paired
     differences against the strongest baseline, ablations, cost sensitivity and calibration.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from logsentinel.detectors.baselines import Detector, all_baselines
from logsentinel.episodes.builder import CONFIG_DIR, EPISODE_DIR, ROOT
from logsentinel.episodes.labeler import LabelConfig
from logsentinel.episodes.splits import verify
from logsentinel.episodes.store import load_manifest, load_view
from logsentinel.eval.bootstrap import cluster_bootstrap
from logsentinel.eval.metrics import (
    HARMFUL,
    RECOMMENDED,
    brier,
    budget_threshold,
    ece,
    outcomes,
    select_threshold,
    summarize,
    threshold_decisions,
)
from logsentinel.expected.baseline import (
    ShiftPrior,
    WorkloadBaseline,
    load_driver,
)
from logsentinel.features.extract import extract
from logsentinel.pipeline import load_policy
from logsentinel.qualify.model import QualifierModel
from logsentinel.qualify.policy import choose_action
from logsentinel.qualify.rules import LABELS, qualify

PERSISTENCE_IMPACT = (
    "imp_material_min",
    "imp_severe_min",
    "imp_material_intervals",
    "imp_min_since_material",
    "imp_excess_burn_slope",
)
EPISODE_COLUMNS = [
    "episode_id", "change_id", "scenario", "label", "change_causal",
    "impact_onset_s", "slo_violation_s", "horizon_min",
]  # fmt: skip


@dataclass
class Context:
    root: Path
    campaign: str
    split_name: str
    parts: dict[str, list[str]]
    episodes: pd.DataFrame
    policy: dict
    label_cfg: LabelConfig
    grid: list[float]

    def part(self, name: str) -> pd.DataFrame:
        return self.episodes[self.episodes["episode_id"].isin(self.parts[name])].reset_index(
            drop=True
        )


def load_context(root: Path, campaign: str, split_path: Path) -> Context:
    manifest = load_manifest(root)
    if "campaign" not in manifest:
        manifest["campaign"] = "pilot"
    manifest = manifest[manifest["campaign"].fillna("pilot") == campaign]
    doc = verify(split_path, manifest, manifest=root / "episodes.jsonl")
    policy = load_policy()
    return Context(
        root=root,
        campaign=campaign,
        split_name=doc["name"],
        parts=doc["episodes"],
        episodes=manifest[EPISODE_COLUMNS].reset_index(drop=True),
        policy=policy,
        label_cfg=LabelConfig.load(CONFIG_DIR / "labels.yaml"),
        grid=[float(g) for g in policy["decision_times_min"]],
    )


def _cutoffs(ctx: Context, episode: pd.Series) -> list[float]:
    return [g for g in ctx.grid if g <= episode["horizon_min"]]


# --- change-conditioned shift prior (train only) --------------------------------------------------
def fit_shift_prior(ctx: Context, train: pd.DataFrame) -> ShiftPrior:
    """Typical post-change log-shift beyond the workload model, per (change type, service, metric),
    from BA training episodes only."""
    fcfg = ctx.policy["features"]
    rows = []
    for ep in train[train["label"] == "BA"].itertuples(index=False):
        view = load_view(ep.episode_id, 1e9, ctx.root)
        s, ch = view.series, view.change
        metrics = list(fcfg["key_metrics"])
        if ch.get("service"):
            metrics += [f"{m}__{ch['service']}" for m in fcfg["changed_service_metrics"]]
        rate, pre = load_driver(s), (s["t"] < 0).to_numpy()
        for m in [m for m in metrics if m in s and s.loc[pre, m].notna().any()]:
            model = WorkloadBaseline(**ctx.policy["expected"]).fit(rate[pre], s.loc[pre, m], m)
            if not model.ok:
                continue
            post = ~pre & np.isfinite(s[m].to_numpy(float)) & (rate > 0)
            if post.sum() < 4:
                continue
            resid = np.log(np.clip(s[m].to_numpy(float)[post], 0, None) + model.c) - (
                model.a + model.b * np.log(rate[post])
            )
            rows.append({"change_type": ch["change_type"], "service": ch.get("service"),
                         "metric": m, "log_shift": float(np.median(resid))})  # fmt: skip
    if not rows:
        return ShiftPrior()
    return ShiftPrior.fit(pd.DataFrame(rows))


# --- feature and score tables ---------------------------------------------------------------------
def feature_table(ctx: Context, episodes: pd.DataFrame, prior: ShiftPrior | None) -> pd.DataFrame:
    rows = []
    for _, ep in episodes.iterrows():
        for c in _cutoffs(ctx, ep):
            fr = extract(
                load_view(ep["episode_id"], c * 60, ctx.root), ctx.policy, ctx.label_cfg, prior
            )
            rows.append({"episode_id": ep["episode_id"], "cutoff_min": c, **fr.features})
    return pd.DataFrame(rows).fillna(0.0)


def score_table(ctx: Context, detectors: list[Detector], episodes: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, ep in episodes.iterrows():
        for c in _cutoffs(ctx, ep):
            view = load_view(ep["episode_id"], c * 60, ctx.root)
            for d in detectors:
                rows.append(
                    {
                        "system": d.name,
                        "episode_id": ep["episode_id"],
                        "cutoff_min": c,
                        "score": d.score(view),
                    }
                )
    return pd.DataFrame(rows)


def fit_melody(ctx: Context, detector: Detector, train: pd.DataFrame) -> Detector:
    views, labels = [], []
    for _, ep in train.iterrows():
        for c in _cutoffs(ctx, ep):
            views.append(load_view(ep["episode_id"], c * 60, ctx.root))
            labels.append(ep["label"])
    return detector.fit(views, labels)


# --- LogSentinel decision variants ----------------------------------------------------------------
def rule_decisions(features: pd.DataFrame, rules: dict) -> pd.DataFrame:
    acts = []
    for _, row in features.iterrows():
        label = qualify(row.drop(["episode_id", "cutoff_min"]).to_dict(), rules).label
        acts.append(RECOMMENDED[label])
    return features[["episode_id", "cutoff_min"]].assign(action=acts)


def cost_decisions(probs: pd.DataFrame, keys: pd.DataFrame, policy: dict) -> pd.DataFrame:
    acts = [
        choose_action(dict(zip(LABELS, p, strict=True)), policy)[0]
        for p in probs[list(LABELS)].to_numpy()
    ]
    return keys[["episode_id", "cutoff_min"]].assign(action=acts)


def argmax_decisions(probs: pd.DataFrame, keys: pd.DataFrame) -> pd.DataFrame:
    return keys[["episode_id", "cutoff_min"]].assign(
        action=[RECOMMENDED[x] for x in probs[list(LABELS)].idxmax(axis=1)]
    )


def harm_scores(probs: pd.DataFrame, keys: pd.DataFrame) -> pd.DataFrame:
    return keys[["episode_id", "cutoff_min"]].assign(
        score=(probs["AR"] + probs["CR"]).to_numpy(),
        p_cr=probs["CR"].to_numpy(),
        p_ar=probs["AR"].to_numpy(),
    )


def matched_decisions(scores: pd.DataFrame, threshold: float) -> pd.DataFrame:
    """Recall-matched: alert when P(AR)+P(CR) >= threshold; escalate when CR is the likelier."""
    alert = scores["score"].to_numpy() >= threshold
    esc = scores["p_cr"].to_numpy() > scores["p_ar"].to_numpy()
    return scores[["episode_id", "cutoff_min"]].assign(
        action=np.where(alert, np.where(esc, "E", "I"), "N")
    )


def train_model(
    feat_train: pd.DataFrame, train: pd.DataFrame, drop: tuple[str, ...], seeds: int
) -> list[QualifierModel]:
    lab = train.set_index("episode_id")
    X = feat_train.drop(columns=["episode_id", "cutoff_min"])
    y = feat_train["episode_id"].map(lab["label"])
    g = feat_train["episode_id"].map(lab["change_id"])
    return [QualifierModel(drop_prefixes=drop, seed=s).fit(X, y, g) for s in range(seeds)]


def ensemble_proba(models: list[QualifierModel], feats: pd.DataFrame) -> pd.DataFrame:
    X = feats.drop(columns=["episode_id", "cutoff_min"])
    return sum(m.predict_proba(X) for m in models) / len(models)


# --- cost sensitivity -----------------------------------------------------------------------------
def scaled_costs(costs: dict, miss: float, false_alert: float) -> dict:
    c = {k: list(v) for k, v in costs.items()}
    for lab in ("AR", "CR"):  # missed regressions: no action / observe on harmful episodes
        c[lab][0] *= miss
        c[lab][1] *= miss
    for lab in ("BA", "TA"):  # false alerts: investigate / escalate on non-harmful episodes
        c[lab][2] *= false_alert
        c[lab][3] *= false_alert
    return c


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True
        ).stdout.strip()
    except OSError:
        return "unknown"


# --- main entry -----------------------------------------------------------------------------------
def run(
    ctx: Context, out_dir: Path, n_boot: int = 2000, seeds: int = 5, recall: float = 0.95
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    policy, costs, actions = ctx.policy, ctx.policy["costs"], ctx.policy["actions"]
    train, val, test = ctx.part("train"), ctx.part("val"), ctx.part("test")
    everything = pd.concat([train, val, test], ignore_index=True)

    # 1. train-split fits
    prior = fit_shift_prior(ctx, train)
    feats = feature_table(ctx, everything, prior)
    feats_noprior = feature_table(ctx, everything, None)
    feats.to_parquet(out_dir / "features.parquet", index=False)
    detectors = all_baselines()
    fit_melody(ctx, detectors[-1], train)
    scores = score_table(ctx, detectors, everything)
    scores.to_parquet(out_dir / "baseline_scores.parquet", index=False)

    def rows_of(table: pd.DataFrame, part: pd.DataFrame) -> pd.DataFrame:
        return table[table["episode_id"].isin(part["episode_id"])].reset_index(drop=True)

    models = train_model(rows_of(feats, train), train, (), seeds)
    probs = {
        name: ensemble_proba(models, rows_of(feats, part))
        for name, part in (("val", val), ("test", test))
    }
    keys = {name: rows_of(feats, part) for name, part in (("val", val), ("test", test))}

    # 2. validation: thresholds and the strongest baseline
    systems_test: dict[str, pd.DataFrame] = {}
    thresholds: dict[str, dict[str, Any]] = {}
    val_rank = []
    budget = float(val["label"].isin(HARMFUL).mean())
    for name in [d.name for d in detectors]:
        sv, st = (
            rows_of(scores[scores.system == name], val),
            rows_of(scores[scores.system == name], test),
        )
        thr, reached = select_threshold(sv, val, costs, actions, recall)
        vsum = summarize(outcomes(val, threshold_decisions(sv, thr), costs, actions))
        thresholds[name] = {
            "recall_threshold": thr,
            "recall_reached_on_val": reached,
            "budget_threshold": budget_threshold(sv, val, budget),
            "val": vsum,
        }
        val_rank.append((vsum["false_alert_rate_nonharmful"], -vsum["harmful_recall"], name))
        systems_test[name] = outcomes(test, threshold_decisions(st, thr), costs, actions)
    strongest = sorted(val_rank)[0][2]

    hv, ht = harm_scores(probs["val"], keys["val"]), harm_scores(probs["test"], keys["test"])
    thr, reached = select_threshold(hv, val, costs, actions, recall)
    thresholds["LS_model_matched"] = {
        "recall_threshold": thr,
        "recall_reached_on_val": reached,
        "budget_threshold": budget_threshold(hv, val, budget),
    }
    systems_test["LS_model_matched"] = outcomes(test, matched_decisions(ht, thr), costs, actions)
    systems_test["LS_model_cost"] = outcomes(
        test, cost_decisions(probs["test"], keys["test"], policy), costs, actions
    )
    systems_test["LS_rules"] = outcomes(
        test, rule_decisions(keys["test"], policy["rules"]), costs, actions
    )

    # 3. test metrics with cluster-bootstrap CIs and paired differences vs the strongest baseline
    boot = cluster_bootstrap(systems_test, summarize, n=n_boot, reference=strongest)
    for name, o in systems_test.items():
        o.to_csv(out_dir / f"outcomes_{name}.csv", index=False)

    # alert-budget operating point
    budget_rows = {}
    for name in [d.name for d in detectors]:
        st = rows_of(scores[scores.system == name], test)
        budget_rows[name] = summarize(
            outcomes(
                test, threshold_decisions(st, thresholds[name]["budget_threshold"]), costs, actions
            )
        )
    budget_rows["LS_model_matched"] = summarize(
        outcomes(
            test,
            matched_decisions(ht, thresholds["LS_model_matched"]["budget_threshold"]),
            costs,
            actions,
        )
    )

    # seed spread of the learned model (each seed alone, recall-matched on val)
    seed_rows = []
    for m in models:
        pv, pt = ensemble_proba([m], keys["val"]), ensemble_proba([m], keys["test"])
        sv_, st_ = harm_scores(pv, keys["val"]), harm_scores(pt, keys["test"])
        t_, _ = select_threshold(sv_, val, costs, actions, recall)
        seed_rows.append(
            {
                "seed": m.seed,
                **summarize(outcomes(test, matched_decisions(st_, t_), costs, actions)),
            }
        )

    # ablations (recall-matched learned qualifier, retrained without one component)
    persistence = ("per_",) + PERSISTENCE_IMPACT
    ablations = {
        "full": ((), feats),
        "no_change_context": (("chg_",), feats_noprior),
        "no_logs (metrics only)": (("log_",), feats),
        "no_metric_deviation": (("dev_", "per_"), feats),
        "logs_only (no fusion)": (("dev_", "per_", "imp_"), feats),
        "no_persistence": (persistence, feats),
        "no_impact_slo": (("imp_",), feats),
    }
    ablation_rows = []
    for name, (drop, table) in ablations.items():
        ms = models if name == "full" else train_model(rows_of(table, train), train, drop, seeds)
        sv_ = harm_scores(ensemble_proba(ms, rows_of(table, val)), rows_of(table, val))
        st_ = harm_scores(ensemble_proba(ms, rows_of(table, test)), rows_of(table, test))
        t_, ok = select_threshold(sv_, val, costs, actions, recall)
        summary = summarize(outcomes(test, matched_decisions(st_, t_), costs, actions))
        ablation_rows.append({"ablation": name, "recall_reached_on_val": ok, **summary})
    no_adaptive = summarize(
        outcomes(test, argmax_decisions(probs["test"], keys["test"]), costs, actions)
    )
    ablation_rows.append(
        {
            "ablation": "no_adaptive_policy (argmax -> action)",
            "recall_reached_on_val": None,
            **no_adaptive,
        }
    )

    # cost sensitivity: the cost-policy variant re-decides under each matrix; others keep actions
    sens_rows = []
    for miss in (0.5, 1, 2, 5):
        for fa in (0.5, 1, 2):
            c = scaled_costs(costs, miss, fa)
            row = {"miss_x": miss, "false_alert_x": fa}
            for name in systems_test:
                if name == "LS_model_cost":
                    p2 = {**policy, "costs": c}
                    o = outcomes(test, cost_decisions(probs["test"], keys["test"], p2), c, actions)
                else:
                    o = outcomes(test, _final_actions_as_plan(systems_test[name], ctx), c, actions)
                row[name] = summarize(o)["utility"]
            sens_rows.append(row)

    # calibration of the learned qualifier on test rows
    y_test = keys["test"]["episode_id"].map(test.set_index("episode_id")["label"])
    calibration = {"brier": brier(probs["test"], y_test), "ece": ece(probs["test"], y_test)}

    results = {
        "campaign": ctx.campaign, "split": ctx.split_name, "git_commit": _git_commit(),
        "n": {k: len(v) for k, v in (("train", train), ("val", val), ("test", test))},
        "label_mix_test": test["label"].value_counts().to_dict(),
        "recall_target": recall, "alert_budget": budget, "strongest_baseline_on_val": strongest,
        "thresholds": thresholds, "shift_prior_entries": len(prior.table),
        "test": boot.to_dict("records"), "budget_operating_point": budget_rows,
        "seeds": seed_rows, "ablations": ablation_rows, "cost_sensitivity": sens_rows,
        "calibration": calibration,
    }  # fmt: skip
    (out_dir / "results.json").write_text(
        json.dumps(results, indent=2, default=_json_default), encoding="utf-8"
    )
    (out_dir / "summary.md").write_text(render_summary(results), encoding="utf-8")
    return results


def _final_actions_as_plan(o: pd.DataFrame, ctx: Context) -> pd.DataFrame:
    """Rebuild a minimal action plan reproducing each episode's first alert / final action."""
    rows = []
    for r in o.itertuples(index=False):
        t = r.first_alert_min if r.alerted else ctx.grid[0]
        rows.append({"episode_id": r.episode_id, "cutoff_min": t, "action": r.final_action})
    return pd.DataFrame(rows)


def _json_default(x):
    if isinstance(x, np.generic):
        return x.item()
    if isinstance(x, float) and np.isnan(x):
        return None
    return str(x)


def render_summary(r: dict[str, Any]) -> str:
    t = pd.DataFrame(r["test"])
    main = t[~t["metric"].str.startswith("diff:")]
    keep = ["false_alerts_per_episode", "harmful_recall", "harmful_precision", "cr_recall",
            "delay_median_min", "unnecessary_rollbacks_per_episode", "utility"]  # fmt: skip
    table = (
        main[main.metric.isin(keep)]
        .assign(
            value=lambda d: d.apply(lambda x: f"{x.estimate:.3g} [{x.lo:.3g}, {x.hi:.3g}]", axis=1)
        )
        .pivot(index="system", columns="metric", values="value")[keep]
    )
    diffs = (
        t[t["metric"].isin([f"diff:{k}" for k in keep])]
        .assign(
            value=lambda d: d.apply(
                lambda x: f"{x.estimate:+.3g} [{x.lo:+.3g}, {x.hi:+.3g}]", axis=1
            )
        )
        .pivot(index="system", columns="metric", values="value")
    )
    lines = [
        f"# Evaluation — campaign `{r['campaign']}`, split `{r['split']}`",
        "",
        f"git {r['git_commit'][:10]} · n train/val/test = {r['n']} · "
        f"test labels {r['label_mix_test']}",
        f"Recall target {r['recall_target']} (thresholds chosen on validation) · alert budget "
        f"{r['alert_budget']:.2f} alerts/episode · strongest baseline on validation: "
        f"**{r['strongest_baseline_on_val']}**",
        "",
        "## Test results (estimate [95% cluster-bootstrap CI over change_id])",
        "",
        table.to_markdown(),
        "",
        f"## Paired differences vs {r['strongest_baseline_on_val']} (system − baseline)",
        "",
        diffs.to_markdown() if len(diffs) else "(none)",
        "",
        "## Ablations (learned qualifier, recall-matched on validation)",
        "",
        pd.DataFrame(r["ablations"])[["ablation", "recall_reached_on_val", *keep]]
        .round(3)
        .to_markdown(index=False),
        "",
        "## Seed spread (learned qualifier, each seed alone)",
        "",
        pd.DataFrame(r["seeds"])[["seed", *keep]].round(3).to_markdown(index=False),
        "",
        "## Cost sensitivity (utility)",
        "",
        pd.DataFrame(r["cost_sensitivity"]).round(3).to_markdown(index=False),
        "",
        f"## Calibration (learned qualifier, test rows): Brier {r['calibration']['brier']:.3f}, "
        f"ECE {r['calibration']['ece']:.3f}",
        "",
    ]
    return "\n".join(lines)


def main(
    campaign: str,
    split: str = "main",
    root: Path = EPISODE_DIR,
    splits_dir: Path | None = None,
    out: Path | None = None,
    n_boot: int = 2000,
    seeds: int = 5,
) -> dict[str, Any]:
    splits_dir = splits_dir or ROOT / "data" / "splits" / campaign
    out = out or ROOT / "experiments" / "reports" / f"{campaign}-{split}"
    ctx = load_context(root, campaign, splits_dir / f"{split}.json")
    return run(ctx, out, n_boot=n_boot, seeds=seeds)
