"""Local desktop catalog for ReleaseShelf."""

import argparse
import ctypes
import json
from math import ceil
import os
from pathlib import Path
from queue import Empty, Queue
import sys
from threading import Event, Thread
import tkinter as tk
from tkinter import font as tkfont, messagebox, ttk
from urllib.parse import urlsplit

from checklist_window import ChecklistWindow
from fetch_sources import Recipe
from fetch_store import Result, apply_published_settings, read_state
from recipe_editor import ACTIONS, ACTION_ICONS, RecipeEditor, save_recipe
from releaseshelf import (DEFAULT_CATALOG, ERRORS, MAX_PARALLEL_ITEMS, ChecklistLoadError, batch_map, default_destination,
                          execute, load_targets, local_result, prepare_fetch, read_jobs, updates_disabled)
from shelf_app_icons import load_app_icon
from shelf_categories import MOVE, RENAME, CategoryDialog, change_category
from shelf_checklist import Checklist, ChecklistHistory, LocalItem, catalog_item, run_checklist_step
from shelf_checkboxes import checkbox_image, header_image
from shelf_delete import DATA, DELETE, FOLDER, delete_item, deletion_plan
from shelf_icons import make_icon
from shelf_i18n import set_language, tr
from shelf_menu import AppMenu, DropdownMenu
from shelf_message import error_detail, message
from shelf_options import OptionsWindow, read_language
from shelf_paint import keep_view_visible, show_window, stage_window
from shelf_progress import DownloadProgress
from shelf_scroll import grid_scrollable
from shelf_text import CELL_TEXT_INSETS, FittedLabel, FittedTreeview, fit_text
from shelf_theme import (PALETTES, WINDOW_SIDE_PADDING, initial_geometry,
                        preference_path, read_appearance, style_titlebar, style_widgets, system_mode,
                        write_appearance, write_preference)
from shelf_version import __version__


APPLY_ACTIONS = {"apply_selected": "Apply selected", "apply_all": "Apply all items"}
ALL_ACTIONS = {"check": "Check for updates", "fetch": "Update all items", "verify": "Verify all items", **APPLY_ACTIONS}
BULK_ICONS = {**ACTION_ICONS, "apply_selected": "list_checks", "apply_all": "list_checks"}
VIEWS = {"checklist": "Checklist", "all": "Catalog", "updates": "Available updates", "progress": "In progress",
         "attention": "Needs attention", "local": "Local only", "disabled": "Disabled"}
WORKING = {"check": "Checking", "fetch": "Updating", "verify": "Verifying", "apply": "Applying"}
RESULT_STATUSES = ("Not checked", "Pending", "Error", "Latest", "Updated", "Cached",
                   "Repair needed", "Update available", "Source error", "Verified", "Disabled", "Local only")
COLUMN_WIDTHS = {"Name": 126, "Category": 100, "Local version": 94,
                 "Available version": 115, "Status": 108, "Source": 110}
SORTABLE_COLUMNS = ("Name", "Category", "Status")
SEARCH_CHAR_LIMIT = 64
ASSETS = Path(__file__).resolve().parent / "assets"


def eligible_for_action(action, job):
    return (not isinstance(job, (Checklist, ChecklistLoadError, LocalItem))
            and (action == "verify" or not updates_disabled(job)))


def initial_result(job, dest, checklist_history):
    if isinstance(job, Result):
        return job
    if isinstance(job, Checklist):
        return Result(checklist_history.summary(job))
    if isinstance(job, LocalItem):
        return local_result(job)
    try:
        root = job.cache_root(dest)
        current = read_state(root, "current.json")
        pending = read_state(root, "pending.json")
    except ERRORS as exc:
        return Result("Error", detail=error_detail(exc))
    local = current["version"] if current else "-"
    if pending:
        return Result("Pending", local, detail="Run Get updates to retry publication.")
    return Result("Not checked", local, detail="Check for updates to see whether a newer version is available.")


def run_batch(events, action, items, dest, targets_path):
    def check(item):
        index, (key, job, cancel_event) = item
        events.put(("started", key, "check" if action == "fetch" else action, index))
        if action == "fetch":
            result = prepare_fetch(job, dest, targets, cancel_event=cancel_event)
            if not isinstance(result, Result):
                events.put(("prepared", key))
                return key, result, cancel_event
        else:
            result = execute(action, job, dest, targets, cancel_event=cancel_event)
        events.put(("result", key, result))

    def download(item):
        key, plan, cancel_event = item
        events.put(("started", key, "fetch", None))

        def progress(received, total):
            events.put(("progress", key, received, total))

        result = plan.run(on_progress=progress, cancel_event=cancel_event)
        events.put(("result", key, result))

    try:
        targets = load_targets(targets_path, dest)
        downloads = [plan for plan in batch_map(action, check, enumerate(items, 1)) if plan]
        if downloads:
            events.put(("downloads", tuple(key for key, _, _ in downloads)))
            list(batch_map(action, download, downloads))
    except Exception as exc:
        events.put(("failed", error_detail(exc)))
    finally:
        events.put(("done",))


def run_checklists(events, items, history):
    try:
        for index, (key, item, token) in enumerate(items, 1):
            if not token.is_set():
                events.put(("started", key, "apply", index))
            for step in item.steps:
                if token.is_set():
                    events.put(("result", key, Result("Canceled")))
                    break
                status, output, history_error = run_checklist_step(
                    item, step, item.software_folder, Path(key).parent, history)
                if history_error or status == "Error":
                    events.put(("failed", f"{item.name or item.id} · {step.label}: {history_error or output}"))
            else:
                events.put(("result", key, Result(history.summary(item))))
    except Exception as exc:
        events.put(("failed", error_detail(exc)))
    finally:
        events.put(("done",))


