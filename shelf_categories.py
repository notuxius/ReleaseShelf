"""Category changes for local catalog entries."""

import json
from pathlib import Path
import tempfile
import tkinter as tk
from tkinter import ttk

from fetch_sources import Recipe
from fetch_store import fetch_lock
from recipe_editor import recipe_bytes
from releaseshelf import ERRORS, read_jobs
from shelf_checklist import LocalItem
from shelf_i18n import tr
from shelf_icons import make_icon
from shelf_message import error_detail
from shelf_paint import show_window, stage_window
from shelf_theme import category_picker, initial_geometry, style_titlebar


MOVE = "Move to category…"
RENAME = "Rename category…"


def change_category(catalog, members, source, destination, *, rename):
    """Update the displayed members under the same catalog lock as the recipe editor."""
    with fetch_lock(catalog), tempfile.TemporaryDirectory(dir=catalog) as work:
        apps = {path: job for path, job in read_jobs(catalog.glob("*.json"))
                if isinstance(job, (Recipe, LocalItem))}
        current = {path: job.id for path, job in apps.items()
                   if job.category == source and (rename or path in members)}
        if current != members:
            raise ValueError("The category changed on disk. Reload the app list and try again.")
        if destination == source:
            return
        if rename and any(job.category == destination for job in apps.values()):
            raise ValueError("A category with this name already exists.")

        stage = Path(work)
        originals = stage / "originals"
        originals.mkdir()
        for path in members:
            raw = path.read_bytes()
            data = json.loads(raw.decode("utf-8-sig"))
            if destination:
                data["category"] = destination
            else:
                data.pop("category", None)
            (originals / path.name).write_bytes(raw)
            (stage / path.name).write_bytes(recipe_bytes(data))
        changed = []
        try:
            for path in members:
                (stage / path.name).replace(path)
                changed.append(path)
        except OSError:
            for path in reversed(changed):
                (originals / path.name).replace(path)
            raise


class CategoryDialog(tk.Toplevel):
    def __init__(self, parent, palette, title, subject, source, choices, on_save):
        super().__init__(parent)
        stage_window(self)
        self.transient(parent)
        self.title(tr(title))
        self.resizable(False, False)
        self.on_save = on_save
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=subject, wraplength=360).pack(anchor="w", pady=(0, 8))
        self.value = tk.StringVar(self, source)
        if choices is None:
            field = ttk.Entry(frame, textvariable=self.value, width=38)
        else:
            field = category_picker(frame, self.value, choices, width=36)
        field.pack(fill="x")
        if choices is not None:
            ttk.Label(frame, text=tr("Leave blank for Uncategorized."), style="Muted.TLabel").pack(anchor="w")
        self.error = tk.StringVar(self)
        ttk.Label(frame, textvariable=self.error, style="EditorError.TLabel", wraplength=360).pack(fill="x")
        actions = ttk.Frame(frame)
        actions.pack(anchor="e", pady=(8, 0))
        self.cancel_icon = make_icon(self, "diskette_x", palette.text)
        self.save_icon = make_icon(self, "save", palette.on_accent)
        ttk.Button(actions, text=tr("Cancel"), image=self.cancel_icon, compound="left",
                   command=self.destroy).pack(side="left", padx=(0, 8))
        save = ttk.Button(actions, text=tr("Save"), image=self.save_icon, compound="left",
                          style="Primary.TButton", command=self.save)
        save.pack(side="left")
        if choices is None:
            def enable_save(*_):
                save.state(["!disabled" if self.value.get().strip() else "disabled"])
            self.value.trace_add("write", enable_save)
            enable_save()
        field.bind("<Return>", lambda _: save.invoke())
        self.bind("<Escape>", lambda _: self.destroy())
        initial_geometry(self, 0, 0, parent=parent)
        style_titlebar(self, palette.is_dark)
        self.grab_set()
        field.focus_set()
        field.selection_range(0, "end")
        show_window(self)

    def save(self):
        try:
            self.on_save(self.value.get().strip())
        except ERRORS as exc:
            self.error.set(tr(error_detail(exc)))
            self.geometry("")  # Fit a wrapped error message.
            return
        self.destroy()
