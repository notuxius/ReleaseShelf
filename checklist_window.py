"""Edit a local setup checklist and explicitly apply its selected adjustments."""

import json
import os
from queue import Empty, Queue
from threading import Thread
import tkinter as tk
from tkinter import filedialog, ttk

from companion_buttons import add_companion_buttons
from recipe_editor import TAB_PADDING, recipe_bytes, save_recipe, text_field
from releaseshelf import ERRORS
from shelf_checkboxes import checkbox_image, header_image
from shelf_checklist import Checklist, SetupStep, catalog_item, run_checklist_step
from shelf_i18n import tr
from shelf_message import error_detail
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
            raise ValueError("The checklist changed. Reopen it from the app list.")
        super().__init__(parent)
        stage_window(self)
        self.path, self.catalog, self.on_saved = path, catalog, on_saved
        self.history, self.on_result = history, on_result
        self.title(f"{tr('Edit item')} — {self.item.name or self.item.id}")
        self.transient(parent)
        self.running = self.busy = False
        self.selected = [step.selected for step in self.item.steps]
        self.outputs = {}
        self.events = Queue()
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.tabs = ttk.Notebook(frame, style="Editor.TNotebook")
        self.tabs.grid(row=0, column=0, sticky="nsew")
        general = ttk.Frame(self.tabs, padding=TAB_PADDING)
        general.columnconfigure(1, weight=1)
        general.rowconfigure(5, weight=1)
        self.tabs.add(general, text=tr("General"))
        self.name = tk.StringVar(self, self.item.name)
        ttk.Label(general, text=tr("Name")).grid(row=0, column=0, sticky="w", padx=(0, 12))
        first = ttk.Entry(general, textvariable=self.name)
        first.grid(row=0, column=1, sticky="ew", pady=(0, 6))
        self.folder = tk.StringVar(self, self.item.software_folder)
        if "software_folder" in self.data:
            ttk.Label(general, text=tr("Software folder")).grid(row=1, column=0, sticky="w", padx=(0, 12))
            paths = ttk.Frame(general)
            paths.grid(row=1, column=1, sticky="ew", pady=(0, 6))
            paths.columnconfigure(0, weight=1)
            ttk.Entry(paths, textvariable=self.folder).grid(row=0, column=0, sticky="ew")
            ttk.Button(paths, text=tr("Browse…"), command=self.choose_folder).grid(row=0, column=1, padx=(6, 0))

        self.steps = [step.copy() for step in self.data["steps"]]
        self.edit_index = 0
        self.picker = None
        if len(self.steps) > 1:
            ttk.Label(general, text=tr("Adjustment")).grid(row=2, column=0, sticky="w")
            self.picker = ttk.Combobox(general, state="readonly")
            self.picker.grid(row=2, column=1, sticky="ew", pady=(2, 6))
            self.picker.bind("<<ComboboxSelected>>", self.switch_step)
        self.label = tk.StringVar(self)
        ttk.Label(general, text=tr("Step name")).grid(row=3, column=0, sticky="w", padx=(0, 12))
        self.label_entry = ttk.Entry(general, textvariable=self.label)
        self.label_entry.grid(row=3, column=1, sticky="ew", pady=(0, 6))
        ttk.Label(general, text=tr("Description")).grid(row=4, column=0, sticky="nw", padx=(0, 12))
        self.description = text_field(general, row=4, column=1, height=3, width=32, wrap="word",
                                      font=("Segoe UI", 10), pady=(0, 8))
        ttk.Label(general, text="PowerShell").grid(row=5, column=0, sticky="nw", padx=(0, 12))
        self.script = text_field(general, row=5, column=1, height=7, width=32, wrap="none", font=("Consolas", 10))
        self.load_step(0)

        body = ttk.Frame(self.tabs, padding=TAB_PADDING)
        self.tabs.add(body, text=tr("Checklist"))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        panel = ttk.Frame(body)
        panel.grid(row=0, column=0, sticky="nsew")
        self.listing = tree = FittedTreeview(panel, columns=("step", "result"), show=("tree", "headings"),
                                             selectmode="browse", height=min(14, len(self.selected)))
        tree.column("#0", width=36, minwidth=36, stretch=False)
        tree.heading("#0", command=self.toggle_all)
        tree.tk.call(tree, "heading", "#0", "state", "alternate")
        for key, label, width in (("step", "Adjustment", 360), ("result", "Result", 115)):
            tree.set_caption(key, tr(label))
            tree.column(key, width=width, minwidth=40, stretch=key == "step", anchor="w" if key == "step" else "center")
        for i in range(len(self.steps)):
            tree.insert("", "end", iid=str(i))
        grid_scrollable(tree)
        tree.bind("<Button-1>", self.click)
        tree.bind("<space>", self.toggle_focused)
        tree.bind("<<TreeviewSelect>>", self.show_detail)
        self.detail = tk.StringVar(self)
        detail = ttk.Label(body, textvariable=self.detail, justify="left", anchor="nw", wraplength=560)
        detail.grid(row=1, column=0, sticky="ew", pady=(8, 4))
        detail.bind("<Configure>", lambda e: detail.configure(wraplength=max(100, e.width - 6)))
        self.error = tk.StringVar(self)
        add_companion_buttons(body, self.data,
                              lambda: self.path.parent / os.path.expandvars(self.folder.get().strip())
                              if "software_folder" in self.data else self.path.parent / "companions" / self.path.stem,
                              self.error.set, 2)
        ttk.Label(frame, textvariable=self.error, wraplength=560, style="EditorError.TLabel").grid(
            row=1, column=0, sticky="w")
        run_actions = ttk.Frame(body)
        run_actions.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        run_actions.columnconfigure(0, weight=1)
        self.summary = FittedLabel(run_actions, anchor="w")
        self.summary.grid(row=0, column=0, sticky="ew")
        self.apply_button = ttk.Button(run_actions, text=tr("Apply selected"), command=self.apply)
        self.apply_button.grid(row=0, column=1)
        actions = ttk.Frame(frame)
        actions.grid(row=2, column=0, sticky="e", pady=(6, 0))
        self.close_button = ttk.Button(actions, text=tr("Close"), command=self.close)
        self.close_button.grid(row=0, column=0)
        self.save_button = ttk.Button(actions, text=tr("Save"), style="Primary.TButton", command=self.save_and_close)
        self.save_button.grid(row=0, column=1, padx=(6, 0))
        self.tabs.bind("<<NotebookTabChanged>>", self.refresh_checklist)
        tree.focus("0")
        tree.selection_set("0")
        self.show_detail()
        self.set_busy(False)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Control-s>", lambda _: self.save_and_close())
        self.bind("<Escape>", lambda _: self.close())
        self.bind("<Map>", lambda e: style_titlebar(self, self.palette.is_dark) if e.widget is self else None)
        self.apply_theme(palette)
        initial_geometry(self, 730, 540, parent=parent)
        self.grab_set()
        first.focus_set()

    def store_step(self):
        self.steps[self.edit_index].update(label=self.label.get(), description=self.description.get("1.0", "end-1c"),
                                          powershell=self.script.get("1.0", "end-1c"))

    def load_step(self, index):
        self.edit_index = index
        step = self.steps[index]
        if self.picker is not None:
            self.picker.configure(values=[f"{i}. {s['label']}" for i, s in enumerate(self.steps, 1)])
            self.picker.current(index)
        self.label.set(step["label"])
        for field, key in ((self.description, "description"), (self.script, "powershell")):
            field.delete("1.0", "end")
            field.insert("1.0", step[key])

    def switch_step(self, _event):
        index = self.picker.current()
        self.store_step()
        self.load_step(index)

    def refresh_checklist(self, _event=None):
        if self.tabs.index("current") != 1 or self.running:
            return
        self.store_step()
        folder = self.folder.get().strip()
        for i, draft in enumerate(self.steps):
            step = SetupStep(draft["label"], draft["description"], draft["powershell"])
            status = self.history.status(self.item.id, folder, step)
            self.listing.set_values(str(i), (step.label, tr(status)))
            saved = self.item.steps[i]
            if (folder, step.label, step.powershell) != (self.item.software_folder, saved.label, saved.powershell):
                self.outputs.pop(i, None)
        self.show_detail()

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
            detail = self.steps[index]["description"]
            if output := self.outputs.get(index):
                detail += "\n" + output
            self.detail.set(detail)

    def choose_folder(self):
        if folder := filedialog.askdirectory(parent=self, title=tr("Software folder")):
            self.folder.set(folder)

    def apply_theme(self, palette):
        self.palette = palette
        self.configure(background=palette.canvas)
        for field in (self.description, self.script):
            field.configure(bg=palette.surface, fg=palette.text, insertbackground=palette.text,
                            selectbackground=palette.selection, selectforeground=palette.text, relief="flat",
                            highlightthickness=1, highlightbackground=palette.line, highlightcolor=palette.accent,
                            padx=6, pady=4)
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
        self.tabs.tab(0, state="disabled" if blocked else "normal")
        self.apply_button.state(["disabled" if blocked or not any(self.selected) else "!disabled"])
        self.summary.set_text(tr("{count} selected", count=count))

    def save(self):
        self.store_step()
        data = {**self.data, "steps": [{**step, "selected": selected}
                                      for step, selected in zip(self.steps, self.selected, strict=True)]}
        if self.name.get() or "name" in data:
            data["name"] = self.name.get()
        if "software_folder" in data:
            data["software_folder"] = self.folder.get().strip()
        for index, step in enumerate(self.steps):
            for key, caption, field in (("label", "Step name", self.label_entry),
                                        ("description", "Description", self.description),
                                        ("powershell", "PowerShell", self.script)):
                if not step[key].strip():
                    self.tabs.select(0)
                    self.load_step(index)
                    field.focus_set()
                    self.error.set(tr("Enter a value for {key}.", key=tr(caption)))
                    return False
        try:
            save_recipe(self.catalog, data, self.path, self.original)
        except ERRORS as exc:
            self.error.set(tr(error_detail(exc)))
            return False
        self.item = catalog_item(data)
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
