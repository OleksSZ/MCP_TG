"""One-time interactive login. This is the only code allowed to authenticate."""

import asyncio
from getpass import getpass

from telethon import TelegramClient, functions
from telethon.sessions import StringSession

from . import vault
from .config import load_config
from .guard import ReadOnlyTelegramClient
from .process import disable_memory_dumps


async def run() -> None:
    disable_memory_dumps()
    config = load_config()
    key = vault.load_key()

    if config.vault_path.exists():
        answer = input("Сессия уже есть. Заменить её? [y/N]: ")
        if answer.strip().lower() != "y":
            print("Отменено.")
            return

    api_id = int(input("api_id (с my.telegram.org): ").strip())
    api_hash = getpass("api_hash (ввод скрыт): ").strip()

    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.start(
        phone=lambda: input("Телефон (+380...): ").strip(),
        code_callback=lambda: getpass("Код из Telegram (ввод скрыт): ").strip(),
        password=lambda: getpass("Облачный пароль 2FA (ввод скрыт): "),
    )
    session = client.session.save()
    has_password = (await client(functions.account.GetPasswordRequest())).has_password
    await client.disconnect()

    vault.save(config.vault_path, key, vault.Secrets(api_id, api_hash, session))

    # Prove that the stored session works through the read-only client.
    secrets = vault.load(config.vault_path, key)
    readonly = ReadOnlyTelegramClient(
        StringSession(secrets.session), secrets.api_id, secrets.api_hash, receive_updates=False
    )
    await readonly.connect()
    try:
        if not await readonly.is_user_authorized():
            raise SystemExit("Сессия сохранена, но проверка не прошла: нет авторизации.")
        chats = await readonly.get_dialogs(limit=3)
    finally:
        await readonly.disconnect()

    print(f"Готово: сессия зашифрована, read-only клиент видит чаты ({len(chats)} в проверке).")
    if not has_password:
        print("ВНИМАНИЕ: облачный пароль (2FA) не включён. Включи его в настройках Telegram.")


if __name__ == "__main__":
    asyncio.run(run())
