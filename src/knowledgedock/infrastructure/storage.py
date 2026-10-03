"""File storage.

Content-addressed layout:

    {root}/{workspace_id}/{content_hash}{ext}

Two properties fall out of that, and both are deliberate.

**The stored name never comes from the client.** The user-supplied filename is
kept on the document record for display, but the path on disk is built from the
workspace id and the SHA-256 of the content. A filename is attacker-controlled
text; interpolating it into a path is how `../../etc/passwd` happens. It also
means re-uploading identical bytes lands on the same path, which is what makes
re-upload a replacement rather than a duplicate.

**The interface is narrow.** `save`, `delete`, `open` and `exists`. Phase 4 only
needs local disk, but keeping the surface this small means an S3 adapter later is
a new class rather than a refactor through the use cases.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import BinaryIO, Protocol
from uuid import UUID

# Only a whitelist, so an unexpected MIME type cannot pick an executable suffix.
CONTENT_TYPE_EXTENSIONS: dict[str, str] = {
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/html": ".html",
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}

HASH_CHUNK_BYTES = 64 * 1024


class StorageError(RuntimeError):
    """Storage refused the operation. Never leaks a filesystem path to a client."""


def extension_for(content_type: str) -> str:
    return CONTENT_TYPE_EXTENSIONS.get(content_type, ".bin")


class FileStorage(Protocol):
    def save(
        self,
        workspace_id: UUID,
        content_hash: str,
        content_type: str,
        source: BinaryIO,
    ) -> tuple[str, int]: ...

    def delete(self, relative_path: str) -> None: ...

    def exists(self, relative_path: str) -> bool: ...


class LocalFileStorage:
    """Stores uploads under a root directory on local disk.

    Render's filesystem is ephemeral, so this is a staging area rather than
    durable storage — the extracted text and vectors live in MongoDB. That is why
    deleting the stored file on document deletion is safe.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root).resolve()

    @property
    def root(self) -> Path:
        return self._root

    def ensure_root(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)

    def _absolute(self, relative_path: str) -> Path:
        """Resolve a stored path and refuse anything escaping the root.

        `resolve()` collapses `..`, so the containment check afterwards is on the
        real path rather than on the string that was supplied.
        """
        candidate = (self._root / relative_path).resolve()
        if not candidate.is_relative_to(self._root):
            raise StorageError("Refusing to access a path outside the storage root.")
        return candidate

    def save(
        self,
        workspace_id: UUID,
        content_hash: str,
        content_type: str,
        source: BinaryIO,
    ) -> tuple[str, int]:
        """Stream `source` to disk, returning `(relative_path, bytes_written)`.

        The caller has already enforced the size limit; this still counts bytes so
        the recorded size is measured rather than trusted.
        """
        directory = self._root / str(workspace_id)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{content_hash}{extension_for(content_type)}"
        written = 0
        try:
            with target.open("wb") as handle:
                while chunk := source.read(HASH_CHUNK_BYTES):
                    written += len(chunk)
                    handle.write(chunk)
        except OSError as exc:
            raise StorageError("Could not write the uploaded file.") from exc
        return str(target.relative_to(self._root)), written

    def delete(self, relative_path: str) -> None:
        """Remove a stored file. Missing is success: deletion is idempotent."""
        try:
            self._absolute(relative_path).unlink(missing_ok=True)
        except (OSError, StorageError):
            # The database row is the source of truth. A stray file on an
            # ephemeral disk is not worth failing a 204 over.
            return

    def delete_workspace_dir(self, workspace_id: UUID) -> None:
        shutil.rmtree(self._root / str(workspace_id), ignore_errors=True)

    def exists(self, relative_path: str) -> bool:
        try:
            return self._absolute(relative_path).is_file()
        except StorageError:
            return False

    def size_of(self, relative_path: str) -> int:
        try:
            return self._absolute(relative_path).stat().st_size
        except (OSError, StorageError):
            return 0


def hash_stream(source: BinaryIO) -> tuple[str, int]:
    """Return `(sha256_hex, byte_count)` for a binary stream, read once.

    SHA-256 rather than MD5: this hash is the document's identity and decides
    whether an upload replaces an existing document. A collision would mean
    silently discarding one user's file in place of another's.
    """
    digest = hashlib.sha256()
    size = 0
    while chunk := source.read(HASH_CHUNK_BYTES):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def hash_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
