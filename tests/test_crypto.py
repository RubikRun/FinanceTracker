"""Snapshot envelope encryption. Synthetic plaintext only; no Drive and no GUI."""

from __future__ import annotations

import ast
import struct
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from finance import crypto
from finance.crypto import (
    HEADER_LENGTH,
    KEY_LENGTH,
    MAX_SCRYPT_N,
    MAX_SCRYPT_P,
    MAX_SCRYPT_R,
    NONCE_LENGTH,
    OFFSET_CREATED_AT,
    OFFSET_GENERATION,
    OFFSET_KDF,
    OFFSET_N,
    OFFSET_NONCE,
    OFFSET_P,
    OFFSET_R,
    OFFSET_SALT,
    OFFSET_VERSION,
    SALT_LENGTH,
    SCRYPT_N,
    SCRYPT_P,
    SCRYPT_R,
    TAG_LENGTH,
    CryptoError,
    SnapshotMetadata,
    decrypt,
    encrypt,
    read_unverified_metadata,
)
from tests.support import SYNTHETIC_SECRET, assert_failure_omits_payload

PASSPHRASE = "synthetic-passphrase-alpha"
OTHER_PASSPHRASE = "synthetic-passphrase-beta"
_PLAINTEXT = (
    b'{"note":"'
    + SYNTHETIC_SECRET.encode("ascii")
    + b'","category":"Groceries","amount":"14.20"}'
)


def _metadata(**overrides: object) -> SnapshotMetadata:
    fields = {
        "generation": 4,
        "created_at_utc": datetime(2026, 10, 21, 15, 4, 5, tzinfo=timezone.utc),
        "spending_count": 2,
        "income_count": 1,
        "next_id": 9,
    }
    fields.update(overrides)
    return SnapshotMetadata(**fields)


def _encrypt(**overrides: object) -> bytes:
    metadata = overrides.pop("metadata", None)
    passphrase = overrides.pop("passphrase", PASSPHRASE)
    plaintext = overrides.pop("plaintext", _PLAINTEXT)
    if overrides:
        raise TypeError(f"unexpected arguments {sorted(overrides)}")
    return encrypt(
        plaintext,
        passphrase,
        metadata if metadata is not None else _metadata(),
    )


def _assert_safe(exc: BaseException) -> None:
    assert_failure_omits_payload(exc, SYNTHETIC_SECRET)
    assert_failure_omits_payload(exc, PASSPHRASE)
    assert_failure_omits_payload(exc, OTHER_PASSPHRASE)
    assert "14.20" not in str(exc)
    assert "Groceries" not in str(exc)


def test_documented_header_and_scrypt_parameters() -> None:
    assert crypto.MAGIC == b"FTEN"
    assert crypto.VERSION == 1
    assert crypto.HEADER_LENGTH == 82
    assert crypto.OFFSET_VERSION == 4
    assert crypto.OFFSET_GENERATION == 46
    assert crypto.OFFSET_CREATED_AT == 50
    assert SCRYPT_N == 2**16
    assert SCRYPT_R == 8
    assert SCRYPT_P == 1
    assert KEY_LENGTH == 32
    assert SALT_LENGTH == 16
    assert NONCE_LENGTH == 12
    assert TAG_LENGTH == 16
    assert (MAX_SCRYPT_N, MAX_SCRYPT_R, MAX_SCRYPT_P) == (SCRYPT_N, SCRYPT_R, SCRYPT_P)
    assert 128 * SCRYPT_N * SCRYPT_R == 64 * 1024 * 1024


def test_crypto_module_does_not_import_qt_drive_or_the_store() -> None:
    source = Path(crypto.__file__).read_text(encoding="utf-8")
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    assert modules.isdisjoint({"PySide6", "google", "finance"})


def test_round_trip_restores_plaintext_and_header() -> None:
    created = datetime(2026, 10, 21, 18, 4, 5, tzinfo=timezone(timedelta(hours=3)))
    metadata = _metadata(created_at_utc=created)
    envelope = _encrypt(metadata=metadata, plaintext=b"")
    opened = decrypt(envelope, PASSPHRASE)

    assert opened.plaintext == b""
    assert opened.metadata == _metadata(
        created_at_utc=datetime(2026, 10, 21, 15, 4, 5, tzinfo=timezone.utc)
    )
    full = _encrypt()
    opened_full = decrypt(full, PASSPHRASE)
    assert opened_full.plaintext == _PLAINTEXT
    assert opened_full.metadata == _metadata()
    assert len(full) == HEADER_LENGTH + len(_PLAINTEXT) + TAG_LENGTH
    n, r, p = struct.unpack_from(">III", full, OFFSET_N)
    assert (n, r, p) == (SCRYPT_N, SCRYPT_R, SCRYPT_P)


