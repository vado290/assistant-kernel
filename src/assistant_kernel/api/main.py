"""kernel-api: health and version for the container health check and deploy.ps1, and the dashboard (ROUND6).

The rebuilt core has no HTTP business API; Discord is the entry for tasks (plan 7.1). The dashboard
(api/dashboard.py) configures the installation: Discord settings and the language.
"""
from __future__ import annotations

import uvicorn
from fastapi import FastAPI, Response

from assistant_kernel import __schema_version__, __version__
from assistant_kernel.api.dashboard import build_router
from assistant_kernel.config import KernelSettings

settings = KernelSettings.from_environment()
app = FastAPI(title="Assistant Kernel", version=__version__)
app.include_router(build_router(settings))


@app.get("/health/live")
async def health_live() -> dict:
    return {"status": "ok", "version": __version__}


@app.get("/health/ready")
async def health_ready(response: Response) -> dict:
    """Readiness of the rebuilt core (docs/REBUILD_PLAN.md phase 4): database and core schema reachable.

    Run backlog and undelivered messages are reported for information; they do not fail readiness.
    """
    from assistant_kernel.core.db import Database
    detail: dict = {"database": False, "core_schema": False}
    try:
        async with Database(settings).tx() as c:
            detail["database"] = True
            row = await (await c.execute("SELECT to_regclass('core.tasks') AS t")).fetchone()
            detail["core_schema"] = row["t"] is not None
            if detail["core_schema"]:
                runs = await (await c.execute(
                    """SELECT count(*) FILTER (WHERE status='queued' AND available_at <= now()) AS queued,
                              count(*) FILTER (WHERE status='running') AS running,
                              COALESCE(EXTRACT(EPOCH FROM now() - min(created_at) FILTER (
                                  WHERE status='queued' AND available_at <= now())), 0)::int AS oldest_queued_seconds
                       FROM core.runs""")).fetchone()
                outbox = await (await c.execute(
                    "SELECT count(*) AS n FROM core.messages WHERE direction='outbound' AND delivery_status='pending'")).fetchone()
                detail.update(runs=dict(runs), outbox_pending=outbox["n"])
    except Exception as exc:  # noqa: BLE001 - readiness reports the failure type, never raises
        detail["error"] = type(exc).__name__
    ready = detail["database"] and detail["core_schema"]
    response.status_code = 200 if ready else 503
    return {"status": "ready" if ready else "not_ready", **detail}


@app.get("/version")
async def version() -> dict:
    return {"version": __version__, "schema_version": __schema_version__}


def run() -> None:
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    run()
