"""In-process pub/sub for reactive task triggers.

A bounded-fanout event bus. Subscribers register a callback for a string event
name; publishers call `publish(name, payload)`. Callbacks run synchronously
inside `publish` — keep them cheap, or shovel the work onto a thread pool.

Why not asyncio queues: the scheduler is sync + threaded, and `publish` happens
from many call sites (action finish, daemon events, manual `baird emit`). Sync
fanout keeps the integration tiny.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from collections.abc import Callable
from typing import Any

log = logging.getLogger("baird.event_bus")


Listener = Callable[[str, dict[str, Any]], None]


class EventBus:
    def __init__(self) -> None:
        self._listeners: dict[str, list[Listener]] = defaultdict(list)
        self._lock = threading.Lock()

    def subscribe(self, event: str, listener: Listener) -> Callable[[], None]:
        """Register `listener`. Returns an unsubscribe callable."""
        with self._lock:
            self._listeners[event].append(listener)

        def _off() -> None:
            with self._lock:
                try:
                    self._listeners[event].remove(listener)
                except ValueError:
                    pass

        return _off

    def publish(self, event: str, payload: dict[str, Any] | None = None) -> None:
        payload = payload or {}
        with self._lock:
            listeners = list(self._listeners.get(event, []))
        for fn in listeners:
            try:
                fn(event, payload)
            except Exception:
                log.exception("event listener for %s raised", event)

    def listener_count(self, event: str) -> int:
        with self._lock:
            return len(self._listeners.get(event, []))


# Process-wide default bus — convenient for callers that don't want to thread
# one through. Library code should still prefer dependency-injection where
# practical.
default_bus = EventBus()
