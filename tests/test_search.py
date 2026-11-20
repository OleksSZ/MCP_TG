"""Search, contact cards and phone lookup: the phone check of the missed-calls project."""

from conftest import ALEX, BOT, at, core_client, make_message
from telethon import types, utils

from tg_mcp.config import ClientPolicy

PARSER = ClientPolicy(name="parser", chats=None, read=True, search=True)
READ_ONLY = ClientPolicy(name="reader", chats=None, read=True)

GROUP = types.Channel(
    id=555, title="Dispatch", photo=types.ChatPhotoEmpty(), date=at(1), megagroup=True, username="dispatch"
)
GROUP_ID = utils.get_peer_id(GROUP)


def card(message_id, phone, name, date):
    message = make_message(message_id, date, "")
    message.media = types.MessageMediaContact(phone_number=phone, first_name=name, last_name="", vcard="", user_id=0)
    return message


def setup(client):
    from conftest import dialog

    client.dialogs.append(dialog(GROUP_ID, GROUP, "Dispatch"))
    client._chats[GROUP_ID] = GROUP
    client.messages[GROUP_ID] = [
        make_message(12, at(8), "Missed call from 18882557090 to your number"),
        make_message(11, at(7), "call 888 255 7090 please"),
        card(10, "+1 (888) 255-7090", "John", at(6)),
    ]
    client.messages[200] = [make_message(20, at(8), "bot says 18882557090", sender=BOT)]


def get(config, client, path, policy=PARSER, **params):
    async def scenario():
        async with core_client(config, client, policy)._http as http:
            return await http.get(path, params=params)

    import asyncio

    return asyncio.run(scenario())


def test_global_search_finds_mentions_with_links_and_skips_bots(config, client):
    setup(client)
    response = get(config, client, "/v1/search", q="18882557090")
    assert response.status_code == 200
    results = response.json()["results"]
    assert [(r["chat_id"], r["message"]["id"]) for r in results] == [(GROUP_ID, 12)]
    assert results[0]["link"] == "https://t.me/dispatch/12" and results[0]["chat_type"] == "group"


def test_search_inside_one_chat_pages_newest_first(config, client):
    setup(client)
    first = get(config, client, f"/v1/chats/{GROUP_ID}/search", q="call", limit=1).json()
    assert [m["id"] for m in first["messages"]] == [12] and first["next_before_id"] == 12
    rest = get(config, client, f"/v1/chats/{GROUP_ID}/search", q="call", limit=5, before_id=12).json()
    assert [m["id"] for m in rest["messages"]] == [11] and rest["next_before_id"] is None


def test_contact_cards_carry_the_phone_digits(config, client):
    setup(client)
    results = get(config, client, "/v1/search/contacts").json()["results"]
    assert [r["message"]["media"] for r in results] == [
        {"type": "contact", "phone": "18882557090", "first_name": "John", "last_name": ""}
    ]


def test_phone_lookup_answers_only_for_the_number_asked(config, client):
    ALEX.phone = "380501112233"
    try:
        found = get(config, client, "/v1/phone/0501112233").json()
        assert [c["chat_id"] for c in found["chats"]] == [100]
        assert "380501112233" not in str(found)  # The number itself is never echoed back.
        assert get(config, client, "/v1/phone/0509999999").json()["chats"] == []
        assert get(config, client, "/v1/phone/123").status_code == 400
    finally:
        ALEX.phone = None


def test_search_needs_its_own_right(config, client):
    setup(client)
    assert get(config, client, "/v1/search", policy=READ_ONLY, q="1888").status_code == 403
    assert get(config, client, "/v1/search/contacts", policy=READ_ONLY).status_code == 403
    assert get(config, client, "/v1/phone/0501112233", policy=READ_ONLY).status_code == 403
    no_read = ClientPolicy(name="x", chats=None, search=True)
    assert get(config, client, f"/v1/chats/{GROUP_ID}/search", policy=no_read, q="call").status_code == 403


def test_a_chat_limit_in_the_config_also_limits_search(config, client):
    setup(client)
    only_alex = ClientPolicy(name="x", chats=frozenset({100}), read=True, search=True)
    assert get(config, client, "/v1/search", policy=only_alex, q="18882557090").json()["results"] == []
    assert get(config, client, f"/v1/chats/{GROUP_ID}/search", policy=only_alex, q="call").status_code == 400


def test_search_queries_are_not_written_to_the_audit_log(config, client):
    setup(client)
    get(config, client, "/v1/search", q="18882557090")
    get(config, client, f"/v1/chats/{GROUP_ID}/search", q="18882557090")
    get(config, client, "/v1/phone/18882557090")
    assert "8882557090" not in config.audit_log.read_text()
