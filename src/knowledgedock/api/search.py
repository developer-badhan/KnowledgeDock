"""`POST /search` — raw semantic search, no LLM.

This endpoint exists to make retrieval debuggable, and it is where that
debuggability lives. Every other view of relevance is downstream of an
answer that may be wrong for reasons unrelated to retrieval, so there is nowhere
else to look when ranking misbehaves.

The response is deliberately verbose about *why* it answered the way it did:
`threshold`, `top_score`, `candidates_returned`, `below_threshold`, the embedding
model, and the assembled context including what the budget dropped. A no-answer
distinguishes an empty index from a threshold set too high — the first is an
ingestion bug, the second is a tuning decision, and they look identical without
this.

Two cutoffs are reported: `threshold` is the confident bar and `weak_threshold`
the weak floor. Chunks in between come back in `weak_hits` with `no_answer`
still False — below the bar, but still evidence a generation might use. The UI
for the wording this service runs on chooses to hand those to the model, which
is allowed to decline.

No per-document score is stored. Scores belong to a query, not to a document, so
persisting one would create state that goes stale against the corpus and is
wrong for the next question. They are returned here and forgotten.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from knowledgedock.api.dependencies import (
    enforce_rate_limit,
    require_configured_ai,
    require_workspace_access,
)
from knowledgedock.application.retrieval.search import SemanticSearch
from knowledgedock.domain.retrieval import SearchOutcome
from knowledgedock.domain.workspaces import WorkspaceAccess

router = APIRouter(prefix="/workspaces/{workspace_id}/search", tags=["search"])

Access = Annotated[WorkspaceAccess, Depends(require_workspace_access)]
# Checked before the workspace lookup: a request that cannot be answered should
# not spend effort proving the caller may ask.
AIConfigured = Annotated[None, Depends(require_configured_ai)]


def _limit(request: Request) -> None:
    return enforce_rate_limit(request, route="ai")


AIQuota = Annotated[None, Depends(_limit)]


def get_semantic_search(request: Request) -> SemanticSearch:
    return request.app.state.semantic_search


SearchDep = Annotated[SemanticSearch, Depends(get_semantic_search)]


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    #: Overrides `RETRIEVAL_TOP_K` for this call. Capped so a single request
    #: cannot ask the index for an unbounded number of 768-dimension vectors.
    limit: int | None = Field(default=None, ge=1, le=50)


class SearchResponse(BaseModel):
    @classmethod
    def of(cls, outcome: SearchOutcome) -> SearchResponse:
        return cls(**outcome.to_dict())

    query: str
    hits: list[dict]
    weak_hits: list[dict] = []
    no_answer: bool
    reason: str | None
    top_score: float | None
    threshold: float
    weak_threshold: float = 0.0
    limit: int
    candidates_returned: int
    below_threshold: int
    context: dict
    embedding: dict


@router.post("", response_model=SearchResponse)
async def search(
    access: Access,
    configured: AIConfigured,
    quota: AIQuota,
    use_case: SearchDep,
    payload: SearchRequest,
) -> SearchResponse:
    outcome = await use_case.execute(access.workspace_id, payload.query, limit=payload.limit)
    return SearchResponse.of(outcome)
