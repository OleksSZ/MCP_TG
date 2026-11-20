"""Deny-by-default filter for every Telegram API request.

All high-level Telethon methods (send_message, delete_messages, ...) end up in
``_call``. Only the exact request types listed here may pass, so a write stays
impossible even if a tool or a future code path tries to issue one.
"""

from telethon import TelegramClient, functions, utils

ALLOWED_REQUESTS = frozenset(
    {
        # Reading chats and messages.
        functions.messages.GetDialogsRequest,
        functions.messages.GetHistoryRequest,
        functions.messages.GetMessagesRequest,
        functions.messages.SearchRequest,  # search inside one chat
        functions.messages.SearchGlobalRequest,  # search across all chats
        functions.channels.GetMessagesRequest,
        functions.users.GetUsersRequest,
        # Downloading files, including from other data centers and CDNs.
        functions.upload.GetFileRequest,
        functions.upload.GetCdnFileRequest,
        functions.upload.ReuploadCdnFileRequest,
        functions.help.GetCdnConfigRequest,
        functions.auth.ExportAuthorizationRequest,
        # Issued by Telethon itself to keep the connection working.
        functions.help.GetConfigRequest,
        functions.updates.GetStateRequest,
        functions.updates.GetDifferenceRequest,
        functions.updates.GetChannelDifferenceRequest,
    }
)


class ForbiddenRequestError(PermissionError):
    """A Telegram API request outside the allowlist was attempted."""


class ReadOnlyTelegramClient(TelegramClient):
    async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
        if utils.is_list_like(request):
            request = list(request)
            items = request
        else:
            items = [request]

        for item in items:
            if type(item) not in ALLOWED_REQUESTS:
                raise ForbiddenRequestError(f"Blocked Telegram request: {type(item).__name__}")

        return await super()._call(
            sender, request, ordered=ordered, flood_sleep_threshold=flood_sleep_threshold
        )
