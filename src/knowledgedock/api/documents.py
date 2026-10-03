"""Document routes, nested under the workspace.

The nesting is load-bearing, not cosmetic. `require_workspace_access` resolves
membership from a path parameter, so `/workspaces/{workspace_id}/documents/...`
is the only shape in which the isolation check can run as a dependency. A flat
`/documents/{id}` would relocate that check into the use case, where the next
endpoint could forget it. See roadmap decision 33.

Authorization is identical for every route here: the workspace is the only
boundary, and every member can see every document in it. The uploader is not
part of any check.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile, status
from pydantic import BaseModel

from knowledgedock.api.dependencies import get_current_user, require_workspace_access
from knowledgedock.application.documents.use_cases import (
    DeleteDocument,
    GetDocument,
    ListDocuments,
    UploadDocument,
)
from knowledgedock.domain.documents import Document
from knowledgedock.domain.users import User
from knowledgedock.domain.workspaces import WorkspaceAccess

router = APIRouter(prefix="/workspaces/{workspace_id}/documents", tags=["documents"])

Access = Annotated[WorkspaceAccess, Depends(require_workspace_access)]
CurrentUser = Annotated[User, Depends(get_current_user)]


def get_upload_document(request: Request) -> UploadDocument:
    return request.app.state.upload_document


def get_list_documents(request: Request) -> ListDocuments:
    return request.app.state.list_documents


def get_get_document(request: Request) -> GetDocument:
    return request.app.state.get_document


def get_delete_document(request: Request) -> DeleteDocument:
    return request.app.state.delete_document


UploadDep = Annotated[UploadDocument, Depends(get_upload_document)]
ListDep = Annotated[ListDocuments, Depends(get_list_documents)]
GetDep = Annotated[GetDocument, Depends(get_get_document)]
DeleteDep = Annotated[DeleteDocument, Depends(get_delete_document)]


class DocumentResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    filename: str
    content_type: str
    size_bytes: int
    status: str
    chunk_count: int
    processing_error: str | None
    # Recorded for display. Explicitly not an access control.
    uploaded_by: UUID
    created_at: str
    updated_at: str
    #: True when this upload replaced identical content already in the workspace.
    replaced: bool = False

    @classmethod
    def of(cls, document: Document, *, replaced: bool = False) -> DocumentResponse:
        return cls(
            id=document.id,
            workspace_id=document.workspace_id,
            filename=document.filename,
            content_type=document.content_type,
            size_bytes=document.size_bytes,
            status=str(document.status),
            chunk_count=document.chunk_count,
            processing_error=document.processing_error,
            uploaded_by=document.uploaded_by,
            created_at=document.created_at.isoformat(),
            updated_at=document.updated_at.isoformat(),
            replaced=replaced,
        )


class DocumentListResponse(BaseModel):
    items: list[DocumentResponse]
    total: int
    limit: int
    offset: int
    has_more: bool


@router.post(
    "",
    response_model=DocumentResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_document(
    access: Access,
    user: CurrentUser,
    use_case: UploadDep,
    file: Annotated[UploadFile, File()],
) -> DocumentResponse:
    """Accept an upload for processing.

    202, not 201: the document exists, but nothing has been extracted or embedded
    yet. `status` is `pending` and the worker picks it up from there.

    Re-uploading identical content updates that document in place and returns 202
    again, with `replaced: true`. There is no rejection and no duplicate row.
    """
    result = await use_case.execute(
        access,
        user,
        filename=file.filename,
        content_type=file.content_type,
        source=file.file,
        declared_size=file.size,
    )
    return DocumentResponse.of(result.document, replaced=result.replaced)


@router.get("", response_model=DocumentListResponse)
async def list_documents(
    access: Access,
    use_case: ListDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DocumentListResponse:
    page = await use_case.execute(access.workspace_id, limit=limit, offset=offset)
    return DocumentListResponse(
        items=[DocumentResponse.of(d) for d in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
        has_more=page.has_more,
    )


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document(access: Access, use_case: GetDep, document_id: UUID) -> DocumentResponse:
    return DocumentResponse.of(await use_case.execute(access.workspace_id, document_id))


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(access: Access, use_case: DeleteDep, document_id: UUID) -> Response:
    """Hard delete: the row, its chunks, and the stored file."""
    await use_case.execute(access.workspace_id, document_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
