import ctypes
import sys
from ctypes import wintypes

import psutil


def get_foreground_app(user32=None, process_factory=None) -> dict | None:
    if user32 is None:
        if sys.platform != "win32":
            return None
        user32 = ctypes.windll.user32
    process_factory = process_factory or psutil.Process

    try:
        window_handle = user32.GetForegroundWindow()
        if not window_handle:
            return None

        title_length = user32.GetWindowTextLengthW(window_handle)
        title_buffer = ctypes.create_unicode_buffer(title_length + 1)
        user32.GetWindowTextW(window_handle, title_buffer, len(title_buffer))

        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window_handle, ctypes.byref(process_id))
        if not process_id.value:
            return None

        process_name = process_factory(process_id.value).name()
        return {
            "appName": process_name,
            "appId": process_name,
            "windowTitle": title_buffer.value,
            "processId": process_id.value,
        }
    except (OSError, psutil.Error):
        return None
