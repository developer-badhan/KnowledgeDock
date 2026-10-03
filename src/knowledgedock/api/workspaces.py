"""Workspace routes.

Thin, as everywhere else: take input, call one use case, shape the output. Note
that authorization is not something a handler does here — it arrives as a
`WorkspaceAccess` dependency that has already consulted the membership
collection. A handler physically cannot skip the isolation check, because the
route will not run without it.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from knowledgedock.api.dependencies import get_current_user, require_workspace_access
from knowledgedock.application.workspaces.use_cases import (
    AddMember,
    CreateWorkspace,
    DeleteWorkspace,
    GetWorkspace,
    ListMembers,
    ListWorkspaces,
    RemoveMember,
    RenameWorkspace,
)
from knowledgedock.domain.users import User
from knowledgedock.domain.workspaces import WorkspaceAccess, WorkspaceMember

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


CurrentUser = Annotated[User, Depends(get_current_user)]
Access = Annotated[WorkspaceAccess, Depends(require_workspace_access)]


def get_create_workspace(request: Request) -> CreateWorkspace:
    return request.app.state.create_workspace


def get_list_workspaces(request: Request) -> ListWorkspaces:
    return request.app.state.list_workspaces


def get_get_workspace(request: Request) -> GetWorkspace:
    return request.app.state.get_workspace


def get_rename_workspace(request: Request) -> RenameWorkspace:
    return request.app.state.rename_workspace


def get_delete_workspace(request: Request) -> DeleteWorkspace:
    return request.app.state.delete_workspace


def get_list_members(request: Request) -> ListMembers:
    return request.app.state.list_members


def get_add_member(request: Request) -> AddMember:
    return request.app.state.add_member


def get_remove_member(request: Request) -> RemoveMember:
    return request.app.state.remove_member


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------
class WorkspaceCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=80)


class WorkspaceRename(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=80)


class WorkspaceResponse(BaseModel):
    id: UUID
    name: str
    role: str
    owner_id: UUID
    created_at: str
    member_count: int | None = None


class MemberAdd(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    email: str = Field(max_length=254)


class MemberResponse(BaseModel):
    user_id: UUID
    role: str
    added_at: str


class MessageResponse(BaseModel):
    message: str


# --------------------------------------------------------------------------
# Workspace collection
# --------------------------------------------------------------------------
@router.post("", response_model=WorkspaceResponse, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    payload: WorkspaceCreate,
    user: CurrentUser,
    use_case: Annotated[CreateWorkspace, Depends(get_create_workspace)],
) -> WorkspaceResponse:
    workspace = await use_case.execute(user, payload.name)
    return WorkspaceResponse(
        id=workspace.id,
        name=workspace.name,
        role="owner",
        owner_id=workspace.owner_id,
        created_at=workspace.created_at.isoformat(),
        member_count=1,
    )


@router.get("", response_model=list[WorkspaceResponse])
async def list_workspaces(
    user: CurrentUser, use_case: Annotated[ListWorkspaces, Depends(get_list_workspaces)]
) -> list[WorkspaceResponse]:
    """Only workspaces the caller belongs to. A user with none gets `[]`."""
    summaries = await use_case.execute_detailed(user)
    return [
        WorkspaceResponse(
            id=summary.workspace.id,
            name=summary.workspace.name,
            role=str(summary.role),
            owner_id=summary.workspace.owner_id,
            created_at=summary.workspace.created_at.isoformat(),
            member_count=summary.member_count,
        )
        for summary in summaries
    ]


@router.get("/{workspace_id}", response_model=WorkspaceResponse)
async def get_workspace(
    workspace_id: UUID,
    access: Access,
    user: CurrentUser,
    use_case: Annotated[GetWorkspace, Depends(get_get_workspace)],
    lister: Annotated[ListWorkspaces, Depends(get_list_workspaces)],
) -> WorkspaceResponse:
    workspace = await use_case.execute(workspace_id, user)
    summaries = await lister.execute_detailed(user)
    count = next((s.member_count for s in summaries if s.workspace.id == workspace_id), None)
    return WorkspaceResponse(
        id=workspace.id,
        name=workspace.name,
        role=str(access.role),
        owner_id=workspace.owner_id,
        created_at=workspace.created_at.isoformat(),
        member_count=count,
    )


@router.patch("/{workspace_id}", response_model=WorkspaceResponse)
async def rename_workspace(
    workspace_id: UUID,
    payload: WorkspaceRename,
    user: CurrentUser,
    use_case: Annotated[RenameWorkspace, Depends(get_rename_workspace)],
) -> WorkspaceResponse:
    workspace = await use_case.execute(workspace_id, user, payload.name)
    return WorkspaceResponse(
        id=workspace.id,
        name=workspace.name,
        role="owner",
        owner_id=workspace.owner_id,
        created_at=workspace.created_at.isoformat(),
    )


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace(
    workspace_id: UUID,
    user: CurrentUser,
    use_case: Annotated[DeleteWorkspace, Depends(get_delete_workspace)],
) -> Response:
    await use_case.execute(workspace_id, user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------
# Membership
# --------------------------------------------------------------------------
@router.get("/{workspace_id}/members", response_model=list[MemberResponse])
async def list_members(
    workspace_id: UUID,
    user: CurrentUser,
    use_case: Annotated[ListMembers, Depends(get_list_members)],
) -> list[MemberResponse]:
    members = await use_case.execute(workspace_id, user)
    return [_member_response(m) for m in members]


@router.post(
    "/{workspace_id}/members",
    response_model=MemberResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_member(
    workspace_id: UUID,
    payload: MemberAdd,
    user: CurrentUser,
    use_case: Annotated[AddMember, Depends(get_add_member)],
) -> MemberResponse:
    member = await use_case.execute(workspace_id, user, payload.email)
    return _member_response(member)


@router.delete("/{workspace_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    workspace_id: UUID,
    user_id: UUID,
    user: CurrentUser,
    use_case: Annotated[RemoveMember, Depends(get_remove_member)],
) -> Response:
    await use_case.execute(workspace_id, user, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _member_response(member: WorkspaceMember) -> MemberResponse:
    return MemberResponse(
        user_id=member.user_id,
        role=str(member.role),
        added_at=member.created_at.isoformat(),
    )
