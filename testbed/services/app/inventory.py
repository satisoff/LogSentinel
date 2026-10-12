"""inventory service: stock lookup and reservation."""

from __future__ import annotations

from contextlib import asynccontextmanager

from pydantic import BaseModel

import db
from common import ServiceError, apply_behaviour, create_app, maybe_inject_error, pick_profile


@asynccontextmanager
async def lifespan(_app):
    await db.connect()
    yield
    await db.close()


app = create_app("inventory", lifespan)


class Reservation(BaseModel):
    sku: str
    qty: int = 1


@app.get("/stock/{sku}")
async def get_stock(sku: str):
    p = pick_profile()
    maybe_inject_error(p)
    await apply_behaviour(p)
    rows = await db.fetch("stock_by_sku", "SELECT sku, qty FROM stock WHERE sku = $1", sku)
    if not rows:
        raise ServiceError(404, f"sku {sku} not found", "NotFound")
    for _ in range(p.n_plus_one):
        await db.fetch("product_price", "SELECT price FROM products WHERE sku = $1", sku)
    return dict(rows[0])


@app.post("/reserve")
async def reserve(r: Reservation):
    p = pick_profile()
    maybe_inject_error(p)
    await apply_behaviour(p)
    rows = await db.fetch(
        "reserve_stock",
        "UPDATE stock SET qty = qty - $2 WHERE sku = $1 AND qty >= $2 RETURNING qty",
        r.sku,
        r.qty,
    )
    if not rows:
        raise ServiceError(409, f"insufficient stock for {r.sku}", "OutOfStock")
    return {"sku": r.sku, "remaining": rows[0]["qty"]}
