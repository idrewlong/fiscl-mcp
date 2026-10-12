"""Localhost streamable-HTTP transport: loopback-only bind, bearer token, strict Host/Origin gate.

This is the one module in the MCP server that opens a *listening* socket (via uvicorn). Every control below maps
to a row of the threat table in specs/2026-10-11-2224-v2-phase5-transport-digest.md and has a test in
tests/test_http_transport.py.

- Bind only the literal `127.0.0.1` or `::1`; anything else is refused before a socket exists.
- Every request needs `Authorization: Bearer <token>`; the token lives only in the OS keychain.
- Any `Origin` header is rejected (browsers always send one cross-origin; native MCP clients do not). No CORS
  headers are ever produced, because this module never writes a header it was not explicitly given below.
- `Host` must equal `127.0.0.1:<port>` (or `[::1]:<port>`) exactly: no wildcard ports.
- The gate is plain ASGI middleware in front of the SDK app. The SDK's OAuth `AuthSettings` is deliberately unused
  (it models an OAuth resource server); the SDK's `TransportSecuritySettings` is a second Host/Origin check.
- Nothing here logs the token, the Authorization header, request bodies, or URLs.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import secrets
import socket
import sys
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
import uvicorn
from mcp.server.transport_security import TransportSecuritySettings

from .sync import base

log = logging.getLogger("fiscl.http")

LOOPBACK_HOSTS = ("127.0.0.1", "::1")
DEFAULT_PORT = 8787
MCP_PATH = "/mcp"
TOKEN_NAME = "fiscl:http:token"  # keychain username under the shared fiscl service

MAX_BODY_BYTES = 1024 * 1024
MAX_SESSIONS = 8
SESSION_IDLE_SECONDS = 30 * 60
MAX_CONCURRENCY = 16
AUTH_MAX_FAILURES = 20
AUTH_WINDOW_SECONDS = 60.0
AUTH_LOCKOUT_SECONDS = 60.0

ALLOWED_METHODS = ("POST", "GET", "DELETE")


class HttpConfigError(ValueError):
    """A startup refusal with a message that is safe to print."""


# --- bind validation ---------------------------------------------------------------------------


def validate_bind(host: str, port: int, *, allow_ephemeral: bool = False) -> None:
    """Refuse anything but the two literal loopback strings, before any socket is created."""
    if not isinstance(host, str) or host not in LOOPBACK_HOSTS:
        raise HttpConfigError("--host must be exactly 127.0.0.1 or ::1 (fiscl never listens on any other address)")
    low = 0 if allow_ephemeral else 1024
    if isinstance(port, bool) or not isinstance(port, int) or not low <= port <= 65535:
        raise HttpConfigError(f"port must be between {low or 1024} and 65535")


def is_loopback_address(addr: object) -> bool:
    if not isinstance(addr, str):
        return False
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def bind_loopback(host: str, port: int, *, allow_ephemeral: bool = False) -> socket.socket:
    """Bind (not listen) a TCP socket on a loopback literal. Never retries another port."""
    validate_bind(host, port, allow_ephemeral=allow_ephemeral)
    family = socket.AF_INET6 if host == "::1" else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if sys.platform != "win32":  # on Windows SO_REUSEADDR would let another process share the port
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.bind((host, port))
        if not is_loopback_address(sock.getsockname()[0]):  # defense in depth; cannot happen with the check above
            raise HttpConfigError("refusing to serve: the socket is not bound to a loopback address")
    except OSError as exc:
        sock.close()
        raise HttpConfigError(
            f"couldn't listen on {host} port {port} ({exc.strerror or 'bind failed'}); "
            "pick another with --port or [http] port. fiscl never tries a different port on its own."
        ) from exc
    except HttpConfigError:
        sock.close()
        raise
    return sock


def allowed_hosts(host: str, port: int) -> tuple[str, ...]:
    """Exact Host header values, port included."""
    if host == "::1":
        return (f"[::1]:{port}",)
    return (f"127.0.0.1:{port}", f"localhost:{port}")


# --- token -------------------------------------------------------------------------------------


def digests_match(presented: str, expected: str) -> bool:
    """Constant-time compare over SHA-256 digests, so neither length nor content of the token leaks."""
    a = hashlib.sha256(presented.encode("utf-8", "surrogatepass")).digest()
    b = hashlib.sha256(expected.encode("utf-8", "surrogatepass")).digest()
    return hmac.compare_digest(a, b)


class TokenStore:
    """The bearer token, kept only in the OS keychain (`fiscl:http:token`)."""

    @staticmethod
    def _kr():
        try:
            return base.keyring_backend()
        except base.SyncError as exc:
            raise HttpConfigError(
                f"{exc}. The HTTP transport keeps its bearer token only in an OS keychain "
                'and refuses to start without one (pip install "fiscl[sync]" on a machine with a keychain).'
            ) from exc

    def get(self) -> str | None:
        return self._kr().get_password(base.KEYRING_SERVICE, TOKEN_NAME) or None

    def ensure(self) -> tuple[str, bool]:
        """The stored token, creating one on first use. Returns (token, created)."""
        existing = self.get()
        if existing:
            return existing, False
        return self.rotate(), True

    def rotate(self) -> str:
        token = secrets.token_urlsafe(32)
        self._kr().set_password(base.KEYRING_SERVICE, TOKEN_NAME, token)
        return token

    def forget(self) -> None:
        base.secret_delete(TOKEN_NAME)


# --- rate limit --------------------------------------------------------------------------------


class AuthLimiter:
    """In-memory backoff: after `max_failures` failed auths inside `window`, refuse everything for `lockout`."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        *,
        max_failures: int = AUTH_MAX_FAILURES,
        window: float = AUTH_WINDOW_SECONDS,
        lockout: float = AUTH_LOCKOUT_SECONDS,
    ) -> None:
        self._clock = clock
        self._max = max_failures
        self._window = window
        self._lockout = lockout
        self._failures: deque[float] = deque()
        self._locked_until = 0.0

    def locked(self) -> bool:
        return self._clock() < self._locked_until

    def retry_after(self) -> int:
        return max(1, int(self._locked_until - self._clock() + 0.999))

    def record_failure(self) -> None:
        now = self._clock()
        while self._failures and now - self._failures[0] > self._window:
            self._failures.popleft()
        self._failures.append(now)
        if len(self._failures) >= self._max:
            self._locked_until = now + self._lockout
            log.warning("http auth locked for %ds after %d failed attempts", self._lockout, len(self._failures))
            self._failures.clear()


