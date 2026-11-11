"""Local filesystem, AES-256-GCM at rest.

Keys are UUID hex, never user-supplied filenames. That kills path traversal at
the source rather than sanitising it downstream: there is no `..` to strip if the
only accepted shape is 32 hex characters.
"""

import os
import re
import tempfile
from pathlib import Path

from app.core.crypto import CipherRing, DecryptionError
from app.core.logging import get_logger
from app.storage.base import ObjectNotFoundError, StorageError

logger = get_logger(__name__)

_KEY_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class EncryptedLocalStorage:
    """Implements `StorageBackend`."""

    def __init__(self, root: Path, cipher: CipherRing) -> None:
        self._root = root
        self._cipher = cipher
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if not _KEY_PATTERN.match(key):
            # An invalid key is a bug, not a user error. Refuse before touching disk.
            raise StorageError("storage key must be 32 lowercase hex characters")
        # Two levels of fan-out: 256 dirs each. A flat directory of 100k files
        # makes every listing operation on ext4 crawl.
        return self._root / key[:2] / key[2:4] / key

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)

        # AAD binds the ciphertext to this key. A file moved or renamed on disk
        # fails authentication instead of decrypting as some other document.
        blob = self._cipher.encrypt(data, aad=key.encode())

        # Write to a temp file in the same directory, then atomically rename.
        # A crash mid-write must not leave a truncated file that decrypts to
        # nothing and cannot be distinguished from tampering.
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(blob)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except Exception:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            blob = path.read_bytes()
        except FileNotFoundError as exc:
            raise ObjectNotFoundError(f"no object for key {key}") from exc

        try:
            return self._cipher.decrypt(blob, aad=key.encode())
        except DecryptionError:
            # Wrong key, corruption, or tampering -- indistinguishable, and all
            # three mean the same thing operationally: this document is not
            # trustworthy. Log the key, never the bytes.
            logger.error("ciphertext_authentication_failed", storage_key=key)
            raise

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()
