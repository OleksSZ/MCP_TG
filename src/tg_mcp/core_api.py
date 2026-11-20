"""Local HTTP API of the Telegram core. Each client gets its own app on its own socket.

The socket a request arrives on decides who the client is, so there are no tokens to
steal. Every route enforces that client's rights before touching Telegram.
"""

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from telethon.errors import FloodWaitError

from . import vault
from .config import ClientPolicy
from .events import CursorError, EventBuffer, Subscription
from .gateway import MEDIA_TYPES, TelegramGateway
from .guard import ForbiddenRequestError
from .policy import RateLimiter, UserError

log = logging.getLogger("tg_mcp")

HEARTBEAT_SECONDS = 15
MAX_STREAMS_PER_CLIENT = 4
AUDITED_RESULT_FIELDS = ("status", "message_count", "files_saved", "bytes_saved", "truncated", "total_matches")


class Denied(Exception):
    """The client lacks the right for this route."""


def audit(path: Path, entry: dict[str, Any]) -> None:
    """Record who-did-what metadata. Never include message contents or search text."""
    line = json.dumps({"ts": datetime.now(UTC).isoformat(), **entry}) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o640)
    with os.fdopen(fd, "a") as file:
        file.write(line)


def _int(value: str | None, name: str, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        raise UserError(f"{name} must be an integer") from None


MAX_SUBSCRIBED_CHATS = 1000


def _id_list(value: str | None, name: str) -> frozenset[int] | None:
    if not value:
        return None
    try:
        ids = frozenset(int(item) for item in value.split(","))
    except ValueError:
        raise UserError(f"{name} must be comma-separated integers") from None
    if len(ids) > MAX_SUBSCRIBED_CHATS:
        raise UserError(f"At most {MAX_SUBSCRIBED_CHATS} {name}")
    return ids


def _sse(event: str, data: dict[str, Any], event_id: str | None = None) -> str:
    head = f"id: {event_id}\n" if event_id else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def create_core_app(
    gateway: TelegramGateway,
    buffer: EventBuffer,
    policy: ClientPolicy,
    audit_path: Path,
    is_connected: Callable[[], bool] = lambda: False,
) -> Starlette:
    calls = RateLimiter(policy.max_calls_per_minute, 60)
    scope = policy.chats
    streams = 0

    def require(right: bool) -> None:
        if not right:
            raise Denied()

    async def guarded(request: Request, action: str, details: dict, work: Callable[[], Awaitable[Any]]):
        entry = {"client": policy.name, "action": action, **details}
        try:
            if not calls.try_acquire():
                raise UserError("Too many requests; try again in a minute")
            result = await work()
        except Denied:
            audit(audit_path, {**entry, "outcome": "denied"})
            return JSONResponse({"error": "This client is not allowed to do that"}, status_code=403)
        except UserError as exc:
            audit(audit_path, {**entry, "outcome": "rejected"})
            return JSONResponse({"error": str(exc)}, status_code=400)
        except FloodWaitError as exc:
            audit(audit_path, {**entry, "outcome": "flood_wait"})
            return JSONResponse(
                {"error": f"Telegram rate limit: retry after {exc.seconds} seconds"}, status_code=429
            )
        except ForbiddenRequestError as exc:
            log.error("%s", exc)
            audit(audit_path, {**entry, "outcome": "blocked_request"})
            return JSONResponse({"error": "Operation not permitted"}, status_code=403)
        except vault.VaultError as exc:
            log.error("%s", exc)
            audit(audit_path, {**entry, "outcome": "vault_error"})
            return JSONResponse({"error": "The core is not ready; see its log"}, status_code=503)
        except Exception as exc:
            log.error("%s for %s failed: %s", action, policy.name, type(exc).__name__)
            audit(audit_path, {**entry, "outcome": "error", "error": type(exc).__name__})
            return JSONResponse({"error": "Internal error"}, status_code=500)

        if isinstance(result, Response):
            audit(audit_path, {**entry, "outcome": "ok"})
            return result
        counts = {key: result[key] for key in AUDITED_RESULT_FIELDS if key in result}
        if "messages" in result:
            counts["message_count"] = len(result["messages"])
        audit(audit_path, {**entry, "outcome": "ok", **counts})
        return JSONResponse(result)

    async def health(request: Request) -> Response:
        return JSONResponse({"client": policy.name, "telegram_connected": is_connected()})

    async def chats(request: Request) -> Response:
        async def work():
            require(policy.read or policy.media or policy.export or policy.search or bool(policy.events))
            limit = _int(request.query_params.get("limit"), "limit", 20)
            query = request.query_params.get("query", "")[:100]
            return await gateway.find_chats(query, max(1, min(limit, 50)), scope=scope)

        return await guarded(request, "find_chats", {}, work)

    async def messages(request: Request) -> Response:
        chat_id = _int(request.path_params["chat_id"], "chat_id")
        params = request.query_params

        async def work():
            require(policy.read)
            return await gateway.read_messages(
                chat_id,
                params.get("date_from", ""),
                params.get("date_to", ""),
                _int(params.get("limit"), "limit", 100),
                _int(params.get("before_id"), "before_id"),
                scope=scope,
            )

        details = {"chat_id": chat_id, "date_from": params.get("date_from"), "date_to": params.get("date_to")}
        return await guarded(request, "read_messages", details, work)

    async def search_chat(request: Request) -> Response:
        chat_id = _int(request.path_params["chat_id"], "chat_id")
        params = request.query_params

        async def work():
            require(policy.read)
            return await gateway.search_chat(
                chat_id,
                params.get("q", ""),
                _int(params.get("limit"), "limit", 100),
                _int(params.get("before_id"), "before_id"),
                scope=scope,
            )

        # The query itself is not logged: it is usually a phone number.
        return await guarded(request, "search_chat", {"chat_id": chat_id}, work)

    async def search(request: Request) -> Response:
        params = request.query_params

        async def work():
            require(policy.search)
            return await gateway.search_messages(params.get("q", ""), _int(params.get("limit"), "limit", 30), scope=scope)

        return await guarded(request, "search", {}, work)

    async def contact_cards(request: Request) -> Response:
        async def work():
            require(policy.search)
            return await gateway.contact_cards(_int(request.query_params.get("limit"), "limit", 200), scope=scope)

        return await guarded(request, "contact_cards", {}, work)

    async def phone_lookup(request: Request) -> Response:
        async def work():
            require(policy.search)
            return await gateway.find_by_phone(request.path_params["phone"][:32], scope=scope)

        return await guarded(request, "phone_lookup", {}, work)

    async def image(request: Request) -> Response:
        chat_id = _int(request.path_params["chat_id"], "chat_id")
        message_id = _int(request.path_params["message_id"], "message_id")

        async def work():
            require(policy.media)
            result = await gateway.get_media(chat_id, message_id, scope=scope)
            return Response(
                result["data"],
                media_type=f"image/{result['format']}",
                headers={"X-Content-Type-Options": "nosniff"},
            )

        return await guarded(request, "get_media", {"chat_id": chat_id, "message_id": message_id}, work)

    async def start_export(request: Request) -> Response:
        async def work():
            require(policy.export)
            try:
                body = await request.json()
            except ValueError:
                raise UserError("Body must be JSON") from None
            media_types = body.get("media_types")
            if media_types is not None and (
                not isinstance(media_types, list) or not set(media_types) <= MEDIA_TYPES
            ):
                raise UserError(f"media_types must be a list of: {', '.join(sorted(MEDIA_TYPES))}")
            chat_id = body.get("chat_id")
            if type(chat_id) is not int:
                raise UserError("chat_id must be an integer")
            return await gateway.start_export(
                chat_id,
                str(body.get("date_from", "")),
                str(body.get("date_to", "")),
                bool(body.get("include_media", False)),
                media_types,
                scope=scope,
                owner=policy.name,
            )

        return await guarded(request, "export_chat", {}, work)

    async def export_status(request: Request) -> Response:
        async def work():
            require(policy.export)
            return await gateway.get_export_status(request.path_params["job_id"][:64], owner=policy.name)

        return await guarded(request, "get_export_status", {}, work)

    async def stream_events(request: Request) -> Response:
        nonlocal streams
        if not policy.events:
            audit(audit_path, {"client": policy.name, "action": "events", "outcome": "denied"})
            return JSONResponse({"error": "This client is not allowed to do that"}, status_code=403)
        if streams >= MAX_STREAMS_PER_CLIENT:
            return JSONResponse({"error": "Too many open event streams"}, status_code=429)

        try:
            chats = _id_list(request.query_params.get("chats"), "chats")
            types = request.query_params.get("types")
            types = frozenset(types.split(",")) if types else None
        except UserError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if (chats is not None and policy.chats is not None and not chats <= policy.chats) or (
            types is not None and not types <= policy.events
        ):
            audit(audit_path, {"client": policy.name, "action": "events", "outcome": "denied"})
            return JSONResponse({"error": "This client is not allowed to do that"}, status_code=403)
        subscription = Subscription(policy, chats, types)

        cursor = request.headers.get("last-event-id") or request.query_params.get("after")
        if request.query_params.get("from") == "latest" and not cursor:
            cursor = buffer.latest_cursor()
        try:
            _, gap = buffer.read_after(cursor)
        except CursorError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        audit(audit_path, {"client": policy.name, "action": "events", "outcome": "ok", "gap": gap})

        async def generate():
            nonlocal streams, cursor
            streams += 1
            buffer.subscribe(subscription)
            try:
                # From here on, events for this stream are kept. Clients wait for this line
                # before filling in what they missed while disconnected.
                yield ": subscribed\n\n"
                if gap:
                    yield _sse("gap", {"type": "gap", "detail": "Some events may be missing; use read history to fill in"})
                while True:
                    signal = buffer.signal
                    items, lost = buffer.read_after(cursor)
                    if lost and cursor and cursor.startswith(buffer.boot):
                        yield _sse("gap", {"type": "gap", "detail": "The client fell behind the buffer"})
                    for item_cursor, event in items:
                        cursor = item_cursor
                        if subscription.wants(event):
                            yield _sse("message", event, item_cursor)
                    try:
                        await asyncio.wait_for(signal.wait(), HEARTBEAT_SECONDS)
                    except TimeoutError:
                        yield ": ping\n\n"
            finally:
                buffer.unsubscribe(subscription)
                streams -= 1

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return Starlette(
        routes=[
            Route("/v1/health", health),
            Route("/v1/chats", chats),
            Route("/v1/chats/{chat_id}/messages", messages),
            Route("/v1/chats/{chat_id}/search", search_chat),
            Route("/v1/search", search),
            Route("/v1/search/contacts", contact_cards),
            Route("/v1/phone/{phone}", phone_lookup),
            Route("/v1/chats/{chat_id}/messages/{message_id}/image", image),
            Route("/v1/exports", start_export, methods=["POST"]),
            Route("/v1/exports/{job_id}", export_status),
            Route("/v1/events", stream_events),
        ]
    )
