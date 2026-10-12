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


def _existing(cfg: Config, kr: Any | None) -> bytes | None:
    """The key already minted on this machine, if any (never creates one)."""
    if kr is not None:
        stored = kr.get_password("fiscl", KEYCHAIN_NAME)
        if stored:
            return _decode(stored)
    path = cfg.home / KEY_FILE
    if path.exists():  # a key already minted on this machine wins, so hashes stay stable if the keychain changes
        return _read_file(path)
    return None


def _load_or_create(cfg: Config) -> bytes:
    path = cfg.home / KEY_FILE
    kr = _keychain()
    found = _existing(cfg, kr)
    if found is not None:
        return found
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


# --- portability: the key must travel with the database (new machine, restored backup) or accounts split ----------


def fingerprint(key: bytes) -> str:
    """A short non-secret identifier of the key. 64 bits of a sha256 of a 256-bit random key reveals nothing usable."""
    return hashlib.sha256(key).hexdigest()[:16]


def stored_fingerprint(conn) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = 'install_key_fingerprint'").fetchone()
    return row[0] if row else None


def record_fingerprint(conn, cfg: Config) -> None:
    """Remember which key this database's account hashes were made with (first use wins; never overwritten)."""
    conn.execute(
        "INSERT OR IGNORE INTO meta (key, value) VALUES ('install_key_fingerprint', ?)",
        (fingerprint(install_key(cfg)),),
    )


def key_status(conn, cfg: Config) -> str:
    """ok | mismatch | unverified (hashed accounts exist but no fingerprint was recorded) | no_accounts."""
    hashed = conn.execute("SELECT 1 FROM accounts WHERE acct_hash IS NOT NULL LIMIT 1").fetchone()
    if not hashed:
        return "no_accounts"
    recorded = stored_fingerprint(conn)
    if recorded is None:
        return "unverified"
    return "ok" if recorded == fingerprint(install_key(cfg)) else "mismatch"


def has_hashed_accounts(conn) -> bool:
    return bool(conn.execute("SELECT 1 FROM accounts WHERE acct_hash IS NOT NULL LIMIT 1").fetchone())


def _write_private(path, key: bytes, *, replace: bool) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp if replace else path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(key.hex() + "\n")
    if replace:
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, path)


def export_key(cfg: Config, dest) -> None:
    """Write the install key to a new 0600 file. Never prints it; refuses to overwrite."""
    from .guard import GuardError

    try:
        _write_private(dest, install_key(cfg), replace=False)
    except FileExistsError as exc:
        raise GuardError("that file already exists; choose a new path") from exc


def import_key(cfg: Config, src, *, force: bool = False, hashed_accounts: bool = False) -> None:
    """Adopt an exported key. Replacing a different key while accounts are hashed with it would orphan them, so that
    needs force."""
    from .guard import GuardError

    try:
        key = _decode(src.read_text(encoding="ascii"))
    except (OSError, ValueError) as exc:
        raise GuardError("that file is not a fiscl install key export") from exc
    kr = _keychain()
    current = _existing(cfg, kr)
    if current is not None and current != key and hashed_accounts and not force:
        raise GuardError(
            "this install already has a different key and accounts hashed with it; importing would split those "
            "accounts. Pass --force only if you are restoring a database that was made with the imported key."
        )
    path = cfg.home / KEY_FILE
    if kr is not None:
        kr.set_password("fiscl", KEYCHAIN_NAME, key.hex())
    if kr is None or path.exists():
        _write_private(path, key, replace=True)
    reset_cache()
