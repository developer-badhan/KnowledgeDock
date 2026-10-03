"""Workspace and membership persistence.

Two collections, because they answer different questions:

  * `workspaces` holds the workspace itself. Indexed on `owner_id` for "workspaces
    I own", which is the one query a user can make without a membership lookup.
  * `workspace_members` holds access. Unique on `(workspace_id, user_id)` so the
    same person cannot be added twice, plus an index on `user_id` for "workspaces I
    can see", which drives the dashboard and every isolation check.

Listing a user's workspaces is a two-step read: find their membership rows, then
load those workspaces. Denormalising the workspace ids onto the user document
would save a round trip but introduces a second source of truth that can drift
whenever a member is removed. Atlas M0 allows 100 ops/sec and this is two
operations on an indexed field, so the join stays.

Both index shapes were verified against a real cluster.
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from pymongo import ASCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from knowledgedock.domain.workspaces import Workspace, WorkspaceMember, WorkspaceRole


class AlreadyAMemberError(Exception):
    """Raised when the unique `(workspace_id, user_id)` index rejects an insert."""


class WorkspaceRepository(Protocol):
    async def ensure_indexes(self) -> None: ...

    async def create(self, workspace: Workspace, owner: WorkspaceMember) -> Workspace: ...

    async def find_by_id(self, workspace_id: UUID) -> Workspace | None: ...

    async def list_for_user(self, user_id: UUID) -> list[tuple[Workspace, WorkspaceRole]]: ...

    async def add_member(self, member: WorkspaceMember) -> WorkspaceMember: ...

    async def find_member(self, workspace_id: UUID, user_id: UUID) -> WorkspaceMember | None: ...

    async def list_members(self, workspace_id: UUID) -> list[WorkspaceMember]: ...

    async def remove_member(self, workspace_id: UUID, user_id: UUID) -> bool: ...

    async def rename(self, workspace_id: UUID, name: str, updated_at: Any) -> Workspace | None: ...

    async def delete(self, workspace_id: UUID) -> bool: ...


class MongoWorkspaceRepository:
    def __init__(self, database: Any) -> None:
        self._workspaces = database["workspaces"]
        self._members = database["workspace_members"]

    async def ensure_indexes(self) -> None:
        await self._workspaces.create_index([("owner_id", ASCENDING)], name="workspaces_owner")
        # The uniqueness constraint that makes double-adding impossible.
        await self._members.create_index(
            [("workspace_id", ASCENDING), ("user_id", ASCENDING)],
            unique=True,
            name="members_workspace_user_unique",
        )
        # Drives "which workspaces can this user see".
        await self._members.create_index([("user_id", ASCENDING)], name="members_user")

    async def create(self, workspace: Workspace, owner: WorkspaceMember) -> Workspace:
        await self._workspaces.insert_one(workspace.to_document())
        try:
            await self._members.insert_one(owner.to_document())
        except DuplicateKeyError as exc:  # pragma: no cover - workspace id is fresh
            # Roll back so a workspace can never exist with no owner row, which
            # would make it invisible to everyone including its creator.
            await self._workspaces.delete_one({"_id": workspace.id})
            raise AlreadyAMemberError from exc
        return workspace

    async def find_by_id(self, workspace_id: UUID) -> Workspace | None:
        document = await self._workspaces.find_one({"_id": workspace_id})
        return Workspace.from_document(document) if document else None

    async def list_for_user(self, user_id: UUID) -> list[tuple[Workspace, WorkspaceRole]]:
        memberships = await self._members.find({"user_id": user_id}).to_list(length=None)
        if not memberships:
            return []
        ids = [m["workspace_id"] for m in memberships]
        # `_id: {$in: ids}` is one indexed round trip, not one per workspace.
        found = await self._workspaces.find({"_id": {"$in": ids}}).to_list(length=None)
        by_id = {w["_id"]: w for w in found}
        results: list[tuple[Workspace, WorkspaceRole]] = []
        for membership in memberships:
            document = by_id.get(membership["workspace_id"])
            if document is not None:
                results.append(
                    (Workspace.from_document(document), WorkspaceRole(membership["role"]))
                )
        results.sort(key=lambda pair: pair[0].created_at)
        return results

    async def add_member(self, member: WorkspaceMember) -> WorkspaceMember:
        try:
            await self._members.insert_one(member.to_document())
        except DuplicateKeyError as exc:
            raise AlreadyAMemberError from exc
        return member

    async def find_member(self, workspace_id: UUID, user_id: UUID) -> WorkspaceMember | None:
        document = await self._members.find_one({"workspace_id": workspace_id, "user_id": user_id})
        return WorkspaceMember.from_document(document) if document else None

    async def list_members(self, workspace_id: UUID) -> list[WorkspaceMember]:
        documents = await self._members.find({"workspace_id": workspace_id}).to_list(length=None)
        return [WorkspaceMember.from_document(d) for d in documents]

    async def remove_member(self, workspace_id: UUID, user_id: UUID) -> bool:
        result = await self._members.delete_one({"workspace_id": workspace_id, "user_id": user_id})
        return result.deleted_count == 1

    async def rename(self, workspace_id: UUID, name: str, updated_at: Any) -> Workspace | None:
        document = await self._workspaces.find_one_and_update(
            {"_id": workspace_id},
            {"$set": {"name": name, "updated_at": updated_at}},
            # PyMongo defaults to ReturnDocument.BEFORE, which returns the
            # document as it was *before* the update. A rename would then reply
            # with the old name. Found by running against a real cluster.
            return_document=ReturnDocument.AFTER,
        )
        return Workspace.from_document(document) if document else None

    async def delete(self, workspace_id: UUID) -> bool:
        # Membership rows first: an orphaned member row pointing at a deleted
        # workspace would still show up in someone's dashboard.
        await self._members.delete_many({"workspace_id": workspace_id})
        result = await self._workspaces.delete_one({"_id": workspace_id})
        return result.deleted_count == 1


class InMemoryWorkspaceRepository:
    """Test double with the same semantics as `MongoWorkspaceRepository`."""

    def __init__(self) -> None:
        self.workspaces: dict[UUID, Workspace] = {}
        self.members: dict[tuple[UUID, UUID], WorkspaceMember] = {}

    async def ensure_indexes(self) -> None:
        return None

    async def create(self, workspace: Workspace, owner: WorkspaceMember) -> Workspace:
        self.workspaces[workspace.id] = workspace
        self.members[(owner.workspace_id, owner.user_id)] = owner
        return workspace

    async def find_by_id(self, workspace_id: UUID) -> Workspace | None:
        return self.workspaces.get(workspace_id)

    async def list_for_user(self, user_id: UUID) -> list[tuple[Workspace, WorkspaceRole]]:
        results = [
            (self.workspaces[wid], m.role)
            for (wid, uid), m in self.members.items()
            if uid == user_id and wid in self.workspaces
        ]
        results.sort(key=lambda pair: pair[0].created_at)
        return results

    async def add_member(self, member: WorkspaceMember) -> WorkspaceMember:
        key = (member.workspace_id, member.user_id)
        if key in self.members:
            raise AlreadyAMemberError
        self.members[key] = member
        return member

    async def find_member(self, workspace_id: UUID, user_id: UUID) -> WorkspaceMember | None:
        return self.members.get((workspace_id, user_id))

    async def list_members(self, workspace_id: UUID) -> list[WorkspaceMember]:
        return [m for (wid, _), m in self.members.items() if wid == workspace_id]

    async def remove_member(self, workspace_id: UUID, user_id: UUID) -> bool:
        return self.members.pop((workspace_id, user_id), None) is not None

    async def rename(self, workspace_id: UUID, name: str, updated_at: Any) -> Workspace | None:
        existing = self.workspaces.get(workspace_id)
        if existing is None:
            return None
        updated = Workspace(
            id=existing.id,
            name=name,
            owner_id=existing.owner_id,
            created_at=existing.created_at,
            updated_at=updated_at,
        )
        self.workspaces[workspace_id] = updated
        return updated

    async def delete(self, workspace_id: UUID) -> bool:
        for key in [k for k in self.members if k[0] == workspace_id]:
            del self.members[key]
        return self.workspaces.pop(workspace_id, None) is not None


class UnavailableWorkspaceRepository:
    """Stands in when the workspace collections could not be reached at startup.

    Mirrors `UnavailableUserRepository`: every call raises `ServiceUnavailable`, so
    an outage is reported as an outage rather than as "you have no workspaces".
    The latter would look identical to a brand new account and would quietly hide
    a broken deployment.
    """

    _message = "Workspaces are unavailable. The database connection failed at startup."

    def __init__(self, cause: Exception | None = None) -> None:
        self.cause = cause

    async def ensure_indexes(self) -> None:
        return None

    async def create(self, workspace: Workspace, owner: WorkspaceMember) -> Workspace:
        raise self._error()

    async def find_by_id(self, workspace_id: UUID) -> Workspace | None:
        raise self._error()

    async def list_for_user(self, user_id: UUID) -> list[tuple[Workspace, WorkspaceRole]]:
        raise self._error()

    async def add_member(self, member: WorkspaceMember) -> WorkspaceMember:
        raise self._error()

    async def find_member(self, workspace_id: UUID, user_id: UUID) -> WorkspaceMember | None:
        raise self._error()

    async def list_members(self, workspace_id: UUID) -> list[WorkspaceMember]:
        raise self._error()

    async def remove_member(self, workspace_id: UUID, user_id: UUID) -> bool:
        raise self._error()

    async def rename(self, workspace_id: UUID, name: str, updated_at: Any) -> Workspace | None:
        raise self._error()

    async def delete(self, workspace_id: UUID) -> bool:
        raise self._error()

    def _error(self) -> Exception:
        from knowledgedock.domain.errors import ServiceUnavailable

        return ServiceUnavailable(self._message, detail=f"cause={self.cause!r}")
