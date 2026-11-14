"""Run unittest on a private Windows desktop so GUI tests can manage focus."""

import ctypes
import os
import sys
import unittest
import uuid
from ctypes import wintypes


def use_test_desktop():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.CreateDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
                                      wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    user32.CreateDesktopW.restype = wintypes.HANDLE
    user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]
    name = f"RelfTests-{uuid.uuid4().hex}"
    desktop = user32.CreateDesktopW(name, None, None, 0, 0x01FF, None)
    if not desktop or not user32.SetThreadDesktop(desktop):
        raise ctypes.WinError(ctypes.get_last_error())
    # Windows releases the desktop when this process and its Tk windows exit.


if __name__ == "__main__":
    if os.name == "nt":
        use_test_desktop()
    unittest.main(module=None, argv=[sys.argv[0], *(sys.argv[1:] or ["discover", "-v"])])
