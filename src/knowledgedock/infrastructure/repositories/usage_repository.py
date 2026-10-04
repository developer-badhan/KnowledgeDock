"""Usage record persistence and quota rollups.

Writes are never allowed to fail a request. `UsageRecorder` already swallows
insert failures, and the queries here exist to answer "did we exhaust the Gemini
quota" -- the question that turns a wall of 503s into an explanation.

`totals_for_workspace` is what that answer needs: a single grouped aggregate
rather than pulling rows into memory. At a free tier the row count stays small,
but "read everything and sum in Python" is the kind of habit that is fine in
development and a page timeout in production.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from pymongo import ASCENDING, DESCENDING

from knowledgedock.domain.usage import AIUsageRecord


class AIUsageRepository(Protocol):
    async def ensure_indexes(self) -> None: ...

    async def insert(self, record: AIUsageRecord) -> None: ...

    async def totals_for_workspace(
        self, workspace_id: UUID, *, since: datetime | None = None
    ) -> dict[str, Any]: ...

    async def recent(self, workspace_id: UUID, *, limit: int = 50) -> list[AIUsageRecord]: ...


class MongoAIUsageRepository:
    def __init__(self, database: Any) -> None:
        self._usage = database["ai_usage"]

    async def ensure_indexes(self) -> None:
        # The only read pattern that matters is "this workspace, newest first".
        await self._usage.create_index(
            [("workspace_id", ASCENDING), ("created_at", DESCENDING)],
            name="ai_usage_workspace_recent",
        )
        await self._usage.create_index([("created_at", DESCENDING)], name="ai_usage_recent")

    async def insert(self, record: AIUsageRecord) -> None:
        await self._usage.insert_one(record.to_document())

    async def totals_for_workspace(
        self, workspace_id: UUID, *, since: datetime | None = None
    ) -> dict[str, Any]:
        query: dict[str, Any] = {"workspace_id": workspace_id}
        if since is not None:
            query["created_at"] = {"$gte": since}
        pipeline = [
            {"$match": query},
            {
                "$group": {
                    "_id": "$operation",
                    "calls": {"$sum": 1},
                    "input_tokens": {"$sum": "$input_tokens"},
                    "output_tokens": {"$sum": "$output_tokens"},
                    "failures": {"$sum": {"$cond": ["$success", 0, 1]}},
                    "duration_ms": {"$sum": "$duration_ms"},
                }
            },
        ]
        totals = {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "failures": 0,
            "duration_ms": 0.0,
        }
        for row in await (await self._usage.aggregate(pipeline)).to_list(length=10):
            operation = str(row["_id"])
            totals["calls"] += row["calls"]
            totals["input_tokens"] += row["input_tokens"]
            totals["output_tokens"] += row["output_tokens"]
            totals["failures"] += row["failures"]
            totals["duration_ms"] += row["duration_ms"]
            totals[operation] = {
                "calls": row["calls"],
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "failures": row["failures"],
            }
        totals["duration_ms"] = round(totals["duration_ms"], 2)
        return totals

    async def recent(self, workspace_id: UUID, *, limit: int = 50) -> list[AIUsageRecord]:
        cursor = (
            self._usage.find({"workspace_id": workspace_id})
            .sort("created_at", DESCENDING)
            .limit(limit)
        )
        return [AIUsageRecord.from_document(d) for d in await cursor.to_list(length=limit)]


class InMemoryAIUsageRepository:
    def __init__(self) -> None:
        self.records: list[AIUsageRecord] = []

    async def ensure_indexes(self) -> None:
        return None

    async def insert(self, record: AIUsageRecord) -> None:
        self.records.append(record)

    async def totals_for_workspace(
        self, workspace_id: UUID, *, since: datetime | None = None
    ) -> dict[str, Any]:
        mine = [
            r
            for r in self.records
            if r.workspace_id == workspace_id and (since is None or r.created_at >= since)
        ]
        totals = {
            "calls": len(mine),
            "input_tokens": sum(r.input_tokens for r in mine),
            "output_tokens": sum(r.output_tokens for r in mine),
            "failures": sum(0 if r.success else 1 for r in mine),
            "duration_ms": round(sum(r.duration_ms for r in mine), 2),
        }
        for record in mine:
            bucket = totals.setdefault(
                record.operation.value,
                {"calls": 0, "input_tokens": 0, "output_tokens": 0, "failures": 0},
            )
            bucket["calls"] += 1
            bucket["input_tokens"] += record.input_tokens
            bucket["output_tokens"] += record.output_tokens
            bucket["failures"] += 0 if record.success else 1
        return totals

    async def recent(self, workspace_id: UUID, *, limit: int = 50) -> list[AIUsageRecord]:
        mine = [r for r in self.records if r.workspace_id == workspace_id]
        mine.sort(key=lambda r: r.created_at, reverse=True)
        return mine[:limit]


class UnavailableAIUsageRepository:
    """Stands in when the database is unreachable at startup (Phase 01 decision 2)."""

    _message = "AI usage is unavailable. The database connection failed at startup."

    def __init__(self, cause: Exception | None = None) -> None:
        self.cause = cause

    async def ensure_indexes(self) -> None:
        return None

    async def insert(self, record: AIUsageRecord) -> None:
        raise RuntimeError(self._message)

    async def totals_for_workspace(
        self, workspace_id: UUID, *, since: datetime | None = None
    ) -> dict[str, Any]:
        raise RuntimeError(self._message)

    async def recent(self, workspace_id: UUID, *, limit: int = 50) -> list[AIUsageRecord]:
        raise RuntimeError(self._message)


def default_window(days: int = 1) -> datetime:
    """Start of the rolling window a quota question is asked about."""
    from knowledgedock.domain.users import utcnow

    return utcnow() - timedelta(days=days)
