"""Tests for the in-process event bus."""

from __future__ import annotations

from baird.event_bus import EventBus


def test_subscribe_and_publish() -> None:
    bus = EventBus()
    received: list[tuple[str, dict]] = []
    bus.subscribe("ping", lambda e, p: received.append((e, p)))
    bus.publish("ping", {"x": 1})
    assert received == [("ping", {"x": 1})]


def test_unsubscribe() -> None:
    bus = EventBus()
    received: list[str] = []
    off = bus.subscribe("ping", lambda e, p: received.append(e))
    off()
    bus.publish("ping")
    assert received == []


def test_listener_exception_does_not_break_others() -> None:
    bus = EventBus()
    received: list[str] = []
    bus.subscribe("ping", lambda e, p: (_ for _ in ()).throw(RuntimeError("boom")))
    bus.subscribe("ping", lambda e, p: received.append(e))
    bus.publish("ping")
    assert received == ["ping"]


def test_no_listeners_no_op() -> None:
    bus = EventBus()
    bus.publish("nope")  # must not raise


def test_listener_count() -> None:
    bus = EventBus()
    assert bus.listener_count("x") == 0
    off = bus.subscribe("x", lambda *_: None)
    assert bus.listener_count("x") == 1
    off()
    assert bus.listener_count("x") == 0
