"""Atlas Vector Search index management.

The index is created by the application rather than by hand in the Atlas UI.
That is a deployment decision worth stating: KnowledgeDock runs against M0, which
allows at most three Atlas Search/Vector indexes, and a hand-made index cannot be
reviewed in version control or reproduced by anyone else deploying the project.

`workspace_id` is declared as a **filter field**, and that is the single most
important line in this file. `$vectorSearch` applies that filter *inside* the
index, so scoping happens before scoring. A post-filter would retrieve the global
top-k and discard what belongs to another tenant — which returns fewer than k
results for a small workspace and leaks information about what else exists.

Creation is best-effort. The process must still bind `$PORT` on Render even if the
cluster is unreachable, so failure is logged loudly and reported through
`/health/ready` rather than raised. An index also cannot be created while another
build is running, which is why an existing index counts as success.
"""

from __future__ import annotations

import logging

from pymongo.operations import SearchIndexModel

logger = logging.getLogger(__name__)

#: Chunks live here; the index is attached to that collection.
CHUNK_COLLECTION = "document_chunks"
DEFAULT_PATH = "embedding"
DEFAULT_FILTER_FIELD = "workspace_id"


def vector_index_definition(
    *,
    dimensions: int,
    similarity: str,
    path: str = DEFAULT_PATH,
    filter_field: str = DEFAULT_FILTER_FIELD,
) -> dict:
    """The `fields` portion of the index definition."""
    return {
        "fields": [
            {
                "type": "vector",
                "path": path,
                "numDimensions": dimensions,
                "similarity": similarity,
            },
            # Declared so `$vectorSearch` can filter on it. Not itself indexed.
            {"type": "filter", "path": filter_field},
        ]
    }


def build_index_model(
    *,
    index_name: str,
    dimensions: int,
    similarity: str,
    path: str = DEFAULT_PATH,
    filter_field: str = DEFAULT_FILTER_FIELD,
) -> SearchIndexModel:
    return SearchIndexModel(
        definition=vector_index_definition(
            dimensions=dimensions,
            similarity=similarity,
            path=path,
            filter_field=filter_field,
        ),
        name=index_name,
        type="vectorSearch",
    )


async def list_index_specs(database, collection: str = CHUNK_COLLECTION) -> list[dict]:
    """Every search index on a collection, including build status.

    The async driver exposes these on the *collection*, not the database, and the
    result is a cursor rather than a list.
    """
    try:
        cursor = await database[collection].list_search_indexes()
        return await cursor.to_list(length=None)
    except Exception:
        # A cluster without Search enabled raises here. Callers treat it as
        # "no index", which is the correct conclusion for readiness.
        logger.warning("vector_index.list_failed", extra={"collection": collection})
        return []


async def ensure_vector_index(
    database,
    *,
    index_name: str,
    dimensions: int,
    similarity: str,
    collection: str = CHUNK_COLLECTION,
    path: str = DEFAULT_PATH,
    filter_field: str = DEFAULT_FILTER_FIELD,
) -> bool:
    """Create the vector index if absent. Return True when one exists."""
    existing = {entry.get("name") for entry in await list_index_specs(database, collection)}
    if index_name in existing:
        return True

    model = build_index_model(
        index_name=index_name,
        dimensions=dimensions,
        similarity=similarity,
        path=path,
        filter_field=filter_field,
    )
    try:
        await database[collection].create_search_index(model)
    except Exception as exc:
        message = str(exc)
        if "already exists" in message:
            return True
        logger.error(
            "vector_index.creation_failed",
            extra={
                "index": index_name,
                "collection": collection,
                "dimensions": dimensions,
                "error": type(exc).__name__,
                "detail": message[:300],
            },
        )
        return False

    logger.info(
        "vector_index.created",
        extra={
            "index": index_name,
            "collection": collection,
            "dimensions": dimensions,
            "similarity": similarity,
            "filter_field": filter_field,
        },
    )
    return True


async def vector_index_ready(database, index_name: str, collection: str = CHUNK_COLLECTION) -> bool:
    """Whether the index exists and has finished building.

    Atlas reports `status` as `PENDING` while building and `READY` once results
    can be returned. Returning True for a building index would make retrieval look
    healthy while every query silently returns nothing.
    """
    for entry in await list_index_specs(database, collection):
        if entry.get("name") != index_name:
            continue
        if entry.get("status") in (None, "READY"):
            return True
        logger.info(
            "vector_index.building",
            extra={"index": index_name, "status": entry.get("status")},
        )
        return False
    return False
