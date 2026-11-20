"""Protects secrets held in this process's memory."""

import ctypes

PR_SET_DUMPABLE = 4
PR_GET_DUMPABLE = 3


def disable_memory_dumps() -> None:
    """Forbid core dumps and memory access via ptrace or /proc, or refuse to run.

    RLIMIT_CORE alone is not enough: a crash can still reach the coredump handler.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0 or libc.prctl(PR_GET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise RuntimeError("Could not disable memory dumps")
