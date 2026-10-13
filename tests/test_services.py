"""In-process tests of the testbed service runtime (no Docker, no database)."""

import importlib
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

APP_DIR = Path(__file__).parents[1] / "testbed" / "services" / "app"


def _load(module: str, service: str, tmp_path, monkeypatch):
    monkeypatch.setenv("SERVICE", service)
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("ACCESS_LOG_SAMPLE", "0")
    monkeypatch.setenv("APP_STARTUP_DELAY_S", "0")
    monkeypatch.syspath_prepend(str(APP_DIR))
    for name in ("common", "db", module):
        sys.modules.pop(name, None)
    # prometheus_client keeps one global registry; drop app metrics from a previous import.
    from prometheus_client import REGISTRY
    from prometheus_client.metrics import MetricWrapperBase

    for collector in list(REGISTRY._collector_to_names):
        if isinstance(collector, MetricWrapperBase):
            REGISTRY.unregister(collector)
    return importlib.import_module(module)


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    gw = _load("gateway", "gateway", tmp_path, monkeypatch)
    calls = {"n": 0, "fail_first": False}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["fail_first"] and calls["n"] == 1:
            return httpx.Response(503, json={"error": "busy"})
        return httpx.Response(200, json={"id": 1, "path": request.url.path})

    with TestClient(gw.app) as client:
        for name in gw._clients:
            gw._clients[name] = httpx.AsyncClient(
                base_url="http://upstream", transport=httpx.MockTransport(handler)
            )
        yield gw, client, calls


def test_gateway_proxies_and_counts(gateway):
    gw, client, _ = gateway
    r = client.get("/api/orders/7")
    assert r.status_code == 200 and r.json()["path"] == "/orders/7"
    metrics = client.get("/metrics/").text
    assert (
        'http_requests_total{route="/api/orders/{order_id}",service="gateway",status="200"}'
        in metrics
    )


def test_gateway_retries_idempotent_call_once(gateway):
    _, client, calls = gateway
    calls["fail_first"] = True
    r = client.get("/api/stock/sku-1")
    assert r.status_code == 200 and calls["n"] == 2
    assert (
        'upstream_retries_total{service="gateway",upstream="inventory"} 1.0'
        in client.get("/metrics/").text
    )


def test_deploy_reset_and_error_injection(gateway):
    gw, client, _ = gateway
    profile = {"version": "2.0.0", "error_fraction": 1.0}
    assert client.post("/admin/deploy", json={"profile": profile}).status_code == 200
    assert client.get("/health").json()["version"] == "2.0.0"
    assert client.get("/api/orders/1").status_code == 500
    assert (
        'exceptions_total{service="gateway",type="InjectedFault"}' in client.get("/metrics/").text
    )
    client.post("/admin/reset")
    assert client.get("/api/orders/1").status_code == 200


def test_canary_routes_fraction_of_requests_to_new_version(gateway):
    gw, client, _ = gateway
    client.post(
        "/admin/deploy",
        json={"profile": {"version": "2.0.0", "error_fraction": 1.0}, "rollout_pct": 30},
    )
    codes = [client.get("/api/orders/1").status_code for _ in range(300)]
    share = codes.count(500) / len(codes)
    assert 0.18 < share < 0.42


def test_deployment_state_survives_restart(tmp_path, monkeypatch):
    gw = _load("gateway", "gateway", tmp_path, monkeypatch)
    with TestClient(gw.app) as client:
        client.post("/admin/deploy", json={"profile": {"version": "3.1.0"}})
    gw2 = _load("gateway", "gateway", tmp_path, monkeypatch)  # simulated OOM restart
    with TestClient(gw2.app) as client:
        assert client.get("/health").json()["version"] == "3.1.0"


def test_logs_are_json_lines(tmp_path, monkeypatch):
    import json

    gw = _load("gateway", "gateway", tmp_path, monkeypatch)
    with TestClient(gw.app) as client:
        client.post("/admin/deploy", json={"profile": {"version": "1.2.0"}})
    lines = (tmp_path / "logs" / "gateway.jsonl").read_text().splitlines()
    doc = json.loads(lines[-1])
    assert doc["service"] == "gateway" and doc["event"] == "deploy" and doc["ts"].endswith("+00:00")


def test_startup_delay_holds_back_serving(tmp_path, monkeypatch):
    import time

    gw = _load("gateway", "gateway", tmp_path, monkeypatch)
    monkeypatch.setattr(sys.modules["common"], "STARTUP_DELAY_S", 0.3)
    start = time.perf_counter()
    with TestClient(gw.app) as client:
        assert time.perf_counter() - start >= 0.3  # lifespan waited before serving
        assert client.get("/health").status_code == 200
