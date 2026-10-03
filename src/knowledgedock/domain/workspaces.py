"""Workspace entities and the membership model.

A workspace is the isolation boundary. Every document and conversation in
KnowledgeDock belongs to exactly one workspace, so the rules here decide who can
reach what.

The `WorkspaceAccess` value is the important part. It is produced only by a check
that has actually consulted the membership collection, which means an authorization
decision cannot be made from something a client controls.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class WorkspaceRole(StrEnum):
    OWNER = "owner"
    MEMBER = "member"

    @property
    def can_manage_members(self) -> bool:
        return self is WorkspaceRole.OWNER

    @property
    def can_modify_workspace(self) -> bool:
        return self is WorkspaceRole.OWNER

    @property
    def can_delete_workspace(self) -> bool:
        return self is WorkspaceRole.OWNER


@dataclass(frozen=True, slots=True)
class Workspace:
    id: UUID
    name: str
    owner_id: UUID
    created_at: datetime
    updated_at: datetime

    def to_document(self) -> dict[str, Any]:
        return {
            "_id": self.id,
            "name": self.name,
            "owner_id": self.owner_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> Workspace:
        return cls(
            id=document["_id"],
            name=document["name"],
            owner_id=document["owner_id"],
            created_at=document["created_at"],
            updated_at=document["updated_at"],
        )


@dataclass(frozen=True, slots=True)
class WorkspaceMember:
    workspace_id: UUID
    user_id: UUID
    role: WorkspaceRole
    added_by: UUID
    created_at: datetime

    def to_document(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "user_id": self.user_id,
            "role": str(self.role),
            "added_by": self.added_by,
            "created_at": self.created_at,
        }

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> WorkspaceMember:
        return cls(
            workspace_id=document["workspace_id"],
            user_id=document["user_id"],
            role=WorkspaceRole(document["role"]),
            added_by=document["added_by"],
            created_at=document["created_at"],
        )


@dataclass(frozen=True, slots=True)
class WorkspaceAccess:
    """Proof that a user may act inside a workspace, and with what role.

    Handing this around rather than a bare workspace id is deliberate: a handler
    that receives `WorkspaceAccess` cannot pretend the check was skipped.
    """

    workspace_id: UUID
    user_id: UUID
    role: WorkspaceRole

    @property
    def is_owner(self) -> bool:
        return self.role is WorkspaceRole.OWNER

    @property
    def can_manage_members(self) -> bool:
        return self.role.can_manage_members

    @property
    def can_modify_workspace(self) -> bool:
        return self.role.can_modify_workspace

    @property
    def can_delete_workspace(self) -> bool:
        return self.role.can_delete_workspace

    @classmethod
    def owner(cls, workspace_id: UUID, user_id: UUID) -> WorkspaceAccess:
        return cls(workspace_id=workspace_id, user_id=user_id, role=WorkspaceRole.OWNER)

    @classmethod
    def member(cls, workspace_id: UUID, user_id: UUID) -> WorkspaceAccess:
        return cls(workspace_id=workspace_id, user_id=user_id, role=WorkspaceRole.MEMBER)