def test_the_same_plaintext_produces_different_salt_and_nonce() -> None:
    first = _encrypt()
    second = _encrypt()

    assert first != second
    assert first[OFFSET_SALT : OFFSET_SALT + SALT_LENGTH] != (
        second[OFFSET_SALT : OFFSET_SALT + SALT_LENGTH]
    )
    assert first[OFFSET_NONCE : OFFSET_NONCE + NONCE_LENGTH] != (
        second[OFFSET_NONCE : OFFSET_NONCE + NONCE_LENGTH]
    )
    assert decrypt(first, PASSPHRASE).plaintext == _PLAINTEXT
    assert decrypt(second, PASSPHRASE).plaintext == _PLAINTEXT


def test_plaintext_markers_do_not_appear_in_the_envelope() -> None:
    envelope = _encrypt()

    assert SYNTHETIC_SECRET.encode("ascii") not in envelope
    assert b"Groceries" not in envelope
    assert b"14.20" not in envelope
    assert PASSPHRASE.encode("ascii") not in envelope
    assert b"2026-10-21T15:04:05Z" in envelope


def test_unicode_passphrase_round_trips() -> None:
    passphrase = "пароль-café"
    envelope = _encrypt(passphrase=passphrase)

    assert passphrase.encode("utf-8") not in envelope
    assert decrypt(envelope, passphrase).plaintext == _PLAINTEXT


def test_passphrase_is_not_trimmed() -> None:
    envelope = _encrypt(passphrase=PASSPHRASE + " ")
    wrong = PASSPHRASE
    with pytest.raises(CryptoError, match="Decryption failed") as exc_info:
        decrypt(envelope, wrong)
    _assert_safe(exc_info.value)


def test_wrong_passphrase_fails() -> None:
    envelope = _encrypt()
    wrong = OTHER_PASSPHRASE
    with pytest.raises(CryptoError, match="Decryption failed") as exc_info:
        decrypt(envelope, wrong)
    _assert_safe(exc_info.value)


@pytest.mark.parametrize("position", ["first", "last"])
def test_changed_ciphertext_byte_fails(position: str) -> None:
    envelope = bytearray(_encrypt())
    index = HEADER_LENGTH if position == "first" else len(envelope) - 1
    envelope[index] ^= 0x01
    payload = bytes(envelope)
    with pytest.raises(CryptoError, match="Decryption failed") as exc_info:
        decrypt(payload, PASSPHRASE)
    _assert_safe(exc_info.value)


def test_changed_generation_fails_and_is_not_returned_as_verified() -> None:
    metadata = _metadata()
    envelope = bytearray(_encrypt(metadata=metadata))
    struct.pack_into(">I", envelope, OFFSET_GENERATION, metadata.generation + 1)
    payload = bytes(envelope)

    assert read_unverified_metadata(payload).generation == metadata.generation + 1
    with pytest.raises(CryptoError, match="Decryption failed") as exc_info:
        decrypt(payload, PASSPHRASE)
    _assert_safe(exc_info.value)


def test_changed_creation_time_fails() -> None:
    envelope = bytearray(_encrypt())
    # 2026-10-21T... -> 2026-10-22T..., still a well-formed timestamp.
    envelope[OFFSET_CREATED_AT + 9] = ord("2")
    payload = bytes(envelope)
    assert read_unverified_metadata(payload).created_at_utc == datetime(
        2026, 10, 22, 15, 4, 5, tzinfo=timezone.utc
    )
    with pytest.raises(CryptoError, match="Decryption failed") as exc_info:
        decrypt(payload, PASSPHRASE)
    _assert_safe(exc_info.value)


@pytest.mark.parametrize(
    "length",
    [0, 4, HEADER_LENGTH, HEADER_LENGTH + TAG_LENGTH - 1],
)
def test_truncated_envelope_fails(length: int) -> None:
    payload = _encrypt()[:length]
    with pytest.raises(CryptoError, match="malformed") as exc_info:
        decrypt(payload, PASSPHRASE)
    _assert_safe(exc_info.value)


