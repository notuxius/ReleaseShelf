"""Explicit restore and manual-file actions in catalog dialogs."""

import base64
import os
from pathlib import Path
import subprocess
from tkinter import ttk

from fetch_sources import companion_actions
from fetch_store import check_link
from shelf_i18n import tr
from shelf_message import error_detail


def open_companion(root, relative, action, admin=False):
    root = Path(root).absolute()
    path = root / relative
    check_link(root.parent)
    check_link(root)
    for part in relative.parts:
        root = root / part
        check_link(root)
    if not path.is_file():
        raise FileNotFoundError(f"Missing companion file: {path}")
    if action == "folder":
        explorer = Path(os.environ["SystemRoot"]) / "explorer.exe"
        os.startfile(explorer, arguments=f'/select,"{path}"')
    elif action == "open":
        os.startfile(path)
    else:
        shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        # Pass the path as a quoted PowerShell literal, including apostrophes and dollar signs.
        literal = str(path).replace("'", "''")
        command = ("$ErrorActionPreference = 'Stop'\n"
                   f"try {{ & '{literal}' }} catch {{ Write-Host $_ -ForegroundColor Red }}\n"
                   "[void](Read-Host 'Press Enter to close')")
        encoded = base64.b64encode(command.encode("utf-16le")).decode("ascii")
        args = subprocess.list2cmdline(["-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                       "-EncodedCommand", encoded])
        os.startfile(shell, "runas" if admin else "open", args, str(path.parent))


def add_companion_buttons(parent, data, get_root, on_error, row, *, open_instructions=True):
    script, admin, manuals = companion_actions(data)
    if not script and not manuals:
        return
    group = ttk.Frame(parent)
    group.grid(row=row, column=0, columnspan=2, sticky="w", pady=(6, 0))

    def invoke(relative, action):
        try:
            open_companion(get_root(), relative, action, admin)
        except (OSError, ValueError) as exc:
            on_error(tr(error_detail(exc)))

    if script:
        ttk.Button(group, text=tr("Run restore script"),
                   command=lambda: invoke(script, "run")).grid(row=0, column=0, sticky="w", pady=2)
    for index, manual in enumerate(manuals, 1):
        folder_label = tr("Show in folder")
        if open_instructions:
            label = tr("Open instructions")
            if len(manuals) > 1:
                label += f" ({manual.parent.name or manual.stem})"
            ttk.Button(group, text=label, command=lambda p=manual: invoke(p, "open")).grid(
                row=index, column=0, sticky="w", pady=2)
        elif len(manuals) > 1:
            folder_label += f" ({manual})"
        ttk.Button(group, text=folder_label, command=lambda p=manual: invoke(p, "folder")).grid(
            row=index, column=1 if open_instructions else 0, sticky="w", padx=6 if open_instructions else 0, pady=2)
