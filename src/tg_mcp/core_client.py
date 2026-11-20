"""Async client for the core's local API, used by the MCP endpoint."""

from pathlib import Path
from typing import Any

import httpx2 as httpx


class CoreError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class CoreClient:
    def __init__(self, socket_path: Path, transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(
            transport=transport or httpx.AsyncHTTPTransport(uds=str(socket_path)),
            base_url="http://core",
            timeout=httpx.Timeout(120, connect=5),
        )

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.TransportError:
            raise CoreError(503, "The Telegram core is unavailable") from None
        if response.status_code >= 400:
            try:
                message = response.json()["error"]
            except (ValueError, KeyError, TypeError):
                message = "The Telegram core returned an error"
            raise CoreError(response.status_code, message)
        return response

    async def _json(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        return (await self._request(method, path, **kwargs)).json()

    async def find_chats(self, query: str, limit: int) -> dict[str, Any]:
        return await self._json("GET", "/v1/chats", params={"query": query, "limit": limit})

    async def read_messages(
        self, chat_id: int, date_from: str, date_to: str, limit: int, before_id: int | None
    ) -> dict[str, Any]:
        params = {"date_from": date_from, "date_to": date_to, "limit": limit}
        if before_id is not None:
            params["before_id"] = before_id
        return await self._json("GET", f"/v1/chats/{chat_id}/messages", params=params)

    async def get_image(self, chat_id: int, message_id: int) -> tuple[bytes, str]:
        response = await self._request("GET", f"/v1/chats/{chat_id}/messages/{message_id}/image")
        return response.content, response.headers["content-type"].removeprefix("image/")

    async def start_export(self, **body: Any) -> dict[str, Any]:
        return await self._json("POST", "/v1/exports", json=body)

    async def export_status(self, job_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/v1/exports/{job_id}")
