"""Add and edit catalog recipes in a local, themed dialog."""

import json
import os
from pathlib import Path
import tempfile
import tkinter as tk
from tkinter import ttk

from companion_buttons import add_companion_buttons
from fetch_sources import Recipe, WINDOWS_ISO_PAGE, WINDOWS_ISO_RESOLVER
from fetch_store import fetch_lock
from releaseshelf import ERRORS
from shelf_icons import make_icon
from shelf_checklist import catalog_item, read_catalog_item
from shelf_i18n import tr
from shelf_message import error_detail, message
from shelf_paint import stage_window
from shelf_progress import DownloadProgress
from shelf_scroll import grid_scrollable
from shelf_text import FittedLabel
from shelf_theme import WINDOW_SIDE_PADDING, category_picker, initial_geometry, style_titlebar


PAGE_FIELDS = ("page", "version_pattern", "download", "download_resolver", "sha256_pattern")
GITHUB_FIELDS = ("github", "asset_pattern")
SOURCE_FIELDS = {"Publisher page": PAGE_FIELDS, "GitHub release": GITHUB_FIELDS,
                 "Direct download": ("direct_download",), "Local file or folder": ("local_path",)}
OPTIONAL_FIELDS = ("filename", "sha256", "product_version", "publish_exe", "extract_folder", "shortcut")
FIELD_LABELS = {
    "id": "Item ID", "name": "Name", "category": "Category",
    "page": "Publisher page URL", "version_pattern": "Version pattern", "download": "Download URL",
    "direct_download": "Download URL", "local_path": "Local path",
    "github": "Repository", "asset_pattern": "Asset pattern", "filename": "Archive filename",
    "sha256": "Archive SHA-256", "sha256_pattern": "SHA-256 pattern (optional)",
    "product_version": "Check EXE version", "publish_exe": "Publish EXE",
    "extract_folder": "Publish archive folder", "shortcut": "Start menu shortcut name",
}
ACTIONS = {"check": "Check for updates", "fetch": "Get updates", "verify": "Verify files"}
ACTION_ICONS = {"check": "check", "fetch": "download", "verify": "verify"}
# The clam notebook contributes two pixels at each edge, one more than the catalog card.
TAB_PADDING = (WINDOW_SIDE_PADDING // 2 - 1, 16)


def text_field(parent, *, row, column=0, pady=0, style="TFrame", **options):
    frame = ttk.Frame(parent, style=style)
    frame.grid(row=row, column=column, sticky="nsew", pady=pady)
    text = tk.Text(frame, **options)
    grid_scrollable(text, horizontal=options.get("wrap") == "none")
    return text


def recipe_bytes(data):
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").replace("\n", os.linesep).encode("utf-8")


def save_recipe(catalog, data, path, original):
    previous = None
    if original is not None:
        try:
            previous = catalog_item(json.loads(original.decode("utf-8-sig")))
        except ERRORS:
            pass  # An invalid recipe can be repaired in the raw editor.
    renaming = isinstance(previous, Recipe) and isinstance(data, dict) and data.get("id") != previous.id
    if renaming:
        history = [old_id for old_id in (*previous.previous_ids, previous.id) if old_id != data.get("id")]
        data = {**data, "previous_ids": history}
    recipe = catalog_item(data)
    adding = path is None
    if adding:
        path = catalog / f"{recipe.id}.json"
    with fetch_lock(catalog):
        if adding and path.exists():
            raise ValueError(message("{filename} already exists. Choose another ID or edit the existing recipe.",
                                     filename=path.name))
        if not adding and path.read_bytes() != original:
            raise ValueError("This recipe changed on disk. Cancel and reopen it to load the latest version.")
        owners = {}
        for other in catalog.glob("*.json"):
            if other == path:
                continue
            try:
                other_recipe = read_catalog_item(other)
            except OSError:
                raise
            except ERRORS:
                continue  # An unrelated invalid recipe can be repaired separately.
            if (isinstance(recipe, Recipe) and recipe.shortcut and isinstance(other_recipe, Recipe)
                    and recipe.shortcut.casefold() == other_recipe.shortcut.casefold()):
                raise ValueError(f"Start menu shortcut {recipe.shortcut!r} is already used by {other.name}.")
            owners.update(dict.fromkeys(other_recipe.id_keys(), other.name))
        if renaming and isinstance(recipe, Recipe):
            # A previously shared ID belongs to the remaining app when repairing a duplicate.
            data["previous_ids"] = [value for value in recipe.previous_ids if value.casefold() not in owners]
        for app_id in (recipe.id, *data.get("previous_ids", [])):
            owner = owners.get(app_id.casefold())
            if owner:
                raise ValueError(message("Item ID “{app_id}” is already used by {owner}.", app_id=app_id, owner=owner))
        with tempfile.NamedTemporaryFile(dir=catalog, suffix=".tmp", delete=False) as handle:
            temp = Path(handle.name)
        try:
            temp.write_bytes(recipe_bytes(data))
            if adding and os.name == "nt":
                temp.rename(path)  # Windows rename refuses to overwrite a new file.
            elif adding:
                os.link(temp, path)
            else:
                temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
    return path


class RecipeEditor(tk.Toplevel):
    def __init__(self, parent, catalog, path, palette, on_saved, *, on_action=None, categories=()):
        self.original = path.read_bytes() if path is not None else None
        self.data = {}
        raw = None
        if self.original is not None:
            content = self.original.decode("utf-8-sig")
            try:
                self.data = json.loads(content)
                catalog_item(self.data, path=path)
            except ERRORS:
                raw = content
        super().__init__(parent)
        stage_window(self)
        self.catalog, self.path, self.on_saved = catalog, path, on_saved
        self.on_action = on_action
        self.title(tr("Edit item" if path else "Add item"))
        self.transient(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        frame = ttk.Frame(self, padding=(WINDOW_SIDE_PADDING // 2, 18))
        frame.grid(sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.fields = {}
        self.buttons = {}
        self.images = {}
        self.tab_icons = {}
        self.detail = None
        self.raw_editor = raw is not None
        self.tabs = tabs = ttk.Notebook(frame, style="Editor.TNotebook")
        tabs.grid(row=0, column=0, sticky="nsew")
        if self.raw_editor:
            area = ttk.Frame(tabs, padding=TAB_PADDING)
            area.columnconfigure(0, weight=1)
            area.rowconfigure(1, weight=1)
            self.add_tab(area, "Recipe JSON", "edit")
            ttk.Label(area, text=tr("Repair the recipe JSON in {filename} before saving.", filename=path.name),
                      style="Muted.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
            self.text_area = text_field(area, row=1, width=64, height=15, undo=True, wrap="word")
            self.text_area.insert("1.0", raw)
            first = self.text_area
        else:
            catalog_tab = ttk.Frame(tabs, padding=TAB_PADDING)
            catalog_tab.columnconfigure(1, weight=1)
            self.add_tab(catalog_tab, "Catalog", "folders")
            first = self.field(catalog_tab, 0, "id")
            ttk.Label(catalog_tab, text=tr("Choose a unique item ID, such as my-tool."),
                      style="Muted.TLabel").grid(row=1, column=1, sticky="w", pady=(0, 10))
            self.field(catalog_tab, 2, "name")
            ttk.Label(catalog_tab, text=tr("Leave blank to show the item ID."),
                      style="Muted.TLabel").grid(row=3, column=1, sticky="w", pady=(0, 10))
            self.field(catalog_tab, 4, "category", choices=categories)
            self.apply_settings = tk.BooleanVar(self, self.data.get("apply_custom_settings", False))
            if path is not None and "local_path" not in self.data:
                item = Recipe.from_data(self.data, path=path)
                if item.apply_custom_settings or item.has_portable_settings():
                    self.settings_button = ttk.Checkbutton(catalog_tab, text=tr("Apply custom settings"),
                                                           variable=self.apply_settings)
                    self.settings_button.grid(row=5, column=1, sticky="w", pady=(8, 0))
                    ttk.Label(catalog_tab, text=tr("Use the saved config on Save and Get updates."),
                              style="Muted.TLabel").grid(row=6, column=1, sticky="w")
                add_companion_buttons(catalog_tab, self.data, lambda: item.companion_dir,
                                      lambda error: self.error.set(error), 7, open_instructions=False)
            source = ttk.Frame(tabs, padding=TAB_PADDING)
            source.columnconfigure(1, weight=1)
            self.add_tab(source, "Source", "language")
            ttk.Label(source, text=tr("Source type")).grid(row=0, column=0, sticky="w", padx=(0, 12))
            switch = ttk.Frame(source)
            switch.grid(row=0, column=1, sticky="w", pady=(2, 9))
            current_source = next((source for source, fields in SOURCE_FIELDS.items() if fields[0] in self.data),
                                  "Publisher page")
            self.source = tk.StringVar(self, current_source)
            self.source_frames = {}
            for name in SOURCE_FIELDS:
                ttk.Radiobutton(switch, text=tr(name), value=name, variable=self.source, style="Theme.Toolbutton",
                                command=self.show_source).pack(side="left")
                group = ttk.Frame(source)
                group.columnconfigure(1, weight=1)
                group.grid(row=1, column=0, columnspan=2, sticky="nsew")
                self.source_frames[name] = group
            page = self.source_frames["Publisher page"]
            self.field(page, 0, "page")
            self.field(page, 1, "version_pattern")
            ttk.Label(page, text=tr("Use one capture group, e.g. v([0-9.]+)"), style="Muted.TLabel"
                      ).grid(row=2, column=1, sticky="w")
            self.field(page, 3, "sha256_pattern")
            self.download_entry = self.field(page, 4, "download")
            resolver = self.fields["download_resolver"] = tk.StringVar(self, self.data.get("download_resolver", ""))
            self.windows_iso_button = ttk.Checkbutton(
                page, text=tr("Request a Windows 11 English x64 ISO link from Microsoft"),
                variable=resolver, onvalue=WINDOWS_ISO_RESOLVER, offvalue="", command=self.show_download)
            self.windows_iso_button.grid(row=5, column=1, sticky="w")
            self.fields["page"].trace_add("write", self.show_download)
            self.show_download()
            ttk.Label(page, text=tr("Use {version} in the download URL or archive filename."),
                      style="Muted.TLabel").grid(row=6, column=1, sticky="w", pady=(5, 0))
            github = self.source_frames["GitHub release"]
            self.field(github, 0, "github")
            self.field(github, 1, "asset_pattern")
            direct = self.source_frames["Direct download"]
            self.field(direct, 0, "direct_download")
            ttk.Label(direct, text=tr("Track the publisher's download modification time."),
                      style="Muted.TLabel").grid(row=1, column=1, sticky="w")
            self.field(self.source_frames["Local file or folder"], 0, "local_path")
            ttk.Label(github, text=tr("Example: owner/repository and *-windows-x64.zip"), style="Muted.TLabel"
                      ).grid(row=2, column=1, sticky="w")
            self.show_source()
            options = ttk.Frame(tabs, padding=TAB_PADDING)
            options.columnconfigure(1, weight=1)
            self.add_tab(options, "Download options", "list_checks")
            self.field(options, 0, "filename")
            ttk.Label(options, text=tr("Archive members")).grid(row=1, column=0, sticky="nw", pady=7)
            self.text_area = text_field(options, row=1, column=1, pady=5, width=40, height=3,
                                        undo=True, wrap="none")
            self.text_area.insert("1.0", "\n".join(self.data.get("extract", [])))
            ttk.Label(options, text=tr("One filename per line, in publication order."), style="Muted.TLabel"
                      ).grid(row=2, column=1, sticky="w")
            self.field(options, 3, "sha256")
            self.field(options, 4, "product_version")
            self.field(options, 5, "publish_exe")
            self.field(options, 6, "extract_folder")
            self.field(options, 7, "shortcut")
            if path is not None:
                tabs.insert("end", source)
                self.saved_form = self.form_data()
        if path is not None:
            self.add_app_info(tabs)
        self.error = tk.StringVar(self)
        ttk.Label(frame, textvariable=self.error, wraplength=530, style="EditorError.TLabel"
                  ).grid(row=1, column=0, sticky="ew", pady=(8, 2))
        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, sticky="e")
        buttons.columnconfigure((0, 2), weight=1, uniform="actions")
        buttons.columnconfigure(1, minsize=8)
        self.cancel_button = ttk.Button(buttons, text=tr("Cancel"), compound="left", command=self.destroy)
        self.cancel_button.grid(row=0, column=0, sticky="nsew")
        self.save_button = ttk.Button(buttons, text=tr("Save"), compound="left",
                                      style="Primary.TButton", command=self.save)
        self.save_button.grid(row=0, column=2, sticky="nsew")
        self.set_busy(False)
        self.bind("<Escape>", lambda _: self.destroy())
        self.bind("<Control-s>", lambda _: self.save())
        self.bind("<Map>", lambda event: self.apply_titlebar() if event.widget is self else None)
        self.apply_theme(palette)
        width, height = (720, 560) if path is not None else (0, 0)
        initial_geometry(self, width, height, parent=parent)
        self.grab_set()
        first.focus_set()

    def add_tab(self, area, label, icon):
        self.tabs.add(area, text=tr(label), compound="left")
        self.tab_icons[area] = icon

    def add_app_info(self, tabs):
        info = ttk.Frame(tabs, padding=TAB_PADDING)
        info.columnconfigure(0, weight=1)
        info.rowconfigure(0, weight=1)
        self.add_tab(info, "Item info", "info")
        panel = ttk.Frame(info, style="Surface.TFrame", padding=(0, 12))
        panel.grid(row=0, column=0, sticky="nsew", pady=(0, 12))
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(1, weight=1)
        self.detail_title = FittedLabel(panel, style="Detail.TLabel")
        self.detail_title.grid(row=0, column=0, sticky="ew", padx=12, pady=(0, 6))
        self.detail = text_field(panel, row=1, style="Surface.TFrame", width=42, height=6,
                                 state="disabled", wrap="word", font=("Segoe UI", 10), padx=12,
                                 spacing1=2, relief="flat", borderwidth=0, highlightthickness=0)
        self.download_progress = DownloadProgress(panel)
        self.download_progress.grid(row=2, column=0, sticky="ew", padx=12, pady=(8, 0))
        self.download_progress.grid_remove()
        actions = ttk.Frame(info)
        actions.grid(row=1, column=0, sticky="w")
        for action, label in ACTIONS.items():
            button = ttk.Button(actions, text=tr(label), compound="left",
                                command=lambda a=action: self.run_action(a))
            button.pack(side="left", padx=(0, 6))
            self.buttons[action] = button

    def set_app_info(self, title, detail):
        self.detail_title.set_text(title)
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", detail)
        self.detail.configure(state="disabled")

    def set_busy(self, busy):
        self.busy = busy
        self.save_button.configure(state="disabled" if busy else "normal")
        enabled = not busy and not self.raw_editor and self.on_action is not None
        for action, button in self.buttons.items():
            allowed = enabled and (action == "verify" or not self.data.get("updates_disabled"))
            button.configure(state="normal" if allowed else "disabled")

    def run_action(self, action):
        if self.busy or self.raw_editor or self.on_action is None:
            return
        try:
            unchanged = self.form_data() == self.saved_form
        except ValueError:
            unchanged = False
        if not unchanged:
            self.error.set(tr("Save your changes before running this action."))
            return
        try:
            latest = self.path.read_bytes()
        except OSError as exc:
            self.error.set(tr(error_detail(exc)))
            return
        if latest != self.original:
            self.error.set(tr("This recipe changed on disk. Close and reopen it to load the latest version."))
            return
        self.error.set("")
        self.on_action(action)

    def field(self, group, row, key, *, choices=None):
        ttk.Label(group, text=tr(FIELD_LABELS[key])).grid(row=row, column=0, sticky="w", padx=(0, 12))
        value = tk.StringVar(self, value=self.data.get(key, ""))
        self.fields[key] = value
        if choices is None:
            entry = ttk.Entry(group, textvariable=value, width=49)
        else:
            entry = category_picker(group, value, choices, width=49)
        entry.grid(row=row, column=1, sticky="ew", pady=5)
        return entry

    def show_source(self):
        for name, group in self.source_frames.items():
            if name == self.source.get():
                group.grid()
            else:
                group.grid_remove()

    def show_download(self, *_):
        resolver = self.fields["download_resolver"]
        if self.fields["page"].get() == WINDOWS_ISO_PAGE:
            self.windows_iso_button.grid()
        else:
            self.windows_iso_button.grid_remove()
            resolver.set("")
        self.download_entry.state(["disabled" if resolver.get() else "!disabled"])

    def apply_titlebar(self):
        style_titlebar(self, self.palette.is_dark)

    def apply_theme(self, palette):
        self.palette = palette
        self.images["cancel"] = make_icon(self, "diskette_x", palette.text)
        self.cancel_button.configure(image=self.images["cancel"])
        normal = make_icon(self, "save", palette.on_accent)
        disabled = make_icon(self, "save", palette.muted)
        self.save_button.configure(image=(normal, "disabled", disabled))
        self.images["save"] = (normal, disabled)
        for area, icon in self.tab_icons.items():
            normal = make_icon(self, icon, palette.muted)
            active = make_icon(self, icon, palette.text)
            self.tabs.tab(area, image=(normal, "selected", active, "active", active))
            self.images[area] = (normal, active)
        for action, button in self.buttons.items():
            normal = make_icon(self, ACTION_ICONS[action], palette.text)
            disabled = make_icon(self, ACTION_ICONS[action], palette.muted)
            button.configure(image=(normal, "disabled", disabled))
            self.images[action] = (normal, disabled)
        self.text_area.configure(bg=palette.surface, fg=palette.text, insertbackground=palette.text,
                                 selectbackground=palette.selection, selectforeground=palette.text,
                                 highlightthickness=1, highlightbackground=palette.line,
                                 highlightcolor=palette.accent, relief="flat", padx=5, pady=3)
        if self.detail is not None:
            self.detail.configure(bg=palette.surface, fg=palette.text, insertbackground=palette.text,
                                  selectbackground=palette.selection, selectforeground=palette.text)
        if self.winfo_ismapped():
            self.apply_titlebar()

    def form_data(self):
        if self.raw_editor:
            return json.loads(self.text_area.get("1.0", "end-1c"))
        data = self.data.copy()
        if self.apply_settings.get() or "apply_custom_settings" in data:
            data["apply_custom_settings"] = self.apply_settings.get()
        for key in ("name", "category"):
            value = self.fields[key].get().strip()
            if value:
                data[key] = value
            else:
                data.pop(key, None)
        source = self.source.get()
        if source == "Local file or folder":
            data.pop("updates_disabled", None)
        for other, fields in SOURCE_FIELDS.items():
            if other != source:
                for key in fields:
                    data.pop(key, None)
        required = SOURCE_FIELDS[source]
        if source == "Publisher page":
            required = ("page", "version_pattern")
            resolver = self.fields["download_resolver"].get()
            if resolver:
                data.pop("download", None)
                data["download_resolver"] = resolver
            else:
                data.pop("download_resolver", None)
                required += ("download",)
            if pattern := self.fields["sha256_pattern"].get():
                data["sha256_pattern"] = pattern
            else:
                data.pop("sha256_pattern", None)
        for key in ("id", *required):
            value = self.fields[key].get()
            if not value:
                raise ValueError(message("Enter a value for {key}.", key=message(FIELD_LABELS[key])))
            data[key] = value
        if source == "Local file or folder":
            return {key: data[key] for key in ("id", "name", "category", "local_path") if key in data}
        for key in OPTIONAL_FIELDS:
            value = self.fields[key].get()
            if value:
                data[key] = value
            elif value != self.data.get(key):
                data.pop(key, None)
        members = [name for name in self.text_area.get("1.0", "end-1c").splitlines() if name]
        if members or "extract" in data:
            data["extract"] = members
        return data

    def save(self):
        if self.busy:
            return
        try:
            path = save_recipe(self.catalog, self.form_data(), self.path, self.original)
        except ERRORS as exc:
            self.error.set(tr(error_detail(exc)))
            return
        self.destroy()
        self.on_saved(path)
