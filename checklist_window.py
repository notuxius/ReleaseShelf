"""Choose and apply individual adjustments from a local setup checklist."""

import json
import os
from queue import Empty, Queue
from threading import Thread
import tkinter as tk
from tkinter import filedialog, ttk

from companion_buttons import add_companion_buttons
from recipe_editor import recipe_bytes, save_recipe
from releaseshelf import ERRORS
from shelf_checkboxes import checkbox_image, header_image
from shelf_checklist import Checklist, catalog_item, run_checklist_step
from shelf_i18n import tr
from shelf_paint import stage_window
from shelf_scroll import grid_scrollable
from shelf_text import FittedLabel, FittedTreeview
from shelf_theme import initial_geometry, style_titlebar


class ChecklistWindow(tk.Toplevel):
    def __init__(self, parent, catalog, path, palette, on_saved, history, on_result):
        self.original = path.read_bytes()
        self.data = json.loads(self.original.decode("utf-8-sig"))
        self.item = catalog_item(self.data)
        if not isinstance(self.item, Checklist):
            raise ValueError("The checklist changed. Reopen it from the catalog.")
        super().__init__(parent)
        stage_window(self)
        self.path, self.catalog, self.on_saved = path, catalog, on_saved
        self.history, self.on_result = history, on_result
        self.title(self.item.name or self.item.id)
        self.transient(parent)
        self.running = self.busy = False
        self.selected = [step.selected for step in self.item.steps]
        self.outputs = {}
        self.events = Queue()
        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)
        ttk.Label(body, text=tr("Choose adjustments for this PC"), style="Detail.TLabel").grid(sticky="w", pady=(0, 8))
        panel = ttk.Frame(body)
        panel.grid(row=1, column=0, sticky="nsew")
        self.listing = tree = FittedTreeview(panel, columns=("step", "result"), show=("tree", "headings"),
                                             selectmode="browse", height=min(14, len(self.selected)))
        tree.column("#0", width=36, minwidth=36, stretch=False)
        tree.heading("#0", command=self.toggle_all)
        tree.tk.call(tree, "heading", "#0", "state", "alternate")
        for key, label, width in (("step", "Adjustment", 360), ("result", "Result", 115)):
            tree.set_caption(key, tr(label))
            tree.column(key, width=width, minwidth=40, stretch=key == "step", anchor="w" if key == "step" else "center")
        for i, step in enumerate(self.item.steps):
            status = history.status(self.item.id, self.item.software_folder, step)
            tree.insert("", "end", iid=str(i))
            tree.set_values(str(i), (step.label, tr(status)))
        grid_scrollable(tree)
        tree.bind("<Button-1>", self.click)
        tree.bind("<space>", self.toggle_focused)
        tree.bind("<<TreeviewSelect>>", self.show_detail)
        self.detail = tk.StringVar(self)
        detail = ttk.Label(body, textvariable=self.detail, justify="left", anchor="nw", wraplength=560)
        detail.grid(row=2, column=0, sticky="ew", pady=(8, 4))
        detail.bind("<Configure>", lambda e: detail.configure(wraplength=max(100, e.width - 6)))
        self.folder = tk.StringVar(self, self.item.software_folder)
        if "software_folder" in self.data:
            paths = ttk.Frame(body)
            paths.grid(row=3, column=0, sticky="ew", pady=4)
            paths.columnconfigure(1, weight=1)
            ttk.Label(paths, text=tr("Software folder")).grid(row=0, column=0, padx=(0, 8))
            self.folder_entry = ttk.Entry(paths, textvariable=self.folder)
            self.folder_entry.grid(row=0, column=1, sticky="ew")
            self.browse = ttk.Button(paths, text=tr("Browse…"), command=self.choose_folder)
            self.browse.grid(row=0, column=2, padx=(6, 0))
        self.error = tk.StringVar(self)
        add_companion_buttons(body, self.data,
                              lambda: self.path.parent / os.path.expandvars(self.folder.get().strip())
                              if "software_folder" in self.data else self.path.parent / "companions" / self.path.stem,
                              self.error.set, 4)
        ttk.Label(body, textvariable=self.error, wraplength=560, style="EditorError.TLabel").grid(
            row=5, column=0, sticky="w")
        actions = ttk.Frame(body)
        actions.grid(row=6, column=0, sticky="ew", pady=(6, 0))
        actions.columnconfigure(0, weight=1)
        self.summary = FittedLabel(actions, anchor="w")
        self.summary.grid(row=0, column=0, sticky="ew")
        self.close_button = ttk.Button(actions, text=tr("Close"), command=self.close)
        self.close_button.grid(row=0, column=1)
        self.save_button = ttk.Button(actions, text=tr("Save choices"), command=self.save_and_close)
        self.save_button.grid(row=0, column=2, padx=6)
        self.apply_button = ttk.Button(actions, text=tr("Apply selected"), style="Primary.TButton", command=self.apply)
        self.apply_button.grid(row=0, column=3)
        tree.focus("0")
        tree.selection_set("0")
        self.show_detail()
        self.set_busy(False)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _: self.close())
        self.bind("<Map>", lambda e: style_titlebar(self, self.palette.is_dark) if e.widget is self else None)
        self.apply_theme(palette)
        initial_geometry(self, 730, 540, parent=parent)
        self.grab_set()
        tree.focus_set()

    def click(self, event):
        row = self.listing.identify_row(event.y)
        if row and self.listing.identify_region(event.x, event.y) == "tree":
            self.toggle(int(row))

    def toggle_focused(self, _event):
        if row := self.listing.focus():
            self.toggle(int(row))
        return "break"

    def toggle(self, index):
        if not (self.running or self.busy):
            self.selected[index] = not self.selected[index]
            self.set_busy(self.busy)

    def toggle_all(self):
        if not (self.running or self.busy):
            self.selected = [not all(self.selected)] * len(self.selected)
            self.set_busy(self.busy)

    def show_detail(self, _event=None):
        if rows := self.listing.selection():
            index = int(rows[0])
            detail = self.item.steps[index].description
            if output := self.outputs.get(index):
                detail += "\n" + output
            self.detail.set(detail)

    def choose_folder(self):
        if folder := filedialog.askdirectory(parent=self, title=tr("Software folder")):
            self.folder.set(folder)

    def apply_theme(self, palette):
        self.palette = palette
        self.configure(background=palette.canvas)
        if self.winfo_ismapped():
            style_titlebar(self, palette.is_dark)

    def set_busy(self, busy):
        self.busy = busy
        blocked = busy or self.running
        count = sum(self.selected)
        self.listing.heading("#0", image=header_image(count, len(self.selected), blocked))
        for i, checked in enumerate(self.selected):
            self.listing.item(str(i), image=checkbox_image(checked, blocked))
        for widget in (self.save_button, self.close_button):
            widget.state(["disabled" if blocked else "!disabled"])
        if "software_folder" in self.data:
            for widget in (self.folder_entry, self.browse):
                widget.state(["disabled" if blocked else "!disabled"])
        self.apply_button.state(["disabled" if blocked or not any(self.selected) else "!disabled"])
        self.summary.set_text(tr("{count} selected", count=count))

    def save(self):
        data = {**self.data, "steps": [{**step, "selected": selected}
                                      for step, selected in zip(self.data["steps"], self.selected, strict=True)]}
        if "software_folder" in data:
            data["software_folder"] = self.folder.get().strip()
        try:
            save_recipe(self.catalog, data, self.path, self.original)
        except ERRORS as exc:
            self.error.set(str(exc))
            return False
        self.data = data
        self.original = recipe_bytes(data)
        self.error.set("")
        self.on_saved(self.path)
        return True

    def save_and_close(self):
        if not self.busy and not self.running and self.save():
            self.destroy()

    def apply(self):
        chosen = [i for i, value in enumerate(self.selected) if value]
        if self.busy or self.running or not chosen or not self.save():
            return
        self.running = True
        self.set_busy(self.busy)
        self.outputs.clear()
        for i in chosen:
            self.listing.set_values(str(i), (self.item.steps[i].label, tr("Queued")))
        self.show_detail()
        Thread(target=self.run_steps, args=(chosen, self.folder.get().strip()), daemon=True).start()
        self.after(50, self.poll)

    def run_steps(self, chosen, folder):
        try:
            for i in chosen:
                self.events.put((i, "Running", ""))
                step = self.item.steps[i]
                status, output, history_error = run_checklist_step(
                    self.item, step, folder, self.path.parent, self.history)
                if history_error:
                    self.events.put((i, "history_error", history_error))
                self.events.put((i, status, output))
        finally:
            self.events.put(None)

    def poll(self):
        while True:
            try:
                event = self.events.get_nowait()
            except Empty:
                self.after(50, self.poll)
                return
            if event is None:
                self.running = False
                self.set_busy(self.busy)
                return
            i, status, detail = event
            if status == "history_error":
                self.error.set(tr("Could not save checklist status: {error}", error=detail))
                continue
            self.listing.set_values(str(i), (self.item.steps[i].label, tr(status)))
            self.outputs[i] = detail
            if status in ("Applied", "Error"):
                self.on_result(self.path)
            self.show_detail()

    def close(self):
        if not self.running:
            self.destroy()
