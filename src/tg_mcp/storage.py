"""Atomic writes for files that hold secrets."""

import os
from pathlib import Path


def write_private_file(path: Path, data: bytes) -> None:
    """Replace a file atomically, readable by its owner only, without following symlinks."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.unlink(missing_ok=True)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)
