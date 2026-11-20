"""Client for the Telegram core. Standard library only: copy this file into a project.

    core = TelegramCore("/run/tg-core/core.sock")

    # New messages are pushed by the core as they arrive:
    for event in core.watch(chats=[-1001234567890], pattern=r"Missed call", state_file="/data/tg-state"):
        handle(event["chat_id"], event["message"]["text"])

    # Or ask the core for new messages every 5 minutes:
    for event in core.watch(mode="poll", interval=300, chats=[-1001234567890], state_file="/data/tg-state"):
        ...

The core keeps nothing for a chat while no script listens to it. The first run starts
from now, without history. After a restart, watch() fills in the new messages missed
while the script was down (needs read = true for the project), up to CATCH_UP_DAYS back.
An event is marked done only when you ask for the next one, so a crash while handling it
redelivers it (at-least-once delivery): drop duplicates by message id.
"""

import http.client
import json
import logging
import os
import re
import socket
import time
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

__all__ = ["CoreError", "TelegramCore"]

STREAM_TIMEOUT_SECONDS = 60  # The core sends a heartbeat every 15 seconds.
MAX_RETRY_DELAY_SECONDS = 60
# Every poll costs one Telegram request per chat; frequent polling raises the ban risk.
MIN_POLL_INTERVAL_SECONDS = 30
POLL_PAGE_SIZE = 20
CATCH_UP_DAYS = 7  # After a longer downtime, older missed messages are skipped.
EVENT_TYPES = ("new_message", "edited", "deleted")

log = logging.getLogger("tgcore_client")


# The core reports its own limits as 400; these messages mean "try again later".
TEMPORARY_ERRORS = ("Too many requests", "Hourly message limit")


def _is_temporary(error: "CoreError") -> bool:
    if error.status in (429, 500, 503):
        return True
    return error.status == 400 and str(error).partition(": ")[2].startswith(TEMPORARY_ERRORS)


def _write_atomic(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text)
    os.replace(temporary, path)


