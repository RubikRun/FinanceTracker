"""Authenticated encryption for one finance snapshot.

This module does not import Qt, Google Drive, or the finance store. It turns
plaintext bytes and a passphrase into a versioned envelope, and back.

Envelope version 1, big-endian integers:

    offset  size  field
    0       4     magic ``FTEN``
    4       1     version = 1
    5       1     KDF id = 1 (scrypt)
    6       4     scrypt N
    10      4     scrypt r
    14      4     scrypt p
    18      16    salt
    34      12    AES-GCM nonce
    46      4     generation
    50      20    created_at_utc, ASCII ``YYYY-MM-DDTHH:MM:SSZ``
    70      4     spending_count
    74      4     income_count
    78      4     next_id
    82            AES-256-GCM ciphertext, with the 16-byte tag at the end

The 82-byte header is stored in the clear and is the AES-GCM associated
data. Generation, time, record counts, and ``next_id`` can be read without
the passphrase. Amounts, categories, notes, the passphrase, and the key are
not in the header. Changing the header or the ciphertext makes decryption
fail. ``read_unverified_metadata`` does not check that tag.

scrypt parameters written by this version are N=2**16, r=8, p=1, with a
32-byte key. That is about 64 MiB and about 120 ms on the development
Windows PC (Python 3.14, October 2026), so a slower laptop still finishes
well under a second. Each call uses a new salt and a new nonce. Decryption
rejects N, r, or p above those values before it derives a key.

A wrong passphrase, a modified envelope, a truncated envelope, an unknown
version, or a malformed envelope raises ``CryptoError`` and returns nothing.
The error text is a fixed sentence.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from secrets import token_bytes

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"FTEN"
VERSION = 1
KDF_SCRYPT = 1

# See the module docstring. MAX_* is the highest cost decrypt will run.
SCRYPT_N = 2**16
SCRYPT_R = 8
SCRYPT_P = 1
MAX_SCRYPT_N = SCRYPT_N
MAX_SCRYPT_R = SCRYPT_R
MAX_SCRYPT_P = SCRYPT_P
KEY_LENGTH = 32
SALT_LENGTH = 16
NONCE_LENGTH = 12
TAG_LENGTH = 16
CREATED_AT_LENGTH = 20

OFFSET_MAGIC = 0
OFFSET_VERSION = 4
OFFSET_KDF = 5
OFFSET_N = 6
OFFSET_R = 10
OFFSET_P = 14
OFFSET_SALT = 18
OFFSET_NONCE = 34
OFFSET_GENERATION = 46
OFFSET_CREATED_AT = 50
OFFSET_SPENDING_COUNT = 70
OFFSET_INCOME_COUNT = 74
OFFSET_NEXT_ID = 78
HEADER_LENGTH = 82

_CREATED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_UNSUPPORTED_VERSION = "Unsupported envelope version."
_MALFORMED = "Encrypted snapshot is malformed."
_KDF_LIMIT = "Encryption parameters are not allowed."
_DECRYPTION_FAILED = "Decryption failed."


def _check_layout() -> None:
    checks = (
        (OFFSET_VERSION, OFFSET_MAGIC + len(MAGIC)),
        (OFFSET_KDF, OFFSET_VERSION + 1),
        (OFFSET_N, OFFSET_KDF + 1),
        (OFFSET_R, OFFSET_N + 4),
        (OFFSET_P, OFFSET_R + 4),
        (OFFSET_SALT, OFFSET_P + 4),
        (OFFSET_NONCE, OFFSET_SALT + SALT_LENGTH),
        (OFFSET_GENERATION, OFFSET_NONCE + NONCE_LENGTH),
        (OFFSET_CREATED_AT, OFFSET_GENERATION + 4),
        (OFFSET_SPENDING_COUNT, OFFSET_CREATED_AT + CREATED_AT_LENGTH),
        (OFFSET_INCOME_COUNT, OFFSET_SPENDING_COUNT + 4),
        (OFFSET_NEXT_ID, OFFSET_INCOME_COUNT + 4),
        (HEADER_LENGTH, OFFSET_NEXT_ID + 4),
    )
    for actual, expected in checks:
        if actual != expected:
            raise RuntimeError("envelope layout constants are inconsistent")


_check_layout()


class CryptoError(Exception):
    """Encryption or decryption failed.

    ``str(error)`` is a fixed sentence. It does not include the passphrase,
    the key, the plaintext, or the ciphertext.
    """


@dataclass(frozen=True)
class SnapshotMetadata:
    """Cleartext facts stored in the envelope header.

    ``created_at_utc`` is timezone-aware and an exact second. Counts and
    ``generation`` fit in an unsigned 32-bit integer.
    """

    generation: int
    created_at_utc: datetime
    spending_count: int
    income_count: int
    next_id: int

    def __post_init__(self) -> None:
        for name in ("generation", "spending_count", "income_count", "next_id"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an int")
            if value < 0 or value > 0xFFFFFFFF:
                raise ValueError(f"{name} is outside the envelope range")
        created = self.created_at_utc
        if not isinstance(created, datetime):
            raise TypeError("created_at_utc must be a datetime")
        if created.tzinfo is None or created.utcoffset() is None:
            raise ValueError("created_at_utc must be timezone-aware")
        if created.astimezone(timezone.utc).microsecond != 0:
            raise ValueError("created_at_utc must be an exact UTC second")


@dataclass(frozen=True)
class DecryptedSnapshot:
    """Plaintext plus the header that authenticated it."""

    plaintext: bytes
    metadata: SnapshotMetadata

    def __repr__(self) -> str:
        return (
            f"DecryptedSnapshot(plaintext=<{len(self.plaintext)} bytes>, "
            f"metadata={self.metadata!r})"
        )


@dataclass(frozen=True)
class _Parts:
    header: bytes
    n: int
    r: int
    p: int
    salt: bytes
    nonce: bytes
    metadata: SnapshotMetadata
    ciphertext: bytes


def encrypt(plaintext: bytes, passphrase: str, metadata: SnapshotMetadata) -> bytes:
    """Encrypt ``plaintext`` with a new salt and nonce.

    ``passphrase`` is encoded as UTF-8 and is not trimmed. The same
    passphrase and plaintext produce a different envelope every call.
    """
    _require_bytes(plaintext, "plaintext")
    _require_passphrase(passphrase)
    if not isinstance(metadata, SnapshotMetadata):
        raise TypeError("metadata must be SnapshotMetadata")
    salt = token_bytes(SALT_LENGTH)
    nonce = token_bytes(NONCE_LENGTH)
    header = _pack_header(SCRYPT_N, SCRYPT_R, SCRYPT_P, salt, nonce, metadata)
    key = _derive_key(passphrase, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, header)
    return header + ciphertext


def decrypt(envelope: bytes, passphrase: str) -> DecryptedSnapshot:
    """Decrypt ``envelope`` and return the plaintext and verified header.

    Raises ``CryptoError`` when the envelope cannot be authenticated.
    Nothing is returned in that case.
    """
    _require_bytes(envelope, "envelope")
    _require_passphrase(passphrase)
    parts = _inspect(envelope, require_tag=True)
    _enforce_kdf_limits(parts.n, parts.r, parts.p)
    key = _derive_key(passphrase, parts.salt, parts.n, parts.r, parts.p)
    try:
        plaintext = AESGCM(key).decrypt(parts.nonce, parts.ciphertext, parts.header)
    except InvalidTag:
        raise CryptoError(_DECRYPTION_FAILED) from None
    return DecryptedSnapshot(plaintext, parts.metadata)


def read_unverified_metadata(envelope: bytes) -> SnapshotMetadata:
    """Read the cleartext header without deriving a key or checking the tag.

    A modified header is reported as written. ``decrypt`` is what detects
    that change.
    """
    _require_bytes(envelope, "envelope")
    return _inspect(envelope, require_tag=False).metadata


def _require_bytes(value: object, label: str) -> None:
    if not isinstance(value, bytes):
        raise TypeError(f"{label} must be bytes")


def _require_passphrase(value: object) -> None:
    if not isinstance(value, str):
        raise TypeError("passphrase must be a string")


def _derive_key(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    if len(salt) != SALT_LENGTH:
        raise CryptoError(_MALFORMED)
    try:
        derived = Scrypt(salt=salt, length=KEY_LENGTH, n=n, r=r, p=p).derive(
            passphrase.encode("utf-8")
        )
    except (ValueError, TypeError):
        raise CryptoError(_MALFORMED) from None
    if len(derived) != KEY_LENGTH:
        raise CryptoError(_MALFORMED)
    return derived


def _enforce_kdf_limits(n: int, r: int, p: int) -> None:
    if n > MAX_SCRYPT_N or r > MAX_SCRYPT_R or p > MAX_SCRYPT_P:
        raise CryptoError(_KDF_LIMIT)
    if r < 1 or p < 1 or n < 2 or n & (n - 1) != 0:
        raise CryptoError(_MALFORMED)


def _pack_header(
    n: int,
    r: int,
    p: int,
    salt: bytes,
    nonce: bytes,
    metadata: SnapshotMetadata,
) -> bytes:
    stamp = _stamp(metadata.created_at_utc)
    if len(salt) != SALT_LENGTH or len(nonce) != NONCE_LENGTH:
        raise RuntimeError("envelope salt or nonce has the wrong length")
    header = bytearray(HEADER_LENGTH)
    header[OFFSET_MAGIC:OFFSET_VERSION] = MAGIC
    header[OFFSET_VERSION] = VERSION
    header[OFFSET_KDF] = KDF_SCRYPT
    struct.pack_into(">III", header, OFFSET_N, n, r, p)
    header[OFFSET_SALT:OFFSET_NONCE] = salt
    header[OFFSET_NONCE:OFFSET_GENERATION] = nonce
    struct.pack_into(">I", header, OFFSET_GENERATION, metadata.generation)
    header[OFFSET_CREATED_AT:OFFSET_SPENDING_COUNT] = stamp
    struct.pack_into(
        ">III",
        header,
        OFFSET_SPENDING_COUNT,
        metadata.spending_count,
        metadata.income_count,
        metadata.next_id,
    )
    return bytes(header)


def _stamp(created_at: datetime) -> bytes:
    stamp = created_at.astimezone(timezone.utc).strftime(_CREATED_AT_FORMAT).encode(
        "ascii"
    )
    if len(stamp) != CREATED_AT_LENGTH:
        raise RuntimeError("timestamp encoding is not 20 bytes")
    return stamp


def _inspect(envelope: bytes, *, require_tag: bool) -> _Parts:
    if len(envelope) < OFFSET_KDF:
        raise CryptoError(_MALFORMED)
    if envelope[: len(MAGIC)] != MAGIC:
        raise CryptoError(_MALFORMED)
    if envelope[OFFSET_VERSION] != VERSION:
        raise CryptoError(_UNSUPPORTED_VERSION)
    minimum = HEADER_LENGTH + (TAG_LENGTH if require_tag else 0)
    if len(envelope) < minimum:
        raise CryptoError(_MALFORMED)
    header = envelope[:HEADER_LENGTH]
    if header[OFFSET_KDF] != KDF_SCRYPT:
        raise CryptoError(_MALFORMED)
    n, r, p = struct.unpack_from(">III", header, OFFSET_N)
    salt = header[OFFSET_SALT:OFFSET_NONCE]
    nonce = header[OFFSET_NONCE:OFFSET_GENERATION]
    metadata = _metadata_from_header(header)
    ciphertext = envelope[HEADER_LENGTH:]
    return _Parts(header, n, r, p, salt, nonce, metadata, ciphertext)


def _metadata_from_header(header: bytes) -> SnapshotMetadata:
    (generation,) = struct.unpack_from(">I", header, OFFSET_GENERATION)
    raw_stamp = header[OFFSET_CREATED_AT:OFFSET_SPENDING_COUNT]
    try:
        text = raw_stamp.decode("ascii")
        parsed = datetime.strptime(text, _CREATED_AT_FORMAT)
    except (UnicodeDecodeError, ValueError):
        raise CryptoError(_MALFORMED) from None
    spending_count, income_count, next_id = struct.unpack_from(
        ">III", header, OFFSET_SPENDING_COUNT
    )
    return SnapshotMetadata(
        generation=generation,
        created_at_utc=parsed.replace(tzinfo=timezone.utc),
        spending_count=spending_count,
        income_count=income_count,
        next_id=next_id,
    )
