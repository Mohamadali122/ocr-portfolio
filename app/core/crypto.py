"""AES-256-GCM. Used for document bodies and for PHI-bearing columns.

What this protects against: a leaked database dump, a copied backup, a stolen
disk image, an operator with filesystem read access but no application key.

What it does not protect against: an attacker who has the key. On the pilot
laptop the key lives in `.env`, on the same disk as the ciphertext. Full-disk
encryption (BitLocker) is the control that covers device theft. Saying "AES-256"
does not change that, and nobody should be allowed to believe otherwise.
"""

import base64
import os
from functools import lru_cache

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings

# GCM's standard nonce width. 96 bits is the only size the construction is
# proven for; other lengths route through an extra GHASH step.
NONCE_BYTES = 12
KEY_BYTES = 32

# Marks a blob as belonging to the versioned key-ring envelope
# (`_MAGIC || id_len || key_id || nonce || ciphertext || tag`), so `decrypt`
# can tell a rotated-format blob from one written before key versioning
# existed (bare `nonce || ciphertext || tag` under the sole key of the day).
# A random nonce starting with these exact 4 bytes is a 1-in-2^32 event, and
# even then GCM's tag still catches a wrong-key misparse -- this framing
# marker doesn't need to be cryptographically strong, only distinctive.
_MAGIC = b"CKR1"


class DecryptionError(Exception):
    """Ciphertext failed authentication: wrong key, corruption, or tampering.

    GCM cannot distinguish between the three, and neither should the caller.
    """


class AesGcmCipher:
    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_BYTES:
            raise ValueError(f"key must be {KEY_BYTES} bytes, got {len(key)}")
        self._aead = AESGCM(key)

    def encrypt(self, plaintext: bytes, *, aad: bytes) -> bytes:
        """Returns `nonce || ciphertext || tag`.

        `aad` is authenticated but not encrypted. Passing the storage key binds
        the ciphertext to its own filename: swapping two files on disk yields
        InvalidTag rather than a silently mismatched document.
        """
        nonce = os.urandom(NONCE_BYTES)
        return nonce + self._aead.encrypt(nonce, plaintext, aad)

    def decrypt(self, blob: bytes, *, aad: bytes) -> bytes:
        if len(blob) <= NONCE_BYTES:
            raise DecryptionError("ciphertext too short to contain a nonce")
        nonce, body = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
        try:
            return self._aead.decrypt(nonce, body, aad)
        except InvalidTag as exc:
            raise DecryptionError("authentication failed") from exc


def load_key(encoded: str) -> bytes:
    """Decode a urlsafe-base64 32-byte key.

    Generate one with:
        python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
    """
    try:
        key = base64.urlsafe_b64decode(encoded)
    except Exception as exc:
        raise ValueError("storage_encryption_key is not valid urlsafe base64") from exc
    if len(key) != KEY_BYTES:
        raise ValueError(f"storage_encryption_key must decode to {KEY_BYTES} bytes")
    return key


class CipherRing:
    """A small key-ring, not a single key -- rotating `current_id` to a new key
    stops encrypting under the old one without making anything already
    encrypted under it unreadable, as long as the old id stays in `ciphers`.

    `encrypt` always uses `current_id` and prefixes the blob with which id it
    used. `decrypt` reads that prefix back and picks the matching cipher, so
    the caller never needs to know or guess which key a given blob is under.
    A blob with no recognized prefix predates this scheme entirely -- exactly
    one key ever existed then, so it's decrypted under `current_id` unprefixed
    (see `_MAGIC`).
    """

    def __init__(self, ciphers: dict[str, AesGcmCipher], *, current_id: str) -> None:
        if current_id not in ciphers:
            raise ValueError(f"storage_encryption_key_current {current_id!r} not in the key ring")
        self._ciphers = ciphers
        self._current_id = current_id

    def encrypt(self, plaintext: bytes, *, aad: bytes) -> bytes:
        body = self._ciphers[self._current_id].encrypt(plaintext, aad=aad)
        key_id = self._current_id.encode("ascii")
        return _MAGIC + bytes([len(key_id)]) + key_id + body

    def decrypt(self, blob: bytes, *, aad: bytes) -> bytes:
        if blob[:4] != _MAGIC:
            # Written before key-ring versioning existed -- one key, unprefixed.
            return self._ciphers[self._current_id].decrypt(blob, aad=aad)
        id_len = blob[4]
        key_id = blob[5 : 5 + id_len].decode("ascii")
        cipher = self._ciphers.get(key_id)
        if cipher is None:
            # Rotated out of the ring too early -- indistinguishable from
            # corruption/tampering to the caller, same as a bad tag.
            raise DecryptionError(f"no key for storage key id {key_id!r}")
        return cipher.decrypt(blob[5 + id_len :], aad=aad)


@lru_cache(maxsize=1)
def get_cipher() -> CipherRing:
    # No argument: Settings is not hashable, and `get_settings` is itself cached.
    settings = get_settings()
    ciphers = {
        key_id: AesGcmCipher(load_key(value)) for key_id, value in settings.storage_key_ring.items()
    }
    return CipherRing(ciphers, current_id=settings.storage_encryption_key_current)
