"""Workspace use cases.

This is where workspace isolation is enforced. `SKILL.md` §6 requires three
things on every protected operation:

    authenticated user  +  workspace membership  +  resource belongs to workspace

`AuthorizeWorkspace` produces the middle term, and nothing downstream is allowed to
skip it: routes receive a `WorkspaceAccess`, which cannot be constructed without a
membership row having been read.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from knowledgedock.application.auth.policies import normalize_email
from knowledgedock.domain.errors import (
    Conflict,
    NotFound,
    PermissionDenied,
    ValidationFailed,
)
from knowledgedock.domain.users import User, utcnow
from knowledgedock.domain.workspaces import (
    Workspace,
    WorkspaceAccess,
    WorkspaceMember,
    WorkspaceRole,
)
from knowledgedock.infrastructure.repositories.user_repository import UserRepository
from knowledgedock.infrastructure.repositories.workspace_repository import (
    AlreadyAMemberError,
    WorkspaceRepository,
)

# Names are not free text: they appear in nav and page titles.
MIN_NAME_LENGTH = 1
MAX_NAME_LENGTH = 80


def validate_workspace_name(raw: str) -> str:
    name = " ".join(raw.split())
    if len(name) < MIN_NAME_LENGTH:
        raise ValidationFailed("Give the workspace a name.")
    if len(name) > MAX_NAME_LENGTH:
        raise ValidationFailed(f"Name must be {MAX_NAME_LENGTH} characters or fewer.")
    return name


@dataclass(frozen=True, slots=True)
class WorkspaceSummary:
    workspace: Workspace
    role: WorkspaceRole
    member_count: int


class CreateWorkspace:
    def __init__(self, workspaces: WorkspaceRepository) -> None:
        self._workspaces = workspaces

    async def execute(self, owner: User, raw_name: str) -> Workspace:
        name = validate_workspace_name(raw_name)
        now = utcnow()
        workspace = Workspace(
            id=uuid4(),
            name=name,
            owner_id=owner.id,
            created_at=now,
            updated_at=now,
        )
        # Creator is inserted as the owner in the same call, so a workspace is
        # never briefly visible to nobody.
        await self._workspaces.create(
            workspace,
            WorkspaceMember(
                workspace_id=workspace.id,
                user_id=owner.id,
                role=WorkspaceRole.OWNER,
                added_by=owner.id,
                created_at=now,
            ),
        )
        return workspace


class ListWorkspaces:
    def __init__(self, workspaces: WorkspaceRepository) -> None:
        self._workspaces = workspaces

    async def execute(self, user: User) -> list[Workspace]:
        """Only workspaces this user is a member of. There is no 'all'."""
        pairs = await self._workspaces.list_for_user(user.id)
        return [workspace for workspace, _role in pairs]

    async def execute_detailed(self, user: User) -> list[WorkspaceSummary]:
        pairs = await self._workspaces.list_for_user(user.id)
        summaries: list[WorkspaceSummary] = []
        for workspace, role in pairs:
            members = await self._workspaces.list_members(workspace.id)
            summaries.append(WorkspaceSummary(workspace, role, len(members)))
        return summaries


class AuthorizeWorkspace:
    """The isolation gate. Every workspace-scoped operation calls this first."""

    def __init__(self, workspaces: WorkspaceRepository) -> None:
        self._workspaces = workspaces

    async def execute(self, workspace_id: UUID, user: User) -> WorkspaceAccess:
        """Return the caller's access, or refuse.

        Deliberately raises `NotFound` rather than `PermissionDenied` for a
        non-member. A 403 would confirm the workspace exists, which lets any user
        enumerate workspace identifiers. 404 makes "not yours" and "does not
        exist" indistinguishable, which is the whole point of an isolation
        boundary.
        """
        membership = await self._workspaces.find_member(workspace_id, user.id)
        if membership is None:
            raise NotFound("Workspace not found.")
        return WorkspaceAccess(
            workspace_id=workspace_id,
            user_id=user.id,
            role=membership.role,
        )


class GetWorkspace:
    def __init__(self, workspaces: WorkspaceRepository, authorize: AuthorizeWorkspace) -> None:
        self._workspaces = workspaces
        self._authorize = authorize

    async def execute(self, workspace_id: UUID, user: User) -> Workspace:
        await self._authorize.execute(workspace_id, user)
        workspace = await self._workspaces.find_by_id(workspace_id)
        if workspace is None:
            # Membership without a workspace: corruption, not a client error.
            raise NotFound("Workspace not found.")
        return workspace


class RenameWorkspace:
    def __init__(self, workspaces: WorkspaceRepository, authorize: AuthorizeWorkspace) -> None:
        self._workspaces = workspaces
        self._authorize = authorize

    async def execute(self, workspace_id: UUID, user: User, raw_name: str) -> Workspace:
        access = await self._authorize.execute(workspace_id, user)
        if not access.can_modify_workspace:
            raise PermissionDenied("Only the workspace owner can rename it.")
        name = validate_workspace_name(raw_name)
        renamed = await self._workspaces.rename(workspace_id, name, utcnow())
        if renamed is None:
            raise NotFound("Workspace not found.")
        return renamed


class DeleteWorkspace:
    def __init__(self, workspaces: WorkspaceRepository, authorize: AuthorizeWorkspace) -> None:
        self._workspaces = workspaces
        self._authorize = authorize

    async def execute(self, workspace_id: UUID, user: User) -> None:
        access = await self._authorize.execute(workspace_id, user)
        if not access.can_delete_workspace:
            raise PermissionDenied("Only the workspace owner can delete it.")
        deleted = await self._workspaces.delete(workspace_id)
        if not deleted:
            raise NotFound("Workspace not found.")


class ListMembers:
    def __init__(self, workspaces: WorkspaceRepository, authorize: AuthorizeWorkspace) -> None:
        self._workspaces = workspaces
        self._authorize = authorize

    async def execute(self, workspace_id: UUID, user: User) -> list[WorkspaceMember]:
        await self._authorize.execute(workspace_id, user)
        members = await self._workspaces.list_members(workspace_id)
        members.sort(key=lambda m: (m.role is not WorkspaceRole.OWNER, m.created_at))
        return members


class AddMember:
    """Add an existing user to a workspace by email.

    There is no pending-invite state. A pending invitation has to be delivered,
    and KnowledgeDock has no mail provider; Phase 2 declined to invent one. So a
    member is added directly, and only if an account with that address exists.

    Rejecting unknown addresses is an enumeration vector, but only to a user who
    is already a member of that workspace and therefore trusted with its roster.
    A silent no-op would be marginally safer and far less useful.
    """

    def __init__(
        self,
        workspaces: WorkspaceRepository,
        users: UserRepository,
        authorize: AuthorizeWorkspace,
    ) -> None:
        self._workspaces = workspaces
        self._users = users
        self._authorize = authorize

    async def execute(self, workspace_id: UUID, actor: User, raw_email: str) -> WorkspaceMember:
        access = await self._authorize.execute(workspace_id, actor)
        if not access.can_manage_members:
            raise PermissionDenied("Only the workspace owner can add members.")

        email = normalize_email(raw_email)
        invitee = await self._users.find_by_email(email)
        if invitee is None:
            raise NotFound("No account exists with that email address.")
        if not invitee.is_authenticatable:
            raise NotFound("No account exists with that email address.")
        if invitee.id == actor.id:
            raise ValidationFailed("You are already a member of this workspace.")

        member = WorkspaceMember(
            workspace_id=workspace_id,
            user_id=invitee.id,
            role=WorkspaceRole.MEMBER,
            added_by=actor.id,
            created_at=utcnow(),
        )
        try:
            return await self._workspaces.add_member(member)
        except AlreadyAMemberError as exc:
            raise Conflict("That person is already a member of this workspace.") from exc


class RemoveMember:
    def __init__(self, workspaces: WorkspaceRepository, authorize: AuthorizeWorkspace) -> None:
        self._workspaces = workspaces
        self._authorize = authorize

    async def execute(self, workspace_id: UUID, actor: User, target_id: UUID) -> None:
        """Remove a member, or let one leave.

        Two rules protect the workspace from becoming unreachable: the owner
        cannot be removed, and a plain member cannot remove anyone but themselves.
        """
        access = await self._authorize.execute(workspace_id, actor)

        if target_id != actor.id and not access.can_manage_members:
            raise PermissionDenied("Only the workspace owner can remove other members.")

        target = await self._workspaces.find_member(workspace_id, target_id)
        if target is None:
            raise NotFound("That person is not a member of this workspace.")

        if target.role is WorkspaceRole.OWNER:
            # Removing the owner would leave documents with nobody who can delete
            # them. Transferring ownership is a separate, explicit operation.
            raise Conflict("The workspace owner cannot be removed. Delete the workspace instead.")

        await self._workspaces.remove_member(workspace_id, target_id)
