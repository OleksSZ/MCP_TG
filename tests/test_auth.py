import asyncio
import base64
import hashlib
import json
import stat
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import httpx2 as httpx
import pytest

from conftest import core_client
from tg_mcp.config import Config
from tg_mcp.pairing import MAX_ATTEMPTS, PairingStore
from tg_mcp.server import create_http_app, validate_public_url

ORIGIN = "https://mcp.example.com"
RESOURCE = f"{ORIGIN}/mcp"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2025-11-25",
}


def with_app(web_config, client, scenario):
    async def main():
        state = web_config.oauth_state_path.parent
        config = Config(public_url=ORIGIN, export_dir=state / "exports", audit_log=state / "audit.log")
        app = create_http_app(core_client(config, client), web_config)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as http:
                return await scenario(http)

    return asyncio.run(main())


async def register(http, **overrides):
    body = {
        "redirect_uris": [CALLBACK],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "client_name": "Claude",
        **overrides,
    }
    return await http.post("/register", json=body)


async def start_authorize(http, client_id, resource=RESOURCE, redirect=CALLBACK):
    return await http.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": redirect,
            "response_type": "code",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "state": "state-value",
            "scope": "telegram.read",
            "resource": resource,
        },
    )


async def connect(http, web_config):
    """Run the whole sign-in as Claude and the owner would. Returns the client id and tokens."""
    client_id = (await register(http)).json()["client_id"]
    authorize = await start_authorize(http, client_id)
    request_id = parse_qs(urlsplit(authorize.headers["location"]).query)["request"][0]
    code = PairingStore(web_config.pairing_path).create()
    approved = await http.post("/pair", data={"request": request_id, "code": code})
    assert approved.status_code == 303
    query = parse_qs(urlsplit(approved.headers["location"]).query)
    assert approved.headers["location"].startswith(CALLBACK) and query["state"] == ["state-value"]
    tokens = await http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": query["code"][0],
            "client_id": client_id,
            "code_verifier": VERIFIER,
            "redirect_uri": CALLBACK,
            "resource": RESOURCE,
        },
    )
    assert tokens.status_code == 200, tokens.text
    return client_id, tokens.json()


async def refresh(http, client_id, token):
    return await http.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": token,
            "client_id": client_id,
            "resource": RESOURCE,
        },
    )


