import asyncio
import json
import re
from types import SimpleNamespace

import httpx2 as httpx
import pytest
from conftest import ALEX, BOT, FULL_ACCESS, ME, TELEGRAM, at, core_app, make_message
from telethon import types, utils
from telethon.sessions import StringSession

from tg_mcp import sessionstate, units, vault
from tg_mcp.config import ClientPolicy, Config, load_config, load_web_config
from tg_mcp.core import Core
from tg_mcp.events import CursorError, EventBuffer, Subscription, wanted_by

MONITOR = ClientPolicy(name="monitor", chats=frozenset({100}), events=frozenset({"new_message"}), read=True)
CALLS = ClientPolicy(
    name="calls",
    chats=frozenset({100}),
    events=frozenset({"new_message"}),
    text_pattern=re.compile("Пропущенный"),
)


def run(coroutine):
    return asyncio.run(coroutine)


def request(config, client, policy, method, path, **kwargs):
    async def scenario():
        transport = httpx.ASGITransport(app=core_app(config, client, policy))
        async with httpx.AsyncClient(transport=transport, base_url="http://core") as http:
            return await http.request(method, path, **kwargs)

    return run(scenario())


# --- Client rights -------------------------------------------------------------------


def test_client_sees_only_its_chats(config, client):
    other = ClientPolicy(name="other", chats=frozenset({999}), read=True)
    assert request(config, client, other, "GET", "/v1/chats").json()["chats"] == []
    assert [c["chat_id"] for c in request(config, client, MONITOR, "GET", "/v1/chats").json()["chats"]] == [100]


def test_client_cannot_read_outside_its_chats(config, client):
    narrow = ClientPolicy(name="narrow", chats=frozenset({555}), read=True)
    response = request(
        config, client, narrow, "GET", "/v1/chats/100/messages", params={"date_from": "2026-11-05", "date_to": "2026-11-07"}
    )
    assert response.status_code == 400 and "access is closed" in response.json()["error"]


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/v1/chats/100/messages?date_from=2026-11-05&date_to=2026-11-07", None),
        ("GET", "/v1/chats/100/messages/1/image", None),
        ("POST", "/v1/exports", {"chat_id": 100, "date_from": "2026-11-05", "date_to": "2026-11-07"}),
        ("GET", "/v1/events", None),
    ],
)
def test_rights_default_to_denied_and_are_audited(config, client, method, path, body):
    nothing = ClientPolicy(name="nothing", chats=frozenset({100}))
    response = request(config, client, nothing, method, path, json=body)
    assert response.status_code == 403
    entry = json.loads(config.audit_log.read_text().splitlines()[-1])
    assert entry["client"] == "nothing" and entry["outcome"] == "denied"


def test_export_jobs_belong_to_the_client_that_started_them(config, client):
    from tg_mcp.gateway import TelegramGateway
    from tg_mcp.core_api import create_core_app

    async def scenario():
        gateway = TelegramGateway(config, lambda: client)
        buffer = EventBuffer(3600, 100)
        first = create_core_app(gateway, buffer, FULL_ACCESS, config.audit_log)
        second = create_core_app(
            gateway, buffer, ClientPolicy(name="second", chats=None, export=True), config.audit_log
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=first), base_url="http://core") as a, \
                httpx.AsyncClient(transport=httpx.ASGITransport(app=second), base_url="http://core") as b:
            started = await a.post(
                "/v1/exports", json={"chat_id": 100, "date_from": "2026-11-05", "date_to": "2026-11-07"}
            )
            job_id = started.json()["job_id"]
            await asyncio.sleep(0.05)
            return (await b.get(f"/v1/exports/{job_id}")), (await a.get(f"/v1/exports/{job_id}"))

    foreign, own = run(scenario())
    assert foreign.status_code == 400 and own.status_code == 200


def test_per_client_rate_limit(config, client):
    limited = ClientPolicy(name="limited", chats=None, read=True, max_calls_per_minute=1)

    async def scenario():
        transport = httpx.ASGITransport(app=core_app(config, client, limited))
        async with httpx.AsyncClient(transport=transport, base_url="http://core") as http:
            return [(await http.get("/v1/chats")).status_code for _ in range(2)]

    assert run(scenario()) == [200, 400]


def test_internal_errors_are_generic(config, client):
    async def broken():
        raise RuntimeError("secret-detail")
        yield

    client.iter_dialogs = broken
    response = request(config, client, FULL_ACCESS, "GET", "/v1/chats")
    assert response.status_code == 500 and "secret-detail" not in response.text


# --- Event buffer ----------------------------------------------------------------------


def event(chat_id=100, text="hello", kind="new_message"):
    return {"type": kind, "chat_id": chat_id, "chat_name": "x", "message": {"text": text}}


def test_buffer_replays_after_a_cursor():
    buffer = EventBuffer(3600, 100)
    first = buffer.publish(event(text="one"))
    buffer.publish(event(text="two"))
    items, gap = buffer.read_after(first)
    assert [e["message"]["text"] for _, e in items] == ["two"] and not gap
    assert [e["message"]["text"] for _, e in buffer.read_after(None)[0]] == ["one", "two"]


