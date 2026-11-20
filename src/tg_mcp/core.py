"""The Telegram core: the only process that holds the session and talks to Telegram.

It stays connected, turns updates into events for local clients, and serves each
client on its own systemd-provided socket. It never opens a listening port itself.
"""

import asyncio
import logging
import os
import signal
import sys

import uvicorn
from telethon import events

from . import sessionstate, vault
from .config import Config, load_config
from .core_api import create_core_app
from .events import EventBuffer
from .gateway import TelegramGateway, serialize_message
from .guard import ReadOnlyTelegramClient
from .process import disable_memory_dumps

log = logging.getLogger("tg_mcp")

SYSTEMD_FIRST_SOCKET_FD = 3
PERSIST_INTERVAL_SECONDS = 300
MAX_RECONNECT_DELAY_SECONDS = 300


def systemd_sockets() -> dict[str, int]:
    """Map client names to the listening sockets systemd passed in."""
    if os.environ.get("LISTEN_PID") != str(os.getpid()):
        raise SystemExit("Start through systemd: this core never opens a socket itself")
    count = int(os.environ.get("LISTEN_FDS", "0"))
    names = os.environ.get("LISTEN_FDNAMES", "").split(":")
    if count == 0 or len(names) != count:
        raise SystemExit("systemd passed no named client sockets")
    return {name: SYSTEMD_FIRST_SOCKET_FD + index for index, name in enumerate(names)}


class Core:
    def __init__(self, config: Config, key: bytes, secrets: vault.Secrets):
        self.config = config
        self._key = key
        self._api_id = secrets.api_id
        self._api_hash = secrets.api_hash
        self.client = ReadOnlyTelegramClient(
            sessionstate.restore(secrets.session, secrets.updates),
            secrets.api_id,
            secrets.api_hash,
            receive_updates=True,
            catch_up=True,
            flood_sleep_threshold=60,
        )
        self.gateway = TelegramGateway(config, lambda: self.client)
        self.buffer = EventBuffer(config.event_retention_hours * 3600, config.max_buffered_events)
        self._persist_lock = asyncio.Lock()
        self.client.add_event_handler(self._on_message, events.NewMessage())
        self.client.add_event_handler(self._on_message, events.MessageEdited())
        self.client.add_event_handler(self._on_deleted, events.MessageDeleted())

    def _publish(self, event: dict) -> None:
        # Nothing is kept unless a connected client is listening to this chat right now.
        if self.buffer.wanted(event):
            self.buffer.publish(event)

    async def _on_message(self, event) -> None:
        try:
            kind = "edited" if isinstance(event, events.MessageEdited.Event) else "new_message"
            name = await self.gateway.event_chat_name(event.chat_id, event.chat)
            if name is not None:
                self._publish(
                    {
                        "type": kind,
                        "chat_id": event.chat_id,
                        "chat_name": name,
                        "message": serialize_message(event.message),
                    }
                )
        except Exception as exc:
            log.error("Could not process a message update: %s", type(exc).__name__)

    async def _on_deleted(self, event) -> None:
        # Telegram names the chat only for channels and supergroups; other deletions
        # cannot be attributed to a chat, so no client may receive them.
        try:
            if event.chat_id is None:
                return
            name = await self.gateway.event_chat_name(event.chat_id, None)
            if name is not None:
                self._publish(
                    {
                        "type": "deleted",
                        "chat_id": event.chat_id,
                        "chat_name": name,
                        "message_ids": list(event.deleted_ids),
                    }
                )
        except Exception as exc:
            log.error("Could not process a deletion update: %s", type(exc).__name__)

    async def persist(self) -> None:
        """Save the update position so a restart catches up instead of losing events."""
        async with self._persist_lock:
            try:
                if self.client.is_connected():
                    await self.client._save_states_and_entities()
                secrets = vault.Secrets(
                    self._api_id,
                    self._api_hash,
                    self.client.session.save(),
                    sessionstate.capture(self.client.session),
                )
                vault.save(self.config.vault_path, self._key, secrets)
            except Exception as exc:
                log.error("Could not save the update position: %s", type(exc).__name__)

    async def keep_connected(self) -> None:
        delay = 5
        while True:
            try:
                client = await self.gateway.connect()
                delay = 5
                await client.disconnected
                log.warning("Telegram disconnected; reconnecting")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("Telegram connection failed: %s", type(exc).__name__)
            await asyncio.sleep(delay)
            delay = min(delay * 2, MAX_RECONNECT_DELAY_SECONDS)

    async def persist_periodically(self) -> None:
        while True:
            await asyncio.sleep(PERSIST_INTERVAL_SECONDS)
            await self.persist()


async def serve(core: Core, sockets: dict[str, int]) -> None:
    unknown = set(sockets) - set(core.config.clients)
    if unknown:
        raise SystemExit(f"Sockets for unconfigured clients: {', '.join(sorted(unknown))}")

    servers = []
    for name, fd in sockets.items():
        app = create_core_app(
            core.gateway, core.buffer, core.config.clients[name], core.config.audit_log, core.client.is_connected
        )
        servers.append(
            uvicorn.Server(
                uvicorn.Config(
                    app,
                    fd=fd,
                    loop="asyncio",
                    http="h11",
                    ws="none",
                    lifespan="off",
                    access_log=False,
                    log_level="warning",
                    proxy_headers=False,
                    server_header=False,
                    date_header=False,
                    timeout_graceful_shutdown=3,
                )
            )
        )

    background = [
        asyncio.create_task(core.keep_connected()),
        asyncio.create_task(core.persist_periodically()),
    ]
    serving = [asyncio.create_task(server.serve()) for server in servers]

    # Each uvicorn server captures signals for itself; once all have started, one handler
    # stops them all together.
    while not all(server.started for server in servers):
        if any(task.done() for task in serving):
            break
        await asyncio.sleep(0.05)

    def stop() -> None:
        for server in servers:
            server.should_exit = True

    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop)

    try:
        await asyncio.gather(*serving)
    finally:
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await core.persist()
        await core.client.disconnect()


def main() -> None:
    disable_memory_dumps()
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    sockets = systemd_sockets()
    config = load_config()
    key = vault.load_key()
    core = Core(config, key, vault.load(config.vault_path, key))
    core.gateway.purge_old_exports()
    asyncio.run(serve(core, sockets))


if __name__ == "__main__":
    main()
