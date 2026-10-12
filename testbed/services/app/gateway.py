"""API gateway: user-facing entry point. Retries idempotent upstream calls once."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse

from common import (
    RETRIES,
    SERVICE,
    TIMEOUTS,
    ServiceError,
    apply_behaviour,
    create_app,
    log,
    maybe_inject_error,
    pick_profile,
)

UPSTREAMS = {
    "orders": os.environ.get("ORDERS_URL", "http://orders:8000"),
    "inventory": os.environ.get("INVENTORY_URL", "http://inventory:8000"),
}
_clients: dict[str, httpx.AsyncClient] = {}


@asynccontextmanager
async def lifespan(_app):
    limits = httpx.Limits(max_connections=100, max_keepalive_connections=20)
    for name, url in UPSTREAMS.items():
        _clients[name] = httpx.AsyncClient(base_url=url, limits=limits)
    yield
    for c in _clients.values():
        await c.aclose()


app = create_app("gateway", lifespan)


async def call(upstream: str, method: str, path: str, *, retry: bool, **kwargs) -> JSONResponse:
    p = pick_profile()
    maybe_inject_error(p)
    await apply_behaviour(p)
    timeout = p.upstream_timeout_ms / 1000
    attempts = 2 if retry else 1
    for attempt in range(attempts):
        try:
            r = await _clients[upstream].request(method, path, timeout=timeout, **kwargs)
            if r.status_code >= 500 and attempt + 1 < attempts:
                RETRIES.labels(SERVICE, upstream).inc()
                log.warning(
                    "retrying %s %s after status %d",
                    upstream,
                    path.split("/")[1],
                    r.status_code,
                    extra={"upstream": upstream, "status": r.status_code},
                )
                continue
            return JSONResponse(r.json(), status_code=r.status_code)
        except httpx.TimeoutException:
            TIMEOUTS.labels(SERVICE, upstream).inc()
            if attempt + 1 < attempts:
                RETRIES.labels(SERVICE, upstream).inc()
                log.warning(
                    "retrying %s after timeout of %.0f ms",
                    upstream,
                    p.upstream_timeout_ms,
                    extra={"upstream": upstream},
                )
                continue
            raise ServiceError(
                504, f"{upstream} timed out after {p.upstream_timeout_ms:.0f} ms", "UpstreamTimeout"
            ) from None
        except httpx.HTTPError as exc:
            if attempt + 1 < attempts:
                RETRIES.labels(SERVICE, upstream).inc()
                continue
            raise ServiceError(
                502, f"{upstream} unreachable: {type(exc).__name__}", "UpstreamError"
            ) from None
    raise ServiceError(502, f"{upstream} failed after retries", "UpstreamError")


@app.get("/api/orders/{order_id}")
async def get_order(order_id: int):
    return await call("orders", "GET", f"/orders/{order_id}", retry=True)


@app.post("/api/orders")
async def create_order(request: Request):
    return await call("orders", "POST", "/orders", retry=False, json=await request.json())


@app.get("/api/stock/{sku}")
async def get_stock(sku: str):
    return await call("inventory", "GET", f"/stock/{sku}", retry=True)