class ShelfWindow:
    def __init__(self, root, catalog, dest, targets, *, appearance_file=None):
        self.root, self.catalog, self.dest, self.targets = root, catalog, dest, targets
        self.jobs, self.results = {}, {}
        self.checked_items = set()
        self.busy = False
        self.requested_action = self.bulk_action = None
        self.cancel_events = {}
        self.active_items = {}
        self.transfers = {}
        self.editor = None
        self.options = None
        self.events = Queue()
        root.title("ReleaseShelf")
        if sys.platform == "win32":
            root.iconbitmap(default=str(ASSETS / "releaseshelf.ico"))
        else:
            root.iconphoto(True, tk.PhotoImage(master=root, file=ASSETS / "releaseshelf-32.png"))
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.columnconfigure(2, weight=1)
        root.rowconfigure(0, weight=1)
        self.style = ttk.Style(root)
        self.style.theme_use("clam")
        self.appearance_file = appearance_file or preference_path()
        self.checklist_history = ChecklistHistory(self.appearance_file.with_name("checklist-results"), catalog)
        self.app_icons_file = self.appearance_file.with_name("app-icons.txt")
        try:
            show_icons = self.app_icons_file.read_text(encoding="utf-8").strip() == "show"
        except (OSError, UnicodeError):
            show_icons = False
        self.show_app_icons = tk.BooleanVar(root, value=show_icons)
        self.app_images = {}
        self.language_file = self.appearance_file.with_name("language.txt")
        self.language = read_language(self.language_file)
        set_language(self.language)
        mode, palette_name = read_appearance(self.appearance_file)
        self.mode = tk.StringVar(root, value=mode)
        self.palette_name = tk.StringVar(root, value=palette_name)
        self.palette = None
        self.theme_timer = None

        self.view = tk.StringVar(root, value="all")
        self.sort_column = "Category"
        self.sort_descending = False
        self.search, self.page_title = tk.StringVar(root), tk.StringVar(root)
        self.empty_title, self.empty_detail = tk.StringVar(root), tk.StringVar(root)
        self.view_counts = {name: tk.StringVar(root) for name in VIEWS}
        self.category_groups = {}
        self.icon_widgets = {}
        self.images = {}
        self.build_sidebar()

        body = ttk.Frame(root, padding=(WINDOW_SIDE_PADDING, 18, WINDOW_SIDE_PADDING, 16))
        body.grid(row=0, column=2, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)
        heading = ttk.Frame(body)
        heading.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        heading.columnconfigure(0, weight=1)
        self.title_label = ttk.Label(heading, textvariable=self.page_title, style="Title.TLabel", anchor="w")
        self.title_label.grid(row=0, column=0, sticky="nsew")
        self.title_measure = ttk.Label(heading, style="Title.TLabel")
        self.title_label.bind("<Configure>", self.fit_page_title)
        self.build_search(heading)
        card = ttk.Frame(body, style="Line.TFrame", padding=1)
        card.grid(row=1, column=0, sticky="nsew")
        card.columnconfigure(0, weight=1)
        card.rowconfigure(2, weight=1)
        self.build_commands(card)
        self.build_table(card)
        self.build_footer()
        self.captions = {widget: str(widget.cget("text")) for widget in (
            *self.nav_buttons.values(), self.categories_heading, self.categories_button,
            self.options_button, self.search_clear, self.reset_search_button,
            self.add_button, self.edit_button, self.more_button)}
        self.search.trace_add("write", self.search_changed)
        self.view.trace_add("write", self.view_changed)
        root.bind("<Control-f>", self.focus_search)
        root.bind("<ButtonPress>", self.blur_search_on_outside_click, add="+")
        root.bind("<Map>", self.on_map, add="+")
        root.bind("<Destroy>", self.on_destroy, add="+")
        self.refresh_theme()
        self.refresh_labels()
        self.highlight_view()
        self.reload()
        self.update_search_hint()
        initial_geometry(root, 1120, 680)
        root.minsize(min(760, root.winfo_width()), min(430, root.winfo_height()))

    def build_sidebar(self):
        rail = ttk.Frame(self.root, style="Sidebar.TFrame", padding=(10, 19, 10, 16))
        rail.grid(row=0, column=0, sticky="ns")
        rail.rowconfigure(len(VIEWS) + 1, weight=1)
        ttk.Frame(self.root, width=1, style="Line.TFrame").grid(row=0, column=1, sticky="ns")
        self.sidebar_compact = False

        self.nav_buttons, self.nav_counts = {}, {}
        nav_icons = {"checklist": "list_checks", "all": "library", "updates": "updates",
                     "progress": "hourglass", "attention": "problems", "local": "folder", "disabled": "pause_circle"}
        for row, (name, title) in enumerate(VIEWS.items()):
            button = ttk.Button(rail, text=title, command=lambda n=name: self.select_view(n), style="Nav.TButton",
                                compound="left")
            button.grid(row=row, column=0, sticky="ew", pady=2)
            self.nav_buttons[name] = button
            self.icon_widgets[button] = (nav_icons[name], "text", 20)
            count = ttk.Label(button, textvariable=self.view_counts[name], style="Count.Nav.TLabel")
            count.place(relx=1, rely=0.5, x=-10, anchor="e")
            # A count is part of the button's hit area, including while it is held down.
            for event in ("<Enter>", "<Leave>", "<ButtonPress-1>", "<ButtonRelease-1>"):
                count.bind(event, lambda e, b=button, seq=event: b.event_generate(
                    seq, x=e.x_root - b.winfo_rootx(), y=e.y_root - b.winfo_rooty(), state=e.state))
            # ttk's class bindings set the button state after widget bindings run.
            for event in ("<Enter>", "<Leave>", "<ButtonPress-1>", "<ButtonRelease-1>", "<space>", "<<Invoke>>"):
                button.bind(event, lambda _, n=name: self.root.after_idle(self.sync_nav_count, n), add="+")
            self.nav_counts[name] = count

        self.categories_heading = ttk.Label(rail, text="Categories", style="SidebarHeading.TLabel")
        self.categories_heading.grid(row=len(VIEWS), column=0, sticky="ew", padx=10, pady=(14, 4))
        self.sidebar_body = ttk.Frame(rail, style="Sidebar.TFrame")
        self.sidebar_body.grid(row=len(VIEWS) + 1, column=0, sticky="nsew")
        self.sidebar_body.columnconfigure(0, weight=1)
        self.sidebar_body.rowconfigure(1, weight=1)
        self.category_panel = ttk.Frame(self.sidebar_body, style="Sidebar.TFrame")
        self.category_panel.grid(row=0, column=0, sticky="ew")
        self.category_list = FittedTreeview(self.category_panel, columns=("name", "count"), show="",
                                            height=1, selectmode="browse", style="Categories.Treeview")
        self.category_list.column("name", width=80, minwidth=1)
        self.category_list.column("count", stretch=False, anchor="e")
        grid_scrollable(self.category_list, reserve_space=False)
        self.category_list.bind("<<TreeviewSelect>>", self.category_selected)
        self.category_menu = AppMenu(self.category_list)
        self.category_menu.add_action(RENAME, lambda: self.edit_category(rename=True), icon="edit")
        self.category_menu_key = None
        self.bind_context_menu(self.category_list)
        self.categories_button = ttk.Button(rail, text="Categories", compound="image",
                                            style="Compact.Nav.TButton", command=self.show_categories)
        self.icon_widgets[self.categories_button] = ("folders", "text", 20)

        self.sidebar_footer = footer = ttk.Frame(self.sidebar_body, style="Sidebar.TFrame")
        footer.grid(row=1, column=0, sticky="sew")
        footer.columnconfigure(0, weight=1)
        self.options_button = ttk.Button(footer, text="Options", command=self.open_options,
                                         style="Nav.TButton", compound="left")
        self.options_button.grid(row=1, column=0, sticky="ew", pady=2)
        self.icon_widgets[self.options_button] = ("settings", "text", 20)

        self.sidebar_toggle = ttk.Button(footer, text="Collapse sidebar", compound="image",
                                         style="Compact.Nav.TButton", command=self.toggle_sidebar)
        self.position_sidebar_toggle()
        self.icon_widgets[self.sidebar_toggle] = ("collapse", "text", 20)
        self.sidebar_body.bind("<Configure>", self.fit_category_rows)

        self.sidebar_hint = ttk.Label(self.root, style="SidebarHint.TLabel")
        for button in (self.sidebar_toggle, *self.nav_buttons.values(), self.categories_button, self.options_button):
            for event in ("<Enter>", "<FocusIn>"):
                button.bind(event, self.show_sidebar_hint, add="+")
            for event in ("<Leave>", "<FocusOut>"):
                button.bind(event, lambda _: self.sidebar_hint.place_forget(), add="+")

    def position_sidebar_toggle(self):
        if self.sidebar_compact:
            self.sidebar_toggle.grid(row=0, column=0, sticky="ew", padx=0, pady=2)
        else:
            self.sidebar_toggle.grid(row=1, column=1, sticky="e", padx=(4, 0), pady=2)

    def show_sidebar_hint(self, event):
        button = event.widget
        if button is self.sidebar_toggle or self.sidebar_compact:
            self.sidebar_hint.configure(text=button.cget("text"))
            self.sidebar_hint.place(in_=button, relx=1, x=8, rely=0.5, anchor="w")
            self.sidebar_hint.lift()

    def toggle_sidebar(self):
        self.sidebar_compact = compact = not self.sidebar_compact
        self.position_sidebar_toggle()
        self.position_categories()
        self.sidebar_hint.place_forget()
        for button in (*self.nav_buttons.values(), self.options_button):
            button.configure(compound="image" if compact else "left",
                             style="Compact.Nav.TButton" if compact else "Nav.TButton")
        for count in self.nav_counts.values():
            if compact:
                count.place_forget()
            else:
                count.place(relx=1, rely=0.5, x=-10, anchor="e")
        self.sidebar_toggle.configure(text=tr("Expand sidebar" if compact else "Collapse sidebar"))
        self.icon_widgets[self.sidebar_toggle] = ("expand" if compact else "collapse", "text", 20)
        self.refresh_icon(self.sidebar_toggle)

    def position_categories(self):
        self.categories_heading.grid_remove()
        self.category_panel.grid_remove()
        self.categories_button.grid_remove()
        expanded = bool(self.category_groups) and not self.sidebar_compact
        self.sidebar_footer.grid_configure(sticky="new" if expanded else "sew")
        if self.category_groups:
            if self.sidebar_compact:
                self.categories_button.grid(row=len(VIEWS), column=0, sticky="ew", pady=(12, 2))
            else:
                self.categories_heading.grid()
                self.category_panel.grid()
                self.fit_category_rows()

    def fit_category_rows(self, event=None):
        if self.sidebar_compact or not self.category_groups:
            return
        available = self.sidebar_body.winfo_height() - self.sidebar_footer.winfo_reqheight()
        row_height = int(self.style.lookup("Categories.Treeview", "rowheight"))
        rows = min(len(self.category_groups), max(0, available // row_height))
        if int(self.category_list.cget("height")) != rows:
            self.category_list.configure(height=rows)

    def show_categories(self):
        self.toggle_sidebar()
        self.category_list.focus_set()

    def category_selected(self, event):
        selected = self.category_list.selection()
        if selected and selected[0] != self.view.get():
            self.select_view(selected[0])

    def refresh_categories(self):
        groups = {}
        for key, job in self.jobs.items():
            if isinstance(job, (Recipe, LocalItem)):
                groups.setdefault("category:" + job.category, []).append(key)
        self.category_groups = groups
        listing = self.category_list
        font = tkfont.Font(root=self.root, font=self.style.lookup(listing.cget("style"), "font"))
        label_width = max((font.measure(self.view_title(view)) for view in groups), default=0)
        listing.column("name", width=max(80, min(label_width + 32, 240)))
        text_width = max((font.measure(str(len(keys))) for keys in groups.values()), default=0)
        # Allow for the themed cell padding and Tk's insets on both sides of the text.
        count_width = max(40, text_width + 32)
        listing.column("count", width=count_width, minwidth=count_width)
        listing.delete(*listing.get_children())
        for view in sorted(groups, key=lambda v: self.view_title(v).casefold()):
            listing.insert("", "end", iid=view)
            listing.set_values(view, (self.view_title(view), len(groups[view])))
        self.position_categories()
        if self.view.get() not in VIEWS and self.view.get() not in groups:
            self.view.set("all")
        else:
            self.highlight_view()

    def view_title(self, view):
        if view in VIEWS:
            return tr(VIEWS[view])
        return view.removeprefix("category:") or tr("Uncategorized")

    def fit_page_title(self, event=None):
        width = self.title_label.winfo_width()
        if width <= 1:
            return
        # Reserve space for every view before a click, including wrapped translations.
        measure = self.title_measure
        measure.configure(text="")
        wrap = max(1, width - measure.winfo_reqwidth())
        self.title_label.configure(wraplength=wrap)
        measure.configure(wraplength=wrap)
        height = 0
        for caption in VIEWS.values():
            measure.configure(text=tr(caption))
            height = max(height, measure.winfo_reqheight())
        self.title_label.master.rowconfigure(0, minsize=height)
        self.update_page_title()

    def update_page_title(self):
        view = self.view.get()
        title = self.view_title(view)
        if view not in VIEWS and self.title_label.winfo_width() > 1:
            width = self.title_label.winfo_width() - 2
            title = fit_text(self.title_label, self.style.lookup("Title.TLabel", "font"), title, width)
        self.page_title.set(title)

    def build_search(self, heading):
        self.search_border = ttk.Frame(heading, style="Line.TFrame", padding=1)
        self.search_border.grid(row=0, column=1, sticky="e", padx=(10, 0))
        field = ttk.Frame(self.search_border, style="Surface.TFrame", padding=(8, 2))
        field.pack()
        lens = ttk.Label(field, style="Small.TLabel")
        lens.grid(row=0, column=0)
        self.icon_widgets[lens] = ("search", "muted", 16)
        self.search_entry = ttk.Entry(field, textvariable=self.search, width=26, style="Search.TEntry",
                                      validate="key", validatecommand=(
                                          field.register(self.limit_search_input), "%P", "%i", "%S"))
        self.search_entry.grid(row=0, column=1, sticky="ew")
        self.search_hint = FittedLabel(self.search_entry, anchor="w", style="Small.TLabel", cursor="xterm")
        for widget in (self.search_border, field, lens, self.search_hint):
            widget.bind("<Button-1>", self.focus_search)
        self.search_clear = ttk.Button(self.search_entry, text="Clear", compound="image", style="Command.TButton",
                                       padding=(3, 2), command=self.clear_search)
        self.icon_widgets[self.search_clear] = ("close", "muted", 16)
        for widget in (self.search_entry, self.search_clear):
            for event in ("<FocusIn>", "<FocusOut>"):
                widget.bind(event, lambda _: self.root.after_idle(self.update_search_hint))
            widget.bind("<Escape>", self.clear_search)

    def limit_search_input(self, proposed, index, inserted):
        if len(proposed) <= SEARCH_CHAR_LIMIT:
            return True
        # For an oversized paste, keep what fits between the existing prefix and suffix.
        remaining = SEARCH_CHAR_LIMIT - len(self.search.get())
        if remaining > 0:
            self.search_entry.insert(index, inserted[:remaining])
        return False

    def focus_search(self, event=None):
        self.search_entry.focus_set()
        self.search_entry.selection_range(0, "end")
        return "break"

    def clear_search(self, event=None):
        self.tree.focus_set()
        self.search.set("")
        return "break"

    def blur_search_on_outside_click(self, event):
        if self.root.focus_get() not in (self.search_entry, self.search_clear):
            return
        widget = event.widget
        while widget is not None:
            if widget is self.search_border:
                return
            widget = widget.master
        # Labels and empty frames do not take focus when clicked.
        self.root.focus_set()

    def update_search_hint(self):
        self.search_hint.set_text(tr("Search checklist" if self.view.get() == "checklist" else "Search catalog"))
        focused = self.root.focus_get() in (self.search_entry, self.search_clear)
        has_query = bool(self.search.get())
        self.search_border.configure(style="Focus.TFrame" if focused else "Line.TFrame")
        if focused or has_query:
            self.search_hint.place_forget()
        else:
            self.search_hint.place(x=4, rely=0.5, relwidth=1, width=-8, anchor="w")
        if has_query:
            self.search_clear.place(relx=1, rely=0.5, anchor="e")
        else:
            self.search_clear.place_forget()

    def search_changed(self, *_):
        self.update_search_hint()
        self.refresh_view()

    def select_view(self, name):
        if self.view.get() == name:
            self.view.set(name)
            return
        with keep_view_visible(self.root):
            self.view.set(name)

    def view_changed(self, *_):
        self.update_search_hint()
        self.show_batch_buttons()
        self.apply_columns()
        self.refresh_headings()
        self.highlight_view()
        self.refresh_view()

    def sync_nav_count(self, name):
        states = self.nav_buttons[name].state()
        self.nav_counts[name].state([s if s in states else f"!{s}" for s in ("active", "pressed", "selected")])

    def highlight_view(self):
        view = self.view.get()
        for name, button in self.nav_buttons.items():
            state = ["selected" if name == view else "!selected"]
            button.state(state)
            self.sync_nav_count(name)
        in_category = view in self.category_groups
        self.categories_button.state(["selected" if in_category else "!selected"])
        listing = self.category_list
        if in_category:
            listing.selection_set(view)
            listing.focus(view)
            listing.see(view)
        elif listing.selection():
            listing.selection_remove(*listing.selection())

    def build_commands(self, card):
        bar = ttk.Frame(card, style="Surface.TFrame", padding=(9, 8))
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(0, weight=1)
        batch = ttk.Frame(bar, style="Surface.TFrame")
        batch.grid(row=0, column=0, sticky="w")
        self.all_buttons = {}
        for action in ("check", "fetch", *APPLY_ACTIONS):
            primary = action in ("check", "apply_selected")
            button = ttk.Button(batch, text=ALL_ACTIONS[action], command=lambda a=action: self.bulk_command(a),
                                style="Primary.TButton" if primary else "Command.TButton", width=-1, compound="left")
            button.grid(row=0, column=0 if primary else 1, padx=(0, 5))
            self.all_buttons[action] = button
            self.icon_widgets[button] = (BULK_ICONS[action], "on_accent" if primary else "text", 16)
        manage = ttk.Frame(bar, style="Surface.TFrame")
        manage.grid(row=0, column=1, sticky="e")
        self.add_button = ttk.Button(manage, text="Add item", command=self.open_editor, width=-1,
                                     style="Command.TButton", compound="left")
        self.edit_button = ttk.Button(manage, text="Edit item", command=self.edit_selected, width=-1,
                                      style="Command.TButton", compound="left")
        for button, icon in ((self.add_button, "add"), (self.edit_button, "edit")):
            button.pack(side="left", padx=(0, 3))
            self.icon_widgets[button] = (icon, "text", 16)
        self.more_button = ttk.Button(manage, text="More", style="More.TMenubutton", width=-1,
                                      compound="left", takefocus=True)
        self.icon_widgets[self.more_button] = ("more", "text", 16)
        self.more_menu = DropdownMenu(self.more_button)
        self.more_menu.add_action(ALL_ACTIONS["verify"], lambda: self.bulk_command("verify"), icon="verify")
        self.more_menu.add_action("Reload catalog", self.reload, icon="reload")
        self.bulk_buttons = {**self.all_buttons, "verify": self.more_menu.buttons[ALL_ACTIONS["verify"]]}
        self.more_button.pack(side="left")

        def fit_commands(event=None):
            if bar.winfo_width() < batch.winfo_reqwidth() + manage.winfo_reqwidth() + 24:
                manage.grid(row=1, column=0, sticky="w", pady=(4, 0))
            else:
                manage.grid(row=0, column=1, sticky="e", pady=0)
        bar.bind("<Configure>", fit_commands)
        batch.bind("<Configure>", fit_commands)
        manage.bind("<Configure>", fit_commands)
        self.show_batch_buttons()
        ttk.Frame(card, height=1, style="Line.TFrame").grid(row=1, column=0, sticky="ew")

    def show_batch_buttons(self):
        for action, button in self.all_buttons.items():
            if (action in APPLY_ACTIONS) == (self.view.get() == "checklist"):
                button.grid()
            else:
                button.grid_remove()

    def build_table(self, card):
        table = ttk.Frame(card, style="Surface.TFrame")
        table.grid(row=2, column=0, sticky="nsew")
        self.column_widths = COLUMN_WIDTHS.copy()
        cols = tuple(self.column_widths)
        show = ("tree", "headings") if self.show_app_icons.get() else "headings"
        self.tree = FittedTreeview(table, columns=cols, show=show, selectmode="browse", height=4)
        self.tree.column("#0", width=36, minwidth=36, stretch=False)
        self.tree.heading("#0", command=self.toggle_visible_checkboxes)
        self.tree.tk.call(self.tree, "heading", "#0", "state", "alternate")
        for name in cols:
            self.tree.heading(name, anchor="center")
            if name in SORTABLE_COLUMNS:
                self.tree.heading(name, command=lambda column=name: self.sort_by(column))
            self.tree.column(name, minwidth=1, anchor="center")
        grid_scrollable(self.tree, reserve_space=False)
        self.row_bars = []
        for _ in range(MAX_PARALLEL_ITEMS):
            bar = ttk.Progressbar(self.tree, style="Download.Horizontal.TProgressbar", takefocus=False)
            for sequence in ("<ButtonPress>", "<ButtonRelease>", "<B1-Motion>", "<MouseWheel>"):
                bar.bind(sequence, self.forward_row_pointer)
            self.row_bars.append(bar)
        self.tree.bind("<Map>", self.fit_columns, add="+")
        self.tree.bind("<<TreeviewSelect>>", self.controls)
        self.tree.bind("<Button-1>", self.click_checkbox)
        self.tree.bind("<space>", self.toggle_focused_checkbox)
        self.tree.bind("<Double-Button-1>", self.open_row)
        self.tree.bind("<Return>", self.open_row)
        self.build_column_menu()
        self.app_menu = AppMenu(self.tree)
        self.app_menu_key = None
        for action, label in ACTIONS.items():
            self.app_menu.add_action(label, lambda a=action: self.item_command(a), icon=ACTION_ICONS[action])
        self.app_menu.add_action("Disable updates", self.toggle_updates, icon="pause_circle")
        self.app_menu.add_separator()
        self.app_menu.add_action("Open app folder", self.open_app_folder, icon="folders")
        self.app_menu.add_action(MOVE, self.edit_category, icon="folders")
        self.app_menu.add_action("Edit item", lambda: self.open_editor(Path(self.app_menu_key)), icon="edit")
        self.app_menu.add_separator()
        for mode, icon in ((DELETE, "close"), (DATA, "diskette_x"), (FOLDER, "folders")):
            self.app_menu.add_action(mode, lambda m=mode: self.delete_selected_item(m), icon=icon)
        self.bind_context_menu(self.tree)
        self.empty_panel = ttk.Frame(self.tree, style="Surface.TFrame")
        title = FittedLabel(self.empty_panel, style="Empty.TLabel", anchor="center")
        title.pack(fill="x")
        self.empty_title.trace_add("write", lambda *_: title.set_text(self.empty_title.get()))
        ttk.Label(self.empty_panel, textvariable=self.empty_detail, style="Small.TLabel",
                  wraplength=300, justify="center").pack(pady=(4, 0))
        self.reset_search_button = ttk.Button(self.empty_panel, text="Reset search", command=self.clear_search)

    def forward_row_pointer(self, event):
        # Let the tree handle selection, context menus and scrolling over the bar.
        options = {"x": event.widget.winfo_x() + event.x, "y": event.widget.winfo_y() + event.y,
                   "state": event.state, "time": event.time}
        if event.type == tk.EventType.MouseWheel:
            options["delta"] = event.delta
        elif event.type in (tk.EventType.ButtonPress, tk.EventType.ButtonRelease):
            options["button"] = event.num
        self.tree.event_generate(f"<{event.type.name}>", **options)
        return "break"

    def position_row_progress(self):
        transfers = iter(self.transfers.items())
        for bar in self.row_bars:
            key, progress = next(transfers, (None, None))
            bounds = self.tree.bbox(key, "Status") if key and "Status" in self.tree["displaycolumns"] else ()
            if bounds:
                x, y, width, height = bounds
                bar_width = min(160, width - 12)
                bar_y = y + height - 12
                if bar_width > 0 and self.tree.identify_row(bar_y) == key:
                    received, total = progress
                    if total:
                        bar.configure(mode="determinate", value=min(100, received * 100 / total))
                    else:
                        bar.configure(mode="indeterminate")
                        bar.step()
                    bar.place(x=x + (width - bar_width) // 2, y=bar_y, width=bar_width, height=10)
                    continue
            bar.place_forget()

    def build_column_menu(self):
        self.column_menu = AppMenu(self.tree)
        # Tk exposes the icon column (#0) separately from the named data columns.
        self.column_menu.add_checkbutton("Item icon", self.show_app_icons, self.change_app_icons)
        columns = self.tree["columns"]
        self.columns_file = self.appearance_file.with_name("hidden-columns.txt")
        try:
            hidden = set(self.columns_file.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeError):
            hidden = set()
        if "App name" in hidden:  # Saved by versions before the column was renamed.
            hidden.add("Name")
        if hidden.issuperset(columns):
            hidden.clear()
        self.column_visibility = {}
        for name in columns:
            visible = tk.BooleanVar(self.tree, value=name not in hidden)
            self.column_visibility[name] = visible
            self.column_menu.add_checkbutton(name, visible, self.change_columns)
        self.apply_columns()

    def apply_columns(self):
        visible = tuple(name for name, flag in self.column_visibility.items() if flag.get())
        for name, button in self.column_menu.buttons.items():
            button.state(["disabled" if visible == (name,) else "!disabled"])
        checklist = self.view.get() == "checklist"
        if checklist:
            visible = ("Name", "Status")
        self.tree.configure(displaycolumns=visible,
                            show=("tree", "headings") if checklist or self.show_app_icons.get() else "headings")
        # Tk's heading state command preserves the native hover and pressed flags.
        for name in visible:
            state = "!alternate" if name == visible[-1] else "alternate"
            self.tree.tk.call(self.tree, "heading", name, "state", state)
        self.fit_columns()

    def fit_columns(self, event=None):
        # Changing displaycolumns alone leaves ttk's old stretch allocation in place.
        # Start from the preferred widths so repeated toggles produce a stable layout.
        for name in self.tree["displaycolumns"]:
            self.tree.column(name, width=self.column_widths[name])
        if self.view.get() == "checklist":
            self.tree.column("Status", stretch=False,
                             width=max(self.column_widths["Status"], self.checklist_status_width))
        else:
            self.tree.column("Status", stretch=True)

    def change_columns(self):
        self.apply_columns()
        hidden = (name for name, flag in self.column_visibility.items() if not flag.get())
        try:
            write_preference(self.columns_file, "".join(f"{name}\n" for name in hidden))
        except OSError as exc:
            self.set_status("Columns changed for this session. Could not save preference: {error}", error=exc)

    def click_checkbox(self, event):
        if self.view.get() == "checklist" and self.tree.identify_region(event.x, event.y) == "tree":
            if key := self.tree.identify_row(event.y):
                self.tree.selection_set(key)
                self.tree.focus(key)
                self.tree.focus_set()
                self.toggle_checked(key)
                return "break"

    def toggle_focused_checkbox(self, event):
        if self.view.get() == "checklist":
            self.toggle_checked(self.tree.focus())
            return "break"

    def toggle_checked(self, key):
        if not self.busy and isinstance(self.jobs.get(key), Checklist):
            self.checked_items.symmetric_difference_update((key,))
            self.tree.item(key, tags=("checked" if key in self.checked_items else "unchecked",))
            self.controls()

    def visible_checklists(self):
        return [key for key in self.tree.get_children() if isinstance(self.jobs[key], Checklist)]

    def toggle_visible_checkboxes(self):
        if self.view.get() != "checklist" or self.busy:
            return
        keys = self.visible_checklists()
        if self.checked_items.issuperset(keys):
            self.checked_items.difference_update(keys)
            tag = "unchecked"
        else:
            self.checked_items.update(keys)
            tag = "checked"
        for key in keys:
            self.tree.item(key, tags=(tag,))
        self.controls()

    def refresh_checkbox_column(self):
        checklist = self.view.get() == "checklist"
        fallback = self.app_column_icon if self.show_app_icons.get() else ""
        for checked, tag in ((False, "unchecked"), (True, "checked")):
            self.tree.tag_configure(tag, image=checkbox_image(checked, self.busy) if checklist else fallback)
        self.tree.tag_configure("invalid_checklist", image="" if checklist else fallback)
        heading = self.app_column_icon
        if checklist:
            keys = self.visible_checklists()
            heading = header_image(len(self.checked_items.intersection(keys)), len(keys), self.busy)
        self.tree.heading("#0", image=heading)

    def open_row(self, event):
        if event.type == tk.EventType.ButtonPress:
            if self.tree.identify_region(event.x, event.y) not in ("tree", "cell"):
                return  # Let Tk handle a second click on a heading or separator.
            key = self.tree.identify_row(event.y)
            if not key:
                return "break"
            if self.view.get() == "checklist" and self.tree.identify_column(event.x) == "#0":
                return self.click_checkbox(event)
            self.tree.selection_set(key)
        self.edit_selected()
        return "break"

    def build_footer(self):
        self.status = tk.StringVar(self.root)
        self.status_message = ""
        ttk.Frame(self.root, height=1, style="Line.TFrame").grid(row=1, column=0, columnspan=3, sticky="ew")
        footer = ttk.Frame(self.root, style="Surface.TFrame", padding=(18, 6))
        footer.grid(row=2, column=0, columnspan=3, sticky="ew")
        footer.columnconfigure(2, weight=1)
        self.count = ttk.Label(footer, style="Small.TLabel")
        self.count.grid(row=0, column=0)
        ttk.Label(footer, text="·", style="Small.TLabel").grid(row=0, column=1, padx=7)
        self.download_progress = DownloadProgress(footer)
        self.download_progress.grid(row=0, column=3, padx=(12, 0))
        self.download_progress.grid_remove()
        ttk.Label(footer, text=f"v{__version__}", style="Small.TLabel").grid(row=0, column=4, padx=(12, 0))
        status_label = FittedLabel(footer, anchor="w", style="Small.TLabel")
        status_label.grid(row=0, column=2, sticky="ew")
        self.status.trace_add("write", lambda *_: status_label.set_text(self.status.get()))

    def set_status(self, template, **values):
        self.status_message = message(template, **values)
        self.status.set(tr(self.status_message))

    def on_map(self, event):
        if event.widget is self.root and self.palette is not None:
            style_titlebar(self.root, self.palette.is_dark)

    def on_destroy(self, event):
        if event.widget is self.root and self.theme_timer is not None:
            self.root.after_cancel(self.theme_timer)
            self.theme_timer = None

    def open_options(self):
        if self.options is None:
            self.options = OptionsWindow(self.root, self.mode, self.palette_name, self.change_mode, self.palette,
                                         language=self.language, on_language_change=self.change_language,
                                         show_app_icons=self.show_app_icons, on_app_icons_change=self.change_app_icons)
            self.options.bind("<Destroy>", self.options_closed, add="+")
            show_window(self.options)
        else:
            self.options.deiconify()
            self.options.lift()
            self.options.mode_buttons[self.mode.get()].focus_set()

    def options_closed(self, event):
        if event.widget is self.options:
            self.options = None

    def change_language(self, code):
        set_language(code)
        self.language = code
        self.refresh_labels()
        self.refresh_categories()
        for key in self.jobs:
            self.paint(key)
        self.refresh_view(select_first=False)
        self.status.set(tr(self.status_message))
        if self.options is not None:
            self.options.apply_language(code)
        try:
            write_preference(self.language_file, code + "\n")
        except OSError as exc:
            self.set_status("Language changed for this session. Could not save preference: {error}", error=exc)

    def refresh_labels(self):
        for widget, label in self.captions.items():
            widget.configure(text=tr(label))
        self.update_search_hint()
        self.fit_page_title()
        self.sidebar_toggle.configure(text=tr("Expand sidebar" if self.sidebar_compact else "Collapse sidebar"))
        self.sidebar_hint.place_forget()
        for menu in (self.app_menu, self.category_menu, self.column_menu, self.more_menu):
            menu.refresh_labels()
        # ttk measures button widths in units of the styled font's zero digit.
        cancel_buttons = [(button, ALL_ACTIONS[action]) for action, button in self.bulk_buttons.items()]
        cancel_buttons.extend((self.app_menu.buttons[label], label) for label in ACTIONS.values())
        for button, label in cancel_buttons:
            font = tkfont.Font(root=self.root, font=self.style.lookup(button.cget("style"), "font"))
            width = max(font.measure(tr(label)), font.measure(tr("Cancel")))
            button.configure(width=ceil(width / font.measure("0")))
        self.refresh_headings()
        heading_font = tkfont.Font(root=self.root, font=self.style.lookup("Treeview.Heading", "font"))
        row_font = tkfont.Font(root=self.root, font=self.style.lookup("Treeview", "font"))
        min_column_width = max(row_font.measure("..."), heading_font.measure("...")) + CELL_TEXT_INSETS
        self.checklist_status_width = max(row_font.measure(tr(s)) for s in
                                          ("Applied", "Not applied", "Partially applied", "Error")) + 36
        statuses = [tr(status) for status in (*RESULT_STATUSES, "Canceling…")]
        statuses.extend(tr("{action}…", action=tr(action)) for action in (*WORKING.values(), "Downloading"))
        for name, width in COLUMN_WIDTHS.items():
            self.tree.column(name, minwidth=min_column_width)
            label = tr(name)
            if name in SORTABLE_COLUMNS:
                label += " ▼"
            if name == "Status":
                width = max(width, max(row_font.measure(text) for text in statuses) + CELL_TEXT_INSETS)
            self.column_widths[name] = max(width, heading_font.measure(label) + 22)
        self.fit_columns()

    def refresh_headings(self):
        for name in COLUMN_WIDTHS:
            suffix = ""
            if name == self.visible_sort_column():
                suffix = " ▼" if self.sort_descending else " ▲"
            self.tree.set_caption(name, tr(name), suffix)

    def sort_by(self, column):
        self.sort_descending = column == self.visible_sort_column() and not self.sort_descending
        self.sort_column = column
        self.refresh_headings()
        self.refresh_view(select_first=False)

    def visible_sort_column(self):
        if self.view.get() == "checklist" and self.sort_column == "Category":
            return "Name"
        return self.sort_column

    def row_sort_key(self, key):
        name = self.name(key).casefold()
        if self.visible_sort_column() == "Category":
            return self.category(key).casefold(), name
        if self.sort_column == "Status":
            return self.displayed_status(key).casefold(), name
        return (name,)

    def change_mode(self):
        self.refresh_theme()
        try:
            write_appearance(self.appearance_file, self.mode.get(), self.palette_name.get())
        except OSError as exc:
            self.set_status("Theme applied for this session. Could not save preference: {error}", error=exc)

    def change_app_icons(self):
        visible = self.show_app_icons.get()
        self.apply_columns()
        for key in self.jobs:
            if visible:
                self.set_app_icon(key)
            else:
                self.tree.item(key, image="")
        self.refresh_checkbox_column()
        try:
            write_preference(self.app_icons_file, "show\n" if visible else "hide\n")
        except OSError as exc:
            self.set_status("Could not save preference: {error}", error=exc)

    def set_app_icon(self, key):
        if isinstance(self.jobs[key], (Checklist, ChecklistLoadError)):
            return  # Their first-column images come from the checklist tags.
        if key not in self.app_images:
            self.app_images[key] = load_app_icon(self.root, self.jobs[key], self.dest)
        self.tree.item(key, image=self.app_images[key] or self.app_column_icon)

    def refresh_theme(self):
        if self.theme_timer is not None:
            self.root.after_cancel(self.theme_timer)
            self.theme_timer = None
        mode = self.mode.get()
        if mode == "System":
            mode = system_mode()
            self.theme_timer = self.root.after(1500, self.refresh_theme)
        palette = PALETTES[self.palette_name.get()][mode]
        if palette is self.palette:
            return
        self.palette = palette
        style_widgets(self.style, palette)
        self.app_column_icon = make_icon(self.root, "app_window", palette.muted)
        self.refresh_checkbox_column()
        if self.show_app_icons.get():
            for key, icon in self.app_images.items():
                if icon is None:
                    self.tree.item(key, image=self.app_column_icon)
        self.root.configure(bg=palette.canvas)
        for widget in self.icon_widgets:
            self.refresh_icon(widget)
        for menu in (self.app_menu, self.category_menu, self.more_menu):
            menu.refresh_icons(palette)
        if self.root.winfo_ismapped():
            style_titlebar(self.root, palette.is_dark)
        if self.options is not None:
            self.options.apply_theme(palette)
        if self.editor is not None:
            self.editor.apply_theme(palette)

    def refresh_icon(self, widget):
        name, color, size = self.icon_widgets[widget]
        icon = make_icon(self.root, name, getattr(self.palette, color), size)
        widget.configure(image=icon)
        self.images[widget] = icon

    def bind_context_menu(self, tree):
        for sequence in ("<<ContextMenu>>", "<Shift-F10>", "<App>" if sys.platform == "win32" else "<Menu>"):
            tree.bind(sequence, self.show_context_menu)

    def show_context_menu(self, event):
        tree = event.widget
        keyboard = event.type == tk.EventType.KeyPress
        if keyboard:
            selected = tree.selection()
            if not selected:
                return "break"
            key = selected[0]
            tree.see(key)
            bounds = tree.bbox(key)
            if not bounds:
                return "break"
            _, y, _, height = bounds
            screen_x = tree.winfo_rootx() + 8
            screen_y = tree.winfo_rooty() + y + height
        else:
            region = tree.identify_region(event.x, event.y)
            if region in ("heading", "separator"):
                if tree is self.tree and self.view.get() != "checklist":
                    self.column_menu.popup(event.x_root, event.y_root)
                return "break"
            key = tree.identify_row(event.y)
            if not key or region not in ("tree", "cell"):
                return "break"
            tree.selection_set(key)
            screen_x, screen_y = event.x_root, event.y_root

        tree.focus(key)
        tree.focus_set()
        if tree is self.category_list:
            self.category_menu_key = key
            self.category_menu.buttons[RENAME].state(["disabled" if self.busy else "!disabled"])
            menu = self.category_menu
        else:
            self.app_menu_key = key
            self.update_app_menu(key)
            menu = self.app_menu
        menu.popup(screen_x, screen_y, keyboard=keyboard)
        return "break"

    def update_app_menu(self, key):
        token = self.cancel_events.get(key)
        cancel_action = self.requested_action if token is not None and not token.is_set() else None
        job = self.jobs[key]
        can_start = not self.busy and isinstance(job, Recipe)
        disabled = updates_disabled(job)
        toggle = self.app_menu.buttons["Disable updates"]
        toggle.configure(text=tr("Enable updates" if disabled else "Disable updates"),
                         state="normal" if can_start else "disabled")
        self.app_menu.set_action_icon(toggle, "updates" if disabled else "pause_circle", self.palette)
        for action, label in ACTIONS.items():
            cancel = action == cancel_action
            button = self.app_menu.buttons[label]
            button.configure(text=tr("Cancel" if cancel else label),
                             state="normal" if cancel or (can_start and (action == "verify" or not disabled)) else "disabled")
            self.app_menu.set_action_icon(button, "cancel" if cancel else ACTION_ICONS[action], self.palette)
        can_open = not self.busy and isinstance(self.jobs[key], (Recipe, LocalItem))
        self.app_menu.buttons["Open app folder"].state(["!disabled" if can_open else "disabled"])
        self.app_menu.buttons[MOVE].state(["!disabled" if can_open else "disabled"])
        self.app_menu.buttons["Edit item"].state(["disabled" if self.busy else "!disabled"])
        self.app_menu.buttons[DELETE].state(["disabled" if self.busy else "!disabled"])
        for mode in (DATA, FOLDER):
            self.app_menu.buttons[mode].state(["!disabled" if can_open else "disabled"])

    def delete_selected_item(self, mode):
        key = self.app_menu_key
        if self.busy or key not in self.jobs:
            return
        path, job = Path(key), self.jobs[key]
        name = self.name(key)
        try:
            original = path.read_bytes()
            plan = deletion_plan(mode, path, job, self.dest, self.targets)
            detail = [tr("All item files will be kept.")]
            if mode != DELETE:
                detail = [tr("These files and folders will be permanently deleted:")]
                if mode == FOLDER and plan.folders:
                    detail.append(tr("Everything inside the listed folders will be lost."))
                detail.extend(str(p) for p in plan.folders)
                detail.extend(str(p) for p in (path, *plan.files[:6]))
                if len(plan.files) > 6:
                    detail.append(tr("Additional app files: {count}", count=len(plan.files) - 6))
                detail.extend(str(p) for p in plan.manifests)
            if not messagebox.askyesno(tr(mode), tr("Delete “{name}” from the catalog?", name=name),
                                       detail="\n".join(detail), icon="warning", default="no", parent=self.root):
                return
            delete_item(mode, path, job, original, plan, self.dest, self.targets)
        except ERRORS as exc:
            self.set_status("{error}", error=error_detail(exc))
            return
        self.app_menu_key = None
        self.reload(keep_results=True)
        self.set_status("Deleted {name}", name=name)

    def toggle_updates(self):
        key = self.app_menu_key
        job = self.jobs.get(key)
        if self.busy or not isinstance(job, Recipe):
            return
        path = Path(key)
        try:
            original = path.read_bytes()
            data = json.loads(original.decode("utf-8-sig"))
            if catalog_item(data, path=path) != job:
                raise ValueError("This recipe changed on disk. Cancel and reopen it to load the latest version.")
            if job.updates_disabled:
                data.pop("updates_disabled")
            else:
                data["updates_disabled"] = True
            save_recipe(self.catalog, data, path, original)
        except ERRORS as exc:
            self.set_status("{error}", error=error_detail(exc))
            return
        self.reload(keep_results=True)

    def category_choices(self):
        return sorted((view.removeprefix("category:") for view in self.category_groups
                       if view != "category:"), key=str.casefold)

    def edit_category(self, *, rename=False):
        if self.busy:
            return
        if rename:
            keys = self.category_groups.get(self.category_menu_key, ())
            if not keys:
                return
            subject = self.view_title(self.category_menu_key)
            choices = None
        else:
            key = self.app_menu_key
            if not isinstance(self.jobs.get(key), (Recipe, LocalItem)):
                return
            keys = [key]
            subject = self.name(key)
            choices = self.category_choices()
        source = self.jobs[keys[0]].category
        members = {Path(key): self.jobs[key].id for key in keys}

        def save(destination):
            change_category(self.catalog, members, source, destination, rename=rename)
            self.reload(keep_results=True)
            if rename:
                self.select_view("category:" + destination)

        CategoryDialog(self.root, self.palette, RENAME if rename else MOVE, subject, source, choices, save)

    def open_app_folder(self):
        job = self.jobs.get(self.app_menu_key)
        if not isinstance(job, (LocalItem, Recipe)):
            return
        try:
            if isinstance(job, LocalItem):
                folder = job.path if job.path.is_dir() else job.path.parent
            else:
                targets = load_targets(self.targets, self.dest)
                mapped = next((targets[key] for key in job.id_keys() if key in targets), None)
                state = read_state(job.cache_root(self.dest), "current.json") or {}
                target = mapped or state.get("published", {}).get("path")
                folder = Path(target).parent if target else None
            if not folder or not folder.is_dir():
                self.set_status("Use Get updates to publish the app first.")
                return
            os.startfile(folder)
        except ERRORS as exc:
            self.set_status("{error}", error=error_detail(exc))

    def item_command(self, action):
        key = self.app_menu_key
        if key not in self.jobs:
            return
        if key in self.cancel_events and action == self.requested_action:
            self.cancel(key)
        else:
            self.start(action, key=key)

    def edit_selected(self):
        selected = self.tree.selection()
        if selected:
            self.open_editor(Path(selected[0]))

    def open_editor(self, path=None):
        if self.busy:
            return
        if self.editor is not None:
            self.editor.lift()
            return
        try:
            on_action = None
            if path is not None:
                # The editor reads the recipe from disk. Use the same catalog for its actions.
                self.reload(keep_results=True)
                key = str(path)
                if key not in self.jobs:
                    self.set_status("Recipe is no longer in the catalog: {filename}", filename=path.name)
                    return
                if isinstance(self.jobs[key], Recipe):
                    on_action = lambda action: self.start(action, key=key)
            if isinstance(self.jobs.get(str(path)), Checklist):
                self.editor = ChecklistWindow(self.root, self.catalog, path, self.palette, self.recipe_saved,
                                              self.checklist_history, self.checklist_result_changed)
            else:
                self.editor = RecipeEditor(self.root, self.catalog, path, self.palette, self.recipe_saved,
                                           on_action=on_action, categories=self.category_choices())
            self.editor.bind("<Destroy>", self.editor_closed, add="+")
            if path is not None:
                self.update_app_info(str(path))
            show_window(self.editor)
        except ERRORS as exc:
            self.set_status("Could not open recipe: {error}", error=error_detail(exc))

    def editor_closed(self, event):
        if event.widget is self.editor:
            self.editor = None

    def checklist_result_changed(self, path):
        key = str(path)
        self.results[key] = Result(self.checklist_history.summary(self.jobs[key]))
        self.paint(key)
        self.refresh_view(select_first=False)

    def recipe_saved(self, path):
        self.reload(saved=str(path), keep_results=True)
        item = self.jobs.get(str(path))
        if isinstance(item, Recipe) and item.apply_custom_settings:
            try:
                applied = apply_published_settings(item, self.dest)
            except ERRORS as exc:
                self.set_status("Item saved. Could not apply custom settings: {error}", error=error_detail(exc))
                return
            self.set_status("Custom settings applied." if applied else "Custom settings will apply on Get updates.")
            return
        self.set_status("Saved {filename}", filename=path.name)

    def reload(self, saved=None, *, keep_results=False):
        if self.busy:
            return
        paths = sorted(self.catalog.glob("*.json"))
        jobs = {str(path): job for path, job in read_jobs(paths)}
        if keep_results and saved is None and jobs == self.jobs:
            return
        previous = self.tree.selection()
        old_jobs, old_results = self.jobs, self.results
        # Filtered rows stay in the tree as detached items.
        if old_jobs:
            self.tree.delete(*old_jobs)
        self.app_images.clear()
        self.jobs = jobs
        self.checked_items.intersection_update(key for key, job in jobs.items() if isinstance(job, Checklist))
        self.results = {}
        for key, job in self.jobs.items():
            previous_job = old_jobs.get(key)
            if (keep_results and isinstance(previous_job, Recipe)
                    and isinstance(job, Recipe) and previous_job.previous_ids == job.previous_ids
                    and previous_job.fingerprint() == job.fingerprint()
                    and previous_job.apply_custom_settings == job.apply_custom_settings
                    and previous_job.settings_files == job.settings_files):
                self.results[key] = old_results[key]
            else:
                self.results[key] = initial_result(job, self.dest, self.checklist_history)
        for key in self.jobs:
            tags = ()
            if isinstance(self.jobs[key], Checklist):
                tags = ("checked" if key in self.checked_items else "unchecked",)
            elif isinstance(self.jobs[key], ChecklistLoadError):
                tags = ("invalid_checklist",)
            self.tree.insert("", "end", iid=key, tags=tags)
            self.paint(key)
            if self.show_app_icons.get():
                self.set_app_icon(key)
        self.refresh_categories()
        if self.view.get() == "disabled" and not any(updates_disabled(job) for job in jobs.values()):
            self.view.set("all")
        self.refresh_view(preferred=saved or next(iter(previous), None))
        self.set_status("Ready" if paths else "No items yet")

    def refresh_view(self, *_trace, preferred=None, select_first=True):
        view = self.view.get()
        self.update_page_title()
        checklists = {key for key, job in self.jobs.items() if isinstance(job, (Checklist, ChecklistLoadError))}
        groups = {"checklist": checklists,
                  "all": tuple(key for key in self.jobs if key not in checklists),
                  "local": tuple(key for key, job in self.jobs.items() if isinstance(job, LocalItem)),
                  "disabled": {key for key, job in self.jobs.items() if updates_disabled(job)},
                  "progress": tuple(self.active_items)}
        for group, statuses in (("updates", {"Update available", "Pending"}),
                                ("attention", {"Repair needed", "Source error", "Error", "Pending"})):
            groups[group] = tuple(key for key in groups["all"] if self.results[key].status in statuses
                                  and (group != "updates" or key not in groups["disabled"]))
        for group, keys in groups.items():
            self.view_counts[group].set(str(len(keys)))
            if group in ("checklist", "all", "local") or keys:
                self.nav_buttons[group].grid()
            else:
                self.nav_buttons[group].grid_remove()

        members = groups[view] if view in VIEWS else self.category_groups[view]
        visible = members
        query = self.search.get().strip().casefold()
        if query:
            visible = tuple(key for key in visible if self.matches_search(key, query))
        visible = tuple(sorted(visible, key=self.row_sort_key, reverse=self.sort_descending))

        previous_rows = self.tree.get_children()
        selected = self.tree.selection()
        key = preferred or next(iter(selected), None)
        if key not in visible:
            key = visible[0] if visible and (select_first or selected or not previous_rows) else None
        if selected and selected != (key,):
            self.tree.selection_remove(*selected)
        if visible != previous_rows:
            self.tree.set_children("", *visible)
        if key and self.tree.selection() != (key,):
            self.tree.selection_set(key)
            self.tree.focus(key)
            self.tree.see(key)
        elif not key and self.tree.focus() not in visible:
            self.tree.focus("")

        total = len(checklists) if view == "checklist" else len(groups["all"])
        count = total if len(visible) == total else tr("{shown} of {total}", shown=len(visible), total=total)
        self.count.configure(text=tr("{count} item" if total == 1 else "{count} items", count=count))
        if visible:
            self.empty_panel.place_forget()
        else:
            if query and members:
                title, detail = "No matching items", "Try a different name, category, or item ID."
            elif view == "checklist":
                title, detail = "No checklists yet", ""
            elif view == "local":
                title, detail = "No local-only items", ""
            elif view == "disabled":
                title, detail = "No apps with updates disabled", ""
            elif not groups["all"]:
                title, detail = "Your catalog is empty", "Add your first item to start tracking releases."
            elif view == "updates":
                title, detail = "No updates listed", "Select Check for updates to compare versions with the publishers."
            elif view == "progress":
                title, detail = "No operations in progress", "Items being checked, updated, or verified appear here."
            else:
                title, detail = "No issues reported", "Items with source, file, or publication problems appear here."
            self.empty_title.set(tr(title))
            self.empty_detail.set(tr(detail))
            if query and members:
                self.reset_search_button.pack(pady=(10, 0))
            else:
                self.reset_search_button.pack_forget()
            self.empty_panel.place(relx=0.5, rely=0.5, relwidth=1, anchor="center")
        self.controls()

    def matches_search(self, key, query):
        job = self.jobs[key]
        fields = [self.name(key)]
        if not isinstance(job, Result):
            fields.extend([job.id, self.category(key)])
            if isinstance(job, Recipe):
                fields.extend(job.previous_ids)
        return any(query in value.casefold() for value in fields)

    def name(self, key):
        job = self.jobs[key]
        if isinstance(job, Result):
            return Path(key).name
        return job.name or job.id

    def category(self, key):
        job = self.jobs[key]
        if isinstance(job, Result):
            return "—"
        return job.category or tr("Checklist" if isinstance(job, Checklist) else "Uncategorized")

    def displayed_status(self, key):
        if key in self.active_items:
            if self.cancel_events[key].is_set():
                return tr("Canceling…")
            action = "Downloading" if key in self.transfers else WORKING[self.active_items[key][0]]
            return tr("{action}…", action=tr(action))
        status = self.results[key].status
        if updates_disabled(self.jobs[key]) and status in ("Not checked", "Update available", "Latest"):
            return tr("Disabled")
        return tr(status)

    def paint(self, key):
        job, result = self.jobs[key], self.results[key]
        source = "—"
        if isinstance(job, Checklist):
            source = tr("Local setup")
        elif isinstance(job, LocalItem):
            source = tr("Local only")
        elif isinstance(job, Recipe):
            source = urlsplit(job.page or job.download).hostname if not job.github else "github.com"
        local = "—" if result.local == "-" else result.local
        available = "—" if result.available == "-" else result.available
        values = (self.name(key), self.category(key), local, available, self.displayed_status(key), source)
        self.tree.set_values(key, values)
        self.update_app_info(key)

    def update_app_info(self, key):
        if self.editor is None or isinstance(self.editor, ChecklistWindow) or self.editor.path != Path(key):
            return
        job, result = self.jobs[key], self.results[key]
        lines = [tr("Status: {value}", value=self.displayed_status(key))]
        if not isinstance(job, Result):
            lines = [tr("Category: {value}", value=self.category(key)), tr("Item ID: {value}", value=job.id), *lines]
        if isinstance(job, Recipe):
            if job.description:
                lines = [job.description, "", *lines]
            lines += [tr("Local version: {value}", value=result.local),
                      tr("Available version: {value}", value=result.available),
                      tr("Source: {value}", value=job.page or job.github or job.download)]
            try:
                lines.append(tr("Cache: {value}", value=job.cache_root(self.dest)))
            except ERRORS as exc:
                if str(exc) != result.detail:
                    lines.append(tr("Cache: {value}", value=tr(error_detail(exc))))
        if result.detail:
            lines += ["", tr(result.detail)]
        self.editor.set_app_info(self.name(key), "\n".join(lines))

    def working_status(self):
        key = next(iter(self.transfers), None) or next(iter(self.active_items), None)
        self.download_progress.update_progress(*self.transfers.get(key, (None, None)))
        if isinstance(self.editor, RecipeEditor):
            self.editor.download_progress.update_progress(*self.transfers.get(str(self.editor.path), (None, None)))
        if key is None:
            return
        if self.cancel_events[key].is_set():
            self.set_status("Canceling…")
            return
        action, index = self.active_items[key]
        name = self.name(key)
        if key in self.transfers:
            remaining = sum(not self.cancel_events[key].is_set() for key in self.pending_downloads)
            total = self.download_index + remaining
            template = ("Downloading {name}  ·  {index} of {total} download" if total == 1 else
                        "Downloading {name}  ·  {index} of {total} downloads")
            self.set_status(template, name=name, index=index, total=total)
        elif action == "fetch":
            self.set_status("Updating {name}…", name=name)
        else:
            self.set_status("{action} {name}  ·  {index} of {total}", action=message(WORKING[action]),
                            name=name, index=index, total=self.total)

    def controls(self, event=None):
        self.refresh_checkbox_column()
        cancel_state = "normal" if any(not token.is_set() for token in self.cancel_events.values()) else "disabled"
        for action, button in self.bulk_buttons.items():
            active = action == self.bulk_action
            icon = "cancel" if active else BULK_ICONS[action]
            if action == "verify":
                self.more_menu.set_action_icon(button, icon, self.palette)
            else:
                current, color, size = self.icon_widgets[button]
                if current != icon:
                    self.icon_widgets[button] = (icon, color, size)
                    self.refresh_icon(button)
            if action in APPLY_ACTIONS:
                eligible = self.checked_items if action == "apply_selected" else any(
                    isinstance(job, Checklist) for job in self.jobs.values())
            else:
                eligible = any(eligible_for_action(action, job) for job in self.jobs.values())
            state = "normal" if eligible and not self.busy else "disabled"
            button.configure(state=cancel_state if active else state,
                             text=tr("Cancel" if active else ALL_ACTIONS[action]))
        selected_state = "disabled" if self.busy or not self.tree.selection() else "normal"
        idle_state = "disabled" if self.busy else "normal"
        self.more_menu.buttons["Reload catalog"].configure(state=idle_state)
        self.more_button.configure(state="normal" if self.bulk_action == "verify" else idle_state)
        self.add_button.configure(state=idle_state)
        self.edit_button.configure(state=selected_state)
        if self.app_menu_key in self.jobs:
            self.update_app_menu(self.app_menu_key)
        if self.editor is not None:
            self.editor.set_busy(self.busy)

    def bulk_command(self, action):
        if action == self.bulk_action:
            self.cancel()
        else:
            self.start(action)

    def cancel(self, key=None):
        if not self.busy:
            return
        if key is None:
            for token in self.cancel_events.values():
                token.set()
        elif key in self.cancel_events:
            self.cancel_events[key].set()
        for active_key in self.active_items:
            if self.cancel_events[active_key].is_set():
                self.paint(active_key)
        if self.active_items:
            self.working_status()
        elif key is None:
            self.set_status("Canceling…")
        self.controls()

    def start(self, action, *, key=None):
        keys = self.jobs if key is None else (key,)
        if action in APPLY_ACTIONS:
            keys = [k for k in keys if isinstance(self.jobs[k], Checklist)
                    and (action == "apply_all" or k in self.checked_items)]
        else:
            keys = [k for k in keys if eligible_for_action(action, self.jobs[k])]
        if self.busy or not keys or (isinstance(self.editor, ChecklistWindow) and self.editor.running):
            return
        self.busy = True
        self.cancel_events = {recipe_key: Event() for recipe_key in keys}
        # A fetch starts with a version check; its menu command stays Get updates.
        self.requested_action = action
        self.bulk_action = action if key is None else None
        self.total, self.finished, self.canceled, self.failure = len(keys), 0, 0, ""
        self.pending_downloads = set()
        self.download_index = 0
        self.controls()
        items = [(k, self.jobs[k], token) for k, token in self.cancel_events.items()]
        if action in APPLY_ACTIONS:
            self.set_status("{action}…", action=message("Applying"))
            Thread(target=run_checklists, args=(self.events, items, self.checklist_history)).start()
        else:
            self.set_status("{action}…", action=message(WORKING[action]))
            Thread(target=run_batch, args=(self.events, action, items, self.dest, self.targets)).start()
        label = ALL_ACTIONS[action] if key is None else ACTIONS[action]
        self.root.after(50, self.poll, action, label)

    def poll(self, action, label):
        view_changed = False
        status_changed = False
        while True:
            try:
                event, *data = self.events.get_nowait()
            except Empty:
                if view_changed:
                    self.refresh_view(select_first=False)
                if status_changed:
                    self.working_status()
                if status_changed or self.transfers:
                    self.position_row_progress()
                self.root.after(50, self.poll, action, label)
                return
            status_changed = True
            if event == "downloads":
                self.pending_downloads = set(data[0])
            elif event == "started":
                key, phase, index = data
                self.active_items[key] = (phase, index)
                self.paint(key)
                view_changed = True
            elif event == "prepared":
                key, = data
                del self.active_items[key]
                self.paint(key)
                view_changed = True
            elif event == "progress":
                key, received, total = data
                was_downloading = key in self.transfers
                if received is None:
                    self.transfers.pop(key, None)
                else:
                    self.transfers[key] = (received, total)
                    if key in self.pending_downloads:
                        self.pending_downloads.remove(key)
                        self.download_index += 1
                        self.active_items[key] = ("fetch", self.download_index)
                if was_downloading != (received is not None):
                    self.paint(key)
                    view_changed |= self.sort_column == "Status"
            elif event == "result":
                key, result = data
                self.active_items.pop(key, None)
                self.transfers.pop(key, None)
                self.pending_downloads.discard(key)
                del self.cancel_events[key]
                if result.status == "Canceled":
                    self.canceled += 1
                    if action in APPLY_ACTIONS:
                        self.results[key] = Result(self.checklist_history.summary(self.jobs[key]))
                else:
                    self.results[key] = result
                    self.finished += 1
                    if action == "fetch":
                        self.app_images.pop(key, None)
                        if self.show_app_icons.get():
                            self.set_app_icon(key)
                self.paint(key)
                view_changed = True
            elif event == "failed":
                self.failure = data[0]
                if self.editor is not None:
                    self.editor.error.set(tr("Could not finish: {error}", error=tr(self.failure)))
            else:
                self.active_items.clear()
                self.transfers.clear()
                self.working_status()
                self.position_row_progress()
                self.busy = False
                self.requested_action = self.bulk_action = None
                self.cancel_events.clear()
                for key in self.jobs:
                    self.paint(key)
                self.refresh_view(select_first=False)
                if self.failure:
                    self.set_status("Could not finish: {error}", error=self.failure)
                elif self.canceled:
                    self.set_status("{label}  ·  {finished} of {total} processed, {canceled} canceled",
                                    label=message(label), finished=self.finished, total=self.total,
                                    canceled=self.canceled)
                else:
                    self.set_status("{label} finished  ·  {finished} of {total} processed", label=message(label),
                                    finished=self.finished, total=self.total)
                return

    def close(self):
        if self.busy or (isinstance(self.editor, ChecklistWindow) and self.editor.running):
            self.set_status("An operation is running. Close the window after it finishes.")
        else:
            self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--dest", type=Path)
    parser.add_argument("--targets", type=Path)
    args = parser.parse_args()
    dest = (args.dest or default_destination()).resolve()
    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ReleaseShelf.Desktop")
    root = tk.Tk()
    stage_window(root)
    ShelfWindow(root, args.catalog.resolve(), dest, args.targets or dest / "targets.json")
    show_window(root)
    root.mainloop()
