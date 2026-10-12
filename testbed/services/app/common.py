"""Shared runtime for testbed services: behaviour profiles, JSON logging, metrics, admin API.

A "deployment" is simulated in-process: POST /admin/deploy swaps the active behaviour profile
atomically (a zero-downtime rollout). With rollout_pct < 100, a fraction of requests keeps the
previous profile (canary). The deployed state is persisted to /state so that an OOM restart
comes back on the same, possibly faulty, version, as a real restarted pod would.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sys
import time
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_client import Counter, Gauge, Histogram, make_asgi_app
from pydantic import BaseModel

SERVICE = os.environ.get("SERVICE", "unknown")
STATE_DIR = Path(os.environ.get("STATE_DIR", "/state"))
LOG_DIR = Path(os.environ.get("LOG_DIR", "/logs"))
ACCESS_LOG_SAMPLE = float(os.environ.get("ACCESS_LOG_SAMPLE", "0.05"))
DEFAULT_VERSION = os.environ.get("APP_VERSION", "1.0.0")


# --- Behaviour profile -----------------------------------------------------------------------
class Profile(BaseModel):
    version: str = DEFAULT_VERSION
    extra_cpu_ms: float = 0.0  # busy work per request (blocks the event loop, like real CPU)
    extra_sleep_ms: float = 0.0  # non-CPU wait per request
    query_mode: Literal["indexed", "unindexed"] = "indexed"
    n_plus_one: int = 0  # extra per-request lookups
    leak_kb_per_req: float = 0.0  # retained small objects per request
    error_fraction: float = 0.0  # fraction of requests answered with 500
    upstream_timeout_ms: float = 1500.0  # gateway -> service timeout
    new_log_event: str | None = None  # emits a new structured log template
    new_log_every: int = 20


class Deployment(BaseModel):
    current: Profile = Profile()
    previous: Profile = Profile()
    rollout_pct: float = 100.0


class DeployRequest(BaseModel):
    profile: Profile
    rollout_pct: float = 100.0


_state_file = STATE_DIR / f"{SERVICE}.json"


def _load_deployment() -> Deployment:
    try:
        return Deployment.model_validate_json(_state_file.read_text())
    except (OSError, ValueError):
        return Deployment()


deployment = _load_deployment()
_leak: list[dict] = []
_request_counter = 0


def pick_profile() -> Profile:
    if deployment.rollout_pct >= 100 or random.random() * 100 < deployment.rollout_pct:
        return deployment.current
    return deployment.previous


async def apply_behaviour(p: Profile) -> None:
    """Per-request side effects of the active profile (CPU, wait, leak, new log event)."""
    global _request_counter
    _request_counter += 1
    if p.extra_cpu_ms > 0:
        end = time.perf_counter() + p.extra_cpu_ms / 1000
        while time.perf_counter() < end:
            pass
    if p.extra_sleep_ms > 0:
        await asyncio.sleep(p.extra_sleep_ms / 1000)
    if p.leak_kb_per_req > 0:
        # Many small objects: RSS grows AND garbage-collector pauses grow, like a real leak.
        n = max(1, int(p.leak_kb_per_req * 1024 / 200))
        _leak.extend({"i": _request_counter, "pad": "x" * 64} for _ in range(n))
        LEAK_OBJECTS.labels(SERVICE).set(len(_leak))
    if p.new_log_event and _request_counter % max(1, p.new_log_every) == 0:
        log.info(
            "%s handled for request %d with version %s",
            p.new_log_event,
            _request_counter,
            p.version,
            extra={"event": p.new_log_event},
        )


# --- Logging ---------------------------------------------------------------------------------
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        doc = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "service": SERVICE,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "version": deployment.current.version,
        }
        for key in ("event", "route", "status", "latency_ms", "exc_type", "upstream"):
            if hasattr(record, key):
                doc[key] = getattr(record, key)
        return json.dumps(doc)


def _setup_logging() -> logging.Logger:
    logger = logging.getLogger(SERVICE)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    fmt = JsonFormatter()
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    logger.addHandler(stream)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(LOG_DIR / f"{SERVICE}.jsonl", maxBytes=20_000_000, backupCount=3)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        logger.warning("log directory %s not writable; logging to stdout only", LOG_DIR)
    return logger


log = _setup_logging()

# --- Metrics ---------------------------------------------------------------------------------
LAT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.0, 5.0)
REQUESTS = Counter("http_requests_total", "Requests served", ["service", "route", "status"])
LATENCY = Histogram(
    "http_request_duration_seconds", "Request latency", ["service", "route"], buckets=LAT_BUCKETS
)
DB_LATENCY = Histogram(
    "db_query_duration_seconds", "DB query latency", ["service", "query"], buckets=LAT_BUCKETS
)
DB_POOL_IN_USE = Gauge("db_pool_in_use", "DB connections in use", ["service"])
RETRIES = Counter("upstream_retries_total", "Upstream retries", ["service", "upstream"])
TIMEOUTS = Counter("timeouts_total", "Timeouts (upstream or DB)", ["service", "target"])
EXCEPTIONS = Counter("exceptions_total", "Unhandled/handled exceptions", ["service", "type"])
VERSION = Gauge("app_version_info", "Active version (1 = current)", ["service", "version"])
LEAK_OBJECTS = Gauge("leak_objects", "Objects retained by the leak scenario", ["service"])


class ServiceError(Exception):
    def __init__(self, status: int, message: str, exc_type: str):
        super().__init__(message)
        self.status, self.message, self.exc_type = status, message, exc_type


def create_app(title: str, lifespan=None) -> FastAPI:
    app = FastAPI(title=title, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.mount("/metrics", make_asgi_app())
    VERSION.labels(SERVICE, deployment.current.version).set(1)

    @app.middleware("http")
    async def instrument(request: Request, call_next):
        path = request.url.path
        if path.startswith(("/metrics", "/admin", "/health")):
            return await call_next(request)
        start = time.perf_counter()
        try:
            response = await call_next(request)
            status = response.status_code
        except Exception as exc:  # noqa: BLE001 - every failure must become a 500 + log line
            status = 500
            EXCEPTIONS.labels(SERVICE, type(exc).__name__).inc()
            log.error(
                "unhandled %s: %s", type(exc).__name__, exc, extra={"exc_type": type(exc).__name__}
            )
            response = JSONResponse({"error": "internal"}, status_code=500)
        elapsed = time.perf_counter() - start
        # FastAPI's router stores the matched route in the shared scope during call_next.
        route_name = getattr(request.scope.get("route"), "path", path)
        REQUESTS.labels(SERVICE, route_name, str(status)).inc()
        LATENCY.labels(SERVICE, route_name).observe(elapsed)
        if random.random() < ACCESS_LOG_SAMPLE:
            log.info(
                "%s %s -> %d in %.1f ms",
                request.method,
                route_name,
                status,
                elapsed * 1000,
                extra={
                    "route": route_name,
                    "status": status,
                    "latency_ms": round(elapsed * 1000, 1),
                },
            )
        return response

    @app.exception_handler(ServiceError)
    async def service_error(_: Request, exc: ServiceError):
        EXCEPTIONS.labels(SERVICE, exc.exc_type).inc()
        level = logging.ERROR if exc.status >= 500 else logging.WARNING
        log.log(level, "%s: %s", exc.exc_type, exc.message, extra={"exc_type": exc.exc_type})
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    @app.get("/health")
    async def health():
        return {"service": SERVICE, "version": deployment.current.version}

    @app.get("/admin/deployment")
    async def get_deployment():
        return deployment.model_dump()

    @app.post("/admin/deploy")
    async def deploy(req: DeployRequest):
        global deployment
        old = deployment.current
        deployment = Deployment(current=req.profile, previous=old, rollout_pct=req.rollout_pct)
        _persist()
        VERSION.labels(SERVICE, old.version).set(0)
        VERSION.labels(SERVICE, req.profile.version).set(1)
        log.info(
            "deployment applied version %s -> %s rollout %.0f%%",
            old.version,
            req.profile.version,
            req.rollout_pct,
            extra={"event": "deploy"},
        )
        return deployment.model_dump()

    @app.post("/admin/reset")
    async def reset():
        global deployment
        for v in {deployment.current.version, deployment.previous.version}:
            VERSION.labels(SERVICE, v).set(0)
        deployment = Deployment()
        _leak.clear()
        LEAK_OBJECTS.labels(SERVICE).set(0)
        _persist()
        VERSION.labels(SERVICE, deployment.current.version).set(1)
        log.info(
            "service reset to version %s", deployment.current.version, extra={"event": "reset"}
        )
        return deployment.model_dump()

    return app


def _persist() -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        _state_file.write_text(deployment.model_dump_json())
    except OSError as exc:
        log.warning("could not persist deployment state: %s", exc)


def maybe_inject_error(p: Profile) -> None:
    if p.error_fraction > 0 and random.random() < p.error_fraction:
        raise ServiceError(500, f"handler failed in version {p.version}", "InjectedFault")
