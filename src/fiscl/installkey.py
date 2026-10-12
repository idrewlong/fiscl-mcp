"""Per-install secret key used to hash account identifiers.

An unsalted sha256 of "BANKID:ACCTID" is brute-forceable (account numbers are short and structured), so account
hashes are HMAC-SHA256 under a random 32-byte key that never leaves this machine. The key lives in the OS
keychain when an allowlisted backend is available, else in a 0600 file in the data directory that is created
atomically (O_EXCL) and never overwritten. Backups are database-only, so the key is never copied into them.

The key is never logged or returned from any tool.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import Any

from .config import FILE_MODE, Config

KEY_FILE = ".install_key"
KEYCHAIN_NAME = "fiscl:install-key"
_CACHE: dict[str, bytes] = {}


def _keychain() -> Any | None:
    """The keyring module when an allowlisted OS keychain exists, else None (keyring stays optional)."""
    from .sync import base

    try:
        return base.keyring_backend()
    except base.SyncError:
        return None


def _decode(text: str) -> bytes:
    key = bytes.fromhex(text.strip())
    if len(key) != 32:
        raise ValueError("install key has the wrong length")
    return key


def _read_file(path) -> bytes:
    return _decode(path.read_text(encoding="ascii"))


def _load_or_create(cfg: Config) -> bytes:
    path = cfg.home / KEY_FILE
    kr = _keychain()
    if kr is not None:
        stored = kr.get_password("fiscl", KEYCHAIN_NAME)
        if stored:
            return _decode(stored)
    if path.exists():  # a key already minted on this machine wins, so hashes stay stable if the keychain changes
        return _read_file(path)
    key = secrets.token_bytes(32)
    if kr is not None:
        kr.set_password("fiscl", KEYCHAIN_NAME, key.hex())
        return key
    cfg.home.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    except FileExistsError:  # lost a race with another process: use theirs
        return _read_file(path)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(key.hex())
    os.chmod(path, FILE_MODE)
    return key


def install_key(cfg: Config) -> bytes:
    cache_id = str(cfg.home)
    if cache_id not in _CACHE:
        _CACHE[cache_id] = _load_or_create(cfg)
    return _CACHE[cache_id]


def reset_cache() -> None:
    _CACHE.clear()


def account_hash(cfg: Config, key: str) -> str:
    """Keyed hash of an institution account identifier (what accounts.acct_hash stores)."""
    return hmac.new(install_key(cfg), key.encode(), hashlib.sha256).hexdigest()


def legacy_account_hash(key: str) -> str:
    """The unkeyed hash older versions stored. Only used to find and upgrade existing accounts."""
    return hashlib.sha256(key.encode()).hexdigest()
