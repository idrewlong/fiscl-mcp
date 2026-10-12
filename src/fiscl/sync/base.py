"""Shared layer for every read-only bank connector (SimpleFIN, Teller, Plaid).

Four things live here so each connector stays small and none can bypass the rules:

* `open_url`: one opener that never follows redirects and always verifies TLS (optionally with a client
  certificate for mTLS).
* `Policy` + `request`: a per-connector allowlist of hosts and (method, path) pairs, checked before any socket
  opens. Anything else raises `SyncError`. This is what keeps a connector read-only in code, not just by intent.
* Keychain helpers: secrets live only in the OS keychain under `fiscl:<provider>:<ref>`; without a real keychain
  the connector refuses to run rather than fall back to plain text.
* `Connector`: the small protocol the sync orchestration (`sync/registry.py`) drives.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from ..importers.types import ParsedStatement

PROVIDERS = ("simplefin", "teller", "plaid")
KEYRING_SERVICE = "fiscl"
TIMEOUT = 30
MAX_RESPONSE_BYTES = 25 * 1024 * 1024


class SyncError(RuntimeError):
    """A connector problem whose message is safe to show (no tokens, names, or amounts)."""

    def __init__(self, message: str, status: int | None = None, body: bytes | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body  # provider error payload (capped); for connectors to read a code, never to display


# --- HTTP -----------------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: the allowlist is checked on the URL we build, and a redirect could
    point anywhere (including plain http) and would resend credentials."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def build_opener(context: ssl.SSLContext | None = None) -> urllib.request.OpenerDirector:
    """An opener with no redirects. TLS certificates and hostnames are verified by urllib's default HTTPS
    context, or by `context` (which must come from `client_context`, i.e. keep its default verification)."""
    handlers: list[Any] = [_NoRedirect()]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    return urllib.request.build_opener(*handlers)


_OPENER = build_opener()

Opener = Callable[..., Any]
SSLContext = ssl.SSLContext  # re-exported so no other module needs to import ssl


def open_url(req: urllib.request.Request, timeout: float):
    return _OPENER.open(req, timeout=timeout)


def opener_for(context: ssl.SSLContext | None) -> Opener:
    """Return an `open_url`-shaped callable bound to an mTLS context (or the default opener)."""
    if context is None:
        return open_url
    custom = build_opener(context)

    def _open(req: urllib.request.Request, timeout: float):
        return custom.open(req, timeout=timeout)

    return _open


@dataclass(frozen=True)
class Policy:
    """What a connector may call. `routes` are (METHOD, path regex) pairs matched against the whole path."""

    label: str
    hosts: tuple[str, ...]
    routes: tuple[tuple[str, str], ...]
    allow_userinfo: bool = False  # SimpleFIN access URLs embed credentials; nothing else may

    def check(self, method: str, url: str) -> urllib.parse.SplitResult:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https":
            raise SyncError(f"{self.label} URLs must use https")
        host = (parts.hostname or "").lower()
        if host not in self.hosts:
            raise SyncError(f"host {host!r} is not in the {self.label} allowlist {list(self.hosts)}")
        try:
            port = parts.port
        except ValueError as exc:
            raise SyncError(f"{self.label} URL has an invalid port") from exc
        if port not in (None, 443):
            raise SyncError(f"{self.label} requests must use the standard https port")
        if (parts.username or parts.password) and not self.allow_userinfo:
            raise SyncError(f"{self.label} URLs must not carry credentials")
        if not any(m == method.upper() and re.fullmatch(rx, parts.path) for m, rx in self.routes):
            raise SyncError(f"{self.label} request {method.upper()} on that path is not allowed (read-only connector)")
        return parts


def _error_body(exc: urllib.error.HTTPError) -> bytes | None:
    try:
        return exc.read(65536)
    except Exception:
        return None


def request(
    policy: Policy,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    secret_headers: dict[str, str] | None = None,
    data: bytes | None = None,
    opener: Opener | None = None,
    timeout: float = TIMEOUT,
) -> tuple[int, bytes]:
    """Send one allowlisted request. The policy is enforced before the opener (and so any socket) is touched.

    `secret_headers` are added as *unredirected* headers, so they can never ride along on a redirect.
    """
    policy.check(method, url)
    req = urllib.request.Request(url, method=method.upper(), data=data, headers=headers or {})
    for name, value in (secret_headers or {}).items():
        req.add_unredirected_header(name, value)
    opener = opener or open_url
    try:
        with opener(req, timeout=timeout) as resp:
            body = resp.read(MAX_RESPONSE_BYTES + 1)
            status = int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise SyncError(
                f"{policy.label} answered with a redirect (HTTP {exc.code}); fiscl doesn't follow redirects",
                exc.code,
            ) from exc
        raise SyncError(f"{policy.label} returned HTTP {exc.code}", exc.code, _error_body(exc)) from exc
    except urllib.error.URLError as exc:
        raise SyncError(f"couldn't reach {policy.label}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise SyncError(f"{policy.label} timed out") from exc
    if len(body) > MAX_RESPONSE_BYTES:
        raise SyncError(f"{policy.label} sent an unexpectedly large response")
    return status, body


# --- mTLS client certificates -------------------------------------------------------------------


def client_context(cert_pem: str, key_pem: str) -> ssl.SSLContext:
    """Build a verifying SSLContext that presents a client certificate loaded from keychain material.

    Python's ssl module only loads certificates from file paths. On Linux the PEM goes into an anonymous
    in-memory file (`memfd_create`). Elsewhere it is written 0600 into a fresh 0700 `mkdtemp` directory,
    loaded, and unlinked in `finally` (it exists for milliseconds, is owner-only, and is never in the data dir).
    """
    ctx = ssl.create_default_context()  # keeps hostname checking and CA verification on
    pem = (cert_pem.strip() + "\n" + key_pem.strip() + "\n").encode()
    if hasattr(os, "memfd_create"):
        try:
            fd = os.memfd_create("fiscl-mtls", os.MFD_CLOEXEC)
        except OSError:
            fd = -1
        if fd >= 0:
            try:
                os.write(fd, pem)
                ctx.load_cert_chain(f"/proc/self/fd/{fd}")
            except ssl.SSLError as exc:
                raise SyncError("the stored client certificate and key could not be loaded") from exc
            finally:
                os.close(fd)
            return ctx
    tmpdir = tempfile.mkdtemp(prefix="fiscl-mtls-")  # 0700
    path = os.path.join(tmpdir, "client.pem")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(pem)
        try:
            ctx.load_cert_chain(path)
        except ssl.SSLError as exc:
            raise SyncError("the stored client certificate and key could not be loaded") from exc
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)
        shutil.rmtree(tmpdir, ignore_errors=True)
    return ctx


# --- keychain ----------------------------------------------------------------------------------

_REF = re.compile(r"[A-Za-z0-9_.\-]{1,64}")


def secret_name(provider: str, ref: str) -> str:
    """The keychain username for a secret: `fiscl:<provider>:<ref>`."""
    if provider not in PROVIDERS or not _REF.fullmatch(ref):
        raise SyncError("invalid keychain reference")
    return f"fiscl:{provider}:{ref}"


# Only these backends are real OS credential stores. Everything else (keyrings.alt's plaintext/encrypted-file
# backends, the null and fail backends, anything chosen through PYTHON_KEYRING_BACKEND or keyringrc.cfg) is refused:
# a file-backed or silently-dropping store is exactly what fiscl promises never to use for bank credentials.
OS_KEYCHAIN_BACKENDS = frozenset(
    {
        "keyring.backends.macOS.Keyring",
        "keyring.backends.Windows.WinVaultKeyring",
        "keyring.backends.SecretService.Keyring",
        "keyring.backends.libsecret.Keyring",
        "keyring.backends.kwallet.DBusKeyring",
        "keyring.backends.kwallet.DBusKeyringKWallet4",
    }
)
_CHAINER = "keyring.backends.chainer.ChainerBackend"
NO_KEYCHAIN = "no OS keychain is available on this machine; fiscl won't store bank credentials in plain text"


def _backend_path(backend: object) -> str:
    cls = type(backend)
    return f"{cls.__module__}.{cls.__qualname__}"


def is_os_keychain(backend: object) -> bool:
    """True only for an allowlisted OS backend, or a chainer whose every member is one (exact class match, so a
    subclass or look-alike elsewhere doesn't count)."""
    if _backend_path(backend) == _CHAINER:
        members = list(getattr(backend, "backends", None) or [])
        return bool(members) and all(is_os_keychain(m) for m in members)
    return _backend_path(backend) in OS_KEYCHAIN_BACKENDS


def keyring_backend():
    """The `keyring` module, but only when a real OS keychain backs it."""
    try:
        import keyring  # optional dependency: pip install "fiscl[sync]"
    except ImportError as exc:
        raise SyncError('bank sync needs the keyring package: pip install "fiscl[sync]"') from exc
    if not is_os_keychain(keyring.get_keyring()):
        raise SyncError(NO_KEYCHAIN)
    return keyring


def secret_set(name: str, value: str) -> None:
    keyring_backend().set_password(KEYRING_SERVICE, name, value)


def secret_get(name: str) -> str | None:
    return keyring_backend().get_password(KEYRING_SERVICE, name)


def secret_delete(name: str) -> None:
    """Delete a keychain entry. "Not found" is fine (idempotent); any other keychain error is surfaced, because
    swallowing it would report a credential as removed while it is still stored."""
    kr = keyring_backend()
    try:
        kr.delete_password(KEYRING_SERVICE, name)
    except Exception as exc:
        if type(exc).__name__ == "PasswordDeleteError":  # keyring.errors.PasswordDeleteError: no such entry
            return
        raise SyncError("couldn't delete a stored credential from the keychain; nothing was disconnected") from exc


# --- protocol ---------------------------------------------------------------------------------


class Connector(Protocol):
    """What the sync orchestration needs from a provider.

    `fetch` returns statements for [start, end] (inclusive) and must not write anything locally. Problems that
    shouldn't abort the whole sync are appended to `warnings` as fixed prose without names or amounts. `forget`
    revokes access at the provider where possible and deletes the keychain entries. Setup is human-only and
    lives in the CLI (`fiscl connect`), never here.
    """

    provider: str
    warnings: list[str]

    def fetch(self, start: date, end: date) -> list[ParsedStatement]: ...

    def forget(self) -> None: ...