class CoreError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float):
        super().__init__("core", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


def _read_error(response: http.client.HTTPResponse) -> CoreError:
    try:
        message = json.loads(response.read())["error"]
    except (ValueError, KeyError, TypeError):
        message = response.reason
    return CoreError(response.status, message)


class TelegramCore:
    def __init__(self, socket_path: str | os.PathLike, timeout: float = 120):
        self._socket_path = str(socket_path)
        self._timeout = timeout

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = {key: value for key, value in (params or {}).items() if value is not None}
        connection = _UnixConnection(self._socket_path, self._timeout)
        try:
            connection.request("GET", f"{path}?{urlencode(query)}" if query else path)
            response = connection.getresponse()
            if response.status >= 400:
                raise _read_error(response)
            return json.loads(response.read())
        finally:
            connection.close()

    def health(self) -> dict[str, Any]:
        return self._get("/v1/health")

    def find_chats(self, query: str = "", limit: int = 20) -> dict[str, Any]:
        return self._get("/v1/chats", {"query": query, "limit": limit})

    def read_messages(
        self, chat_id: int, date_from: str, date_to: str, limit: int = 100, before_id: int | None = None
    ) -> dict[str, Any]:
        """One page, newest first. Pass next_before_id from the result to get the next page."""
        params = {"date_from": date_from, "date_to": date_to, "limit": limit, "before_id": before_id}
        return self._get(f"/v1/chats/{chat_id}/messages", params)

    def search_chat(self, chat_id: int, query: str, limit: int = 100, before_id: int | None = None) -> dict[str, Any]:
        """Messages in one chat containing the query, newest first (right: read).
        Pass next_before_id from the result to get the next page."""
        return self._get(f"/v1/chats/{chat_id}/search", {"q": query, "limit": limit, "before_id": before_id})

    def search(self, query: str, limit: int = 30) -> dict[str, Any]:
        """Telegram's search across all chats, newest first (right: search).
        Each result has chat_id, chat_name, chat_type, username, link and message."""
        return self._get("/v1/search", {"q": query, "limit": limit})

    def contact_cards(self, limit: int = 200) -> dict[str, Any]:
        """The most recent contact cards shared in any chat (right: search).
        message["media"] has phone (digits only), first_name and last_name."""
        return self._get("/v1/search/contacts", {"limit": limit})

    def find_by_phone(self, phone: str) -> dict[str, Any]:
        """Private chats with the owner of this number, when Telegram shows the number to
        this account (right: search). Matches by the last 10 digits."""
        digits = re.sub(r"\D", "", phone)
        if not digits:
            raise ValueError("phone has no digits")
        return self._get(f"/v1/phone/{digits}")

    def _stream(self, params: dict[str, Any]) -> Iterator[tuple[str | None, str | None, dict[str, Any] | None]]:
        """One connection. Yields (None, None, None) once the core is listening for us,
        then (event_id, event_name, payload) for each event, until the connection breaks."""
        connection = _UnixConnection(self._socket_path, STREAM_TIMEOUT_SECONDS)
        try:
            query = {key: value for key, value in params.items() if value is not None}
            connection.request("GET", f"/v1/events?{urlencode(query)}")
            response = connection.getresponse()
            if response.status >= 400:
                error = _read_error(response)
                if error.status in (400, 403):
                    raise error
                raise ConnectionError(str(error))

            event_id, event_name, data = None, None, []
            while True:
                line = response.readline()
                if not line:
                    raise ConnectionError("Event stream closed")
                line = line.decode("utf-8").rstrip("\r\n")
                if line.startswith(":"):
                    if line == ": subscribed":
                        yield None, None, None
                    continue
                if line:
                    field, _, value = line.partition(":")
                    value = value.removeprefix(" ")
                    if field == "id":
                        event_id = value
                    elif field == "event":
                        event_name = value
                    elif field == "data":
                        data.append(value)
                    continue
                if data:
                    yield event_id, event_name, json.loads("\n".join(data))
                event_id, event_name, data = None, None, []
        finally:
            connection.close()

    def _reconnecting(self, params_for) -> Iterator[tuple[str | None, str | None, dict[str, Any] | None]]:
        delay = 1
        while True:
            try:
                for item in self._stream(params_for()):
                    delay = 1
                    yield item
            except (OSError, ConnectionError, http.client.HTTPException) as error:
                log.warning("Event stream interrupted, reconnecting: %s", error)
                time.sleep(delay)
                delay = min(delay * 2, MAX_RETRY_DELAY_SECONDS)

    def events(
        self,
        cursor_file: str | os.PathLike | None = None,
        start: str = "all",
        chats: Iterable[int] | None = None,
        types: Iterable[str] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Raw event stream, forever. Prefer watch(): this one does not fill in what was
        missed while disconnected. start="latest" skips what the core holds right now."""
        cursor_path = Path(cursor_file) if cursor_file else None
        cursor = cursor_path.read_text().strip() if cursor_path and cursor_path.exists() else None
        chat_list = _join(chats)
        type_list = _join(types)

        def params():
            if cursor:
                base = {"after": cursor}
            else:
                base = {"from": "latest"} if start == "latest" else {}
            return {**base, "chats": chat_list, "types": type_list}

        for event_id, event_name, payload in self._reconnecting(params):
            if payload is None:
                continue
            yield payload
            # Reached only when the caller asks for the next event: this one is done.
            if event_name == "message" and event_id:
                cursor = event_id
                if cursor_path:
                    _write_atomic(cursor_path, cursor)

    def watch(
        self,
        *,
        mode: str = "stream",
        chats: Iterable[int] | None = None,
        pattern: str | re.Pattern | None = None,
        events: Iterable[str] = ("new_message",),
        interval: float = 300,
        state_file: str | os.PathLike | None = None,
        catch_up: bool = True,
        catch_up_days: int = CATCH_UP_DAYS,
    ) -> Iterator[dict[str, Any]]:
        """Yield {"type", "chat_id", "chat_name", "message"} for matching messages, forever.

        mode="stream": the core pushes each event the moment it arrives. No Telegram
            requests while waiting; sees edits and deletions too.
        mode="poll": every `interval` seconds, ask the core for new messages in `chats`.
        chats: chat ids to listen to (required for poll; without it, stream gets every
            chat the project may see). pattern: regular expression the text must contain.
        events: for stream, any of "new_message", "edited", "deleted".
        state_file: remembers the last message per chat. With it, a restart fills in new
            messages missed while down (catch_up, up to catch_up_days back). Edits and
            deletions made while down are not filled in. The first run starts from now.
        """
        wanted_chats = None if chats is None else frozenset(int(chat) for chat in chats)
        regex = re.compile(pattern) if isinstance(pattern, str) else pattern
        positions = _Positions(Path(state_file) if state_file else None)

        def matches(event: dict[str, Any]) -> bool:
            if wanted_chats is not None and event.get("chat_id") not in wanted_chats:
                return False
            if regex is None:
                return True
            text = (event.get("message") or {}).get("text") or ""
            return regex.search(text) is not None

        if mode == "stream":
            wanted_events = frozenset(events)
            if not wanted_events or not wanted_events <= set(EVENT_TYPES):
                raise ValueError(f"events must be a non-empty subset of {EVENT_TYPES}")
            yield from self._watch_stream(wanted_chats, wanted_events, matches, positions, catch_up, catch_up_days)
        elif mode == "poll":
            if not wanted_chats:
                raise ValueError("poll mode needs the list of chats")
            if interval < MIN_POLL_INTERVAL_SECONDS:
                raise ValueError(f"interval must be at least {MIN_POLL_INTERVAL_SECONDS} seconds")
            yield from self._watch_poll(sorted(wanted_chats), matches, positions, interval, catch_up_days)
        else:
            raise ValueError('mode must be "stream" or "poll"')

    def _catch_up(self, chat_id, positions, matches, days) -> Iterator[dict[str, Any]]:
        """New messages in one chat since its saved position, oldest first."""
        position = positions.get(chat_id)
        if position is None:
            positions.start(chat_id)  # First run for this chat: no history.
            return
        new, chat_name = self._new_messages(chat_id, position, days)
        for message in new:
            event = {"type": "new_message", "chat_id": chat_id, "chat_name": chat_name, "message": message}
            if matches(event):
                yield event
            # Reached only when the caller asks for the next one: this message is done.
            positions.advance(chat_id, message)

    def _watch_stream(self, chats, types, matches, positions, catch_up, days) -> Iterator[dict[str, Any]]:
        params = {"from": "latest", "chats": _join(chats), "types": ",".join(sorted(types))}
        for _, event_name, payload in self._reconnecting(lambda: params):
            if payload is None:
                # The core is now keeping our events, so nothing slips between the
                # catch-up below and the live stream. Duplicates are skipped by id.
                for chat_id in sorted(chats if chats is not None else positions.chats()):
                    if not catch_up:
                        if positions.get(chat_id) is None:
                            positions.start(chat_id)
                        continue
                    try:
                        yield from self._catch_up(chat_id, positions, matches, days)
                    except CoreError as error:
                        if error.status == 403:
                            log.warning("Catch-up needs read = true for this project; continuing without it")
                            catch_up = False
                        else:
                            log.warning("Catch-up of chat %s failed: %s", chat_id, error)
                    except (OSError, ConnectionError, http.client.HTTPException) as error:
                        log.warning("Catch-up of chat %s failed: %s", chat_id, error)
                continue
            if event_name != "message":
                continue  # A gap: the catch-up after each reconnect covers new messages.
            chat_id = payload.get("chat_id")
            message = payload.get("message") or {}
            is_new = payload.get("type") == "new_message"
            if is_new and positions.seen(chat_id, message.get("id")):
                continue
            if matches(payload):
                yield payload
            if is_new:
                positions.advance(chat_id, message)

    def _watch_poll(self, chats, matches, positions, interval, days) -> Iterator[dict[str, Any]]:
        while True:
            for chat_id in chats:
                try:
                    yield from self._catch_up(chat_id, positions, matches, days)
                except (OSError, ConnectionError, http.client.HTTPException) as error:
                    log.warning("Telegram core unavailable, retrying later: %s", error)
                    break
                except CoreError as error:
                    if not _is_temporary(error):
                        raise  # No right to this chat or a wrong option: fix the script or config.toml.
                    log.warning("Poll of chat %s failed, retrying later: %s", chat_id, error)
            time.sleep(interval)

    def _new_messages(self, chat_id: int, position: dict[str, Any], days: int) -> tuple[list[dict[str, Any]], str | None]:
        """Messages newer than position, oldest first."""
        now = datetime.now(UTC)
        since = max(datetime.fromisoformat(position["last_date"]), now - timedelta(days=days))
        until = (now + timedelta(minutes=1)).isoformat()
        collected: list[dict[str, Any]] = []
        chat_name, before_id = None, None
        while True:
            page = self.read_messages(chat_id, since.isoformat(), until, POLL_PAGE_SIZE, before_id)
            chat_name = page.get("chat_name")
            fresh = [m for m in page["messages"] if m["id"] > position["last_id"]]
            collected.extend(fresh)
            before_id = page.get("next_before_id")
            if not before_id or len(fresh) < len(page["messages"]):
                break
        collected.sort(key=lambda message: message["id"])
        return collected, chat_name


def _join(values: Iterable[Any] | None) -> str | None:
    return None if values is None else ",".join(str(value) for value in sorted(values))


class _Positions:
    """Last delivered message per chat, kept in the state file."""

    def __init__(self, path: Path | None):
        self._path = path
        self._chats: dict[str, dict[str, Any]] = {}
        if path and path.exists():
            self._chats = json.loads(path.read_text()).get("chats", {})

    def chats(self) -> list[int]:
        return [int(chat_id) for chat_id in self._chats]

    def get(self, chat_id: int) -> dict[str, Any] | None:
        return self._chats.get(str(chat_id))

    def seen(self, chat_id: int, message_id: int | None) -> bool:
        position = self.get(chat_id)
        return position is not None and message_id is not None and message_id <= position["last_id"]

    def start(self, chat_id: int) -> None:
        self._chats[str(chat_id)] = {"last_id": 0, "last_date": datetime.now(UTC).isoformat()}
        self._save()

    def advance(self, chat_id: int, message: dict[str, Any]) -> None:
        position = self.get(chat_id)
        if position is not None and message["id"] <= position["last_id"]:
            return
        self._chats[str(chat_id)] = {"last_id": message["id"], "last_date": message["date"]}
        self._save()

    def _save(self) -> None:
        if self._path:
            _write_atomic(self._path, json.dumps({"chats": self._chats}))