def test_buffer_reports_gaps_after_restart_or_trimming():
    buffer = EventBuffer(3600, 2)
    cursor = buffer.publish(event(text="one"))
    buffer.publish(event(text="two"))
    buffer.publish(event(text="three"))
    assert buffer.read_after(cursor)[1] is False  # "two" is still there: nothing lost yet.
    buffer.publish(event(text="four"))
    assert buffer.read_after(cursor)[1] is True
    assert buffer.read_after("deadbeef-1")[1] is True


def test_buffer_forgets_old_events():
    now = [0.0]
    buffer = EventBuffer(60, 100, clock=lambda: now[0])
    buffer.publish(event(text="old"))
    now[0] = 61
    buffer.publish(event(text="new"))
    assert [e["message"]["text"] for _, e in buffer.read_after(None)[0]] == ["new"]


@pytest.mark.parametrize("cursor", ["garbage", "abc-x", "zz-"])
def test_malformed_cursor_is_rejected(cursor):
    with pytest.raises(CursorError):
        EventBuffer(3600, 10).read_after(cursor)


def test_cursor_from_the_future_is_rejected():
    buffer = EventBuffer(3600, 10)
    with pytest.raises(CursorError):
        buffer.read_after(f"{buffer.boot}-99")


def test_event_filter_applies_chats_types_and_pattern():
    assert wanted_by(MONITOR, event())
    assert not wanted_by(MONITOR, event(chat_id=200))
    assert not wanted_by(MONITOR, event(kind="edited"))
    assert wanted_by(CALLS, event(text="Пропущенный звонок от +380..."))
    assert not wanted_by(CALLS, event(text="Привет"))
    deleted = {"type": "new_message", "chat_id": 100, "chat_name": "x"}
    assert not wanted_by(CALLS, deleted)


# --- Telegram updates turned into events -----------------------------------------------


def make_core(config):
    secrets = vault.Secrets(api_id=1, api_hash="x", session="")
    return Core(config, bytes(32), secrets)


def update(chat_id, chat, text="hello", sender=ALEX):
    return SimpleNamespace(chat_id=chat_id, chat=chat, message=make_message(1, at(6), text, sender=sender))


def test_updates_become_events_only_for_allowed_chats(config):
    config = Config(**{**config.__dict__, "clients": {"monitor": MONITOR}})

    async def scenario():
        core = make_core(config)
        core.buffer.subscribe(Subscription(ClientPolicy(name="all", chats=None, events=MONITOR.events)))
        await core._on_message(update(100, ALEX, "нужное"))
        await core._on_message(update(777000, TELEGRAM, "Login code: 12345", sender=TELEGRAM))
        await core._on_message(update(200, BOT, "bot", sender=BOT))
        await core._on_message(update(400, ME, "saved", sender=ME))
        return core.buffer.read_after(None)[0]

    published = run(scenario())
    assert [e["message"]["text"] for _, e in published] == ["нужное"]


def test_nothing_is_kept_while_nobody_listens_to_a_chat(config):
    everyone = ClientPolicy(name="parser", chats=None, events=frozenset({"new_message"}))
    config = Config(**{**config.__dict__, "clients": {"parser": everyone}})

    async def scenario():
        core = make_core(config)
        await core._on_message(update(100, ALEX, "before anyone listens"))
        listening = Subscription(everyone, chats=frozenset({100}))
        core.buffer.subscribe(listening)
        await core._on_message(update(100, ALEX, "while listening"))
        await core._on_message(update(101, ALEX, "another chat"))
        core.buffer.unsubscribe(listening)
        await core._on_message(update(100, ALEX, "after disconnect"))
        return core.buffer.read_after(None)[0]

    assert [e["message"]["text"] for _, e in run(scenario())] == ["while listening"]


def test_config_text_pattern_still_limits_what_is_kept(config):
    config = Config(**{**config.__dict__, "clients": {"calls": CALLS}})

    async def scenario():
        core = make_core(config)
        core.buffer.subscribe(Subscription(CALLS))
        await core._on_message(update(100, ALEX, "обычное сообщение"))
        await core._on_message(update(100, ALEX, "Пропущенный звонок"))
        return core.buffer.read_after(None)[0]

    assert [e["message"]["text"] for _, e in run(scenario())] == ["Пропущенный звонок"]


def test_deletions_without_a_known_chat_are_dropped(config):
    everything = ClientPolicy(name="all", chats=None, events=frozenset({"deleted"}))
    config = Config(**{**config.__dict__, "clients": {"all": everything}})

    async def scenario():
        core = make_core(config)
        core.buffer.subscribe(Subscription(everything))
        await core._on_deleted(SimpleNamespace(chat_id=None, deleted_ids=[5]))
        return core.buffer.read_after(None)[0]

    assert run(scenario()) == []


# --- Update position persistence --------------------------------------------------------


