import base64
import os
from pathlib import Path

import pytest

from app.core.crypto import (
    KEY_BYTES,
    NONCE_BYTES,
    AesGcmCipher,
    CipherRing,
    DecryptionError,
    load_key,
)
from app.storage.base import ObjectNotFoundError, StorageBackend, StorageError
from app.storage.encrypted_local import EncryptedLocalStorage

PLAINTEXT = b"%PDF-1.7 patient face sheet, Testerson, DOB 04/17/1958"


@pytest.fixture
def cipher() -> AesGcmCipher:
    return AesGcmCipher(os.urandom(KEY_BYTES))


@pytest.fixture
def storage(tmp_path: Path, cipher: AesGcmCipher) -> EncryptedLocalStorage:
    return EncryptedLocalStorage(root=tmp_path, cipher=cipher)


class TestCipher:
    def test_round_trip(self, cipher: AesGcmCipher) -> None:
        blob = cipher.encrypt(PLAINTEXT, aad=b"key")
        assert cipher.decrypt(blob, aad=b"key") == PLAINTEXT

    def test_plaintext_is_not_present_in_the_ciphertext(self, cipher: AesGcmCipher) -> None:
        assert b"Testerson" not in cipher.encrypt(PLAINTEXT, aad=b"key")

    def test_nonce_is_fresh_per_call(self, cipher: AesGcmCipher) -> None:
        # A repeated nonce under the same key destroys GCM's confidentiality and
        # leaks the authentication key. This is the single fatal misuse.
        blobs = {cipher.encrypt(PLAINTEXT, aad=b"key")[:NONCE_BYTES] for _ in range(100)}
        assert len(blobs) == 100

    def test_wrong_aad_fails_authentication(self, cipher: AesGcmCipher) -> None:
        # This is what stops an attacker swapping two files on disk: the AAD is
        # the storage key, so a moved ciphertext no longer authenticates.
        blob = cipher.encrypt(PLAINTEXT, aad=b"key-a")
        with pytest.raises(DecryptionError):
            cipher.decrypt(blob, aad=b"key-b")

    def test_flipped_bit_fails_authentication(self, cipher: AesGcmCipher) -> None:
        blob = bytearray(cipher.encrypt(PLAINTEXT, aad=b"key"))
        blob[-1] ^= 0x01
        with pytest.raises(DecryptionError):
            cipher.decrypt(bytes(blob), aad=b"key")

    def test_wrong_key_fails_authentication(self, cipher: AesGcmCipher) -> None:
        blob = cipher.encrypt(PLAINTEXT, aad=b"key")
        with pytest.raises(DecryptionError):
            AesGcmCipher(os.urandom(KEY_BYTES)).decrypt(blob, aad=b"key")

    def test_truncated_blob_is_rejected(self, cipher: AesGcmCipher) -> None:
        with pytest.raises(DecryptionError):
            cipher.decrypt(b"short", aad=b"key")

    def test_key_must_be_32_bytes(self) -> None:
        with pytest.raises(ValueError, match="32 bytes"):
            AesGcmCipher(os.urandom(16))

    def test_load_key_rejects_wrong_length(self) -> None:
        with pytest.raises(ValueError, match="32 bytes"):
            load_key(base64.urlsafe_b64encode(os.urandom(16)).decode())

    def test_load_key_accepts_a_generated_key(self) -> None:
        assert len(load_key(base64.urlsafe_b64encode(os.urandom(32)).decode())) == KEY_BYTES


class TestCipherRing:
    def test_round_trip_under_current_key(self) -> None:
        ring = CipherRing({"v1": AesGcmCipher(os.urandom(KEY_BYTES))}, current_id="v1")
        blob = ring.encrypt(PLAINTEXT, aad=b"key")
        assert ring.decrypt(blob, aad=b"key") == PLAINTEXT

    def test_decrypts_a_blob_written_under_an_older_key_still_in_the_ring(self) -> None:
        v0_cipher = AesGcmCipher(os.urandom(KEY_BYTES))
        old_ring = CipherRing({"v0": v0_cipher}, current_id="v0")
        blob = old_ring.encrypt(PLAINTEXT, aad=b"key")

        # Rotated: v1 is now current, but v0 stays in the ring for old ciphertext.
        rotated = CipherRing(
            {"v0": v0_cipher, "v1": AesGcmCipher(os.urandom(KEY_BYTES))}, current_id="v1"
        )
        assert rotated.decrypt(blob, aad=b"key") == PLAINTEXT
        # New writes use the current key, not the one that decrypted the old blob.
        assert rotated.encrypt(PLAINTEXT, aad=b"key") != blob

    def test_unknown_key_id_fails_cleanly_not_a_crash(self) -> None:
        v0_cipher = AesGcmCipher(os.urandom(KEY_BYTES))
        blob = CipherRing({"v0": v0_cipher}, current_id="v0").encrypt(PLAINTEXT, aad=b"key")

        # v0 dropped from the ring too early -- same failure as corruption/tampering.
        ring_without_v0 = CipherRing({"v1": AesGcmCipher(os.urandom(KEY_BYTES))}, current_id="v1")
        with pytest.raises(DecryptionError):
            ring_without_v0.decrypt(blob, aad=b"key")

    def test_legacy_unprefixed_blob_still_decrypts_under_the_current_key(self) -> None:
        # A blob written before key-ring versioning existed: bare
        # nonce||ciphertext||tag under the sole key of the day, no envelope.
        current = AesGcmCipher(os.urandom(KEY_BYTES))
        legacy_blob = current.encrypt(PLAINTEXT, aad=b"key")

        ring = CipherRing({"v1": current}, current_id="v1")
        assert ring.decrypt(legacy_blob, aad=b"key") == PLAINTEXT

    def test_current_id_must_be_in_the_ring(self) -> None:
        with pytest.raises(ValueError, match="not in the key ring"):
            CipherRing({"v0": AesGcmCipher(os.urandom(KEY_BYTES))}, current_id="v1")


