import asyncio
import json

from conftest import at, core_client, make_message
from mcp.client import Client

from tg_mcp.server import build_server


def with_client(config, client, scenario):
    async def main():
        server = build_server(core_client(config, client))
        async with Client(server) as session:
            return await scenario(session)

    return asyncio.run(main())


def test_exposes_exactly_the_read_only_tools(config, client):
    tools = with_client(config, client, lambda session: session.list_tools()).tools
    assert {tool.name for tool in tools} == {
        "find_chats", "read_messages", "get_media", "export_chat", "get_export_status",
    }
    assert all(tool.annotations.read_only_hint and not tool.annotations.destructive_hint for tool in tools)


def test_full_flow_finds_a_chat_and_reads_it(config, client):
    async def scenario(session):
        found = await session.call_tool("find_chats", {"query": "Александр"})
        chat_id = json.loads(found.content[0].text)["chats"][0]["chat_id"]
        return await session.call_tool(
            "read_messages", {"chat_id": chat_id, "date_from": "2026-11-05", "date_to": "2026-11-07"}
        )

    result = with_client(config, client, scenario)
    assert not result.is_error
    assert [m["text"] for m in json.loads(result.content[0].text)["messages"]] == ["четыре", "три", "два"]


def test_login_code_chat_is_unreachable_through_mcp(config, client):
    async def scenario(session):
        return await session.call_tool(
            "read_messages", {"chat_id": 777000, "date_from": "2026-11-05", "date_to": "2026-11-07"}
        )

    result = with_client(config, client, scenario)
    assert result.is_error
    assert "12345" not in result.content[0].text


def test_invalid_arguments_are_rejected_by_the_schema(config, client):
    async def scenario(session):
        return await session.call_tool("find_chats", {"query": "x" * 101})

    assert with_client(config, client, scenario).is_error


def test_internal_errors_do_not_leak_details(config, client):
    async def broken():
        raise RuntimeError("secret-token-abc")
        yield

    client.iter_dialogs = broken

    async def scenario(session):
        return await session.call_tool("find_chats", {})

    result = with_client(config, client, scenario)
    assert result.is_error
    assert "secret-token-abc" not in result.content[0].text
    assert "secret-token-abc" not in config.audit_log.read_text()


def test_audit_log_has_metadata_but_no_content(config, client):
    client.messages[100] = [make_message(1, at(6), "очень личный текст")]

    async def scenario(session):
        await session.call_tool("find_chats", {"query": "тайный запрос"})
        await session.call_tool(
            "read_messages", {"chat_id": 100, "date_from": "2026-11-06", "date_to": "2026-11-06"}
        )

    with_client(config, client, scenario)
    log = config.audit_log.read_text()
    assert "очень личный текст" not in log and "тайный запрос" not in log

    entries = [json.loads(line) for line in log.splitlines()]
    assert entries[-1]["action"] == "read_messages" and entries[-1]["client"] == "claude"
    assert entries[-1]["chat_id"] == 100 and entries[-1]["message_count"] == 1
