"""Liveness and readiness probes.

`/health` answers "is this process serving requests?" and must never touch the
database — a dead database should not make Render restart an otherwise healthy
container.

`/health/ready` answers "can this process serve traffic?" and does a real
`ping` against Atlas.

`/healthz` and `/readyz` are aliases. They are the conventional Kubernetes-style
probe names, and they are what several hosts configure by default. Serving both
means a host's probe path is never a silent 404 that keeps a healthy deploy
stuck in "in progress" forever.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status

from knowledgedock.infrastructure.mongo import MongoManager

router = APIRouter(tags=["health"])


def get_mongo(request: Request) -> MongoManager:
    return request.app.state.mongo


MongoDep = Annotated[MongoManager, Depends(get_mongo)]


@router.get("/health")
@router.get("/healthz", include_in_schema=False)
async def health() -> dict[str, Any]:
    return {"status": "ok"}


@router.get("/health/ready")
@router.get("/readyz", include_in_schema=False)
async def readiness(response: Response, mongo: MongoDep) -> dict[str, Any]:
    ready = await mongo.ping()
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ready" if ready else "degraded",
        "database": "up" if ready else "down",
    }
