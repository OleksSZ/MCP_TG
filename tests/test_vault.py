import os
import stat

import pytest

from tg_mcp import vault

KEY = bytes(range(32))
SECRETS = vault.Secrets(api_id=123, api_hash="hash-value", session="session-value")


def test_round_trip_with_private_permissions_and_no_plaintext(tmp_path):
    path = tmp_path / "vault.bin"
    vault.save(path, KEY, SECRETS)

    assert vault.load(path, KEY) == SECRETS
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    raw = path.read_bytes()
    assert b"session-value" not in raw and b"hash-value" not in raw


def test_each_save_uses_a_new_nonce(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    vault.save(first, KEY, SECRETS)
    vault.save(second, KEY, SECRETS)
    assert first.read_bytes() != second.read_bytes()


def test_wrong_key_is_rejected(tmp_path):
    path = tmp_path / "vault.bin"
    vault.save(path, KEY, SECRETS)
    with pytest.raises(vault.VaultError):
        vault.load(path, bytes(32))


def test_tampering_is_detected(tmp_path):
    path = tmp_path / "vault.bin"
    vault.save(path, KEY, SECRETS)
    raw = bytearray(path.read_bytes())
    raw[-1] ^= 1
    path.write_bytes(raw)
    with pytest.raises(vault.VaultError):
        vault.load(path, KEY)


def test_save_refuses_a_planted_symlink_at_the_temporary_path(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("original")
    path = tmp_path / "vault.bin"
    os.symlink(victim, path.with_name("vault.bin.tmp"))

    vault.save(path, KEY, SECRETS)

    assert victim.read_text() == "original"
    assert vault.load(path, KEY) == SECRETS


def test_secrets_do_not_appear_in_repr():
    assert "session-value" not in repr(SECRETS)
    assert "hash-value" not in repr(SECRETS)


def test_key_must_come_from_the_credentials_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    with pytest.raises(vault.VaultError):
        vault.load_key()

    (tmp_path / vault.CREDENTIAL_NAME).write_bytes(b"short")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    with pytest.raises(vault.VaultError):
        vault.load_key()

    (tmp_path / vault.CREDENTIAL_NAME).write_bytes(KEY)
    assert vault.load_key() == KEY
