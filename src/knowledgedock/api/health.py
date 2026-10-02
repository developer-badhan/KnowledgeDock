"""Liveness and readiness probes.

`/health` answers "is this process serving requests?" and must never touch the
database — a dead database should not make Render restart an otherwise healthy
container.

`/health/ready` answers "can this process serve traffic?" and does a real
`ping` against Atlas.
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
async def health() -> dict[str, Any]:
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(response: Response, mongo: MongoDep) -> dict[str, Any]:
    ready = await mongo.ping()
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ready" if ready else "degraded",
        "database": "up" if ready else "down",
    }
