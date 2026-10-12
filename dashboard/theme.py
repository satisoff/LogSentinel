"""Chart tokens (light/dark) and Plotly helpers for the dashboard.

Palette: the dataviz reference instance. The 3 service series use categorical slots 1-3, validated
all-pairs in both modes (scripts/validate_palette.js, 2026-10-12). Light slot 3 (aqua) is below 3:1
on the light surface, so service lines always carry direct end labels as well as a legend.
"""

from __future__ import annotations

import plotly.graph_objects as go

TOKENS = {
    "light": {
        "surface": "#fcfcfb",
        "ink": "#0b0b0b",
        "ink2": "#52514e",
        "muted": "#898781",
        "grid": "#e1e0d9",
        "axis": "#c3c2b7",
        "series": ["#2a78d6", "#eb6834", "#1baf7a"],
        "band": "rgba(42,120,214,0.16)",
        "hidden": "rgba(137,135,129,0.10)",
    },
    "dark": {
        "surface": "#1a1a19",
        "ink": "#f0efec",
        "ink2": "#c3c2b7",
        "muted": "#898781",
        "grid": "#2c2c2a",
        "axis": "#383835",
        "series": ["#3987e5", "#d95926", "#199e70"],
        "band": "rgba(57,135,229,0.22)",
        "hidden": "rgba(137,135,129,0.14)",
    },
}
# Status colours are reserved for actions/thresholds and always paired with an icon + label.
STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}
ACTION_STATUS = {
    "N": ("good", "✓", "No action"),
    "O": ("warning", "◔", "Observe"),
    "I": ("serious", "▲", "Investigate"),
    "E": ("critical", "■", "Escalate / consider rollback"),
}
SERVICES = ("gateway", "orders", "inventory")
FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'


def base_figure(tok: dict, title: str, y_title: str, height: int = 260) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(
        title={"text": title, "font": {"size": 14, "color": tok["ink"]}, "x": 0, "xanchor": "left"},
        height=height,
        margin={"l": 56, "r": 90, "t": 40, "b": 36},
        paper_bgcolor=tok["surface"],
        plot_bgcolor=tok["surface"],
        font={"family": FONT, "color": tok["ink2"], "size": 12},
        hovermode="x unified",
        hoverlabel={"bgcolor": tok["surface"], "font": {"color": tok["ink"]}},
        showlegend=False,
        xaxis={
            "title": {"text": "minutes from change (t0)", "font": {"color": tok["muted"]}},
            "gridcolor": tok["grid"], "linecolor": tok["axis"], "zeroline": False,
            "tickfont": {"color": tok["muted"]},
        },
        yaxis={
            "title": {"text": y_title, "font": {"color": tok["muted"]}},
            "gridcolor": tok["grid"], "linecolor": tok["axis"], "zeroline": False,
            "tickfont": {"color": tok["muted"]}, "rangemode": "tozero",
        },
    )  # fmt: skip
    return fig


def mark_times(fig: go.Figure, tok: dict, cutoff_min: float, x_max: float) -> None:
    """t0 line, decision-time line, and a wash over data the system could not see yet."""
    fig.add_vline(x=0, line={"color": tok["ink2"], "width": 1})
    fig.add_annotation(x=0, y=1, yref="paper", text="change", showarrow=False,
                       xanchor="left", yanchor="bottom", font={"color": tok["ink2"], "size": 11})  # fmt: skip
    if cutoff_min < x_max:
        fig.add_vrect(x0=cutoff_min, x1=x_max, fillcolor=tok["hidden"], line_width=0, layer="below")
        fig.add_vline(x=cutoff_min, line={"color": tok["ink2"], "width": 1, "dash": "dot"})
        fig.add_annotation(x=cutoff_min, y=1, yref="paper", text="decision time · later data hidden from system",
                           showarrow=False, xanchor="left", yanchor="bottom",
                           font={"color": tok["muted"], "size": 11})  # fmt: skip


def threshold(fig: go.Figure, tok: dict, y: float, label: str, status: str) -> None:
    fig.add_hline(y=y, line={"color": STATUS[status], "width": 1, "dash": "dash"})
    fig.add_annotation(x=1, xref="paper", y=y, text=label, showarrow=False, xanchor="left",
                       font={"color": tok["ink2"], "size": 11})  # fmt: skip
