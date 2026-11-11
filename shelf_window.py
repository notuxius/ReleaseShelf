"""Local desktop catalog for ReleaseShelf."""

import argparse
import ctypes
from math import ceil
from pathlib import Path
from queue import Empty, Queue
import sys
from threading import Event, Thread
import tkinter as tk
from tkinter import font as tkfont, ttk
from urllib.parse import urlsplit

from fetch_store import Result, read_state
from recipe_editor import ACTIONS, ACTION_ICONS, RecipeEditor
from releaseshelf import DEFAULT_CATALOG, ERRORS, default_destination, execute, load_targets, prepare_fetch, read_jobs
from shelf_app_icons import load_app_icon
from shelf_icons import make_icon
from shelf_i18n import set_language, tr
from shelf_menu import AppMenu, DropdownMenu
from shelf_message import error_detail, message
from shelf_options import OptionsWindow, read_language
from shelf_paint import keep_view_visible, show_window, stage_window
from shelf_progress import DownloadProgress
from shelf_scroll import grid_scrollable
from shelf_theme import (PALETTES, WINDOW_SIDE_PADDING, initial_geometry,
                        preference_path, read_appearance, style_titlebar, style_widgets, system_mode,
                        write_appearance, write_preference)
from shelf_version import __version__


ALL_ACTIONS = {"check": "Check for updates", "fetch": "Update all items", "verify": "Verify all items"}
VIEWS = {"all": "Catalog", "updates": "Available updates", "progress": "In progress",
         "attention": "Needs attention"}
WORKING = {"check": "Checking", "fetch": "Updating", "verify": "Verifying"}
RESULT_STATUSES = ("Not checked", "Pending", "Error", "Latest", "Updated", "Cached",
                   "Repair needed", "Update available", "Source error", "Verified")
COLUMN_WIDTHS = {"Name": 126, "Category": 100, "Local version": 94,
                 "Available version": 115, "Status": 108, "Source": 110}
SORTABLE_COLUMNS = ("Name", "Category", "Status")
SEARCH_CHAR_LIMIT = 64
ASSETS = Path(__file__).resolve().parent / "assets"


def initial_result(job, dest):
    if isinstance(job, Result):
        return job
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
    try:
        targets = load_targets(targets_path, dest)
        downloads = {}
        for index, (key, job, cancel_event) in enumerate(items, 1):
            events.put(("started", key, "check" if action == "fetch" else action, index))
            if cancel_event.is_set():
                result = Result("Canceled")
            elif action == "fetch":
                result = prepare_fetch(job, dest, targets, cancel_event=cancel_event)
                if not isinstance(result, Result):
                    downloads[key] = (result, cancel_event)
                    continue
            else:
                result = execute(action, job, dest, targets, cancel_event=cancel_event)
            events.put(("result", key, result))

        if downloads:
            events.put(("downloads", tuple(downloads)))
        for key, (plan, cancel_event) in downloads.items():
            events.put(("started", key, "fetch", None))

            def progress(received, total, key=key):
                events.put(("progress", key, received, total))

            result = plan.run(on_progress=progress, cancel_event=cancel_event)
            events.put(("result", key, result))
    except Exception as exc:
        events.put(("failed", error_detail(exc)))
    finally:
        events.put(("done",))


