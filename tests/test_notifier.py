"""Tests for the Notifier — inbox row always written, Telegram push tier-gated."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from baird.memory_client import HubClient
from baird.notifier import FakeTelegramTransport, Notifier, TelegramConfig


class _Hub(HubClient):
    def __init__(self, client: TestClient) -> None:
        self._client = client


@pytest.fixture
def setup(client: TestClient) -> tuple[Notifier, FakeTelegramTransport, _Hub]:
    hub = _Hub(client)
    tg = FakeTelegramTransport()
    notifier = Notifier(
        hub=hub,
        telegram=TelegramConfig(bot_token="t", chat_id="42"),
        transport=tg,
    )
    return notifier, tg, hub


def test_approval_pushes_and_writes_inbox(setup: tuple[Notifier, FakeTelegramTransport, _Hub]) -> None:
    notifier, tg, hub = setup
    row = notifier.notify(kind="approval", title="tier-2 write")
    assert row["kind"] == "approval"
    # Inbox row exists
    assert any(r["id"] == row["id"] for r in hub.list_notifications())
    # Telegram got the push
    assert tg.sent
    chat, text = tg.sent[0]
    assert chat == "42"
    assert "approval" in text and "tier-2 write" in text


def test_result_pushes(setup: tuple[Notifier, FakeTelegramTransport, _Hub]) -> None:
    notifier, tg, _ = setup
    notifier.notify(kind="result", title="task done", body="ok")
    assert tg.sent


def test_failure_pushes(setup: tuple[Notifier, FakeTelegramTransport, _Hub]) -> None:
    notifier, tg, _ = setup
    notifier.notify(kind="failure", title="boom")
    assert tg.sent


def test_logged_does_not_push_but_still_inboxes(
    setup: tuple[Notifier, FakeTelegramTransport, _Hub],
) -> None:
    notifier, tg, hub = setup
    row = notifier.notify(kind="logged", title="routine")
    assert not tg.sent
    assert any(r["id"] == row["id"] for r in hub.list_notifications())


def test_notifier_without_telegram_just_inboxes(client: TestClient) -> None:
    hub = _Hub(client)
    notifier = Notifier(hub=hub, telegram=None)
    row = notifier.notify(kind="approval", title="needs approval")
    assert any(r["id"] == row["id"] for r in hub.list_notifications())


def test_telegram_send_failure_does_not_crash(client: TestClient) -> None:
    class BoomTransport:
        def send_message(self, **_): raise RuntimeError("network down")
        def get_updates(self, **_): return []

    hub = _Hub(client)
    notifier = Notifier(
        hub=hub,
        telegram=TelegramConfig(bot_token="t", chat_id="42"),
        transport=BoomTransport(),
    )
    row = notifier.notify(kind="approval", title="x")
    # The inbox row must have been written even though Telegram blew up.
    assert any(r["id"] == row["id"] for r in hub.list_notifications())
