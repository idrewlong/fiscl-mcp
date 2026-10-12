"""Reserve the *other* loopback family on a port, so a local program can't squat it.

`localhost` may resolve to ::1 as well as 127.0.0.1, and the two families have separate port spaces. A server bound
to one literal address would leave the other free for any local process to bind, and a client configured with
`localhost` could then hand it a bearer token or an authorization code. For the server's lifetime we bind (never
listen on) the other family on the same port: connections to a bound, non-listening socket are refused and the client
falls back. No SO_REUSEADDR/SO_REUSEPORT: the reservation must fail if anything else holds the port.

This module only binds; it never listens, accepts or connects. It is a named exception in the network-import guard
(tests/test_phase2_fixes.py).
"""

from __future__ import annotations

import errno
import socket

OTHER_FAMILY = {"127.0.0.1": ("::1", socket.AF_INET6), "::1": ("127.0.0.1", socket.AF_INET)}


class LoopbackBusy(OSError):
    """The other loopback family is already held by another program on this port."""


def reserve_other_family(host: str, port: int) -> socket.socket | None:
    """Bind the loopback family opposite `host` on `port`. None when that family doesn't exist on this machine."""
    other, family = OTHER_FAMILY[host]
    try:
        sock = socket.socket(family, socket.SOCK_STREAM)
    except OSError:
        return None
    try:
        if family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)  # only ::1; 127.0.0.1 stays separate
        sock.bind((other, port))
    except OSError as exc:
        sock.close()
        if exc.errno in (errno.EADDRINUSE, errno.EACCES):
            raise LoopbackBusy(exc.errno, f"{other} port {port} is in use") from exc
        return None  # EADDRNOTAVAIL and friends: no such loopback address on this machine
    return sock