def test_update_position_round_trip_keeps_no_personal_data():
    session = StringSession()
    session.set_update_state(0, types.updates.State(10, 2, at(6), 3, 0))
    session.set_update_state(555, types.updates.State(77, 0, at(6), 0, 0))
    channel_peer = utils.get_peer_id(types.PeerChannel(555))
    session._entities |= {
        (0, 400, None, None, None),
        (400, 4000, "me", "380500000000", "Me"),
        (channel_peer, 5550, "channel", None, "Channel"),
        (100, 1000, "alex", "380511111111", "Alex"),
    }

    saved = sessionstate.capture(session)
    text = json.dumps(saved)
    assert "380" not in text and "alex" not in text and "Alex" not in text

    restored = sessionstate.restore("", saved)
    states = dict(restored.get_update_states())
    assert states[0].pts == 10 and states[555].pts == 77
    assert restored.get_input_entity(555).access_hash == 5550


def test_vault_keeps_the_update_position(tmp_path):
    secrets = vault.Secrets(1, "hash", "session", {"states": {"0": [1, 2, 3, 4]}, "entities": []})
    vault.save(tmp_path / "vault.bin", bytes(32), secrets)
    assert vault.load(tmp_path / "vault.bin", bytes(32)).updates == secrets.updates


# --- Settings and generated sockets ------------------------------------------------------


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_client_settings_are_parsed(tmp_path):
    config = load_config(
        write(
            tmp_path,
            '[clients.missed-calls]\nchats = [-100123]\nevents = ["new_message"]\n'
            'text_pattern = "Пропущенный"\n\n[clients.claude]\nchats = "all"\nread = true\n',
        )
    )
    calls = config.clients["missed-calls"]
    assert calls.chats == frozenset({-100123}) and not calls.read and calls.text_pattern
    assert config.clients["claude"].chats is None and config.clients["claude"].read


def test_chats_may_be_left_to_the_script(tmp_path):
    config = load_config(write(tmp_path, "[clients.parser]\nevents = true\nread = true\n"))
    parser = config.clients["parser"]
    assert parser.chats is None and parser.events == {"new_message", "edited", "deleted"}


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ('[clients.x]\nevents = "yes"\n', "true, false or a list"),
        ("[clients.x]\nchats = []\n", "non-empty"),
        ('[clients.x]\nchats = "everything"\n', "non-empty"),
        ('[clients.x]\nchats = [1]\nevents = ["send"]\n', "unknown events"),
        ('[clients.x]\nchats = [1]\nsocket_mode = "0666"\n', "never open to others"),
        ('[clients.x]\nchats = [1]\nsocket_user = "root; rm -rf /"\n', "invalid socket owner"),
        ('[clients.x]\nchats = [1]\nwrite = true\n', "Unknown settings"),
        ('[clients."Bad Name"]\nchats = [1]\n', "lowercase"),
        ('[clients.x]\nchats = [1]\nread = "yes"\n', "true or false"),
    ],
)
def test_invalid_client_settings_are_refused(tmp_path, text, error):
    with pytest.raises(ValueError, match=error):
        load_config(write(tmp_path, text))


def test_web_settings_reject_core_keys(tmp_path):
    path = tmp_path / "web.toml"
    path.write_text('public_url = "https://x.example"\nvault_path = "/tmp/steal"\n')
    with pytest.raises(ValueError, match="Unknown settings"):
        load_web_config(path)


def test_socket_units_are_generated_per_client(tmp_path):
    config = load_config(
        write(
            tmp_path,
            '[clients.claude]\nchats = "all"\nsocket_user = "tgmcp-web"\nsocket_group = "tgmcp-web"\n\n'
            '[clients.missed-calls]\nchats = [1]\nevents = ["new_message"]\nsocket_mode = "0660"\n',
        )
    )
    files = units.render(config)
    claude = files["tg-core-claude.socket"]
    assert "ListenStream=/run/tg-core/claude/core.sock" in claude
    assert "FileDescriptorName=claude" in claude and "SocketUser=tgmcp-web" in claude
    assert "SocketMode=0660" in files["tg-core-missed-calls.socket"]
    assert "Sockets=tg-core-claude.socket tg-core-missed-calls.socket" in files["tg-core.service.d/sockets.conf"]


def test_core_saves_its_update_position_into_the_vault(config, tmp_path):
    config = Config(**{**config.__dict__, "vault_path": tmp_path / "vault.bin"})
    key = bytes(range(32))

    async def scenario():
        core = make_core(config)
        core._key = key
        core.client.session.set_update_state(0, types.updates.State(42, 1, at(6), 2, 0))
        await core.persist()

    run(scenario())
    saved = vault.load(tmp_path / "vault.bin", key)
    assert saved.updates["states"]["0"][0] == 42 and saved.api_id == 1


@pytest.mark.parametrize(
    ("query", "status"),
    [("chats=200", 403), ("types=edited", 403), ("chats=abc", 400)],
)
def test_a_stream_cannot_subscribe_beyond_its_rights(config, client, query, status):
    response = request(config, client, CALLS, "GET", f"/v1/events?{query}")
    assert response.status_code == status
