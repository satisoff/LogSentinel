"""orders service: order lookup and creation (creation reserves stock via inventory)."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import httpx
from pydantic import BaseModel

import db
from common import ServiceError, apply_behaviour, create_app, maybe_inject_error, pick_profile

INVENTORY_URL = os.environ.get("INVENTORY_URL", "http://inventory:8000")
_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(_app):
    global _client
    await db.connect()
    _client = httpx.AsyncClient(base_url=INVENTORY_URL, timeout=1.0)
    yield
    await _client.aclose()
    await db.close()


app = create_app("orders", lifespan)


class NewOrder(BaseModel):
    sku: str
    qty: int = 1
    customer_ref: str


@app.get("/orders/{order_id}")
async def get_order(order_id: int):
    p = pick_profile()
    maybe_inject_error(p)
    await apply_behaviour(p)
    if p.query_mode == "unindexed":
        # Inefficient deploy: filter on the unindexed customer_ref column, no LIMIT -> full scan.
        rows = await db.fetch(
            "order_by_customer",
            "SELECT id, customer_ref, sku, qty FROM orders WHERE customer_ref = $1",
            f"c{order_id}",
        )
    else:
        rows = await db.fetch(
            "order_by_id", "SELECT id, customer_ref, sku, qty FROM orders WHERE id = $1", order_id
        )
    if not rows:
        raise ServiceError(404, f"order {order_id} not found", "NotFound")
    order = dict(rows[0])
    for _ in range(p.n_plus_one):
        await db.fetch("product_name", "SELECT name FROM products WHERE sku = $1", order["sku"])
    return order


@app.post("/orders", status_code=201)
async def create_order(new: NewOrder):
    p = pick_profile()
    maybe_inject_error(p)
    await apply_behaviour(p)
    try:
        r = await _client.post("/reserve", json={"sku": new.sku, "qty": new.qty})
    except httpx.TimeoutException:
        raise ServiceError(504, "inventory reserve timed out", "UpstreamTimeout") from None
    except httpx.HTTPError as exc:
        raise ServiceError(
            502, f"inventory unreachable: {type(exc).__name__}", "UpstreamError"
        ) from None
    if r.status_code >= 400:
        raise ServiceError(
            502 if r.status_code >= 500 else 409,
            f"reserve failed with {r.status_code}",
            "ReserveFailed",
        )
    rows = await db.fetch(
        "insert_order",
        "INSERT INTO orders (customer_ref, sku, qty) VALUES ($1, $2, $3) RETURNING id",
        new.customer_ref,
        new.sku,
        new.qty,
    )
    return {"id": rows[0]["id"]}
