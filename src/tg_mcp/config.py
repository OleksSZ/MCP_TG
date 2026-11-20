"""Settings, read from root-owned files that no client can change.

config.toml configures the Telegram core and the clients allowed to use it.
web.toml configures the public MCP endpoint for Claude, which has no Telegram access.
"""

import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("/etc/tg-mcp/config.toml")
DEFAULT_WEB_CONFIG_PATH = Path("/etc/tg-mcp/web.toml")

EVENT_TYPES = frozenset({"new_message", "edited", "deleted"})
CLIENT_NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
SOCKET_MODE = re.compile(r"^0[0-7]{3}$")
SOCKET_OWNER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$|^[0-9]{1,10}$")


@dataclass(frozen=True)
class ClientPolicy:
    """What one local client may do. Everything defaults to denied."""

    name: str
    # None means every chat the core can reach; otherwise only these ids.
    chats: frozenset[int] | None
    events: frozenset[str] = frozenset()
    read: bool = False
    media: bool = False
    export: bool = False
    # Global search, contact cards and phone lookup across every chat the client may see.
    search: bool = False
    # When set, message events are delivered only if their text matches.
    text_pattern: re.Pattern | None = None
    max_calls_per_minute: int = 60
    socket_user: str = "root"
    socket_group: str = "root"
    socket_mode: str = "0600"

    def allows_chat(self, chat_id: int) -> bool:
        return self.chats is None or chat_id in self.chats


@dataclass(frozen=True)
class Config:
    # Public HTTPS origin, used only to show download commands for exports.
    public_url: str = ""
    vault_path: Path = Path("/var/lib/tg-mcp/private/vault.bin")
    export_dir: Path = Path("/var/lib/tg-mcp/exports")
    audit_log: Path = Path("/var/lib/tg-mcp/audit.log")
    max_image_mb: int = 3
    # Global rules, applied before any client rule. Use ids shown by find_chats.
    allowed_chat_ids: frozenset[int] = frozenset()
    denied_chat_ids: frozenset[int] = frozenset()
    block_saved_messages: bool = True
    max_range_days: int = 92
    max_page_messages: int = 200
    max_export_messages: int = 20_000
    max_file_mb: int = 100
    max_export_mb: int = 500
    export_retention_hours: int = 24
    max_calls_per_minute: int = 120
    max_messages_per_hour: int = 50_000
    # Live events are kept in RAM only, for this long and up to this many.
    event_retention_hours: int = 24
    max_buffered_events: int = 100_000
    clients: dict[str, ClientPolicy] = field(default_factory=dict)


@dataclass(frozen=True)
class WebConfig:
    # Public HTTPS origin of the MCP endpoint, with no path, e.g. https://mcp.example.com
    public_url: str = ""
    core_socket: Path = Path("/run/tg-core/claude/core.sock")
    oauth_state_path: Path = Path("/var/lib/tg-mcp-web/oauth.json")
    pairing_path: Path = Path("/var/lib/tg-mcp-web/pairing.json")
    # After this many days Claude must be reconnected with a new pairing code.
    session_days: int = 30


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as file:
        return tomllib.load(file)


def _check_keys(raw: dict, allowed: set[str], where: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"Unknown settings in {where}: {', '.join(sorted(unknown))}")


def _check_positive(config) -> None:
    for item in fields(config):
        value = getattr(config, item.name)
        if item.type is int and (type(value) is not int or value <= 0):
            raise ValueError(f"{item.name} must be a positive integer")


def _parse_client(name: str, raw: dict) -> ClientPolicy:
    if not CLIENT_NAME.match(name):
        raise ValueError(f"Client name {name!r} must be lowercase letters, digits and dashes")
    _check_keys(raw, {f.name for f in fields(ClientPolicy)} - {"name"}, f"[clients.{name}]")
    # Without chats the project may subscribe to any chat it names, except the globally
    # closed ones (bots, Telegram service chats, Saved Messages, denied_chat_ids).
    chats = raw.get("chats", "all")
    if chats == "all":
        chat_ids = None
    elif isinstance(chats, list) and chats and all(type(c) is int for c in chats):
        chat_ids = frozenset(chats)
    else:
        raise ValueError(f"[clients.{name}] chats must be \"all\" or a non-empty list of chat ids")

    raw_events = raw.get("events", [])
    if raw_events is True:
        events = EVENT_TYPES
    elif raw_events is False:
        events = frozenset()
    elif isinstance(raw_events, list) and all(isinstance(e, str) for e in raw_events):
        events = frozenset(raw_events)
    else:
        raise ValueError(f"[clients.{name}] events must be true, false or a list")
    if events - EVENT_TYPES:
        raise ValueError(f"[clients.{name}] unknown events: {', '.join(sorted(events - EVENT_TYPES))}")

    pattern = raw.get("text_pattern")
    policy = ClientPolicy(
        name=name,
        chats=chat_ids,
        events=events,
        read=raw.get("read", False),
        media=raw.get("media", False),
        export=raw.get("export", False),
        search=raw.get("search", False),
        text_pattern=re.compile(pattern) if pattern else None,
        max_calls_per_minute=raw.get("max_calls_per_minute", 60),
        socket_user=str(raw.get("socket_user", "root")),
        socket_group=str(raw.get("socket_group", "root")),
        socket_mode=raw.get("socket_mode", "0600"),
    )
    for flag in ("read", "media", "export", "search"):
        if type(getattr(policy, flag)) is not bool:
            raise ValueError(f"[clients.{name}] {flag} must be true or false")
    if type(policy.max_calls_per_minute) is not int or policy.max_calls_per_minute <= 0:
        raise ValueError(f"[clients.{name}] max_calls_per_minute must be a positive integer")
    if not SOCKET_MODE.match(policy.socket_mode) or int(policy.socket_mode, 8) & 0o007:
        raise ValueError(f"[clients.{name}] socket_mode must be like 0600 or 0660, never open to others")
    for owner in (policy.socket_user, policy.socket_group):
        if not SOCKET_OWNER.match(owner):
            raise ValueError(f"[clients.{name}] invalid socket owner {owner!r}")
    return policy


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    raw = _read(path)
    _check_keys(raw, {f.name for f in fields(Config)}, str(path))

    values = dict(raw)
    for name in ("vault_path", "export_dir", "audit_log"):
        if name in values:
            values[name] = Path(values[name])
    for name in ("allowed_chat_ids", "denied_chat_ids"):
        if name in values:
            values[name] = frozenset(int(chat_id) for chat_id in values[name])
    values["clients"] = {
        name: _parse_client(name, client) for name, client in raw.get("clients", {}).items()
    }

    config = Config(**values)
    _check_positive(config)
    return config


def load_web_config(path: Path = DEFAULT_WEB_CONFIG_PATH) -> WebConfig:
    raw = _read(path)
    _check_keys(raw, {f.name for f in fields(WebConfig)}, str(path))
    values = dict(raw)
    for name in ("core_socket", "oauth_state_path", "pairing_path"):
        if name in values:
            values[name] = Path(values[name])
    config = WebConfig(**values)
    _check_positive(config)
    return config
