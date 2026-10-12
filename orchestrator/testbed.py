"""Thin client for the running testbed: compose, service admin APIs, Toxiproxy, Prometheus clock."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "testbed" / "docker-compose.yml"

# Several isolated stacks can run on one machine (cloud VM). Stack k uses host ports +100*k,
# its own compose project and its own live-log directory. Stack 0 keeps the original layout.
STACK = int(os.environ.get("LS_STACK", "0"))
PROJECT = "logsentinel" if STACK == 0 else f"logsentinel-s{STACK}"
_OFF = 100 * STACK
PORTS = {
    "gateway": 8080 + _OFF,
    "orders": 8081 + _OFF,
    "inventory": 8082 + _OFF,
    "loadgen": 8089 + _OFF,
    "prometheus": 9090 + _OFF,
    "toxiproxy": 8474 + _OFF,
    "grafana": 3000 + _OFF,
}
LIVE_LOGS = ROOT / "data" / "raw" / ("_live_logs" if STACK == 0 else f"_live_logs_s{STACK}")
SERVICE_URLS = {
    svc: f"http://127.0.0.1:{PORTS[svc]}" for svc in ("gateway", "orders", "inventory", "loadgen")
}
APP_SERVICES = ("gateway", "orders", "inventory")
COMPOSE_SERVICES = {"postgres", "toxiproxy", "prometheus", "grafana", "loadgen", *APP_SERVICES}
PROMETHEUS_URL = f"http://127.0.0.1:{PORTS['prometheus']}"
TOXIPROXY_URL = f"http://127.0.0.1:{PORTS['toxiproxy']}"
TIMEOUT = 5


def compose_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update({f"LS_PORT_{svc.upper()}": str(port) for svc, port in PORTS.items()})
    env["LS_LIVE_LOGS"] = str(LIVE_LOGS)
    env["LS_LATENCY_SLO_MS"] = str(latency_slo_ms())
    return env


def latency_slo_ms() -> float:
    """Single source of truth: the label config's SLO is what the load generator counts as bad."""
    import yaml

    with open(ROOT / "experiments" / "configs" / "labels.yaml", encoding="utf-8") as f:
        return float(yaml.safe_load(f)["slo"]["latency_slo_ms"])


class TestbedError(RuntimeError):
    pass


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE_FILE), *args]
    return subprocess.run(
        cmd,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=compose_env(),
    )


def _post(url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    r = requests.post(url, json=payload, timeout=TIMEOUT)
    if r.status_code >= 400:
        raise TestbedError(f"POST {url} -> {r.status_code}: {r.text[:200]}")
    return r.json() if r.content else {}


def prom_time() -> float:
    """Current time on the Docker VM clock (the clock all telemetry is stamped with)."""
    r = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": "time()"}, timeout=TIMEOUT)
    r.raise_for_status()
    return float(r.json()["data"]["result"][0])


def wait_healthy(timeout_s: float = 180) -> None:
    deadline = time.monotonic() + timeout_s
    pending = {**SERVICE_URLS, "prometheus": PROMETHEUS_URL, "toxiproxy": TOXIPROXY_URL}
    while pending and time.monotonic() < deadline:
        for name, url in list(pending.items()):
            path = {"prometheus": "/-/ready", "toxiproxy": "/version"}.get(name, "/health")
            try:
                if requests.get(url + path, timeout=2).ok:
                    pending.pop(name)
            except requests.RequestException:
                pass
        if pending:
            time.sleep(2)
    if pending:
        raise TestbedError(f"services not healthy: {sorted(pending)}")


def deploy(service: str, profile: dict[str, Any], rollout_pct: float = 100.0) -> dict[str, Any]:
    url = f"{SERVICE_URLS[service]}/admin/deploy"
    return _post(url, {"profile": profile, "rollout_pct": rollout_pct})


def current_version(service: str) -> str:
    r = requests.get(f"{SERVICE_URLS[service]}/admin/deployment", timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()["current"]["version"]


def set_load(**cfg: Any) -> dict[str, Any]:
    return _post(f"{SERVICE_URLS['loadgen']}/admin/load", cfg)


def get_load() -> dict[str, Any]:
    return requests.get(f"{SERVICE_URLS['loadgen']}/admin/load", timeout=TIMEOUT).json()


def host_context() -> list[dict[str, str]]:
    """Snapshot of containers running outside the testbed (a source of CPU/RAM noise)."""
    cmd = [
        "docker",
        "stats",
        "--no-stream",
        "--format",
        "{{.Name}}	{{.CPUPerc}}	{{.MemUsage}}",
    ]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return [{"error": "docker stats unavailable"}]
    rows = [line.split("	") for line in out.splitlines() if line.strip()]
    return [
        {"name": r[0], "cpu": r[1], "mem": r[2]} for r in rows if len(r) == 3 and not _own(r[0])
    ]


def _own(container: str) -> bool:
    """True for this stack's containers (<project>-<service>-<n>); other stacks count as noise."""
    prefix = f"{PROJECT}-"
    return (
        container.startswith(prefix) and container[len(prefix) :].split("-")[0] in COMPOSE_SERVICES
    )


# --- Faults ----------------------------------------------------------------------------------
def toxiproxy_reset() -> None:
    _post(f"{TOXIPROXY_URL}/reset")


def db_latency(latency_ms: float, jitter_ms: float = 0.0) -> None:
    for stream in ("downstream", "upstream"):
        _post(
            f"{TOXIPROXY_URL}/proxies/postgres/toxics",
            {
                "name": f"latency_{stream}",
                "type": "latency",
                "stream": stream,
                "attributes": {"latency": int(latency_ms / 2), "jitter": int(jitter_ms / 2)},
            },
        )


def db_enable(enabled: bool) -> None:
    _post(f"{TOXIPROXY_URL}/proxies/postgres", {"enabled": enabled})


def pause(service: str, paused: bool) -> None:
    compose("pause" if paused else "unpause", service)


# --- Reset between episodes -------------------------------------------------------------------
SEED_ORDERS = 600_000  # rows inserted by testbed/db/init.sql
RESET_SQL = f"DELETE FROM orders WHERE id > {SEED_ORDERS}; UPDATE stock SET qty = 1000000000;"


def reset_all() -> None:
    """Return the stack to a clean baseline state (fresh-run independence, dataset.md §6)."""
    toxiproxy_reset()
    for svc in APP_SERVICES:
        compose("unpause", svc, check=False)
    compose("exec", "-T", "postgres", "psql", "-U", "app", "-d", "app", "-q", "-c", RESET_SQL)
    for svc in APP_SERVICES:
        _post(f"{SERVICE_URLS[svc]}/admin/reset")
