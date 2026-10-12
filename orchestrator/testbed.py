"""Thin client for the running testbed: compose, service admin APIs, Toxiproxy, Prometheus clock."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "testbed" / "docker-compose.yml"
SERVICE_URLS = {
    "gateway": "http://127.0.0.1:8080",
    "orders": "http://127.0.0.1:8081",
    "inventory": "http://127.0.0.1:8082",
    "loadgen": "http://127.0.0.1:8089",
}
APP_SERVICES = ("gateway", "orders", "inventory")
PROMETHEUS_URL = "http://127.0.0.1:9090"
TOXIPROXY_URL = "http://127.0.0.1:8474"
TIMEOUT = 5


class TestbedError(RuntimeError):
    pass


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["docker", "compose", "-f", str(COMPOSE_FILE), *args]
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


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
RESET_SQL = "DELETE FROM orders WHERE id > 200000; UPDATE stock SET qty = 1000000000;"


def reset_all() -> None:
    """Return the stack to a clean baseline state (fresh-run independence, dataset.md §6)."""
    toxiproxy_reset()
    for svc in APP_SERVICES:
        compose("unpause", svc, check=False)
    compose("exec", "-T", "postgres", "psql", "-U", "app", "-d", "app", "-q", "-c", RESET_SQL)
    for svc in APP_SERVICES:
        _post(f"{SERVICE_URLS[svc]}/admin/reset")
