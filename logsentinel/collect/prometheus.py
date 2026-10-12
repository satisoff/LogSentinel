"""Export a time window of testbed telemetry from Prometheus as a long-format table."""

from __future__ import annotations

import json

import pandas as pd
import requests

STEP_S = 15
_HQ = "histogram_quantile({q}, sum by ({by}le) (rate({metric}_bucket[30s]))) * 1000"

# name -> PromQL. Counts per step use increase() over exactly one step.
QUERIES: dict[str, str] = {
    # user-facing SLI (client side)
    "requests": "sum(increase(client_requests_total[15s]))",
    "bad": "sum(increase(client_bad_total[15s]))",
    "errors_5xx": 'sum(increase(client_requests_total{outcome="error_5xx"}[15s]))',
    "client_timeouts": (
        'sum(increase(client_requests_total{outcome=~"timeout|conn_error|dropped"}[15s]))'
    ),
    "target_rps": "max(client_target_rps)",
    "gw_latency_p50_ms": _HQ.format(q=0.50, by="", metric="client_latency_seconds"),
    "gw_latency_p95_ms": _HQ.format(q=0.95, by="", metric="client_latency_seconds"),
    "gw_latency_p99_ms": _HQ.format(q=0.99, by="", metric="client_latency_seconds"),
    # per service
    "svc_rps": "sum by (service) (rate(http_requests_total[30s]))",
    "svc_5xx_rps": 'sum by (service) (rate(http_requests_total{status=~"5.."}[30s]))',
    "svc_latency_p95_ms": _HQ.format(
        q=0.95, by="service, ", metric="http_request_duration_seconds"
    ),
    "db_latency_p95_ms": _HQ.format(q=0.95, by="service, ", metric="db_query_duration_seconds"),
    "db_pool_in_use": "max by (service) (db_pool_in_use)",
    "retries": "sum by (service) (increase(upstream_retries_total[15s]))",
    "timeouts": "sum by (service) (increase(timeouts_total[15s]))",
    "exceptions": "sum by (service, type) (increase(exceptions_total[15s]))",
    "leak_objects": "max by (service) (leak_objects)",
    "version": "max by (service, version) (app_version_info) == 1",
    # process-level (labelled by instance, mapped to service below)
    "svc_cpu": "sum by (instance) (rate(process_cpu_seconds_total[30s]))",
    "svc_rss_mb": "max by (instance) (process_resident_memory_bytes) / 1e6",
    "gc_gen2": 'sum by (instance) (increase(python_gc_collections_total{generation="2"}[15s]))',
    "process_start": "max by (instance) (process_start_time_seconds)",
    "up": "max by (instance) (up)",
}


def range_query(
    base_url: str, query: str, start: float, end: float, step: int = STEP_S
) -> list[dict]:
    r = requests.get(
        f"{base_url}/api/v1/query_range",
        params={"query": query, "start": start, "end": end, "step": step},
        timeout=60,
    )
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "success":
        raise RuntimeError(f"Prometheus error for {query!r}: {body}")
    return body["data"]["result"]


def export_window(base_url: str, start: float, end: float) -> pd.DataFrame:
    """Return columns: ts (unix s), name, service, labels (json), value."""
    rows = []
    for name, query in QUERIES.items():
        for series in range_query(base_url, query, start, end):
            labels = dict(series["metric"])
            service = (
                labels.pop("service", None) or labels.pop("instance", "").split(":")[0] or None
            )
            for ts, value in series["values"]:
                rows.append(
                    (float(ts), name, service, json.dumps(labels, sort_keys=True), float(value))
                )
    return pd.DataFrame(rows, columns=["ts", "name", "service", "labels", "value"])
