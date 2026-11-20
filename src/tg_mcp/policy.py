"""Chat access rules, date ranges and rate limits."""

import time
from collections import deque
from datetime import UTC, date, datetime, timedelta

from telethon import types

from .config import Config

# Telegram's own service accounts. 777000 delivers login codes, so exposing it
# would hand an attacker what they need to take over the account.
SERVICE_USER_IDS = frozenset({777000, 42777, 333000, 708513, 1271266957})


class UserError(Exception):
    """An expected failure whose message is safe to show to the client."""


def block_reason(chat_id: int, entity, config: Config) -> str | None:
    """Return why a chat is closed, or None when it is reachable."""
    if isinstance(entity, types.User):
        if entity.id in SERVICE_USER_IDS or entity.support:
            return "telegram_service"
        if entity.bot:
            return "bot"
        if entity.is_self and config.block_saved_messages:
            return "saved_messages"
    if chat_id in config.denied_chat_ids:
        return "denied"
    if config.allowed_chat_ids and chat_id not in config.allowed_chat_ids:
        return "not_allowed"
    return None


def _parse_bound(value: str, is_end: bool) -> datetime:
    try:
        if len(value) == 10:
            parsed = datetime.combine(date.fromisoformat(value), datetime.min.time())
            return parsed + timedelta(days=1) if is_end else parsed
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise UserError(f"Invalid date: {value!r}. Use ISO 8601, e.g. 2026-11-06") from exc


def parse_range(date_from: str, date_to: str, max_days: int) -> tuple[datetime, datetime]:
    """Return an aware [start, end) range. A date-only date_to covers that whole day."""
    start = _parse_bound(date_from, is_end=False)
    end = _parse_bound(date_to, is_end=True)
    start = start.replace(tzinfo=UTC) if start.tzinfo is None else start
    end = end.replace(tzinfo=UTC) if end.tzinfo is None else end

    if start >= end:
        raise UserError("date_from must be earlier than date_to")
    if end - start > timedelta(days=max_days):
        raise UserError(f"Date range exceeds the {max_days}-day limit")
    return start, end


class RateLimiter:
    """Sliding-window limit. It stops runaway loops; it is not an access control."""

    def __init__(self, limit: int, window_seconds: float, clock=time.monotonic):
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._events: deque[tuple[float, int]] = deque()
        self._used = 0

    def _expire(self, now: float) -> None:
        while self._events and now - self._events[0][0] >= self._window:
            self._used -= self._events.popleft()[1]

    def remaining(self) -> int:
        self._expire(self._clock())
        return max(0, self._limit - self._used)

    def try_acquire(self, amount: int = 1) -> bool:
        now = self._clock()
        self._expire(now)
        if self._used + amount > self._limit:
            return False
        self._events.append((now, amount))
        self._used += amount
        return True
