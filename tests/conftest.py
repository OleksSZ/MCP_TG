from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx2 as httpx
import pytest
from telethon import types, utils

from tg_mcp.config import ClientPolicy, Config, WebConfig
from tg_mcp.core_api import create_core_app
from tg_mcp.core_client import CoreClient
from tg_mcp.events import EventBuffer
from tg_mcp.gateway import TelegramGateway

FULL_ACCESS = ClientPolicy(name="claude", chats=None, read=True, media=True, export=True)

ALEX = types.User(id=100, first_name="Александр", username="alex")
BOT = types.User(id=200, first_name="Helper", bot=True)
TELEGRAM = types.User(id=777000, first_name="Telegram")
SUPPORT = types.User(id=300, first_name="Volunteer", support=True)
ME = types.User(id=400, first_name="Me", is_self=True)


def at(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 11, day, hour, tzinfo=UTC)


def make_message(message_id, date, text="", sender=ALEX, **media):
    defaults = dict(
        media=None, file=None, photo=None, voice=None, video_note=None, gif=None,
        sticker=None, video=None, audio=None, document=None,
    )
    return SimpleNamespace(
        id=message_id, date=date, message=text, sender=sender, sender_id=sender.id, out=False,
        reply_to_msg_id=None, fwd_from=None, action=None, **{**defaults, **media},
    )


def make_photo(message_id, date, size=10, ext=".jpg", name=None):
    file = SimpleNamespace(ext=ext, size=size, name=name, mime_type="image/jpeg")
    return make_message(message_id, date, media=object(), photo=object(), file=file)


def _matches(message, search, filter) -> bool:
    if search and search not in message.message:
        return False
    if filter is not None and not isinstance(message.media, types.MessageMediaContact):
        return False
    return True


class FakeClient:
    """Implements only what the gateway uses. Messages are stored newest first."""

    def __init__(self, dialogs, messages=None):
        self.dialogs = dialogs
        self.messages = messages or {}
        self.connected = False
        self.downloads = []

    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connected = True

    async def is_user_authorized(self):
        return True

    async def iter_dialogs(self):
        for dialog in self.dialogs:
            yield dialog

    async def iter_messages(self, entity, limit=None, offset_id=None, offset_date=None, search=None, filter=None):
        if entity is None:  # Global search: every chat, newest first.
            found = [
                message
                for chat in self.dialogs
                for message in self.messages.get(chat.id, [])
                if _matches(message, search, filter)
            ]
            for message in sorted(found, key=lambda m: m.date, reverse=True)[:limit]:
                yield message
            return
        returned = 0
        for message in self.messages.get(utils.get_peer_id(entity), []):
            if offset_id and message.id >= offset_id:
                continue
            if offset_date and message.date >= offset_date:
                continue
            if not _matches(message, search, filter):
                continue
            if limit is not None and returned >= limit:
                return
            returned += 1
            yield message

    async def get_messages(self, entity, ids):
        return next((m for m in self.messages.get(utils.get_peer_id(entity), []) if m.id == ids), None)

    async def download_media(self, message, file):
        self.downloads.append(message.id)
        file.write(getattr(message, "content", b"x" * (message.file.size or 0)))
        return file


def dialog(chat_id, entity, name):
    return SimpleNamespace(id=chat_id, entity=entity, name=name, date=at(9))


@pytest.fixture
def config(tmp_path):
    return Config(
        public_url="https://mcp.example.com",
        export_dir=tmp_path / "exports",
        audit_log=tmp_path / "audit.log",
        clients={"claude": FULL_ACCESS},
    )


@pytest.fixture
def web_config(tmp_path):
    return WebConfig(
        public_url="https://mcp.example.com",
        core_socket=tmp_path / "core.sock",
        oauth_state_path=tmp_path / "oauth.json",
        pairing_path=tmp_path / "pairing.json",
    )


def core_app(config, client, policy=FULL_ACCESS, buffer=None):
    gateway = TelegramGateway(config, lambda: client)
    return create_core_app(gateway, buffer or EventBuffer(3600, 1000), policy, config.audit_log)


def core_client(config, client, policy=FULL_ACCESS, buffer=None):
    """A CoreClient talking to a real core API in memory, as the MCP endpoint does."""
    app = core_app(config, client, policy, buffer)
    return CoreClient(Path("/unused"), transport=httpx.ASGITransport(app=app))


class ChatAwareClient(FakeClient):
    """Like Telethon, messages know their chat."""

    def __init__(self, dialogs, messages=None):
        super().__init__(dialogs, messages)
        self._chats = {d.id: d.entity for d in dialogs}

    async def iter_messages(self, entity, *args, **kwargs):
        async for message in super().iter_messages(entity, *args, **kwargs):
            chat_id = utils.get_peer_id(entity) if entity is not None else self._chat_of(message)
            message.chat_id, message.chat = chat_id, self._chats.get(chat_id)
            yield message

    def _chat_of(self, message):
        return next(chat_id for chat_id, items in self.messages.items() if message in items)


@pytest.fixture
def client():
    return ChatAwareClient(
        dialogs=[
            dialog(100, ALEX, "Александр"),
            dialog(200, BOT, "Helper"),
            dialog(777000, TELEGRAM, "Telegram"),
            dialog(300, SUPPORT, "Volunteer"),
            dialog(400, ME, "Saved Messages"),
        ],
        messages={
            100: [
                make_message(5, at(9), "после периода"),
                make_message(4, at(7), "четыре"),
                make_message(3, at(6), "три"),
                make_message(2, at(5), "два"),
                make_message(1, at(1), "до периода"),
            ],
            777000: [make_message(9, at(6), "Login code: 12345", sender=TELEGRAM)],
            200: [make_message(8, at(6), "bot secret", sender=BOT)],
        },
    )