class ShelfWindow:
    def __init__(self, root, catalog, dest, targets, *, appearance_file=None):
        self.root, self.catalog, self.dest, self.targets = root, catalog, dest, targets
        self.jobs, self.results = {}, {}
        self.busy = False
        self.bulk_action = None
        self.cancel_events = {}
        self.working_key = self.working_action = None
        self.downloading = False
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
            self.options_button, self.search_hint, self.search_clear,
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
        nav_icons = {"all": "library", "updates": "updates", "progress": "hourglass", "attention": "problems"}
        for row, (name, title) in enumerate(VIEWS.items()):
            button = ttk.Button(rail, text=title, command=lambda n=name: self.select_view(n), style="Nav.TButton",
                                compound="left")
            button.grid(row=row, column=0, sticky="ew", pady=2)
            self.nav_buttons[name] = button
            self.icon_widgets[button] = (nav_icons[name], "text", 20)
            count = ttk.Label(button, textvariable=self.view_counts[name], style="Count.Nav.TLabel")
            count.place(relx=1, rely=0.5, x=-10, anchor="e")
            count.bind("<Button-1>", lambda _, b=button: b.invoke())
            for event, state in (("<Enter>", "active"), ("<Leave>", "!active")):
                button.bind(event, lambda _, c=count, s=state: c.state([s]), add="+")
            self.nav_counts[name] = count

        self.categories_heading = ttk.Label(rail, text="Categories", style="SidebarHeading.TLabel")
        self.categories_heading.grid(row=len(VIEWS), column=0, sticky="ew", padx=10, pady=(14, 4))
        self.category_panel = ttk.Frame(rail, style="Sidebar.TFrame")
        self.category_panel.grid(row=len(VIEWS) + 1, column=0, sticky="nsew", pady=(0, 8))
        self.category_list = ttk.Treeview(self.category_panel, columns=("name", "count"), show="",
                                          height=1, selectmode="browse", style="Categories.Treeview")
        self.category_list.column("name", width=80, minwidth=1)
        self.category_list.column("count", width=40, minwidth=40, stretch=False, anchor="e")
        grid_scrollable(self.category_list, reserve_space=False)
        self.category_list.bind("<<TreeviewSelect>>", self.category_selected)
        self.categories_button = ttk.Button(rail, text="Categories", compound="image",
                                            style="Compact.Nav.TButton", command=self.show_categories)
        self.icon_widgets[self.categories_button] = ("folders", "text", 20)

        footer = ttk.Frame(rail, style="Sidebar.TFrame")
        footer.grid(row=len(VIEWS) + 2, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        self.options_button = ttk.Button(footer, text="Options", command=self.open_options,
                                         style="Nav.TButton", compound="left")
        self.options_button.grid(row=1, column=0, sticky="ew", pady=2)
        self.icon_widgets[self.options_button] = ("settings", "text", 20)

        self.sidebar_toggle = ttk.Button(footer, text="Collapse sidebar", compound="image",
                                         style="Compact.Nav.TButton", command=self.toggle_sidebar)
        self.position_sidebar_toggle()
        self.icon_widgets[self.sidebar_toggle] = ("collapse", "text", 20)

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
        if self.category_groups:
            if self.sidebar_compact:
                self.categories_button.grid(row=len(VIEWS), column=0, sticky="ew", pady=(12, 2))
            else:
                self.categories_heading.grid()
                self.category_panel.grid()

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
            if not isinstance(job, Result):
                groups.setdefault("category:" + job.category, []).append(key)
        self.category_groups = groups
        listing = self.category_list
        listing.delete(*listing.get_children())
        for view in sorted(groups, key=lambda v: self.view_title(v).casefold()):
            listing.insert("", "end", iid=view, values=(self.view_title(view), len(groups[view])))
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
        self.search_hint = ttk.Label(self.search_entry, text="Search catalog", style="Small.TLabel", cursor="xterm")
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
        focused = self.root.focus_get() in (self.search_entry, self.search_clear)
        has_query = bool(self.search.get())
        self.search_border.configure(style="Focus.TFrame" if focused else "Line.TFrame")
        if focused or has_query:
            self.search_hint.place_forget()
        else:
            self.search_hint.place(x=4, rely=0.5, anchor="w")
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
        self.highlight_view()
        self.refresh_view()

    def highlight_view(self):
        view = self.view.get()
        for name, button in self.nav_buttons.items():
            state = ["selected" if name == view else "!selected"]
            button.state(state)
            self.nav_counts[name].state(state)
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
        for action in ("check", "fetch"):
            primary = action == "check"
            button = ttk.Button(batch, text=ALL_ACTIONS[action], command=lambda a=action: self.bulk_command(a),
                                style="Primary.TButton" if primary else "Command.TButton", width=-1, compound="left")
            button.pack(side="left", padx=(0, 5))
            self.all_buttons[action] = button
            self.icon_widgets[button] = (ACTION_ICONS[action], "on_accent" if primary else "text", 16)

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
        self.fit_commands = fit_commands
        ttk.Frame(card, height=1, style="Line.TFrame").grid(row=1, column=0, sticky="ew")

    def build_table(self, card):
        table = ttk.Frame(card, style="Surface.TFrame")
        table.grid(row=2, column=0, sticky="nsew")
        self.column_widths = COLUMN_WIDTHS.copy()
        cols = tuple(self.column_widths)
        show = ("tree", "headings") if self.show_app_icons.get() else "headings"
        self.tree = ttk.Treeview(table, columns=cols, show=show, selectmode="browse", height=4)
        self.tree.column("#0", width=36, minwidth=36, stretch=False)
        self.tree.tk.call(self.tree, "heading", "#0", "state", "alternate")
        for name in cols:
            self.tree.heading(name, anchor="center")
            if name in SORTABLE_COLUMNS:
                self.tree.heading(name, command=lambda column=name: self.sort_by(column))
            self.tree.column(name, minwidth=1, anchor="center")
        grid_scrollable(self.tree, reserve_space=False)
        self.row_progress = ttk.Progressbar(self.tree, style="Download.Horizontal.TProgressbar", takefocus=False)
        for sequence in ("<ButtonPress>", "<ButtonRelease>", "<B1-Motion>", "<MouseWheel>"):
            self.row_progress.bind(sequence, self.forward_row_pointer)
        self.tree.bind("<Map>", self.fit_columns)
        self.tree.bind("<<TreeviewSelect>>", self.controls)
        self.tree.bind("<Double-Button-1>", self.open_row)
        self.tree.bind("<Return>", self.open_row)
        self.build_column_menu()
        self.app_menu = AppMenu(self.tree)
        self.app_menu_key = None
        for action, label in ACTIONS.items():
            self.app_menu.add_action(label, lambda a=action: self.start(a, key=self.app_menu_key),
                                     icon=ACTION_ICONS[action])
        self.app_menu.add_action("Cancel", lambda: self.cancel(self.app_menu_key), icon="cancel")
        self.app_menu.add_separator()
        self.app_menu.add_action("Edit item", lambda: self.open_editor(Path(self.app_menu_key)), icon="edit")
        self.tree.bind("<<ContextMenu>>", self.show_app_menu)
        self.tree.bind("<Shift-F10>", self.show_app_menu)
        self.tree.bind("<App>" if sys.platform == "win32" else "<Menu>", self.show_app_menu)
        self.empty_panel = ttk.Frame(self.tree, style="Surface.TFrame")
        ttk.Label(self.empty_panel, textvariable=self.empty_title, style="Empty.TLabel").pack()
        ttk.Label(self.empty_panel, textvariable=self.empty_detail, style="Small.TLabel",
                  wraplength=300, justify="center").pack(pady=(4, 0))

    def forward_row_pointer(self, event):
        # Let the tree handle selection, context menus and scrolling over the bar.
        options = {"x": self.row_progress.winfo_x() + event.x, "y": self.row_progress.winfo_y() + event.y,
                   "state": event.state, "time": event.time}
        if event.type == tk.EventType.MouseWheel:
            options["delta"] = event.delta
        elif event.type in (tk.EventType.ButtonPress, tk.EventType.ButtonRelease):
            options["button"] = event.num
        self.tree.event_generate(f"<{event.type.name}>", **options)
        return "break"

    def position_row_progress(self):
        if "Status" in self.tree["displaycolumns"]:
            bounds = self.tree.bbox(self.working_key, "Status")
            if bounds:
                x, y, width, height = bounds
                bar_width = min(160, width - 12)
                bar_y = y + height - 12
                if bar_width > 0 and self.tree.identify_row(bar_y) == self.working_key:
                    footer_bar = self.download_progress.bar
                    self.row_progress.configure(mode=footer_bar["mode"], value=footer_bar["value"])
                    self.row_progress.place(x=x + (width - bar_width) // 2, y=bar_y, width=bar_width, height=10)
                    return
        self.row_progress.place_forget()

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
        self.tree.configure(displaycolumns=visible)
        # Tk's heading state command preserves the native hover and pressed flags.
        for name in visible:
            state = "!alternate" if name == visible[-1] else "alternate"
            self.tree.tk.call(self.tree, "heading", name, "state", state)
        self.fit_columns()
        for name, button in self.column_menu.buttons.items():
            button.state(["disabled" if visible == (name,) else "!disabled"])

    def fit_columns(self, event=None):
        # Changing displaycolumns alone leaves ttk's old stretch allocation in place.
        # Start from the preferred widths so repeated toggles produce a stable layout.
        for name in self.tree["displaycolumns"]:
            self.tree.column(name, width=self.column_widths[name])

    def change_columns(self):
        self.apply_columns()
        hidden = (name for name, flag in self.column_visibility.items() if not flag.get())
        try:
            write_preference(self.columns_file, "".join(f"{name}\n" for name in hidden))
        except OSError as exc:
            self.set_status("Columns changed for this session. Could not save preference: {error}", error=exc)

    def open_row(self, event):
        if event.type == tk.EventType.ButtonPress:
            if self.tree.identify_region(event.x, event.y) not in ("tree", "cell"):
                return  # Let Tk handle a second click on a heading or separator.
            key = self.tree.identify_row(event.y)
            if not key:
                return "break"
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
        status_label = ttk.Label(footer, textvariable=self.status, anchor="w", style="Small.TLabel")
        status_label.grid(row=0, column=2, sticky="ew")
        status_label.bind("<Configure>", lambda event: status_label.configure(wraplength=event.width))

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
        for menu in (self.app_menu, self.column_menu, self.more_menu):
            menu.refresh_labels()
        # ttk measures button widths in units of the styled font's zero digit.
        for action, button in self.bulk_buttons.items():
            font = tkfont.Font(root=self.root, font=self.style.lookup(button.cget("style"), "font"))
            width = max(font.measure(tr(ALL_ACTIONS[action])), font.measure(tr("Cancel")))
            button.configure(width=ceil(width / font.measure("0")))
        self.refresh_headings()
        heading_font = tkfont.Font(root=self.root, font=self.style.lookup("Treeview.Heading", "font"))
        row_font = tkfont.Font(root=self.root, font=self.style.lookup("Treeview", "font"))
        statuses = [tr(status) for status in (*RESULT_STATUSES, "Canceling…")]
        statuses.extend(tr("{action}…", action=tr(action)) for action in (*WORKING.values(), "Downloading"))
        for name, width in COLUMN_WIDTHS.items():
            label = tr(name)
            if name in SORTABLE_COLUMNS:
                label += " ▼"
            if name == "Status":
                width = max(width, max(row_font.measure(text) for text in statuses) + 20)
            self.column_widths[name] = max(width, heading_font.measure(label) + 22)
        self.fit_columns()
        self.root.after_idle(self.fit_commands)

    def refresh_headings(self):
        for name in COLUMN_WIDTHS:
            label = tr(name)
            if name == self.sort_column:
                label += " ▼" if self.sort_descending else " ▲"
            self.tree.heading(name, text=label)

    def sort_by(self, column):
        self.sort_descending = column == self.sort_column and not self.sort_descending
        self.sort_column = column
        self.refresh_headings()
        self.refresh_view(select_first=False)

    def row_sort_key(self, key):
        name = self.name(key).casefold()
        if self.sort_column == "Category":
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
        self.tree.configure(show=("tree", "headings") if visible else "headings")
        for key in self.jobs:
            if visible:
                self.set_app_icon(key)
            else:
                self.tree.item(key, image="")
        self.fit_columns()
        try:
            write_preference(self.app_icons_file, "show\n" if visible else "hide\n")
        except OSError as exc:
            self.set_status("Could not save preference: {error}", error=exc)

    def set_app_icon(self, key):
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
        self.tree.heading("#0", image=self.app_column_icon)
        if self.show_app_icons.get():
            for key, icon in self.app_images.items():
                if icon is None:
                    self.tree.item(key, image=self.app_column_icon)
        self.root.configure(bg=palette.canvas)
        for widget in self.icon_widgets:
            self.refresh_icon(widget)
        for menu in (self.app_menu, self.more_menu):
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

    def show_app_menu(self, event):
        keyboard = event.type == tk.EventType.KeyPress
        if keyboard:
            selected = self.tree.selection()
            if not selected:
                return "break"
            key = selected[0]
            self.tree.see(key)
            bounds = self.tree.bbox(key)
            if not bounds:
                return "break"
            _, y, _, height = bounds
            screen_x = self.tree.winfo_rootx() + 8
            screen_y = self.tree.winfo_rooty() + y + height
        else:
            region = self.tree.identify_region(event.x, event.y)
            if region in ("heading", "separator"):
                self.column_menu.popup(event.x_root, event.y_root)
                return "break"
            key = self.tree.identify_row(event.y)
            if not key or region not in ("tree", "cell"):
                return "break"
            self.tree.selection_set(key)
            screen_x, screen_y = event.x_root, event.y_root

        self.tree.focus(key)
        self.tree.focus_set()
        self.app_menu_key = key
        self.update_app_menu(key)
        self.app_menu.popup(screen_x, screen_y, keyboard=keyboard)
        return "break"

    def update_app_menu(self, key):
        action_state = "disabled" if self.busy or isinstance(self.jobs[key], Result) else "!disabled"
        for label in ACTIONS.values():
            self.app_menu.buttons[label].state([action_state])
        token = self.cancel_events.get(key)
        self.app_menu.buttons["Cancel"].state(["!disabled" if token and not token.is_set() else "disabled"])
        self.app_menu.buttons["Edit item"].state(["disabled" if self.busy else "!disabled"])

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
                if not isinstance(self.jobs[key], Result):
                    on_action = lambda action: self.start(action, key=key)
            self.editor = RecipeEditor(self.root, self.catalog, path, self.palette, self.recipe_saved,
                                       on_action=on_action)
            self.editor.bind("<Destroy>", self.editor_closed, add="+")
            if path is not None:
                self.update_app_info(str(path))
            show_window(self.editor)
        except ERRORS as exc:
            self.set_status("Could not open recipe: {error}", error=error_detail(exc))

    def editor_closed(self, event):
        if event.widget is self.editor:
            self.editor = None

    def recipe_saved(self, path):
        self.reload(saved=str(path), keep_results=True)
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
        self.results = {}
        for key, job in self.jobs.items():
            previous_job = old_jobs.get(key)
            if (keep_results and previous_job is not None and not isinstance(previous_job, Result)
                    and not isinstance(job, Result) and previous_job.previous_ids == job.previous_ids
                    and previous_job.fingerprint() == job.fingerprint()):
                self.results[key] = old_results[key]
            else:
                self.results[key] = initial_result(job, self.dest)
        for key in self.jobs:
            self.tree.insert("", "end", iid=key)
            self.paint(key)
            if self.show_app_icons.get():
                self.set_app_icon(key)
        self.refresh_categories()
        self.refresh_view(preferred=saved or next(iter(previous), None))
        self.set_status("Ready" if paths else "No items yet")

    def refresh_view(self, *_trace, preferred=None, select_first=True):
        view = self.view.get()
        self.page_title.set(self.view_title(view))
        groups = {"all": tuple(self.jobs),
                  "progress": (self.working_key,) if self.working_key in self.jobs else ()}
        for group, statuses in (("updates", {"Update available", "Pending"}),
                                ("attention", {"Repair needed", "Source error", "Error", "Pending"})):
            groups[group] = tuple(key for key in self.jobs if self.results[key].status in statuses)
        for group, keys in groups.items():
            self.view_counts[group].set(str(len(keys)))

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

        total = len(self.jobs)
        count = total if len(visible) == total else tr("{shown} of {total}", shown=len(visible), total=total)
        self.count.configure(text=tr("{count} item" if total == 1 else "{count} items", count=count))
        if visible:
            self.empty_panel.place_forget()
        else:
            if query and members:
                title, detail = "No matching items", "Try a different name, category, or item ID."
            elif not self.jobs:
                title, detail = "Your catalog is empty", "Add your first item to start tracking releases."
            elif view == "updates":
                title, detail = "No updates listed", "Select Check for updates to compare versions with the publishers."
            elif view == "progress":
                title, detail = "No operations in progress", "Items being checked, updated, or verified appear here."
            else:
                title, detail = "No issues reported", "Items with source, file, or publication problems appear here."
            self.empty_title.set(tr(title))
            self.empty_detail.set(tr(detail))
            self.empty_panel.place(relx=0.5, rely=0.5, anchor="center")
        self.controls()

    def matches_search(self, key, query):
        job = self.jobs[key]
        fields = [self.name(key)]
        if not isinstance(job, Result):
            fields.extend([job.id, *job.previous_ids, self.category(key)])
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
        return job.category or tr("Uncategorized")

    def displayed_status(self, key):
        if self.working_key == key:
            if self.cancel_events[key].is_set():
                return tr("Canceling…")
            action = "Downloading" if self.downloading else WORKING[self.working_action]
            return tr("{action}…", action=tr(action))
        return tr(self.results[key].status)

    def paint(self, key):
        job, result = self.jobs[key], self.results[key]
        source = "—"
        if not isinstance(job, Result):
            source = urlsplit(job.page).hostname if job.page else "github.com"
        local = "—" if result.local == "-" else result.local
        available = "—" if result.available == "-" else result.available
        values = (self.name(key), self.category(key), local, available, self.displayed_status(key), source)
        self.tree.item(key, values=values)
        self.update_app_info(key)

    def update_app_info(self, key):
        if self.editor is None or self.editor.path != Path(key):
            return
        job, result = self.jobs[key], self.results[key]
        lines = [tr("Status: {value}", value=self.displayed_status(key))]
        if not isinstance(job, Result):
            lines = [tr("Category: {value}", value=self.category(key)), tr("Item ID: {value}", value=job.id), *lines]
            if job.description:
                lines = [job.description, "", *lines]
            lines += [tr("Local version: {value}", value=result.local),
                      tr("Available version: {value}", value=result.available),
                      tr("Source: {value}", value=job.page or job.github)]
            try:
                lines.append(tr("Cache: {value}", value=job.cache_root(self.dest)))
            except ERRORS as exc:
                if str(exc) != result.detail:
                    lines.append(tr("Cache: {value}", value=tr(error_detail(exc))))
        if result.detail:
            lines += ["", tr(result.detail)]
        self.editor.set_app_info(self.name(key), "\n".join(lines))

    def update_progress(self, received=None, total=None):
        downloading = received is not None
        self.download_progress.update_progress(received, total)
        if self.editor is not None and str(self.editor.path) == self.working_key:
            self.editor.download_progress.update_progress(received, total)
        if downloading != self.downloading:
            self.downloading = downloading
            if not downloading:
                self.row_progress.place_forget()
            if self.working_key is not None:
                self.paint(self.working_key)
                self.working_status()

    def working_status(self):
        if self.cancel_events[self.working_key].is_set():
            self.set_status("Canceling…")
            return
        name = self.name(self.working_key)
        if self.downloading:
            remaining = sum(not self.cancel_events[key].is_set() for key in self.pending_downloads)
            total = self.download_index + remaining
            template = ("Downloading {name}  ·  {index} of {total} download" if total == 1 else
                        "Downloading {name}  ·  {index} of {total} downloads")
            self.set_status(template, name=name, index=self.download_index, total=total)
        elif self.working_index is None:
            self.set_status("Updating {name}…", name=name)
        else:
            self.set_status("{action} {name}  ·  {index} of {total}", action=message(WORKING[self.working_action]),
                            name=name, index=self.working_index, total=self.total)

    def controls(self, event=None):
        batch_state = "disabled" if self.busy or not self.jobs else "normal"
        cancel_state = "normal" if any(not token.is_set() for token in self.cancel_events.values()) else "disabled"
        for action, button in self.bulk_buttons.items():
            active = action == self.bulk_action
            icon = "cancel" if active else ACTION_ICONS[action]
            if action == "verify":
                if self.more_menu.icon_names[button] != icon:
                    self.more_menu.icon_names[button] = icon
                    self.more_menu.refresh_icons(self.palette)
            else:
                current, color, size = self.icon_widgets[button]
                if current != icon:
                    self.icon_widgets[button] = (icon, color, size)
                    self.refresh_icon(button)
            button.configure(state=cancel_state if active else batch_state,
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
        if self.working_key is not None and self.cancel_events[self.working_key].is_set():
            self.paint(self.working_key)
            self.working_status()
        elif key is None:
            self.set_status("Canceling…")
        elif self.downloading:
            self.working_status()
        self.controls()

    def start(self, action, *, key=None):
        keys = self.jobs if key is None else (key,)
        if self.busy or not keys:
            return
        self.busy = True
        self.cancel_events = {recipe_key: Event() for recipe_key in keys}
        self.bulk_action = action if key is None else None
        self.total, self.finished, self.canceled, self.failure = len(keys), 0, 0, ""
        self.pending_downloads = set()
        self.download_index = 0
        self.working_action = action
        self.controls()
        self.set_status("{action}…", action=message(WORKING[action]))
        items = [(k, self.jobs[k], token) for k, token in self.cancel_events.items()]
        Thread(target=run_batch, args=(self.events, action, items, self.dest, self.targets)).start()
        label = ALL_ACTIONS[action] if key is None else ACTIONS[action]
        self.root.after(50, self.poll, action, label)

    def poll(self, action, label):
        view_changed = False
        while True:
            try:
                event, *data = self.events.get_nowait()
            except Empty:
                if view_changed:
                    self.refresh_view(select_first=False)
                if self.downloading:
                    # The table can scroll or reorder even while the network is idle.
                    self.position_row_progress()
                self.root.after(50, self.poll, action, label)
                return
            if event == "downloads":
                self.pending_downloads = set(data[0])
            elif event == "started":
                previous = self.working_key
                self.working_key, self.working_action, self.working_index = data
                if previous is not None and previous != self.working_key:
                    self.paint(previous)
                self.update_progress()
                self.paint(self.working_key)
                self.working_status()
                view_changed = True
            elif event == "progress":
                key, received, total = data
                if key == self.working_key:
                    if received is not None and key in self.pending_downloads:
                        self.pending_downloads.remove(key)
                        self.download_index += 1
                    if not self.downloading and self.sort_column == "Status":
                        view_changed = True
                    self.update_progress(received, total)
            elif event == "result":
                key, result = data
                self.update_progress()
                self.working_key = None
                self.pending_downloads.discard(key)
                del self.cancel_events[key]
                if result.status == "Canceled":
                    self.canceled += 1
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
                self.update_progress()
                self.busy = False
                self.working_key = self.working_action = self.bulk_action = None
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
        if self.busy:
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
