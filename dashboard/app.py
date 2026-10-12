"""LogSentinel dashboard: episode explorer (decision + evidence + telemetry) and dataset overview.

Run:  .venv/Scripts/python.exe -m streamlit run dashboard/app.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from theme import ACTION_STATUS, SERVICES, STATUS, TOKENS, base_figure, mark_times, threshold  # noqa: E402

from logsentinel.episodes.builder import CONFIG_DIR, EPISODE_DIR  # noqa: E402
from logsentinel.episodes.labeler import LabelConfig  # noqa: E402
from logsentinel.episodes.store import load_label, load_manifest  # noqa: E402
from logsentinel.pipeline import decide, load_policy  # noqa: E402
from logsentinel.ts import trailing_ratio  # noqa: E402

st.set_page_config(page_title="LogSentinel", layout="wide")


@st.cache_data
def manifest(include_rejected: bool) -> pd.DataFrame:
    return load_manifest(include_rejected=include_rejected)


@st.cache_data
def series(episode_id: str) -> pd.DataFrame:
    return pd.read_parquet(EPISODE_DIR / episode_id / "series.parquet")


@st.cache_data
def logs(episode_id: str) -> pd.DataFrame:
    return pd.read_parquet(EPISODE_DIR / episode_id / "logs.parquet")


@st.cache_data
def decision(episode_id: str, cutoff_s: float):
    d, fr = decide(episode_id, cutoff_s, return_features=True)
    return d, fr.features, fr.bands


def tokens() -> dict:
    try:
        mode = st.context.theme.type or "light"
    except AttributeError:
        mode = "light"
    return TOKENS["dark" if mode == "dark" else "light"]


def status_tile(col, heading: str, action: str, label: str, note: str) -> None:
    status, icon, text = ACTION_STATUS[action]
    col.markdown(
        f"<div style='border:1px solid rgba(128,128,128,.25);border-radius:8px;padding:10px 14px'>"
        f"<div style='font-size:12px;opacity:.7'>{heading}</div>"
        f"<div style='font-size:22px;font-weight:600'>"
        f"<span style='color:{STATUS[status]}'>{icon}</span> {label} → {text}</div>"
        f"<div style='font-size:12px;opacity:.7'>{note}</div></div>",
        unsafe_allow_html=True,
    )


def band_chart(
    tok,
    band: pd.DataFrame,
    title: str,
    y_title: str,
    cutoff_min: float,
    full: pd.DataFrame | None = None,
    column: str | None = None,
    scale=1.0,
):
    """Expected band exists only up to the decision time; observed data after it is drawn in the hidden region."""
    t = band["t"] / 60
    fig = base_figure(tok, title, y_title)
    x_max = float(t.max())
    if full is not None and column in full:
        later = full[full["t"] > band["t"].max() - 15]
        fig.add_trace(
            go.Scatter(
                x=later["t"] / 60,
                y=later[column] * scale,
                name="observed (hidden from system)",
                mode="lines",
                line={"color": tok["series"][0], "width": 2},
                opacity=0.45,
            )
        )
        x_max = float(full["t"].max() / 60)
    fig.add_trace(go.Scatter(x=t, y=band["hi"] * scale, line={"width": 0}, hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=t, y=band["lo"] * scale, fill="tonexty", fillcolor=tok["band"],
                             line={"width": 0}, name="expected range"))  # fmt: skip
    fig.add_trace(go.Scatter(x=t, y=band["expected"] * scale, name="expected",
                             line={"color": tok["series"][0], "width": 1, "dash": "dot"}))  # fmt: skip
    fig.add_trace(go.Scatter(x=t, y=band["observed"] * scale, name="observed",
                             line={"color": tok["series"][0], "width": 2}))  # fmt: skip
    mark_times(fig, tok, cutoff_min, x_max)
    return fig


def service_chart(tok, s: pd.DataFrame, metric: str, title: str, y_title: str, cutoff_min: float):
    fig = base_figure(tok, title, y_title)
    fig.update_layout(
        showlegend=True,
        legend={"orientation": "h", "y": -0.38, "x": 0},
        height=290,
        margin={"b": 80},
    )
    t = s["t"] / 60
    for i, svc in enumerate(SERVICES):
        col = f"{metric}__{svc}"
        if col not in s:
            continue
        color = tok["series"][i]
        fig.add_trace(go.Scatter(x=t, y=s[col], name=svc, line={"color": color, "width": 2}))
        last = s[col].dropna()
        if len(last):  # direct label (relief for the low-contrast light slot)
            fig.add_annotation(x=float(t[last.index[-1]]), y=float(last.iloc[-1]), text=svc,
                               showarrow=False, xanchor="left", xshift=4,
                               font={"color": tok["ink2"], "size": 11})  # fmt: skip
    mark_times(fig, tok, cutoff_min, float(t.max()))
    return fig


def explorer() -> None:
    tok = tokens()
    policy, lcfg = load_policy(), LabelConfig.load(CONFIG_DIR / "labels.yaml")
    with st.sidebar:
        st.header("Episode")
        include_rejected = st.toggle("Include rejected episodes", value=False)
        m = manifest(include_rejected)
        if m.empty:
            st.info("No episodes yet. Run `ls-change run ...` then `ls-episodes build`.")
            return
        options = m["episode_id"].tolist()
        fmt = dict(
            zip(
                m["episode_id"],
                m["scenario"] + " · " + m["scenario_name"] + " · seed " + m["seed"].astype(str),
                strict=True,
            )
        )
        ep = st.selectbox("Episode", options, format_func=lambda e: f"{fmt[e]}  ({e[-24:]})")
        row = m.set_index("episode_id").loc[ep]
        grid = [g for g in policy["decision_times_min"] if g <= row["horizon_min"]] or [
            row["horizon_min"]
        ]
        cutoff_min = st.select_slider(
            "Decision time (min after change)", options=grid, value=grid[min(2, len(grid) - 1)]
        )
        evaluator = st.toggle("Evaluator view (show ground truth)", value=True,
                              help="Ground truth is never visible to LogSentinel; this toggle only affects the page.")  # fmt: skip

    d, feats, bands = decision(ep, cutoff_min * 60)
    s = series(ep)
    change = f"{row['change_type']} on {row['service'] or 'workload'}"
    st.title("LogSentinel · episode explorer")
    st.caption(f"{row['scenario']} {row['scenario_name']} · {change} · workload {row['workload_profile']} "
               f"{row['workload_rps']} rps · status {row['status']} {', '.join(row['flags']) or ''}")  # fmt: skip

    c1, c2, c3 = st.columns(3)
    status_tile(
        c1, f"LogSentinel decision at t0 + {cutoff_min:g} min", d.action, d.label, f"rule: {d.rule}"
    )
    online = [decision(ep, g * 60)[0] for g in grid]
    first = next((g for g, x in zip(grid, online, strict=True) if x.action in ("I", "E")), None)
    c2.metric(
        "First alert (investigate or escalate)",
        f"t0 + {first:g} min" if first is not None else "none",
    )
    if evaluator:
        gt = load_label(ep)
        action_for = {"BA": "N", "TA": "O", "AR": "I", "CR": "E"}
        status_tile(c3, "Ground truth (labeler, full horizon)", action_for[gt["label"]], gt["label"],
                    f"rule: {gt['rule']} · change caused it: {gt['change_causal']}")  # fmt: skip
    st.markdown(f"**Why:** {d.summary}")

    left, right = st.columns(2)
    if "gw_latency_p95_ms" in bands:
        left.plotly_chart(band_chart(tok, bands["gw_latency_p95_ms"], "User-facing latency p95 vs expected range",
                                     "ms", cutoff_min, s, "gw_latency_p95_ms"), width="stretch", theme=None)  # fmt: skip
    burn = (
        trailing_ratio(s["bad"], s["requests"], lcfg.steps(lcfg.impact.short_window_min))
        / lcfg.slo.budget
    )
    fig = base_figure(tok, "SLO burn rate (1-min window)", "× error budget")
    fig.add_trace(
        go.Scatter(x=s["t"] / 60, y=burn, name="burn", line={"color": tok["series"][0], "width": 2})
    )
    threshold(fig, tok, feats["imp_burn_pre"] + lcfg.impact.b_mat, "material", "serious")
    threshold(fig, tok, lcfg.impact.b_crit, "severe", "critical")
    mark_times(fig, tok, cutoff_min, float(s["t"].max() / 60))
    right.plotly_chart(fig, width="stretch", theme=None)

    left, right = st.columns(2)
    err = 100 * s["errors_5xx"] / s["requests"].replace(0, np.nan)
    fig = base_figure(tok, "5xx error rate", "% of requests")
    fig.add_trace(
        go.Scatter(x=s["t"] / 60, y=err, name="5xx %", line={"color": tok["series"][0], "width": 2})
    )
    threshold(fig, tok, 100 * lcfg.impact.e_crit, "severe", "critical")
    mark_times(fig, tok, cutoff_min, float(s["t"].max() / 60))
    left.plotly_chart(fig, width="stretch", theme=None)
    chg = [k for k in bands if "__" in k]
    if chg:
        pick = right.selectbox("Changed-service metric", chg, label_visibility="collapsed")
        right.plotly_chart(band_chart(tok, bands[pick], f"{pick.replace('__', ' · ')} vs expected range",
                                      pick.split("__")[0], cutoff_min, s, pick), width="stretch", theme=None)  # fmt: skip

    left, right = st.columns(2)
    left.plotly_chart(service_chart(tok, s, "svc_cpu", "CPU by service", "cores", cutoff_min),
                      width="stretch", theme=None)  # fmt: skip
    right.plotly_chart(service_chart(tok, s, "svc_rss_mb", "Memory (RSS) by service", "MB", cutoff_min),
                       width="stretch", theme=None)  # fmt: skip

    st.subheader("Evidence")
    devs = pd.DataFrame(d.evidence["deviations"]).T
    if not devs.empty:
        devs = devs[["frac_out", "longest_out_min", "n_excursions", "slope", "slope_t", "z_now"]]
        st.dataframe(devs.sort_values("frac_out", ascending=False).round(2), width="stretch")
    lg = logs(ep)
    new = lg[lg["new_template"] & (lg["t"] <= cutoff_min * 60)]
    if len(new):
        tbl = new.groupby(["service", "level", "template"]).agg(
            lines=("msg", "size"), first_seen_s=("t", "min")
        )
        st.markdown("**New log templates (first seen after the change)**")
        st.dataframe(tbl.reset_index().round(1), width="stretch", hide_index=True)
    st.markdown("**Decisions over time (online protocol)**")
    st.dataframe(pd.DataFrame([{"t0 + min": g, "label": x.label, "action": x.action, "rule": x.rule}
                               for g, x in zip(grid, online, strict=True)]), hide_index=True)  # fmt: skip


def dataset() -> None:
    st.title("LogSentinel · dataset")
    m = manifest(True)
    if m.empty:
        st.info("No episodes yet.")
        return
    ok = m[m["status"] == "ok"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Episodes", len(m))
    c2.metric("Usable", len(ok))
    c3.metric("Rejected", len(m) - len(ok))
    c4.metric("Change identities", m["change_id"].nunique())
    st.markdown("**Labels by scenario (usable episodes)**")
    if len(ok):
        st.dataframe(
            pd.crosstab(ok["scenario"], ok["label"]).reindex(
                columns=["BA", "TA", "AR", "CR"], fill_value=0
            )
        )
    st.markdown("**All episodes**")
    cols = ["episode_id", "scenario", "change_id", "seed", "workload_profile", "label", "label_rule",
            "ambiguous", "status", "flags"]  # fmt: skip
    st.dataframe(m[cols], width="stretch", hide_index=True)


page = st.sidebar.radio("View", ["Episode explorer", "Dataset"], label_visibility="collapsed")
explorer() if page == "Episode explorer" else dataset()
