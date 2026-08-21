"""Probe numeric Windows event handles without touching external resources."""

from __future__ import annotations

import ctypes
import json
import sys
from ctypes import wintypes


KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
KERNEL32.GetHandleInformation.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(wintypes.DWORD),
]
KERNEL32.GetHandleInformation.restype = wintypes.BOOL
KERNEL32.SetEvent.argtypes = [wintypes.HANDLE]
KERNEL32.SetEvent.restype = wintypes.BOOL


def probe_handle(raw_handle: str) -> dict[str, object]:
    handle = wintypes.HANDLE(int(raw_handle))
    flags = wintypes.DWORD()
    ctypes.set_last_error(0)
    valid = bool(KERNEL32.GetHandleInformation(handle, ctypes.byref(flags)))
    get_handle_information_error = ctypes.get_last_error() if not valid else 0

    ctypes.set_last_error(0)
    set_event_success = bool(KERNEL32.SetEvent(handle))
    set_event_error = ctypes.get_last_error() if not set_event_success else 0
    return {
        "valid": valid,
        "inherit_flag": bool(flags.value & 1) if valid else False,
        "set_event_success": set_event_success,
        "win32_error": set_event_error if not set_event_success else 0,
        "get_handle_information_error": get_handle_information_error,
    }


def main() -> int:
    if len(sys.argv) < 2:
        return 2
    probes = [probe_handle(raw_handle) for raw_handle in sys.argv[1:]]
    payload = probes[0] if len(probes) == 1 else {"handles": probes}
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
