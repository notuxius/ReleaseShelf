"""Explicit restore and manual-file actions in catalog dialogs."""

import base64
from dataclasses import replace
import os
from pathlib import Path
import subprocess
from tkinter import ttk

from fetch_sources import companion_actions
from fetch_store import apply_published_settings, check_link
from shelf_i18n import tr
from shelf_message import error_detail


def install_and_restore(path, job, recipe_path, dest, restore_settings):
    """Wait for Windows to finish installation before restoring this item's companions."""
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    # Supply paths as data so quotes and PowerShell metacharacters stay literal.
    env = {**os.environ, "RELEASESHELF_INSTALLER": str(path)}
    suffix = path.suffix.lower()
    if suffix in {".appx", ".msix", ".appxbundle", ".msixbundle"}:
        command = "Add-AppxPackage -Path $env:RELEASESHELF_INSTALLER"
    else:
        if suffix == ".msi":
            start = ('-FilePath "$env:SystemRoot/System32/msiexec.exe" '
                     """-ArgumentList ('/i "' + $env:RELEASESHELF_INSTALLER + '"')""")
        else:
            start = "-FilePath $env:RELEASESHELF_INSTALLER"
        command = f"$process = Start-Process {start} -Wait -PassThru\nexit $process.ExitCode"
    command = ("$ErrorActionPreference = 'Stop'\n"
               "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n" + command)
    result = subprocess.run([str(shell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
                            cwd=path.parent, env=env, capture_output=True, encoding="utf-8", errors="replace",
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode not in (0, 1641, 3010):
        raise ValueError(result.stderr.strip() or f"{path.name} exited with code {result.returncode}.")
    if restore_settings and not job.companions.script:
        if not apply_published_settings(replace(job, apply_custom_settings=True), dest):
            raise ValueError(f"Could not locate the published settings destination for {path.name}.")
    if job.companions.script:
        open_companion(recipe_path.parent / "companions" / recipe_path.stem,
                       job.companions.script, "run", job.companions.admin, app_folder=path.parent)


def open_companion(root, relative, action, admin=False, *, app_folder=None):
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
    elif action == "edit":
        editor = Path(os.environ["SystemRoot"]) / "System32/notepad.exe"
        os.startfile(editor, arguments=subprocess.list2cmdline([str(path)]))
    elif action == "run":
        shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        # Pass the path as a quoted PowerShell literal, including apostrophes and dollar signs.
        literal = str(path).replace("'", "''")
        folder = str(app_folder or "").replace("'", "''")
        command = ("$ErrorActionPreference = 'Stop'\n"
                   f"$env:RELEASESHELF_APP_FOLDER = '{folder}'\n"
                   f"try {{ & '{literal}' }} catch {{ Write-Host $_ -ForegroundColor Red }}\n"
                   "[void](Read-Host 'Press Enter to close')")
        encoded = base64.b64encode(command.encode("utf-16le")).decode("ascii")
        args = subprocess.list2cmdline(["-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                       "-EncodedCommand", encoded])
        os.startfile(shell, "runas" if admin else "open", args, str(path.parent))


def add_companion_buttons(parent, data, get_root, on_error, row, *, open_instructions=True, get_app_folder=None):
    actions = companion_actions(data)
    script, admin, manuals = actions.script, actions.admin, actions.manuals
    if not script and not manuals:
        return
    group = ttk.Frame(parent)
    group.grid(row=row, column=0, columnspan=2, sticky="w", pady=(6, 0))

    def invoke(relative, action):
        try:
            folder = get_app_folder() if action == "run" and get_app_folder else None
            open_companion(get_root(), relative, action, admin, app_folder=folder)
        except (OSError, ValueError) as exc:
            on_error(tr(error_detail(exc)))

    if script:
        ttk.Button(group, text=tr("Run restore script"),
                   command=lambda: invoke(script, "run")).grid(row=0, column=0, sticky="w", pady=2)
        ttk.Button(group, text=tr("Edit restore script"),
                   command=lambda: invoke(script, "edit")).grid(row=0, column=1, sticky="w", padx=6, pady=2)
        ttk.Label(group, text=str(script), style="Muted.TLabel").grid(row=1, column=0, columnspan=2, sticky="w")
    for index, manual in enumerate(manuals, 2 if script else 0):
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
