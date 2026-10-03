"""Request and response schemas.

These are transport shapes. Business rules live in `application.auth.policies`;
keeping them separate is what stops the JSON API and the HTML forms from drifting.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RegisterRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    email: str = Field(max_length=254)
    password: str = Field(min_length=1, max_length=128)


class LoginRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    email: str = Field(max_length=254)
    password: str = Field(min_length=1, max_length=128)


class PasswordResetRequestBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    email: str = Field(max_length=254)


class PasswordResetConfirmBody(BaseModel):
    token: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=128)


class UserResponse(BaseModel):
    """Never includes `password_hash` or `session_version`.

    Declared field by field on purpose: returning the ORM/document shape would
    publish the hash the moment a column is added.
    """

    id: UUID
    email: str
    created_at: str


class SessionResponse(BaseModel):
    user: UserResponse
    expires_in_seconds: int


class PasswordResetAccepted(BaseModel):
    """Identical for known and unknown addresses, on purpose.

    There is deliberately no token field. Returning one only for registered
    addresses would make this endpoint an account-enumeration oracle.
    """

    accepted: bool
    message: str


class MessageResponse(BaseModel):
    message: str
