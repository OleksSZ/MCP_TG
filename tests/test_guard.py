import asyncio
import importlib
import inspect
import pkgutil

import pytest
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl import functions
from telethon.tl.tlobject import TLRequest

from tg_mcp.guard import ALLOWED_REQUESTS, ForbiddenRequestError, ReadOnlyTelegramClient


def all_requests() -> set[type]:
    found = set()
    modules = [functions] + [
        importlib.import_module(f"{functions.__name__}.{info.name}")
        for info in pkgutil.iter_modules(functions.__path__)
    ]
    for module in modules:
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, TLRequest) and cls is not TLRequest:
                found.add(cls)
    return found


def run_against_client(request):
    """Send a request through the guard; the parent method records instead of using the network."""
    reached = []

    async def scenario():
        client = ReadOnlyTelegramClient(StringSession(), 1, "x")

        async def record(self, sender, req, ordered=False, flood_sleep_threshold=None):
            reached.append(req)
            return "sent"

        original = TelegramClient._call
        TelegramClient._call = record
        try:
            return await client._call(None, request)
        finally:
            TelegramClient._call = original

    return asyncio.run(scenario()), reached


def test_every_other_telegram_request_is_blocked():
    forbidden = all_requests() - ALLOWED_REQUESTS
    assert len(forbidden) > 500

    for cls in forbidden:
        with pytest.raises(ForbiddenRequestError):
            run_against_client(cls.__new__(cls))


@pytest.mark.parametrize(
    "request_type",
    [
        functions.messages.SendMessageRequest,
        functions.messages.ForwardMessagesRequest,
        functions.messages.DeleteMessagesRequest,
        functions.messages.ReadHistoryRequest,
        functions.channels.JoinChannelRequest,
        functions.contacts.ResolveUsernameRequest,
        functions.account.DeleteAccountRequest,
        functions.account.GetAuthorizationsRequest,
        functions.auth.ResetAuthorizationsRequest,
        functions.upload.SaveFilePartRequest,
    ],
)
def test_dangerous_requests_never_reach_the_network(request_type):
    with pytest.raises(ForbiddenRequestError):
        run_against_client(request_type.__new__(request_type))


def test_a_blocked_request_inside_a_batch_stops_the_whole_batch():
    allowed = functions.updates.GetStateRequest()
    blocked = functions.messages.SendMessageRequest.__new__(functions.messages.SendMessageRequest)
    with pytest.raises(ForbiddenRequestError):
        run_against_client([allowed, blocked])


def test_wrapper_requests_cannot_smuggle_a_write():
    write = functions.messages.SendMessageRequest.__new__(functions.messages.SendMessageRequest)
    wrapped = functions.InvokeWithoutUpdatesRequest(write)
    with pytest.raises(ForbiddenRequestError):
        run_against_client(wrapped)


def test_allowed_request_passes():
    request = functions.updates.GetStateRequest()
    result, reached = run_against_client(request)
    assert result == "sent"
    assert reached == [request]


def test_allowlist_contains_no_mutating_name():
    prefixes = ("Send", "Delete", "Edit", "Forward", "Read", "Join", "Leave", "Save", "Set", "Update")
    assert not [cls.__name__ for cls in ALLOWED_REQUESTS if cls.__name__.startswith(prefixes)]