def test_unsupported_version_fails_without_deriving(monkeypatch: pytest.MonkeyPatch) -> None:
    envelope = bytearray(_encrypt())
    envelope[OFFSET_VERSION] = 2
    payload = bytes(envelope)
    monkeypatch.setattr(crypto, "_derive_key", _derivation_forbidden)
    with pytest.raises(CryptoError, match="Unsupported envelope version") as exc_info:
        decrypt(payload, PASSPHRASE)
    _assert_safe(exc_info.value)


@pytest.mark.parametrize(
    ("offset", "value"),
    [(OFFSET_N, 2**17), (OFFSET_R, 9), (OFFSET_P, 2)],
)
def test_kdf_parameters_above_the_maximum_do_not_derive(
    monkeypatch: pytest.MonkeyPatch, offset: int, value: int
) -> None:
    envelope = bytearray(_encrypt())
    struct.pack_into(">I", envelope, offset, value)
    payload = bytes(envelope)
    monkeypatch.setattr(crypto, "_derive_key", _derivation_forbidden)
    with pytest.raises(CryptoError, match="not allowed") as exc_info:
        decrypt(payload, PASSPHRASE)
    _assert_safe(exc_info.value)


def test_scrypt_n_that_is_not_a_power_of_two_is_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = bytearray(_encrypt())
    struct.pack_into(">I", envelope, OFFSET_N, 3)
    payload = bytes(envelope)
    monkeypatch.setattr(crypto, "_derive_key", _derivation_forbidden)
    with pytest.raises(CryptoError, match="malformed") as exc_info:
        decrypt(payload, PASSPHRASE)
    _assert_safe(exc_info.value)


def test_weaker_legal_kdf_parameters_still_fail_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"count": 0}
    real = crypto._derive_key

    def counting(*args: object, **kwargs: object) -> bytes:
        calls["count"] += 1
        return real(*args, **kwargs)

    envelope = bytearray(_encrypt())
    struct.pack_into(">I", envelope, OFFSET_N, 2**14)
    payload = bytes(envelope)
    monkeypatch.setattr(crypto, "_derive_key", counting)
    with pytest.raises(CryptoError, match="Decryption failed") as exc_info:
        decrypt(payload, PASSPHRASE)
    assert calls["count"] == 1
    _assert_safe(exc_info.value)


def test_bad_magic_and_unknown_kdf_are_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _encrypt()
    monkeypatch.setattr(crypto, "_derive_key", _derivation_forbidden)
    bad_magic = b"XXXX" + original[4:]
    bad_kdf = bytearray(original)
    bad_kdf[OFFSET_KDF] = 9
    for payload in (bad_magic, bytes(bad_kdf), b""):
        with pytest.raises(CryptoError, match="malformed") as exc_info:
            decrypt(payload, PASSPHRASE)
        _assert_safe(exc_info.value)


def test_read_unverified_metadata_does_not_derive_or_check_the_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = _metadata()
    envelope = bytearray(_encrypt(metadata=metadata))
    envelope[-1] ^= 0x01
    payload = bytes(envelope)
    monkeypatch.setattr(crypto, "_derive_key", _derivation_forbidden)

    assert read_unverified_metadata(payload) == metadata


def test_encrypt_rejects_a_header_that_was_not_validated() -> None:
    with pytest.raises(TypeError, match="SnapshotMetadata"):
        encrypt(b"synthetic", PASSPHRASE, object())


def test_round_trip_stays_under_two_seconds() -> None:
    # Two derivations. Isolated, each one is about 120 ms; the limit leaves
    # room when the machine is already busy. A much larger scrypt N still fails.
    passphrase = PASSPHRASE
    metadata = _metadata()
    started = time.perf_counter()
    envelope = encrypt(b"synthetic", passphrase, metadata)
    decrypt(envelope, passphrase)
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, elapsed


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        (
            {"created_at_utc": datetime(2026, 10, 21, 15, 4, 5)},
            ValueError,
        ),
        (
            {
                "created_at_utc": datetime(
                    2026, 10, 21, 15, 4, 5, 1, tzinfo=timezone.utc
                )
            },
            ValueError,
        ),
        ({"generation": -1}, ValueError),
        ({"next_id": 2**32}, ValueError),
        ({"spending_count": True}, TypeError),
    ],
)
def test_metadata_rejects_values_the_header_cannot_store(
    kwargs: dict, error: type[Exception]
) -> None:
    with pytest.raises(error):
        _metadata(**kwargs)


def _derivation_forbidden(*args: object, **kwargs: object) -> bytes:
    raise AssertionError("key derivation was attempted")