# --- the gate ----------------------------------------------------------------------------------

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


async def _reply(send: Send, status: int, body: bytes = b"", headers: tuple[tuple[bytes, bytes], ...] = ()) -> None:
    # Only these headers are ever sent by the gate: in particular never any Access-Control-* header.
    out = [(b"content-length", str(len(body)).encode()), (b"content-type", b"text/plain; charset=utf-8"), *headers]
    await send({"type": "http.response.start", "status": status, "headers": out})
    await send({"type": "http.response.body", "body": body})


def _bearer(values: list[str]) -> str | None:
    if len(values) != 1:
        return None
    scheme, _, rest = values[0].partition(" ")
    if scheme.lower() != "bearer":
        return None
    return rest.strip(" ") or None


class GateMiddleware:
    """Outermost ASGI app. Order: client address, Host, Origin, path/method, rate limit, bearer, content type."""

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        token_source: Callable[[], str | None],
        hosts: tuple[str, ...],
        limiter: AuthLimiter | None = None,
    ) -> None:
        self.app = app
        self._token_source = token_source
        self._hosts = frozenset(hosts)
        self._limiter = limiter or AuthLimiter()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind == "lifespan":
            await self.app(scope, receive, send)
            return
        if kind != "http":
            await send({"type": "websocket.close", "code": 1008})
            return

        client = scope.get("client")
        if not client or not is_loopback_address(client[0]):
            await _reply(send, 403)
            return

        host_values: list[str] = []
        auth_values: list[str] = []
        content_types: list[str] = []
        has_origin = False
        for k, v in scope.get("headers", []):
            if k == b"host":
                host_values.append(v.decode("latin-1"))
            elif k == b"origin":
                has_origin = True
            elif k == b"authorization":
                auth_values.append(v.decode("latin-1"))
            elif k == b"content-type":
                content_types.append(v.decode("latin-1"))

        if len(host_values) != 1 or host_values[0] not in self._hosts:
            await _reply(send, 421, b"misdirected request")
            return
        if has_origin:
            await _reply(send, 403)
            return
        if scope.get("path") != MCP_PATH:
            await _reply(send, 404)
            return
        method = scope.get("method", "")
        if method not in ALLOWED_METHODS:
            await _reply(send, 405, headers=((b"allow", ", ".join(ALLOWED_METHODS).encode()),))
            return

        if self._limiter.locked():
            await _reply(send, 429, headers=((b"retry-after", str(self._limiter.retry_after()).encode()),))
            return
        try:
            expected = await anyio.to_thread.run_sync(self._token_source)
        except Exception:
            log.error("http token lookup failed")
            await _reply(send, 503, b"token unavailable")
            return
        if not expected:
            log.error("http token is missing from the keychain")
            await _reply(send, 503, b"token unavailable")
            return
        presented = _bearer(auth_values)
        if presented is None or not digests_match(presented, expected):
            self._limiter.record_failure()
            await _reply(send, 401, headers=((b"www-authenticate", b"Bearer"),))
            return

        if method == "POST" and not (
            len(content_types) == 1 and content_types[0].split(";", 1)[0].strip().lower() == "application/json"
        ):
            await _reply(send, 415)
            return

        await self.app(scope, receive, send)