async def list_tools(http, access_token, host=None):
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {access_token}"}
    if host:
        headers["Host"] = host
    return await http.post(
        "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )


def test_unauthenticated_requests_get_401_with_a_discovery_pointer(web_config, client):
    async def scenario(http):
        response = await http.post(
            "/mcp", headers=MCP_HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        assert response.status_code == 401
        assert f'resource_metadata="{ORIGIN}/.well-known/oauth-protected-resource/mcp"' in response.headers[
            "www-authenticate"
        ]
        assert "telegram" not in response.text

    with_app(web_config, client, scenario)


def test_discovery_metadata_meets_claude_requirements(web_config, client):
    async def scenario(http):
        resource = (await http.get("/.well-known/oauth-protected-resource/mcp")).json()
        assert resource["resource"] == RESOURCE
        assert resource["authorization_servers"][0].rstrip("/") == ORIGIN
        assert resource["scopes_supported"] == ["telegram.read"]

        server = (await http.get("/.well-known/oauth-authorization-server")).json()
        assert server["issuer"].rstrip("/") == ORIGIN
        assert server["registration_endpoint"] == f"{ORIGIN}/register"
        assert server["code_challenge_methods_supported"] == ["S256"]
        assert "revocation_endpoint" not in server

    with_app(web_config, client, scenario)


def test_full_sign_in_then_tools_work(web_config, client):
    async def scenario(http):
        _, tokens = await connect(http, web_config)
        assert tokens["expires_in"] <= 900
        response = await list_tools(http, tokens["access_token"])
        assert response.status_code == 200, response.text
        names = {tool["name"] for tool in response.json()["result"]["tools"]}
        assert "read_messages" in names

    with_app(web_config, client, scenario)


@pytest.mark.parametrize(
    "uris", [["https://evil.example/cb"], [CALLBACK, "https://evil.example/cb"], ["http://localhost:3118/callback"]]
)
def test_registration_refuses_foreign_redirects(web_config, client, uris):
    async def scenario(http):
        response = await register(http, redirect_uris=uris)
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_redirect_uri"

    with_app(web_config, client, scenario)


def test_authorize_refuses_a_token_for_another_resource(web_config, client):
    async def scenario(http):
        client_id = (await register(http)).json()["client_id"]
        response = await start_authorize(http, client_id, resource="https://other.example/mcp")
        assert "/pair" not in response.headers.get("location", "")

    with_app(web_config, client, scenario)


def test_wrong_code_gives_no_access_and_burns_after_limit(web_config, client):
    async def scenario(http):
        client_id = (await register(http)).json()["client_id"]
        authorize = await start_authorize(http, client_id)
        request_id = parse_qs(urlsplit(authorize.headers["location"]).query)["request"][0]
        code = PairingStore(web_config.pairing_path).create()

        for _ in range(MAX_ATTEMPTS):
            wrong = await http.post("/pair", data={"request": request_id, "code": "WRONG"})
            assert wrong.status_code == 403 and "location" not in wrong.headers

        burned = await http.post("/pair", data={"request": request_id, "code": code})
        assert burned.status_code == 403

    with_app(web_config, client, scenario)


def test_pair_page_escapes_input_and_rejects_cross_site_posts(web_config, client):
    async def scenario(http):
        client_id = (await register(http)).json()["client_id"]
        authorize = await start_authorize(http, client_id)
        request_id = parse_qs(urlsplit(authorize.headers["location"]).query)["request"][0]

        page = await http.get("/pair", params={"request": request_id})
        assert page.status_code == 200
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert page.headers["cache-control"] == "no-store"

        unknown = await http.get("/pair", params={"request": "<script>alert(1)</script>"})
        assert unknown.status_code == 400 and "<script>alert" not in unknown.text

        code = PairingStore(web_config.pairing_path).create()
        forged = await http.post(
            "/pair",
            data={"request": request_id, "code": code},
            headers={"Origin": "https://evil.example"},
        )
        assert forged.status_code == 403 and "отклонён" in forged.text
        assert web_config.pairing_path.exists()

    with_app(web_config, client, scenario)


def test_authorization_code_is_single_use_and_requires_pkce(web_config, client):
    async def scenario(http):
        client_id = (await register(http)).json()["client_id"]
        authorize = await start_authorize(http, client_id)
        request_id = parse_qs(urlsplit(authorize.headers["location"]).query)["request"][0]
        code = PairingStore(web_config.pairing_path).create()
        approved = await http.post("/pair", data={"request": request_id, "code": code})
        auth_code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]

        def exchange(verifier):
            return http.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "code": auth_code,
                    "client_id": client_id,
                    "code_verifier": verifier,
                    "redirect_uri": CALLBACK,
                },
            )

        assert (await exchange("w" * 64)).status_code == 400
        assert (await exchange(VERIFIER)).status_code == 200
        assert (await exchange(VERIFIER)).status_code == 400

    with_app(web_config, client, scenario)


def test_refresh_rotates_and_replay_ends_the_connection(web_config, client):
    async def scenario(http):
        client_id, tokens = await connect(http, web_config)

        rotated = await refresh(http, client_id, tokens["refresh_token"])
        assert rotated.status_code == 200, rotated.text
        new = rotated.json()
        assert new["refresh_token"] != tokens["refresh_token"]
        assert (await list_tools(http, new["access_token"])).status_code == 200

        replay = await refresh(http, client_id, tokens["refresh_token"])
        assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
        assert (await list_tools(http, new["access_token"])).status_code == 401
        assert (await refresh(http, client_id, new["refresh_token"])).status_code == 400

    with_app(web_config, client, scenario)


