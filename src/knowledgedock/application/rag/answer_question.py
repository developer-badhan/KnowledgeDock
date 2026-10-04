"""The `/query` pipeline: retrieve → build context → generate → answer + sources.

Ordering is the whole design. Retrieval runs *before* any generation, so a
question with no supporting evidence costs one embedding call and never reaches
the model at all. That is the no-answer path, and it is cheaper and more honest
than asking the model to decline.

A second ordering choice matters just as much: the user's turn is persisted before
the answer is generated, not after. If generation fails, the question they asked
is still in the history, so the retry is a follow-up rather than a hole.
"""

from __future__ import annotations

import logging
from uuid import UUID

from knowledgedock.application.rag.prompt import build_grounded_prompt, build_retrieval_query
from knowledgedock.domain.conversation import (
    NO_ANSWER_TEXT,
    Answer,
    Citation,
    MessageRole,
    new_message,
)
from knowledgedock.domain.errors import NotFound
from knowledgedock.infrastructure.ai.llm import LLMProvider

logger = logging.getLogger(__name__)


class AskQuestion:
    def __init__(
        self,
        *,
        search: object,
        llm: LLMProvider,
        conversations: object,
        top_k: int,
        min_score: float,
        history_max_messages: int,
        max_question_characters: int,
    ) -> None:
        self._search = search
        self._llm = llm
        self._conversations = conversations
        self._top_k = top_k
        self._min_score = min_score
        self._history_max_messages = history_max_messages
        self._max_question_characters = max_question_characters

    async def execute(
        self,
        workspace_id: UUID,
        question: str,
        *,
        conversation_id: UUID | None = None,
        user_id: UUID | None = None,
    ) -> Answer:
        question = question.strip()
        if not question:
            raise ValueError("A question is required.")
        if len(question) > self._max_question_characters:
            # Truncated rather than refused: a long paste is a normal thing to
            # ask, and the first sentence usually carries the intent.
            question = question[: self._max_question_characters].strip()

        if conversation_id is None:
            from knowledgedock.domain.conversation import new_conversation

            conversation = await self._conversations.create(
                new_conversation(workspace_id, user_id or UUID(int=0))
            )
        else:
            # Scoped lookup: a conversation in another workspace is not found,
            # never merely unauthorised.
            conversation = await self._conversations.get(conversation_id, workspace_id)
            if conversation is None:
                raise NotFound("Conversation not found.")

        history = await self._conversations.history(
            conversation_id=conversation.id,
            workspace_id=workspace_id,
            limit=self._history_max_messages,
        )
        # Recorded before generation. A provider failure must not lose the
        # question, or the retry becomes a follow-up to a turn that never existed.
        await self._conversations.append(
            new_message(conversation.id, workspace_id, MessageRole.USER, question)
        )

        retrieval_query = build_retrieval_query(question, history)
        outcome = await self._search.execute(workspace_id, retrieval_query, limit=self._top_k)

        if outcome.no_answer or outcome.context.is_empty:
            # No model call. Cheapest honest path, and it cannot be talked into
            # answering from outside knowledge.
            answer = Answer(
                question=question,
                answer=NO_ANSWER_TEXT,
                no_answer=True,
                conversation_id=conversation.id,
                top_score=outcome.top_score,
                threshold=self._min_score,
                context_characters=0,
                model=self._llm.model,
                provider=self._llm.provider_name,
            )
            await self._conversations.append(
                new_message(
                    conversation.id,
                    workspace_id,
                    MessageRole.ASSISTANT,
                    NO_ANSWER_TEXT,
                    no_answer=True,
                )
            )
            return answer

        citations = tuple(
            Citation(
                document_id=block.chunk.document_id,
                filename=block.chunk.filename,
                chunk_index=block.chunk.chunk_index,
                score=block.chunk.score,
            )
            for block in outcome.context.blocks
        )
        prompt = build_grounded_prompt(question, outcome.context.blocks)
        try:
            generated = await self._llm.generate_answer(prompt)
        except Exception:
            # The question is already stored. Log and re-raise so the client sees
            # a real failure instead of an answer that quietly claims to be one.
            logger.exception("rag.generation_failed", extra={"workspace_id": str(workspace_id)})
            raise

        await self._conversations.append(
            new_message(
                conversation.id,
                workspace_id,
                MessageRole.ASSISTANT,
                generated.text,
                citations=citations,
            )
        )
        return Answer(
            question=question,
            answer=generated.text,
            no_answer=False,
            conversation_id=conversation.id,
            citations=citations,
            top_score=outcome.top_score,
            threshold=self._min_score,
            context_characters=outcome.context.character_count,
            model=generated.model,
            provider=generated.provider,
            input_tokens=generated.input_tokens,
            output_tokens=generated.output_tokens,
        )


class StartConversation:
    """Create an empty conversation.

    Separate from `/query` so a UI can open a thread before the first question,
    and so the conversation id is a stable handle rather than something that
    appears only as a side effect of asking.
    """

    def __init__(self, conversations: object) -> None:
        self._conversations = conversations

    async def execute(self, workspace_id: UUID, user_id: UUID) -> object:
        from knowledgedock.domain.conversation import new_conversation

        return await self._conversations.create(new_conversation(workspace_id, user_id))


class ListConversations:
    def __init__(self, conversations: object) -> None:
        self._conversations = conversations

    async def execute(
        self, workspace_id: UUID, *, limit: int = 20, offset: int = 0
    ) -> tuple[list[object], int]:
        return await self._conversations.list_recent(workspace_id, limit=limit, offset=offset)


class GetConversationHistory:
    """Return a conversation's turns, oldest first.

    Reads the same scoped lookup as everything else, so a conversation in another
    workspace is not found rather than forbidden.
    """

    def __init__(self, conversations: object) -> None:
        self._conversations = conversations

    async def execute(
        self, workspace_id: UUID, conversation_id: UUID, *, limit: int = 50
    ) -> list[object]:
        found = await self._conversations.get(conversation_id, workspace_id)
        if found is None:
            raise NotFound("Conversation not found.")
        return await self._conversations.history(conversation_id, workspace_id, limit=limit)


class DeleteConversation:
    def __init__(self, conversations: object) -> None:
        self._conversations = conversations

    async def execute(self, workspace_id: UUID, conversation_id: UUID) -> None:
        if not await self._conversations.delete(conversation_id, workspace_id):
            raise NotFound("Conversation not found.")


class GetUsage:
    """AI spend for one workspace over a rolling window.

    Scoped by workspace like everything else: usage is a property of the workspace
    that caused it, and a report that could be widened to "all workspaces" would be
    one query away from disclosing another tenant's activity.

    The window is bounded rather than "all time". An unbounded report on a
    long-lived workspace walks every row it has ever written, which is exactly the
    query shape that stops being fast; and the operational question is always
    "are we about to hit the free tier's daily ceiling", which is a rolling
    window question.
    """

    def __init__(self, usage: object, *, default_days: int = 1) -> None:
        self._usage = usage
        self._default_days = default_days

    async def execute(self, workspace_id: UUID, *, days: int | None = None) -> dict:
        from knowledgedock.infrastructure.repositories.usage_repository import default_window

        days = self._default_days if days is None else max(1, min(days, 90))
        totals = await self._usage.totals_for_workspace(workspace_id, since=default_window(days))
        return {
            "workspace_id": str(workspace_id),
            "window_days": days,
            "totals": totals,
        }
