from __future__ import annotations

import os
from tempfile import TemporaryDirectory
from unittest.mock import patch

from cryptography.fernet import Fernet

from parity.profile_store import decrypt_archive, encrypt_archive, ensure_profile_key


def test_encrypted_profile_round_trip_does_not_store_plaintext():
    key = Fernet.generate_key().decode("ascii")
    plaintext = b"browser-profile-secret"

    with TemporaryDirectory() as temp_dir, patch.dict(
        os.environ, {"PARITY_PROFILE_KEY": key}, clear=False
    ):
        encrypted_path = encrypt_archive(plaintext, f"{temp_dir}/profile.fernet")
        assert plaintext not in encrypted_path.read_bytes()
        assert decrypt_archive(encrypted_path) == plaintext


def test_init_key_writes_key_without_returning_it():
    with TemporaryDirectory() as temp_dir, patch.dict(os.environ, {}, clear=True):
        env_path = f"{temp_dir}/.env"
        created = ensure_profile_key(env_path)

        assert created is True
        assert "PARITY_PROFILE_KEY=" in open(env_path, encoding="utf-8").read()