def test_new_pairing_replaces_the_previous_connection(web_config, client):
    async def scenario(http):
        _, old = await connect(http, web_config)
        _, new = await connect(http, web_config)
        assert (await list_tools(http, old["access_token"])).status_code == 401
        assert (await list_tools(http, new["access_token"])).status_code == 200

    with_app(web_config, client, scenario)


def test_wrong_host_is_rejected(web_config, client):
    async def scenario(http):
        _, tokens = await connect(http, web_config)
        response = await list_tools(http, tokens["access_token"], host="evil.example")
        assert response.status_code in (400, 421)

    with_app(web_config, client, scenario)


def test_session_survives_a_restart_and_stores_only_hashes(web_config, client):
    async def first(http):
        return await connect(http, web_config)

    client_id, tokens = with_app(web_config, client, first)
    raw = web_config.oauth_state_path.read_text()
    assert tokens["refresh_token"] not in raw and tokens["access_token"] not in raw
    assert stat.S_IMODE(web_config.oauth_state_path.stat().st_mode) == 0o600
    assert json.loads(raw)["refresh"]

    async def second(http):
        assert (await list_tools(http, tokens["access_token"])).status_code == 401
        renewed = await refresh(http, client_id, tokens["refresh_token"])
        assert renewed.status_code == 200, renewed.text

    with_app(web_config, client, second)


def test_session_lifetime_is_not_extended_by_refreshing(web_config, client):
    limited = replace(web_config, session_days=1)

    async def scenario(http):
        client_id, tokens = await connect(http, limited)
        state = json.loads(limited.oauth_state_path.read_text())
        deadline = next(iter(state["refresh"].values()))["expires_at"]
        await refresh(http, client_id, tokens["refresh_token"])
        state = json.loads(limited.oauth_state_path.read_text())
        assert next(iter(state["refresh"].values()))["expires_at"] == deadline

    with_app(limited, client, scenario)


def test_idle_registrations_cannot_grow_without_limit(web_config, client):
    async def scenario(http):
        for _ in range(50):
            assert (await register(http)).status_code == 201
        assert len(json.loads(web_config.oauth_state_path.read_text())["clients"]) <= 32

    with_app(web_config, client, scenario)


def test_expired_pairing_code_is_refused(tmp_path):
    now = [1000.0]
    store = PairingStore(tmp_path / "pairing.json", clock=lambda: now[0])
    code = store.create()
    now[0] += 601
    assert not store.consume(code)
    assert not (tmp_path / "pairing.json").exists()


def test_pairing_code_works_once_and_tolerates_formatting(tmp_path):
    store = PairingStore(tmp_path / "pairing.json")
    code = store.create()
    assert code not in (tmp_path / "pairing.json").read_text()
    assert store.consume(f" {code.lower().replace('-', ' ')} ")
    assert not store.consume(code)


@pytest.mark.parametrize(
    "url", ["http://mcp.example.com", "https://mcp.example.com/path", "https://MCP.example.com", ""]
)
def test_public_url_must_be_a_clean_https_origin(url):
    with pytest.raises(ValueError):
        validate_public_url(url)


def test_browser_form_submission_succeeds(web_config, client):
    """A real browser derives the Origin of the form POST from the page's referrer policy."""

    async def scenario(http):
        client_id = (await register(http)).json()["client_id"]
        authorize = await start_authorize(http, client_id)
        request_id = parse_qs(urlsplit(authorize.headers["location"]).query)["request"][0]
        page = await http.get("/pair", params={"request": request_id})

        # Fetch standard: a "no-referrer" policy makes the browser send "Origin: null".
        policy = page.headers["referrer-policy"]
        browser_origin = "null" if policy == "no-referrer" else ORIGIN

        code = PairingStore(web_config.pairing_path).create()
        approved = await http.post(
            "/pair", data={"request": request_id, "code": code}, headers={"Origin": browser_origin}
        )
        assert approved.status_code == 303, approved.text
        assert approved.headers["location"].startswith(CALLBACK)

    with_app(web_config, client, scenario)
