"""`POST /query` and the conversation routes it hangs off.

Grounded answers and their sources, plus enough conversation management to make
follow-up questions possible. The retrieval diagnostics come back on every
response — `no_answer`, `reason`, `top_score`, `threshold` — so a thin answer is
distinguishable from a complete one without a second search.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel, Field, field_validator

from knowledgedock.api.dependencies import (
    enforce_rate_limit,
    get_current_user,
    require_configured_ai,
    require_workspace_access,
)
from knowledgedock.application.rag.answer_question import (
    AskQuestion,
    DeleteConversation,
    GetConversationHistory,
    ListConversations,
    StartConversation,
)
from knowledgedock.domain.conversation import Answer, Conversation, Message
from knowledgedock.domain.users import User
from knowledgedock.domain.workspaces import WorkspaceAccess

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["rag"])

Access = Annotated[WorkspaceAccess, Depends(require_workspace_access)]
AIConfigured = Annotated[None, Depends(require_configured_ai)]


def _limit(request: Request) -> None:
    return enforce_rate_limit(request, route="ai")


AIQuota = Annotated[None, Depends(_limit)]
CurrentUser = Annotated[User, Depends(get_current_user)]


def get_ask_question(request: Request) -> AskQuestion:
    return request.app.state.ask_question


def get_start_conversation(request: Request) -> StartConversation:
    return request.app.state.start_conversation


def get_list_conversations(request: Request) -> ListConversations:
    return request.app.state.list_conversations


def get_history(request: Request) -> GetConversationHistory:
    return request.app.state.get_conversation_history


def get_delete_conversation(request: Request) -> DeleteConversation:
    return request.app.state.delete_conversation


# Each one is a named function with an annotated `request: Request`. A bare
# lambda cannot carry that annotation, so FastAPI cannot tell `request` is the
# ASGI request and instead treats it as a body field the caller must send.
AskDep = Annotated[AskQuestion, Depends(get_ask_question)]
StartDep = Annotated[StartConversation, Depends(get_start_conversation)]
ListDep = Annotated[ListConversations, Depends(get_list_conversations)]
HistoryDep = Annotated[GetConversationHistory, Depends(get_history)]
DeleteConvDep = Annotated[DeleteConversation, Depends(get_delete_conversation)]


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=8000)
    #: Omit to start a new conversation. Supplying one makes this a follow-up,
    #: and the prior turns are used to interpret it.
    conversation_id: UUID | None = None

    @field_validator("question")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        # `min_length=1` accepts "   ". Rejecting it here keeps the answer to
        # "you sent nothing" a 422, instead of a ValueError surfacing as a 500
        # from the middle of the use case.
        if not value.strip():
            raise ValueError("A question is required.")
        return value


class SourceModel(BaseModel):
    document_id: UUID
    filename: str
    chunk_index: int
    score: float


class AnswerResponse(BaseModel):
    @classmethod
    def of(cls, answer: Answer) -> AnswerResponse:
        payload = answer.to_dict()
        return cls(
            question=payload["question"],
            answer=payload["answer"],
            no_answer=payload["no_answer"],
            conversation_id=payload["conversation_id"],
            sources=[SourceModel(**s) for s in payload["sources"]],
            retrieval=payload["retrieval"],
            model=payload["model"],
            provider=payload["provider"],
            tokens=payload["tokens"],
        )

    question: str
    answer: str
    no_answer: bool
    conversation_id: str | None
    sources: list[SourceModel]
    retrieval: dict
    model: str
    provider: str
    tokens: dict


class ConversationResponse(BaseModel):
    @classmethod
    def of(cls, conversation: Conversation) -> ConversationResponse:
        return cls(
            id=str(conversation.id),
            created_by=str(conversation.created_by),
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
        )

    id: str
    created_by: str
    created_at: object
    updated_at: object


class MessageResponse(BaseModel):
    @classmethod
    def of(cls, message: Message) -> MessageResponse:
        return cls(
            id=str(message.id),
            role=message.role.value,
            content=message.content,
            created_at=message.created_at,
            sources=[SourceModel(**c.to_dict()) for c in message.citations],
            no_answer=message.no_answer,
        )

    id: str
    role: str
    content: str
    created_at: object
    sources: list[SourceModel]
    no_answer: bool


@router.post("/query", response_model=AnswerResponse)
async def ask(
    access: Access,
    configured: AIConfigured,
    quota: AIQuota,
    user: CurrentUser,
    use_case: AskDep,
    payload: AskRequest,
) -> AnswerResponse:
    answer = await use_case.execute(
        access.workspace_id,
        payload.question,
        conversation_id=payload.conversation_id,
        user_id=user.id,
    )
    return AnswerResponse.of(answer)


@router.post(
    "/conversations",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def start_conversation(
    access: Access, user: CurrentUser, use_case: StartDep
) -> ConversationResponse:
    conversation = await use_case.execute(access.workspace_id, user.id)
    return ConversationResponse.of(conversation)


@router.get("/conversations")
async def list_conversations(
    access: Access,
    use_case: ListDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    items, total = await use_case.execute(access.workspace_id, limit=limit, offset=offset)
    return {
        "items": [ConversationResponse.of(c) for c in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/conversations/{conversation_id}")
async def get_conversation(access: Access, use_case: HistoryDep, conversation_id: UUID) -> dict:
    messages = await use_case.execute(access.workspace_id, conversation_id)
    return {
        "id": str(conversation_id),
        "messages": [MessageResponse.of(m) for m in messages],
    }


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    access: Access, use_case: DeleteConvDep, conversation_id: UUID
) -> Response:
    await use_case.execute(access.workspace_id, conversation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
