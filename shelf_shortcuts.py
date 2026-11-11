"""Publish a current-user Start menu shortcut using the Windows Shell."""

import ctypes
import os
from pathlib import Path
import subprocess
import uuid


def shortcut_path(name):
    programs = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 2, None, 0, programs):
        raise OSError("Could not locate the current user's Start menu Programs folder")
    return Path(programs.value) / (name + ".lnk")


def write_shortcut(path, target):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".releaseshelf-{uuid.uuid4().hex}.lnk")
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    script = """
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($env:RELEASESHELF_LINK)
$link.TargetPath = $env:RELEASESHELF_EXE
$link.WorkingDirectory = [IO.Path]::GetDirectoryName($env:RELEASESHELF_EXE)
$link.Save()
if ($shell.CreateShortcut($env:RELEASESHELF_LINK).TargetPath -ne $env:RELEASESHELF_EXE) {
    throw 'The saved shortcut target differs from the published executable.'
}
"""
    try:
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", script],
            env={**os.environ, "RELEASESHELF_LINK": str(temp), "RELEASESHELF_EXE": str(target.resolve())},
            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=30,
        )
        if result.returncode:
            raise OSError(result.stderr.decode(errors="replace").strip() or "Windows could not create the shortcut")
        os.replace(temp, path)
    except subprocess.TimeoutExpired as exc:
        raise OSError("Windows timed out creating the shortcut") from exc
    finally:
        temp.unlink(missing_ok=True)
