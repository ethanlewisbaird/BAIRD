"""Notification routing — Phase 4 design (#3).

Every notification gets an inbox row on the hub (universal backstop). On top
of that:

  - `approval`  : Telegram push with inline-keyboard accept/reject buttons
  - `failure`   : Telegram push, informational
  - `result`    : Telegram one-liner + inbox with body
  - `logged`    : inbox row only, no push

The Telegram transport is pluggable. In tests we inject a fake; in prod the
default `TelegramHTTPTransport` polls `getUpdates` for inbound responses to
approval buttons.

The bot is hub-local: one bot token, one chat_id allowlist (just the user).
Inbound webhooks are not exposed — the bot polls outbound.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

import httpx

from .memory_client import HubClient


# Push tiers — which kinds get a Telegram push in addition to the inbox row.
PUSH_KINDS = {"approval", "failure", "result"}


@dataclass
class TelegramConfig:
    bot_token: str
    chat_id: str  # allowlist of one


class TelegramTransport(Protocol):
    def send_message(self, *, chat_id: str, text: str) -> dict: ...
    def get_updates(self, *, offset: int | None = None) -> list[dict]: ...


# ---- Default HTTP transport --------------------------------------------


class TelegramHTTPTransport:
    """Default transport — speaks to api.telegram.org."""

    BASE = "https://api.telegram.org"

    def __init__(self, bot_token: str, timeout: float = 30.0) -> None:
        self._token = bot_token
        self._timeout = timeout

    def _url(self, method: str) -> str:
        return f"{self.BASE}/bot{self._token}/{method}"

    def send_message(self, *, chat_id: str, text: str) -> dict:
        r = httpx.post(
            self._url("sendMessage"),
            json={"chat_id": chat_id, "text": text},
            timeout=self._timeout,
        )
        r.raise_for_status()
        return r.json()

    def get_updates(self, *, offset: int | None = None) -> list[dict]:
        params: dict[str, object] = {"timeout": 0}
        if offset is not None:
            params["offset"] = offset
        r = httpx.get(self._url("getUpdates"), params=params, timeout=self._timeout)
        r.raise_for_status()
        return r.json().get("result", [])


# ---- Fake transport (tests) --------------------------------------------


class FakeTelegramTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.queued_updates: list[dict] = []

    def send_message(self, *, chat_id: str, text: str) -> dict:
        self.sent.append((chat_id, text))
        return {"ok": True}

    def get_updates(self, *, offset: int | None = None) -> list[dict]:
        out = list(self.queued_updates)
        self.queued_updates.clear()
        return out


# ---- Notifier ----------------------------------------------------------


class Notifier:
    """Front door for every harness notification."""

    def __init__(
        self,
        *,
        hub: HubClient,
        telegram: TelegramConfig | None = None,
        transport: TelegramTransport | None = None,
    ) -> None:
        self._hub = hub
        self._tg_cfg = telegram
        if transport is not None:
            self._tg = transport
        elif telegram is not None:
            self._tg = TelegramHTTPTransport(telegram.bot_token)
        else:
            self._tg = None  # type: ignore[assignment]

    def notify(
        self,
        *,
        kind: str,
        title: str,
        body: str | None = None,
        project_id: str | None = None,
        action_id: str | None = None,
        task_id: str | None = None,
    ) -> dict:
        """Always writes an inbox row. Pushes to Telegram for tiered kinds."""
        row = self._hub.create_notification(
            kind=kind,
            title=title,
            body=body,
            project_id=project_id,
            action_id=action_id,
            task_id=task_id,
        )
        if kind in PUSH_KINDS and self._tg is not None and self._tg_cfg is not None:
            text = self._render_push(row)
            try:
                self._tg.send_message(chat_id=self._tg_cfg.chat_id, text=text)
            except Exception:
                # Inbox row still wrote — Telegram failure must not crash the firing.
                pass
        return row

    @staticmethod
    def _render_push(row: dict) -> str:
        kind = row.get("kind", "?")
        title = row.get("title", "")
        body = row.get("body") or ""
        rid_short = row.get("id", "")[:8]
        bits = [f"[{kind}] {title}", f"id: {rid_short}"]
        if body:
            bits.append("")
            bits.append(body[:500])
        return "\n".join(bits)
