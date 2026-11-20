"""Single-owner OAuth authorization server for connecting claude.ai.

Only Claude's hosted callback may receive tokens, each new pairing replaces the
previous connection, and token stores keep hashes rather than usable tokens.
"""

import hashlib
import html
import json
import logging
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .config import WebConfig
from .pairing import PairingStore
from .policy import RateLimiter
from .storage import write_private_file

log = logging.getLogger("tg_mcp")

SCOPE = "telegram.read"
ALLOWED_REDIRECT_URIS = frozenset(
    {"https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback"}
)
ACCESS_TOKEN_SECONDS = 900
AUTH_CODE_SECONDS = 60
PENDING_REQUEST_SECONDS = 600
MAX_PENDING_REQUESTS = 32
MAX_CLIENTS = 32
MAX_RETIRED_TOKENS = 64
MAX_PAIR_ATTEMPTS_PER_MINUTE = 20


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class OwnerOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(self, config: WebConfig, clock=time.time):
        self._clock = clock
        self._issuer = config.public_url
        self._resource = f"{config.public_url}/mcp"
        self._session_seconds = config.session_days * 86400
        self._state_path: Path = config.oauth_state_path
        self.pairing = PairingStore(config.pairing_path, clock)
        self._pending: dict[str, tuple[float, str, AuthorizationParams]] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._access: dict[str, AccessToken] = {}
        self._state = self._load_state()

    def _load_state(self) -> dict:
        try:
            return json.loads(self._state_path.read_bytes())
        except FileNotFoundError:
            return {"clients": {}, "refresh": {}, "retired": []}

    def _save_state(self) -> None:
        write_private_file(self._state_path, json.dumps(self._state).encode())

    def _revoke_all(self) -> None:
        self._state["refresh"] = {}
        self._state["retired"] = []
        self._access.clear()
        self._codes.clear()
        self._save_state()

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        stored = self._state["clients"].get(client_id)
        return OAuthClientInformationFull.model_validate(stored) if stored else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = {str(uri) for uri in client_info.redirect_uris or []}
        if not uris or not uris <= ALLOWED_REDIRECT_URIS:
            raise RegistrationError(
                error="invalid_redirect_uri", error_description="Only Claude's callback is allowed"
            )

        clients = self._state["clients"]
        connected = {grant["client_id"] for grant in self._state["refresh"].values()}
        idle = sorted(
            (c for c in clients if c not in connected),
            key=lambda c: clients[c].get("client_id_issued_at") or 0,
        )
        # Anyone can reach this endpoint through Claude, so idle entries must not pile up.
        while len(clients) >= MAX_CLIENTS and idle:
            del clients[idle.pop(0)]

        clients[client_info.client_id] = client_info.model_dump(mode="json", exclude_none=True)
        self._save_state()

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if (params.resource or "").rstrip("/") != self._resource:
            raise AuthorizeError(error="invalid_request", error_description="Unknown resource")

        now = self._clock()
        self._pending = {
            key: value for key, value in self._pending.items() if value[0] > now
        }
        while len(self._pending) >= MAX_PENDING_REQUESTS:
            del self._pending[next(iter(self._pending))]

        request_id = secrets.token_urlsafe(32)
        self._pending[request_id] = (now + PENDING_REQUEST_SECONDS, client.client_id, params)
        return f"{self._issuer}/pair?request={request_id}"

    def has_pending(self, request_id: str) -> bool:
        pending = self._pending.get(request_id)
        return pending is not None and pending[0] > self._clock()

    def complete_pairing(self, request_id: str, code: str) -> str | None:
        """Exchange a valid pairing code for a redirect back to Claude."""
        if not self.has_pending(request_id) or not self.pairing.consume(code):
            return None

        _, client_id, params = self._pending.pop(request_id)
        self._revoke_all()
        auth_code = secrets.token_urlsafe(32)
        self._codes[auth_code] = AuthorizationCode(
            code=auth_code,
            scopes=[SCOPE],
            expires_at=self._clock() + AUTH_CODE_SECONDS,
            client_id=client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=self._resource,
            subject="owner",
        )
        return construct_redirect_uri(str(params.redirect_uri), code=auth_code, state=params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._codes.get(authorization_code)
        return code if code and code.client_id == client.client_id else None

    def _issue_tokens(self, client_id: str, session_expires_at: int) -> OAuthToken:
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(48)
        now = int(self._clock())
        access_expires_at = min(now + ACCESS_TOKEN_SECONDS, session_expires_at)
        self._access[_hash(access)] = AccessToken(
            token=access,
            client_id=client_id,
            scopes=[SCOPE],
            expires_at=access_expires_at,
            resource=self._resource,
            subject="owner",
        )
        self._state["refresh"][_hash(refresh)] = {
            "client_id": client_id,
            "expires_at": session_expires_at,
        }
        self._save_state()
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=access_expires_at - now,
            refresh_token=refresh,
            scope=SCOPE,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        del self._codes[authorization_code.code]
        return self._issue_tokens(client.client_id, int(self._clock()) + self._session_seconds)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        digest = _hash(refresh_token)
        if digest in self._state["retired"]:
            # A rotated token came back, so it was stolen or replayed. End the connection.
            self._revoke_all()
            return None

        grant = self._state["refresh"].get(digest)
        if grant is None or grant["client_id"] != client.client_id:
            return None
        if grant["expires_at"] <= self._clock():
            del self._state["refresh"][digest]
            self._save_state()
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=grant["client_id"],
            scopes=[SCOPE],
            expires_at=grant["expires_at"],
            resource=self._resource,
            subject="owner",
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        digest = _hash(refresh_token.token)
        del self._state["refresh"][digest]
        self._state["retired"] = [*self._state["retired"], digest][-MAX_RETIRED_TOKENS:]
        # Rotation keeps the original deadline, so a session cannot be extended forever.
        return self._issue_tokens(client.client_id, refresh_token.expires_at)

    async def load_access_token(self, token: str) -> AccessToken | None:
        digest = _hash(token)
        access = self._access.get(digest)
        if access is None:
            return None
        if access.expires_at <= self._clock():
            del self._access[digest]
            return None
        return access

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self._revoke_all()


PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Подключение Claude</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ font: 16px/1.5 system-ui, sans-serif; max-width: 30rem; margin: 3rem auto; padding: 0 1rem; }}
input, button {{ font: inherit; width: 100%; box-sizing: border-box; padding: .6rem; margin-top: .5rem; }}
.error {{ color: #c62828; font-weight: 600; }}
</style></head>
<body>{body}</body></html>"""

FORM = """<h1>Подключить Claude к Telegram?</h1>
<p>Claude получит доступ <strong>только на чтение</strong> к разрешённым чатам.
После подтверждения вход выполнит: <strong>{redirect_host}</strong>.</p>
<p><strong>Продолжай, только если эта страница открылась сразу после твоего нажатия «Connect» в Claude.</strong>
Если она открыта по чужой ссылке, закрой её и никому не сообщай код.</p>
{error}
<form method="post" action="/pair" autocomplete="off">
<input type="hidden" name="request" value="{request_id}">
<label>Код из команды <code>tg-mcp-pair</code>
<input name="code" required maxlength="64" autofocus spellcheck="false"></label>
<button type="submit">Подключить</button>
</form>"""

EXPIRED = "<h1>Запрос устарел</h1><p>Вернись в Claude и нажми «Connect» ещё раз.</p>"
FOREIGN = "<h1>Запрос отклонён</h1><p>Форма отправлена не со страницы этого сервера.</p>"
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self' https://claude.ai https://claude.com"
    ),
    # Not "no-referrer": with it, browsers send "Origin: null" on the form POST, which the
    # cross-site check below rejects. "same-origin" still sends no referrer to other sites.
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def _page(body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(PAGE.format(body=body), status_code=status_code, headers=SECURITY_HEADERS)


def _form(request_id: str, error: str = "", status_code: int = 200) -> HTMLResponse:
    # The host is fixed by ALLOWED_REDIRECT_URIS; both values belong to Claude.
    block = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return _page(
        FORM.format(
            redirect_host=html.escape(urlparse(next(iter(sorted(ALLOWED_REDIRECT_URIS)))).hostname),
            error=block,
            request_id=html.escape(request_id, quote=True),
        ),
        status_code,
    )


def create_pair_endpoint(provider: OwnerOAuthProvider, config: WebConfig):
    attempts = RateLimiter(MAX_PAIR_ATTEMPTS_PER_MINUTE, 60)

    async def pair(request: Request) -> Response:
        if request.method == "GET":
            request_id = request.query_params.get("request", "")
            if not provider.has_pending(request_id):
                log.warning("Pairing page refused: unknown or expired request")
                return _page(EXPIRED, 400)
            return _form(request_id)

        origin = request.headers.get("origin")
        if origin is not None and origin != config.public_url:
            log.warning("Pairing form rejected: foreign origin %r", origin[:100])
            return _page(FOREIGN, 403)
        if not attempts.try_acquire():
            return _page("<h1>Слишком много попыток</h1><p>Подожди минуту.</p>", 429)

        form = await request.form()
        request_id = str(form.get("request", ""))
        if not provider.has_pending(request_id):
            log.warning("Pairing form rejected: unknown or expired request")
            return _page(EXPIRED, 400)

        redirect = provider.complete_pairing(request_id, str(form.get("code", ""))[:64])
        if redirect is None:
            log.warning("Pairing form rejected: wrong, expired or missing code")
            return _form(request_id, "Код неверный или устарел. Создай новый: tg-mcp-pair", 403)
        return RedirectResponse(redirect, status_code=303, headers=SECURITY_HEADERS)

    return pair
