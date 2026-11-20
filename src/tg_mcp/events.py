"""In-memory buffer of live Telegram events, replayable by cursor.

Events never touch the disk. Only events some connected client has subscribed to are
kept: while nobody listens to a chat, nothing from it is stored. A cursor is "<boot>-<seq>":
when the core restarts the boot part changes, and a reconnecting client is told about a gap.
"""

import asyncio
import secrets
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from .config import ClientPolicy


class CursorError(ValueError):
    """The client sent a cursor that this buffer never issued."""


class EventBuffer:
    def __init__(self, retention_seconds: float, max_events: int, clock=time.monotonic):
        self.boot = secrets.token_hex(4)
        self._retention = retention_seconds
        self._max_events = max_events
        self._clock = clock
        self._events: deque[tuple[int, float, dict[str, Any]]] = deque()
        self._seq = 0
        self._signal = asyncio.Event()
        self._subscriptions: set[Subscription] = set()

    def subscribe(self, subscription: "Subscription") -> None:
        self._subscriptions.add(subscription)

    def unsubscribe(self, subscription: "Subscription") -> None:
        self._subscriptions.discard(subscription)

    def wanted(self, event: dict[str, Any]) -> bool:
        """Whether any connected client listens for this event right now."""
        return any(subscription.wants(event) for subscription in self._subscriptions)

    @property
    def signal(self) -> asyncio.Event:
        """Set once the next event arrives. Take it before reading to never miss one."""
        return self._signal

    def _expire(self) -> None:
        cutoff = self._clock() - self._retention
        while self._events and (self._events[0][1] < cutoff or len(self._events) > self._max_events):
            self._events.popleft()

    def publish(self, event: dict[str, Any]) -> str:
        self._seq += 1
        self._events.append((self._seq, self._clock(), event))
        self._expire()
        signal, self._signal = self._signal, asyncio.Event()
        signal.set()
        return f"{self.boot}-{self._seq}"

    def latest_cursor(self) -> str:
        return f"{self.boot}-{self._seq}"

    def read_after(self, cursor: str | None) -> tuple[list[tuple[str, dict[str, Any]]], bool]:
        """Return events after the cursor, and whether some may have been lost before them."""
        self._expire()
        gap = False
        after = 0
        if cursor:
            boot, _, seq = cursor.partition("-")
            if not seq.isdigit():
                raise CursorError("Malformed cursor")
            if boot != self.boot:
                gap = True
            else:
                after = int(seq)
                if after > self._seq:
                    raise CursorError("Unknown cursor")
                oldest = self._events[0][0] if self._events else self._seq + 1
                gap = after < oldest - 1
        events = [(f"{self.boot}-{seq}", event) for seq, _, event in self._events if seq > after]
        return events, gap


def wanted_by(policy: ClientPolicy, event: dict[str, Any]) -> bool:
    """Server-side filter: a client never receives an event outside its rights."""
    if event["type"] not in policy.events or not policy.allows_chat(event["chat_id"]):
        return False
    if policy.text_pattern is None:
        return True
    message = event.get("message")
    return message is not None and policy.text_pattern.search(message.get("text", "")) is not None


@dataclass(frozen=True, eq=False)
class Subscription:
    """What one open event stream asked for, always within its client's rights."""

    policy: ClientPolicy
    chats: frozenset[int] | None = None
    types: frozenset[str] | None = None

    def wants(self, event: dict[str, Any]) -> bool:
        if self.chats is not None and event["chat_id"] not in self.chats:
            return False
        if self.types is not None and event["type"] not in self.types:
            return False
        return wanted_by(self.policy, event)
