"""Domain entities and business rules.

Nothing in this package imports FastAPI, PyMongo, or any provider SDK. If a
rule needs to know how something is stored or transported, it belongs elsewhere.
"""

from knowledgedock.domain.errors import (
    AppError,
    AuthenticationFailed,
    Conflict,
    ErrorCode,
    NotFound,
    PermissionDenied,
    ValidationFailed,
)
from knowledgedock.domain.users import User
from knowledgedock.domain.workspaces import (
    Workspace,
    WorkspaceAccess,
    WorkspaceMember,
    WorkspaceRole,
)

__all__ = [
    "AppError",
    "AuthenticationFailed",
    "Conflict",
    "ErrorCode",
    "NotFound",
    "PermissionDenied",
    "User",
    "ValidationFailed",
    "Workspace",
    "WorkspaceAccess",
    "WorkspaceMember",
    "WorkspaceRole",
]
