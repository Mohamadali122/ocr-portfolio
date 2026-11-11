"""The storage seam.

`services/` imports this file. It must never import `encrypted_local.py`.
Swapping in S3, MinIO, or a KMS-backed store is a new adapter and one line in
`api/deps.py` -- not a change to any use-case.
"""

from typing import Protocol, runtime_checkable


class StorageError(Exception):
    """The backend could not satisfy the request."""


class ObjectNotFoundError(StorageError):
    pass


@runtime_checkable
class StorageBackend(Protocol):
    def put(self, key: str, data: bytes) -> None:
        """Persist `data` under `key`. Overwrites are a programming error."""
        ...

    def get(self, key: str) -> bytes:
        """Return the bytes stored under `key`. Raises `ObjectNotFoundError`."""
        ...

    def delete(self, key: str) -> None:
        """Remove `key`. Idempotent."""
        ...

    def exists(self, key: str) -> bool: ...
