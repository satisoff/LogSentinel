"""Open-loop load generator. It is also the user-facing SLI source.

Arrivals are Poisson at a target rate that does not slow down when the system does. A closed-loop
generator would hide latency regressions by sending fewer requests. Each request outcome is
recorded as client-side Prometheus metrics. A request is "bad" if it returns 5xx, times out, is
dropped, or is slower than the latency SLO.
"""

from __future__ import annotations

import asyncio
import math
import os
import random
import time
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from prometheus_client import Counter, Gauge, Histogram
from pydantic import BaseModel

from common import LAT_BUCKETS, create_app, log

TARGET = os.environ.get("TARGET_URL", "http://gateway:8000")
SLO_MS = float(os.environ.get("LATENCY_SLO_MS", "300"))
CLIENT_TIMEOUT_S = float(os.environ.get("CLIENT_TIMEOUT_S", "2.0"))
MAX_INFLIGHT = int(os.environ.get("MAX_INFLIGHT", "300"))
N_ORDERS, N_SKUS = 200_000, 1_000

REQS = Counter("client_requests_total", "Client requests by outcome", ["route", "outcome"])
BAD = Counter(
    "client_bad_total", "Bad requests (5xx, timeout, dropped, or > latency SLO)", ["route"]
)
LAT = Histogram("client_latency_seconds", "Client-observed latency", ["route"], buckets=LAT_BUCKETS)
TARGET_RPS = Gauge("client_target_rps", "Current target request rate")
INFLIGHT = Gauge("client_inflight", "In-flight requests")
ROUTES = ("get_order", "get_stock", "create_order")
OUTCOMES = ("ok", "error_5xx", "error_4xx", "timeout", "conn_error", "dropped")
# Pre-create every labelled child so Prometheus exports explicit zeros (not "no series").
for _route in ROUTES:
    BAD.labels(_route)
    LAT.labels(_route)
    for _outcome in OUTCOMES:
        REQS.labels(_route, _outcome)


class LoadConfig(BaseModel):
    rps: float = float(os.environ.get("LOAD_RPS", "20"))
    profile: Literal["steady", "diurnal", "bursty"] = "steady"
    seed: int = 0
    ramp_s: float = 0.0  # transition time when rps changes
    mix: dict[str, float] = {"get_order": 0.7, "get_stock": 0.2, "create_order": 0.1}
    running: bool = True


class _State:
    cfg = LoadConfig()
    prev_rps = cfg.rps
    changed_at = time.monotonic()
    started_at = time.monotonic()
    rng = random.Random(0)
    next_burst = 0.0
    burst_until = 0.0
    inflight = 0


S = _State()
_client: httpx.AsyncClient | None = None


def current_rate(now: float) -> float:
    cfg = S.cfg
    rate = cfg.rps
    if cfg.ramp_s > 0 and now - S.changed_at < cfg.ramp_s:
        frac = (now - S.changed_at) / cfg.ramp_s
        rate = S.prev_rps + (cfg.rps - S.prev_rps) * frac
    if cfg.profile == "diurnal":
        rate *= 1 + 0.2 * math.sin(2 * math.pi * (now - S.started_at) / 1200)
    elif cfg.profile == "bursty":
        if now >= S.next_burst:
            S.burst_until = now + 30
            S.next_burst = now + S.rng.expovariate(1 / 180)
        if now < S.burst_until:
            rate *= 1.6
    return max(rate, 0.1)


async def one_request(route: str) -> None:
    S.inflight += 1
    INFLIGHT.set(S.inflight)
    start = time.perf_counter()
    outcome = "ok"
    try:
        if route == "get_order":
            r = await _client.get(f"/api/orders/{S.rng.randint(1, N_ORDERS)}")
        elif route == "get_stock":
            r = await _client.get(f"/api/stock/sku-{S.rng.randint(1, N_SKUS)}")
        else:
            body = {"sku": f"sku-{S.rng.randint(1, N_SKUS)}", "qty": 1, "customer_ref": "load"}
            r = await _client.post("/api/orders", json=body)
        if r.status_code >= 500:
            outcome = "error_5xx"
        elif r.status_code >= 400:
            outcome = "error_4xx"
    except httpx.TimeoutException:
        outcome = "timeout"
    except httpx.HTTPError:
        outcome = "conn_error"
    elapsed = time.perf_counter() - start
    S.inflight -= 1
    INFLIGHT.set(S.inflight)
    REQS.labels(route, outcome).inc()
    LAT.labels(route).observe(elapsed)
    if outcome in ("error_5xx", "timeout", "conn_error") or elapsed * 1000 > SLO_MS:
        BAD.labels(route).inc()


async def arrival_loop() -> None:
    routes, weights = zip(*S.cfg.mix.items(), strict=True)
    while True:
        if not S.cfg.running:
            await asyncio.sleep(0.5)
            continue
        now = time.monotonic()
        rate = current_rate(now)
        TARGET_RPS.set(rate)
        await asyncio.sleep(S.rng.expovariate(rate))
        route = S.rng.choices(routes, weights)[0]
        if S.inflight >= MAX_INFLIGHT:
            REQS.labels(route, "dropped").inc()
            BAD.labels(route).inc()
            continue
        asyncio.create_task(one_request(route))


@asynccontextmanager
async def lifespan(_app):
    global _client
    _client = httpx.AsyncClient(
        base_url=TARGET,
        timeout=CLIENT_TIMEOUT_S,
        limits=httpx.Limits(max_connections=MAX_INFLIGHT, max_keepalive_connections=50),
    )
    task = asyncio.create_task(arrival_loop())
    yield
    task.cancel()
    await _client.aclose()


app = create_app("loadgen", lifespan)


@app.get("/admin/load")
async def get_load():
    return S.cfg.model_dump()


@app.post("/admin/load")
async def set_load(cfg: LoadConfig):
    now = time.monotonic()
    S.prev_rps = current_rate(now) if S.cfg.profile == "steady" else S.cfg.rps
    S.changed_at = now
    if cfg.seed != S.cfg.seed:
        S.rng = random.Random(cfg.seed)
    S.cfg = cfg
    log.info(
        "load set to %.1f rps profile %s ramp %.0f s",
        cfg.rps,
        cfg.profile,
        cfg.ramp_s,
        extra={"event": "load_change"},
    )
    return S.cfg.model_dump()
