"""The ingestion worker.

A poller, not `BackgroundTasks`. The reason is the deployment target: Render's
free tier has no separate worker and no always-on instance, so a task scheduled
inside a request dies with the process. A document would be left in `PROCESSING`
forever and only a manual retry would clear it. A poller keyed off MongoDB
survives restarts, because the queue *is* the collection.

```text
startup -> reclaim abandoned work -> loop { claim one PENDING -> process it }
```

**Strictly sequential, on purpose.** Two independent limits make concurrency a
liability rather than a feature: Atlas M0 allows 100 operations per second, and
Gemini's free tier is rate limited per minute. A single worker keeps both well
inside their budgets and is trivially correct — there is never a second writer on
the same document. Revisit only if measured throughput demands it
(`SKILL.md` §33).

**Claiming is atomic.** `find_one_and_update` with a `status: pending` filter
moves the document to `processing` and returns it in one operation, so two workers
cannot pick up the same document even if a second instance ever runs.

**Startup reclaims abandoned work.** A document still `PROCESSING` when the
process starts was interrupted mid-job. Those go back to `PENDING`, and so do
`FAILED` documents with attempts left, so a crash loop cannot strand a workspace.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass

from knowledgedock.application.ingestion.process_document import (
    IngestionResult,
    ProcessDocument,
)
from knowledgedock.domain.documents import DocumentStatus
from knowledgedock.infrastructure.repositories.document_repository import DocumentRepository

logger = logging.getLogger(__name__)

#: Pause after a failure so one bad document cannot spin the loop.
ERROR_BACKOFF_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class WorkerStats:
    processed: int = 0
    failed: int = 0
    skipped: int = 0


class IngestionWorker:
    def __init__(
        self,
        *,
        documents: DocumentRepository,
        process: ProcessDocument,
        poll_interval_seconds: float,
        max_attempts: int,
    ) -> None:
        self._documents = documents
        self._process = process
        self._interval = poll_interval_seconds
        self._max_attempts = max_attempts

    async def reclaim_abandoned(self) -> int:
        """Return interrupted and retryable documents to PENDING at startup.

        On a single worker, every PROCESSING row observed at startup is
        abandoned: the only process that could have claimed it is this one, and
        it has just started. There is no staleness cut-off to apply — a document
        that was being processed when the previous instance died is abandoned no
        matter how recently it moved to PROCESSING. The returned count is logged
        because a non-zero value right after a deploy is the normal signal that
        an instance was killed mid-job.
        """
        requeued = 0
        try:
            for document in await self._documents.find_interrupted():
                await self._documents.replace_for_reupload(document.requeue_from_interrupted())
                requeued += 1

            for document in await self._documents.find_retryable_failures(self._max_attempts):
                await self._documents.replace_for_reupload(document.requeue())
                requeued += 1
        except Exception:
            # Startup recovery is best effort. An unreachable database must not
            # stop the process binding $PORT (Phase 01 decision 2), and the next
            # poll picks up whatever this sweep missed.
            logger.error("ingestion.reclaim_failed", exc_info=True)
            return 0

        if requeued:
            logger.info("ingestion.reclaimed", extra={"count": requeued})
        return requeued

    async def run_once(self) -> IngestionResult | None:
        """Claim and process at most one document. None when the queue is empty."""
        claimed = await self._documents.claim_next_pending()
        if claimed is None:
            return None
        return await self._process.execute(claimed)

    async def run_forever(self, should_stop: asyncio.Event) -> WorkerStats:
        stats = WorkerStats()
        logger.info(
            "ingestion.worker_started",
            extra={
                "interval_seconds": self._interval,
                "max_attempts": self._max_attempts,
            },
        )
        while not should_stop.is_set():
            try:
                result = await self.run_once()
            except asyncio.CancelledError:
                logger.info("ingestion.worker_cancelled")
                raise
            except Exception:
                # The loop must outlive any single failure.
                logger.exception("ingestion.worker_iteration_failed")
                await self._sleep(ERROR_BACKOFF_SECONDS, should_stop)
                continue

            if result is None:
                await self._sleep(self._interval, should_stop)
                continue

            stats = WorkerStats(
                processed=stats.processed + (result.status is DocumentStatus.READY),
                failed=stats.failed + (result.status is DocumentStatus.FAILED),
                skipped=stats.skipped + (result.status not in TERMINAL),
            )

        logger.info("ingestion.worker_stopped", extra={"stats": repr(stats)})
        return stats

    async def _sleep(self, seconds: float, should_stop: asyncio.Event) -> None:
        """Interruptible sleep, so shutdown does not wait out the interval."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(should_stop.wait(), timeout=seconds)


TERMINAL = frozenset({DocumentStatus.READY, DocumentStatus.FAILED})


def build_worker_task(worker: IngestionWorker, should_stop: asyncio.Event) -> asyncio.Task:
    return asyncio.create_task(worker.run_forever(should_stop), name="ingestion-worker")
