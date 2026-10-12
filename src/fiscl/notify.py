"""Resource change notifications: tell connected clients that fiscl:// resources changed.

Triggers (imports, syncs, applied edits) call `ChangeNotifier.changed(kind)` from any thread: tool worker threads
and the inbox watcher's thread. Calls within a short window coalesce into one publish per URI. The publish hops to
the server's event loop with the token captured at startup (`anyio.from_thread.run(..., token=...)`), so the
watcher thread needs no loop of its own. Events carry only resource URIs, never data.

Delivery reaches `subscriptions/listen` streams (2026-07-28 protocol) over stdio and HTTP. Clients still on
`resources/subscribe` get nothing from this module (best effort, per the spec).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable
from datetime import date

import anyio.from_thread
import anyio.lowlevel
from mcp.server.subscriptions import ResourceUpdated, SubscriptionBus

log = logging.getLogger("fiscl.notify")

DEBOUNCE_SECONDS = 2.0
KINDS = ("import", "sync", "edit")

_BASE = ("fiscl://summary/current-month", "fiscl://recurring", "fiscl://budgets", "fiscl://goals")


def uris_for(kind: str, extra: Iterable[str] = (), year: int | None = None) -> frozenset[str]:
    """The resource URIs a kind of change can affect. `extra` adds resources a specific tool touched."""
    if kind not in KINDS:
        raise ValueError("unknown change kind")
    year = year or date.today().year
    uris = {*_BASE, f"fiscl://tax/{year}", *extra}
    if kind in ("import", "sync"):
        uris.add("fiscl://accounts")
    return frozenset(uris)


class ChangeNotifier:
    def __init__(self, bus: SubscriptionBus, debounce: float = DEBOUNCE_SECONDS) -> None:
        self._bus = bus
        self._debounce = debounce
        self._lock = threading.Lock()
        self._pending: set[str] = set()
        self._timer: threading.Timer | None = None
        self._token = None

    def attach_loop(self) -> None:
        """Remember the running event loop. Call from async code on the server's loop (the lifespan hook)."""
        self._token = anyio.lowlevel.current_token()

    def changed(self, kind: str, extra: Iterable[str] = ()) -> None:
        """Record that data changed; safe from any thread. The publish happens after the debounce window."""
        uris = uris_for(kind, extra)
        with self._lock:
            self._pending |= uris
            if self._timer is None:
                self._timer = threading.Timer(self._debounce, self._flush)
                self._timer.daemon = True
                self._timer.start()

    def close(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer, self._pending = None, set()

    def _flush(self) -> None:
        with self._lock:
            uris = sorted(self._pending)
            self._pending, self._timer = set(), None
        token = self._token
        if token is None or not uris:
            return  # no client has ever connected, so nobody is listening
        try:
            anyio.from_thread.run(self._publish, uris, token=token)
        except Exception:  # the loop may be shutting down; a lost notification is harmless (clients re-read)
            log.warning("resource-updated notification not delivered", exc_info=True)

    async def _publish(self, uris: list[str]) -> None:
        for uri in uris:
            await self._bus.publish(ResourceUpdated(uri=uri))
