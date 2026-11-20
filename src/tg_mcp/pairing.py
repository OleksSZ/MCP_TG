"""One-time codes that authorize connecting Claude, created over SSH.

There is no permanent password to steal or guess. Connecting requires a short-lived
code that only someone with shell access to the server can create.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

from .config import load_web_config
from .storage import write_private_file

CODE_BYTES = 15  # 120 bits, 24 base32 characters.
CODE_TTL_SECONDS = 600
MAX_ATTEMPTS = 5


def _normalize(code: str) -> str:
    return code.replace("-", "").replace(" ", "").strip().upper()


def _digest(code: str) -> str:
    return hashlib.sha256(_normalize(code).encode()).hexdigest()


class PairingStore:
    def __init__(self, path: Path, clock=time.time):
        self._path = path
        self._clock = clock

    def create(self) -> str:
        code = base64.b32encode(secrets.token_bytes(CODE_BYTES)).decode()
        record = {
            "digest": _digest(code),
            "expires_at": self._clock() + CODE_TTL_SECONDS,
            "attempts": 0,
        }
        write_private_file(self._path, json.dumps(record).encode())
        return "-".join(code[i : i + 4] for i in range(0, len(code), 4))

    def consume(self, code: str) -> bool:
        """Accept a valid code once. Expiry or too many wrong attempts destroys it."""
        try:
            record = json.loads(self._path.read_bytes())
        except FileNotFoundError:
            return False

        if self._clock() >= record["expires_at"]:
            self._path.unlink(missing_ok=True)
            return False
        if hmac.compare_digest(record["digest"], _digest(code)):
            self._path.unlink(missing_ok=True)
            return True

        record["attempts"] += 1
        if record["attempts"] >= MAX_ATTEMPTS:
            self._path.unlink(missing_ok=True)
        else:
            write_private_file(self._path, json.dumps(record).encode())
        return False


def main() -> None:
    code = PairingStore(load_web_config().pairing_path).create()
    minutes = CODE_TTL_SECONDS // 60
    print(f"Код подключения (одноразовый, действует {minutes} минут):\n\n    {code}\n")
    print("Вводи его только на странице подтверждения своего сервера.")


if __name__ == "__main__":
    main()
