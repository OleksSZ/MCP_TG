"""AES-256-GCM storage for the Telegram session and API credentials.

The key is never stored next to the data. In production systemd decrypts it from
a TPM-bound credential and exposes it only to this service.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .storage import write_private_file

KEY_SIZE = 32
NONCE_SIZE = 12
MAGIC = b"TGMCPV1\0"
CREDENTIAL_NAME = "vault-key"


class VaultError(Exception):
    """The vault or its key is missing, corrupted or tampered with."""


@dataclass(frozen=True, repr=False)
class Secrets:
    api_id: int
    api_hash: str
    session: str
    # Telegram update position, so events missed while stopped are caught up.
    updates: dict[str, Any] | None = None


def load_key() -> bytes:
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not directory:
        raise VaultError("CREDENTIALS_DIRECTORY is not set; start through the systemd launcher")
    try:
        key = (Path(directory) / CREDENTIAL_NAME).read_bytes()
    except OSError as exc:
        raise VaultError("Vault key credential is unavailable") from exc
    if len(key) != KEY_SIZE:
        raise VaultError("Vault key has an invalid length")
    return key


def save(path: Path, key: bytes, secrets: Secrets) -> None:
    plaintext = json.dumps(
        {
            "api_id": secrets.api_id,
            "api_hash": secrets.api_hash,
            "session": secrets.session,
            "updates": secrets.updates,
        }
    ).encode()
    nonce = os.urandom(NONCE_SIZE)
    write_private_file(path, MAGIC + nonce + AESGCM(key).encrypt(nonce, plaintext, MAGIC))


def load(path: Path, key: bytes) -> Secrets:
    try:
        blob = path.read_bytes()
    except OSError as exc:
        raise VaultError("Vault is unavailable; run the login step first") from exc

    header = len(MAGIC) + NONCE_SIZE
    if len(blob) <= header or not blob.startswith(MAGIC):
        raise VaultError("Vault has an invalid format")
    try:
        plaintext = AESGCM(key).decrypt(blob[len(MAGIC) : header], blob[header:], MAGIC)
    except InvalidTag as exc:
        raise VaultError("Vault failed authentication: wrong key or tampered file") from exc

    data = json.loads(plaintext)
    return Secrets(
        api_id=data["api_id"],
        api_hash=data["api_hash"],
        session=data["session"],
        updates=data.get("updates"),
    )