class TestEncryptedLocalStorage:
    def test_satisfies_the_protocol(self, storage: EncryptedLocalStorage) -> None:
        assert isinstance(storage, StorageBackend)

    def test_round_trip(self, storage: EncryptedLocalStorage) -> None:
        key = "a" * 32
        storage.put(key, PLAINTEXT)
        assert storage.get(key) == PLAINTEXT

    def test_bytes_on_disk_are_not_the_plaintext(
        self, storage: EncryptedLocalStorage, tmp_path: Path
    ) -> None:
        key = "b" * 32
        storage.put(key, PLAINTEXT)
        on_disk = (tmp_path / "bb" / "bb" / key).read_bytes()
        assert PLAINTEXT not in on_disk
        assert b"Testerson" not in on_disk
        assert len(on_disk) == NONCE_BYTES + len(PLAINTEXT) + 16  # nonce + ct + tag

    def test_missing_key_raises_object_not_found(self, storage: EncryptedLocalStorage) -> None:
        with pytest.raises(ObjectNotFoundError):
            storage.get("c" * 32)

    @pytest.mark.parametrize(
        "key", ["../../etc/passwd", "..", "short", "A" * 32, "g" * 32, "", "a/b"]
    )
    def test_path_traversal_and_malformed_keys_are_refused(
        self, storage: EncryptedLocalStorage, key: str
    ) -> None:
        # Keys are UUID hex, never user input. Refusing anything else means there
        # is no `..` to sanitise, because there is no path to build.
        with pytest.raises(StorageError):
            storage.get(key)

    def test_a_file_moved_to_another_key_does_not_decrypt(
        self, storage: EncryptedLocalStorage, tmp_path: Path
    ) -> None:
        src, dst = "d" * 32, "e" * 32
        storage.put(src, PLAINTEXT)
        moved = tmp_path / "ee" / "ee" / dst
        moved.parent.mkdir(parents=True)
        moved.write_bytes((tmp_path / "dd" / "dd" / src).read_bytes())

        with pytest.raises(DecryptionError):
            storage.get(dst)

    def test_delete_is_idempotent(self, storage: EncryptedLocalStorage) -> None:
        key = "f" * 32
        storage.put(key, PLAINTEXT)
        assert storage.exists(key)
        storage.delete(key)
        storage.delete(key)
        assert not storage.exists(key)

    def test_no_temp_files_survive_a_successful_write(
        self, storage: EncryptedLocalStorage, tmp_path: Path
    ) -> None:
        storage.put("1" * 32, PLAINTEXT)
        assert not list(tmp_path.rglob("*.tmp"))

    def test_key_rotation_through_storage_keeps_old_documents_readable(
        self, tmp_path: Path
    ) -> None:
        v0 = AesGcmCipher(os.urandom(KEY_BYTES))
        v0_ring = CipherRing({"v0": v0}, current_id="v0")
        before_rotation = EncryptedLocalStorage(root=tmp_path, cipher=v0_ring)
        key = "9" * 32
        before_rotation.put(key, PLAINTEXT)

        # Rotate: v1 becomes current, v0 stays in the ring for what it already wrote.
        rotated_ring = CipherRing(
            {"v0": v0, "v1": AesGcmCipher(os.urandom(KEY_BYTES))}, current_id="v1"
        )
        after_rotation = EncryptedLocalStorage(root=tmp_path, cipher=rotated_ring)
        assert after_rotation.get(key) == PLAINTEXT

        # A fresh write after rotation is unreadable under a ring that dropped v0.
        new_key = "8" * 32
        after_rotation.put(new_key, PLAINTEXT)
        v0_only = EncryptedLocalStorage(root=tmp_path, cipher=v0_ring)
        with pytest.raises(DecryptionError):
            v0_only.get(new_key)
