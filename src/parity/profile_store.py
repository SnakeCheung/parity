"""Encrypted local storage for browser Profile archives.

The archive is encrypted before it is written to persistent local storage.
The encryption key is supplied separately through ``PARITY_PROFILE_KEY``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


PROFILE_KEY_ENV = "PARITY_PROFILE_KEY"
DEFAULT_PROFILE_ARCHIVE = Path("artifacts/profiles/jd-default.tar.gz.fernet")


class ProfileKeyError(RuntimeError):
    """Raised when the Profile encryption key is missing or invalid."""


def _fernet():
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise ProfileKeyError(
            "cryptography is not installed. Run: pip install -e './parity[test]'"
        ) from exc

    raw_key = os.getenv(PROFILE_KEY_ENV, "").strip()
    if not raw_key:
        raise ProfileKeyError(
            f"{PROFILE_KEY_ENV} is missing. Run: python -m parity.profile_store init-key"
        )
    try:
        return Fernet(raw_key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ProfileKeyError(f"{PROFILE_KEY_ENV} is invalid") from exc


def encrypt_archive(data: bytes, destination: str | Path) -> Path:
    """Encrypt one compressed Profile archive and atomically replace its destination."""

    destination_path = Path(destination).resolve()
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = destination_path.with_suffix(destination_path.suffix + ".tmp")
    temporary_path.write_bytes(_fernet().encrypt(data))
    temporary_path.replace(destination_path)
    return destination_path


def decrypt_archive(source: str | Path) -> bytes:
    """Decrypt a Profile archive without writing a plaintext copy to disk."""

    source_path = Path(source).resolve()
    try:
        return _fernet().decrypt(source_path.read_bytes())
    except Exception as exc:
        if isinstance(exc, ProfileKeyError):
            raise
        raise ProfileKeyError(
            "The encrypted Profile could not be opened with PARITY_PROFILE_KEY"
        ) from exc


def ensure_profile_key(env_file: str | Path = ".env") -> bool:
    """Create a Profile key in a local .env file if one is not already configured."""

    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise ProfileKeyError("cryptography is not installed") from exc

    if os.getenv(PROFILE_KEY_ENV):
        _fernet()
        return False

    env_path = Path(env_file).resolve()
    existing = env_path.read_text(encoding="utf-8") if env_path.is_file() else ""
    for line in existing.splitlines():
        if line.strip().startswith(f"{PROFILE_KEY_ENV}="):
            value = line.split("=", 1)[1].strip()
            os.environ[PROFILE_KEY_ENV] = value
            _fernet()
            return False

    key = Fernet.generate_key().decode("ascii")
    separator = "" if not existing or existing.endswith(("\n", "\r")) else "\n"
    env_path.write_text(
        f"{existing}{separator}{PROFILE_KEY_ENV}={key}\n", encoding="utf-8"
    )
    os.environ[PROFILE_KEY_ENV] = key
    return True


def _main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Manage Parity's encrypted Profile key")
    subparsers = parser.add_subparsers(dest="operation", required=True)
    init_key = subparsers.add_parser("init-key")
    init_key.add_argument("--env-file", default=".env")
    args = parser.parse_args()

    if args.operation == "init-key":
        created = ensure_profile_key(args.env_file)
        print(json.dumps({"status": "ok", "key_created": created}, ensure_ascii=False))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
