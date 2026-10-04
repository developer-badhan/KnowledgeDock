"""Conversation and message persistence.

Every query takes `(conversation_id, workspace_id)`, never a bare id. That is the
same rule Phase 04 applied to documents and it is not optional: a bare id would
let anyone who learned a conversation UUID read another workspace's history,
because the id is a bearer capability that HTTP clients cannot be trusted to scope
themselves.
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from pymongo import ASCENDING, DESCENDING
from pymongo.errors import BulkWriteError, DuplicateKeyError

from knowledgedock.domain.conversation import Conversation, Message
from knowledgedock.domain.errors import ServiceUnavailable


class ConversationRepository(Protocol):
    async def ensure_indexes(self) -> None: ...

    async def create(self, conversation: Conversation) -> Conversation: ...

    async def get(self, conversation_id: UUID, workspace_id: UUID) -> Conversation | None: ...

    async def list_recent(
        self, workspace_id: UUID, *, limit: int, offset: int
    ) -> tuple[list[Conversation], int]: ...

    async def append(self, message: Message) -> Message: ...

    async def history(
        self, conversation_id: UUID, workspace_id: UUID, *, limit: int
    ) -> list[Message]: ...

    async def delete(self, conversation_id: UUID, workspace_id: UUID) -> bool: ...


class MongoConversationRepository:
    def __init__(self, database: Any) -> None:
        self._conversations = database["conversations"]
        self._messages = database["messages"]

    async def ensure_indexes(self) -> None:
        await self._conversations.create_index(
            [("workspace_id", ASCENDING), ("updated_at", DESCENDING)],
            name="conversations_workspace_recent",
        )
        # One place to find a conversation's turns in order, already scoped.
        await self._messages.create_index(
            [("conversation_id", ASCENDING), ("created_at", ASCENDING)],
            name="messages_conversation_ordered",
        )
        # Conversation deletion must not orphan turns. Atlas does not offer
        # cascades, so this is the query the cleanup path uses.
        await self._messages.create_index([("workspace_id", ASCENDING)], name="messages_workspace")

    async def create(self, conversation: Conversation) -> Conversation:
        try:
            await self._conversations.insert_one(conversation.to_document())
        except (DuplicateKeyError, BulkWriteError) as exc:
            raise ServiceUnavailable(
                "Could not start the conversation.", detail=f"{type(exc).__name__}: {exc}"
            ) from exc
        return conversation

    async def get(self, conversation_id: UUID, workspace_id: UUID) -> Conversation | None:
        document = await self._conversations.find_one(
            {"_id": conversation_id, "workspace_id": workspace_id}
        )
        return Conversation.from_document(document)

    async def list_recent(
        self, workspace_id: UUID, *, limit: int, offset: int
    ) -> tuple[list[Conversation], int]:
        query = {"workspace_id": workspace_id}
        total = await self._conversations.count_documents(query)
        cursor = self._conversations.find(query).sort("updated_at", DESCENDING)
        cursor = cursor.skip(offset).limit(limit)
        documents = await cursor.to_list(length=limit)
        return [Conversation.from_document(d) for d in documents if d], total

    async def append(self, message: Message) -> Message:
        try:
            await self._messages.insert_one(message.to_document())
        except (DuplicateKeyError, BulkWriteError) as exc:
            raise ServiceUnavailable(
                "Could not record the message.", detail=f"{type(exc).__name__}: {exc}"
            ) from exc
        # Newest first on the conversation list, so a conversation appears at the
        # top the moment it has been used. Failures here are not fatal: the
        # message itself is already stored, and a stale sort key is recoverable.
        await self._conversations.update_one(
            {"_id": message.conversation_id}, {"$set": {"updated_at": message.created_at}}
        )
        return message

    async def history(
        self, conversation_id: UUID, workspace_id: UUID, *, limit: int
    ) -> list[Message]:
        """Most recent `limit` turns, returned oldest-first.

        Sorting newest-first and then reversing is what "the last N turns in
        order" requires. Taking `limit` from the wrong end would silently drop the
        oldest turns, which are usually the ones that carry the topic a follow-up
        depends on.
        """
        if limit <= 0:
            return []
        cursor = (
            self._messages.find({"conversation_id": conversation_id, "workspace_id": workspace_id})
            .sort("created_at", DESCENDING)
            .limit(limit)
        )
        documents = await cursor.to_list(length=limit)
        return [Message.from_document(d) for d in reversed(documents)]

    async def delete(self, conversation_id: UUID, workspace_id: UUID) -> bool:
        deleted = await self._conversations.delete_one(
            {"_id": conversation_id, "workspace_id": workspace_id}
        )
        if deleted.deleted_count:
            # Atlas has no cascade, so the turns go explicitly or not at all.
            await self._messages.delete_many(
                {"conversation_id": conversation_id, "workspace_id": workspace_id}
            )
            return True
        return False


class InMemoryConversationRepository:
    def __init__(self) -> None:
        self.conversations: dict[UUID, Conversation] = {}
        self.messages: dict[UUID, list[Message]] = {}

    async def ensure_indexes(self) -> None:
        return None

    async def create(self, conversation: Conversation) -> Conversation:
        self.conversations[conversation.id] = conversation
        return conversation

    async def get(self, conversation_id: UUID, workspace_id: UUID) -> Conversation | None:
        found = self.conversations.get(conversation_id)
        if found is None or found.workspace_id != workspace_id:
            return None
        return found

    async def list_recent(
        self, workspace_id: UUID, *, limit: int, offset: int
    ) -> tuple[list[Conversation], int]:
        mine = [c for c in self.conversations.values() if c.workspace_id == workspace_id]
        mine.sort(key=lambda c: c.updated_at, reverse=True)
        return mine[offset : offset + limit], len(mine)

    async def append(self, message: Message) -> Message:
        self.messages.setdefault(message.conversation_id, []).append(message)
        conversation = self.conversations.get(message.conversation_id)
        if conversation is not None:
            self.conversations[conversation.id] = Conversation(
                id=conversation.id,
                workspace_id=conversation.workspace_id,
                created_by=conversation.created_by,
                created_at=conversation.created_at,
                updated_at=max(conversation.updated_at, message.created_at),
            )
        return message

    async def history(
        self, conversation_id: UUID, workspace_id: UUID, *, limit: int
    ) -> list[Message]:
        turns = [
            m for m in self.messages.get(conversation_id, []) if m.workspace_id == workspace_id
        ]
        turns.sort(key=lambda m: m.created_at)
        return turns[-limit:] if limit > 0 else []

    async def delete(self, conversation_id: UUID, workspace_id: UUID) -> bool:
        found = await self.get(conversation_id, workspace_id)
        if found is None:
            return False
        del self.conversations[conversation_id]
        self.messages.pop(conversation_id, None)
        return True


class UnavailableConversationRepository:
    """Stands in when the database is unreachable at startup.

    Phase 01 decision 2: the process must still bind `$PORT`, so this raises on
    use rather than at construction.
    """

    _message = "Conversations are unavailable. The database connection failed at startup."

    def __init__(self, cause: Exception | None = None) -> None:
        self.cause = cause

    async def ensure_indexes(self) -> None:
        return None

    async def create(self, conversation: Conversation) -> Conversation:
        raise self._error()

    async def get(self, conversation_id: UUID, workspace_id: UUID) -> Conversation | None:
        raise self._error()

    async def list_recent(
        self, workspace_id: UUID, *, limit: int, offset: int
    ) -> tuple[list[Conversation], int]:
        raise self._error()

    async def append(self, message: Message) -> Message:
        raise self._error()

    async def history(
        self, conversation_id: UUID, workspace_id: UUID, *, limit: int
    ) -> list[Message]:
        raise self._error()

    async def delete(self, conversation_id: UUID, workspace_id: UUID) -> bool:
        raise self._error()

    def _error(self) -> Exception:
        return ServiceUnavailable(self._message, detail=f"cause={self.cause!r}")