def build_app(
    mcp,
    token_source: Callable[[], str | None],
    host: str,
    port: int,
    *,
    limiter: AuthLimiter | None = None,
) -> GateMiddleware:
    """The SDK's streamable-HTTP app behind the gate. Stateful sessions (notifications need them)."""
    hosts = allowed_hosts(host, port)
    inner = mcp.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=False,
        max_request_body_size=MAX_BODY_BYTES,
        session_idle_timeout=SESSION_IDLE_SECONDS,
        max_sessions=MAX_SESSIONS,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=list(hosts), allowed_origins=[]
        ),
        host=host,
    )
    gate = GateMiddleware(inner, token_source=token_source, hosts=hosts, limiter=limiter)
    gate.inner = inner  # type: ignore[attr-defined]  # tests drive the SDK lifespan through this
    return gate


def make_server(app: GateMiddleware) -> uvicorn.Server:
    """A uvicorn server that logs nothing about requests and trusts no forwarded headers."""
    # sse_starlette logs every response chunk (tool results, i.e. financial data) at DEBUG, which `fiscl -v` enables.
    for noisy in ("sse_starlette", "uvicorn", "uvicorn.access", "uvicorn.error", "httpx2", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    config = uvicorn.Config(
        app,
        log_config=None,  # fiscl's own file logging (root logger) applies; nothing goes to stdout
        log_level="warning",
        access_log=False,
        server_header=False,
        date_header=False,
        proxy_headers=False,
        limit_concurrency=MAX_CONCURRENCY,
        ws="none",
        lifespan="on",
    )
    return uvicorn.Server(config)


def serve(mcp, sock: socket.socket, token_source: Callable[[], str | None]) -> None:
    """Block serving on an already-bound loopback socket until interrupted."""
    host, port = sock.getsockname()[:2]
    if not is_loopback_address(host):
        sock.close()
        raise HttpConfigError("refusing to serve: the socket is not bound to a loopback address")
    server = make_server(build_app(mcp, token_source, host, port))
    try:
        anyio.run(lambda: server.serve(sockets=[sock]))
    finally:
        sock.close()
