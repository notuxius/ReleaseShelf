"""Appearance settings and ttk palettes for the ReleaseShelf desktop window."""

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
from pathlib import Path
import tempfile
from tkinter import ttk

from shelf_checkboxes import style_checkboxes
from shelf_text import TREE_TEXT_PAD

THEME_MODES = ("System", "Light", "Dark")
WINDOW_SIDE_PADDING = 22
ARROW_UP = "▲"
ARROW_DOWN = "▼"
HEADING_FONT = ("Segoe UI Semibold", 9)


@dataclass(frozen=True)
class Palette:
    canvas: str
    surface: str
    subtle: str
    line: str
    text: str
    muted: str
    accent: str
    on_accent: str
    hover_accent: str
    selection: str
    is_dark: bool = False
    tab_indicator: str = ""

    @property
    def error(self):
        return "#f5afa6" if self.is_dark else "#a12d26"


LIGHT = Palette("#f8f9f7", "#ffffff", "#eff2ef", "#d8dedb", "#202c2a",
                "#536460", "#096b60", "#ffffff", "#07594f", "#d8eee6")
DARK = Palette("#10171c", "#19232b", "#24313a", "#384951", "#e9f1f4",
               "#adc0c9", "#85ddc7", "#102922", "#a9eddb", "#28493f", is_dark=True)
DEFAULT_PALETTE = "Slate mint"
PALETTES = {
    "OLED mint": {
        "Light": Palette("#ffffff", "#fbfffc", "#e9f6ee", "#b9d7c6", "#102b20",
                         "#456655", "#006444", "#ffffff", "#005137", "#ccefdc"),
        "Dark": Palette("#000000", "#080c0b", "#121a17", "#35443d", "#eff4f1",
                        "#a7bdb2", "#85ddc7", "#102922", "#a9eddb", "#1d3b32", is_dark=True),
    },
    "Pure carbon": {
        "Light": Palette("#ffffff", "#ffffff", "#f1f1f1", "#c8c8c8", "#101010",
                         "#525252", "#202020", "#ffffff", "#000000", "#dfdfdf"),
        "Dark": Palette("#000000", "#000000", "#141414", "#414141", "#f1f1f1",
                        "#b2b2b2", "#dedede", "#161616", "#f5f5f5", "#303030", is_dark=True),
    },
    "Soft graphite": {
        "Light": Palette("#f2f2f2", "#fafafa", "#e7e7e7", "#c4c4c4", "#292929",
                         "#545454", "#585858", "#fafafa", "#464646", "#d6d6d6"),
        "Dark": Palette("#0c0c0c", "#171717", "#242424", "#4d4d4d", "#dfdfdf",
                        "#adadad", "#484848", "#fafafa", "#585858", "#353535",
                        is_dark=True, tab_indicator="#dfdfdf"),
    },
    "Blue ink": {
        "Light": Palette("#f3f7fe", "#ffffff", "#e8effa", "#c5d2e4", "#192d49",
                         "#4e617e", "#295da5", "#ffffff", "#204c89", "#d3e4fc"),
        "Dark": Palette("#020305", "#080b10", "#121720", "#343f50", "#edf1f8",
                        "#acbacd", "#72a8ff", "#08182f", "#a2c7ff", "#1a3154", is_dark=True),
    },
    "Warm amber": {
        "Light": Palette("#fcf7ed", "#fffdf8", "#f4eddf", "#d8cbb7", "#392b18",
                         "#695a43", "#845000", "#ffffff", "#6b4000", "#f4dfba"),
        "Dark": Palette("#050402", "#0d0b08", "#19160f", "#474034", "#f6f0e4",
                        "#c3b8a4", "#f3b751", "#251907", "#ffd48a", "#3c2d16", is_dark=True),
    },
    DEFAULT_PALETTE: {"Light": LIGHT, "Dark": DARK},
    "Orchid": {
        "Light": Palette("#f8f5fc", "#fefcff", "#f0eaf7", "#d5c8e4", "#31233f",
                         "#665373", "#754393", "#ffffff", "#603477", "#e6d9f3"),
        "Dark": Palette("#18131d", "#231c2b", "#302538", "#51445c", "#f2ebf7",
                        "#c6b5d2", "#d4a8ef", "#301b40", "#e5c0fa", "#473151", is_dark=True),
    },
    "Rosewood": {
        "Light": Palette("#fcf5f3", "#fffdfb", "#f6eae6", "#decac5", "#3f2526",
                         "#765653", "#944451", "#ffffff", "#793541", "#f3d7d4"),
        "Dark": Palette("#1b1414", "#281d1e", "#372729", "#594045", "#f6ebea",
                        "#d2b5b5", "#eeaaa9", "#3c1d20", "#f8c6c2", "#503135", is_dark=True),
    },
}


def preference_path():
    base = os.environ.get("LOCALAPPDATA") or Path.home() / ".config"
    return Path(base) / "ReleaseShelf" / "appearance.txt"


def read_appearance(path):
    try:
        settings = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return "System", DEFAULT_PALETTE
    # Older releases wrote only the mode.
    mode = settings[0].strip() if settings else ""
    palette_name = settings[1].strip() if len(settings) > 1 else ""
    if palette_name == "Current mint":
        palette_name = DEFAULT_PALETTE
    return (mode if mode in THEME_MODES else "System",
            palette_name if palette_name in PALETTES else DEFAULT_PALETTE)


def write_appearance(path, mode, palette_name):
    write_preference(path, f"{mode}\n{palette_name}\n")


def write_preference(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temp = Path(handle.name)
    try:
        temp.write_text(text, encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def read_mode(path):
    return read_appearance(path)[0]


def write_mode(path, mode):
    write_appearance(path, mode, read_appearance(path)[1])


def system_mode():
    if os.name == "nt":
        import winreg
        settings = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, settings) as key:
                light, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                return "Light" if light else "Dark"
        except OSError:
            pass
    return "Light"


def style_titlebar(root, dark):
    if os.name == "nt":
        # Tk's wm frame is the native top-level HWND once the window is mapped.
        hwnd = int(root.tk.call("wm", "frame", root._w), 0)
        value = ctypes.c_int(dark)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 20,
                                                   ctypes.byref(value), ctypes.sizeof(value))


def work_area(window, *, bounds=None):
    if os.name == "nt":
        class MonitorInfo(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                        ("work", wintypes.RECT), ("flags", wintypes.DWORD)]

        user32 = ctypes.windll.user32
        if bounds is None:
            user32.MonitorFromWindow.restype = wintypes.HANDLE
            monitor = user32.MonitorFromWindow(wintypes.HWND(window.winfo_id()), 2)
        else:
            x, y, width, height = bounds
            rect = wintypes.RECT(x, y, x + width, y + height)
            user32.MonitorFromRect.restype = wintypes.HANDLE
            monitor = user32.MonitorFromRect(ctypes.byref(rect), 2)
        info = MonitorInfo()
        info.size = ctypes.sizeof(info)
        if user32.GetMonitorInfoW(wintypes.HANDLE(monitor), ctypes.byref(info)):
            return info.work.left, info.work.top, info.work.right, info.work.bottom
    return 0, 0, window.winfo_screenwidth(), window.winfo_screenheight()


def initial_geometry(window, width, height, *, parent=None):
    window.update_idletasks()
    left, top, right, bottom = work_area(parent or window)
    # Leave room for the window frame and title bar around Tk's client dimensions.
    margin = round(window.winfo_fpixels("0.15i"))
    available_width = right - left - 2 * margin
    available_height = bottom - top - 4 * margin
    width = min(max(width, window.winfo_reqwidth()), available_width)
    height = min(max(height, window.winfo_reqheight()), available_height)
    x, y = left + (available_width - width) // 2, top + (available_height - height) // 2
    if parent is not None:
        x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - height) // 2
    x = max(left, min(x, left + available_width - width))
    y = max(top, min(y, top + available_height - height))
    window.geometry(f"{width}x{height}+{x}+{y}")


def style_heading_dividers(style, color):
    element = "ReleaseShelf.headingDivider"
    bottom = "ReleaseShelf.headingBottom"
    right_line = "ReleaseShelfHeadingRightLine"
    bottom_line = "ReleaseShelfHeadingBottomLine"
    if element not in style.element_names():
        blank = "ReleaseShelfHeadingBlank"
        # ttk tiles image elements. Paint typical heading edges in one blit
        # while keeping their requested thickness and minimum length at 1 px.
        for image, width, height in ((blank, 1, 64), (right_line, 1, 64), (bottom_line, 256, 1)):
            style.tk.call("image", "create", "photo", image, "-width", width, "-height", height)
        style.element_create(element, "image", blank, ("alternate", right_line), height=1)
        style.element_create(bottom, "image", bottom_line, width=1)
        # Each heading has either a caption or an icon. Center both in the same space.
        style.layout("Treeview.Heading", [
            ("Treeheading.cell", {"sticky": "nsew"}),
            ("Treeheading.border", {"sticky": "nsew", "children": [
                ("Treeheading.padding", {"sticky": "nsew", "children": [
                    ("Treeheading.text", {"sticky": "we"}),
                    ("Treeheading.image", {"sticky": ""})]})]}),
            (element, {"sticky": "nse"}),
            (bottom, {"sticky": "sew"})])
    style.tk.call(right_line, "put", color, "-to", 0, 0, 1, 64)
    style.tk.call(bottom_line, "put", color, "-to", 0, 0, 256, 1)


def bind_button_badge(button, badge, sync):
    for event in ("<Enter>", "<Leave>", "<ButtonPress-1>", "<ButtonRelease-1>"):
        badge.bind(event, lambda e, seq=event: button.event_generate(
            seq, x=e.x_root - button.winfo_rootx(), y=e.y_root - button.winfo_rooty(), state=e.state))
    # ttk's class bindings set the button state after widget bindings run.
    for event in ("<Enter>", "<Leave>", "<ButtonPress-1>", "<ButtonRelease-1>", "<space>", "<<Invoke>>"):
        button.bind(event, lambda _: button.after_idle(sync), add="+")

def style_dropdowns(style, palette):
    combo_arrow = "ReleaseShelf.comboArrow"
    down, up = "ReleaseShelfDropdownDown", "ReleaseShelfDropdownUp"
    field, focus = "ReleaseShelfComboField", "ReleaseShelfComboFocus"
    if combo_arrow not in style.element_names():
        # Leave 8 px before the triangle and 12 px between it and the button edge.
        for image in (down, up):
            style.tk.call("image", "create", "photo", image, "-width", 31, "-height", 6)
        style.element_create(combo_arrow, "image", down, ("pressed", up))
        # A square, fully painted field avoids clam's exposed corner pixels.
        for image in (field, focus):
            style.tk.call("image", "create", "photo", image, "-width", 64, "-height", 32)
        combo_field = "ReleaseShelf.comboField"
        style.element_create(combo_field, "image", field, ("pressed", focus), ("focus", focus),
                             ("active", focus), border=1, width=1, height=1, sticky="nsew")
        style.layout("TCombobox", [(combo_field, {"sticky": "nswe", "children": [
            (combo_arrow, {"side": "right", "sticky": ""}),
            ("Combobox.padding", {"sticky": "nswe", "children": [
                ("Combobox.textarea", {"sticky": "nswe"})]})]})])
        style.layout("TMenubutton", [("Menubutton.border", {"sticky": "nswe", "children": [
            ("Menubutton.focus", {"sticky": "nswe", "children": [
                ("Menubutton.padding", {"sticky": "we", "children": [
                    ("Menubutton.label", {"side": "left", "sticky": ""})]})]})]})])
    for row in range(6):
        for image, y in ((down, row), (up, 5 - row)):
            style.tk.call(image, "put", palette.muted, "-to", 8 + row, y, 19 - row, y + 1)
    for image, outline in ((field, palette.line), (focus, palette.accent)):
        style.tk.call(image, "put", outline, "-to", 0, 0, 64, 32)
        style.tk.call(image, "put", palette.surface, "-to", 1, 1, 63, 31)


def category_picker(parent, variable, choices, *, width):
    field = ttk.Combobox(parent, textvariable=variable, values=choices, width=width, height=10,
                         font=("Segoe UI", 10))
    style = ttk.Style(parent)
    popup = field.tk.call("ttk::combobox::PopdownWindow", field)
    frame = f"{popup}.f"
    call = field.tk.call
    call(frame, "configure", "-style", "Surface.TFrame")
    call("grid", "configure", frame, "-padx", 1, "-pady", (0, 1))

    def join_popup():
        below = call("winfo", "rooty", popup) >= field.winfo_rooty()
        call("grid", "configure", frame, "-pady", (0, 1) if below else (1, 0))

    call("bind", popup, "<Configure>", field.register(join_popup))

    def style_popup():
        # Tk builds the popup with a classic listbox outside the ttk style system.
        call(popup, "configure", "-background", style.lookup("Focus.TFrame", "background"))
        field.tk.call(f"{popup}.f.l", "configure", "-font", field.cget("font"),
                      "-background", style.lookup("TCombobox", "fieldbackground"),
                      "-foreground", style.lookup("TCombobox", "foreground"),
                      "-selectbackground", style.lookup("TCombobox", "selectbackground", ("focus",)),
                      "-selectforeground", style.lookup("TCombobox", "selectforeground", ("focus",)),
                      "-borderwidth", 5, "-relief", "flat", "-selectborderwidth", 0)

    field.configure(postcommand=style_popup)
    return field


def style_editor_tabs(style, palette):
    tab = "Editor.TNotebook.Tab"
    face = "ReleaseShelf.editorTab"
    normal, hover, selected = "ReleaseShelfTabNormal", "ReleaseShelfTabHover", "ReleaseShelfTabSelected"
    if face not in style.element_names():
        # Tk tiles the image for every repaint. Cover a typical tab in one or
        # two blits while letting the caption determine the actual tab size.
        for image in (normal, hover, selected):
            style.tk.call("image", "create", "photo", image, "-width", 128, "-height", 48)
        style.element_create(face, "image", normal, ("selected", selected), ("active", hover),
                             width=1, height=4, border=(0, 0, 0, 3), padding=(0, 0, 0, 3), sticky="nsew")
        style.layout(tab, [(face, {"sticky": "nsew", "children": [
            ("Notebook.padding", {"sticky": "nsew", "children": [
                ("Notebook.focus", {"sticky": "nsew", "children": [
                    ("Notebook.label", {"sticky": ""})]})]})]})])
    for image, fill in ((normal, palette.canvas), (hover, palette.subtle), (selected, palette.selection)):
        style.tk.call(image, "put", fill, "-to", 0, 0, 128, 48)
    style.tk.call(selected, "put", palette.tab_indicator or palette.accent, "-to", 0, 45, 128, 48)
    style.configure("Editor.TNotebook", tabmargins=0, background=palette.canvas, bordercolor=palette.line,
                    lightcolor=palette.canvas, darkcolor=palette.canvas)
    padding = (15, 8)
    style.configure(tab, padding=padding, font=("Segoe UI Semibold", 10), foreground=palette.muted,
                    focuscolor=palette.muted)
    # Clam normally changes the padding on selection, which moves the neighboring tabs.
    style.map(tab, padding=[("selected", padding)], foreground=[("selected", palette.text), ("active", palette.text)])


def style_widgets(s, p):
    s.configure(".", background=p.canvas, foreground=p.text, font=("Segoe UI", 10),
                bordercolor=p.line, lightcolor=p.surface, darkcolor=p.surface,
                troughcolor=p.canvas, focuscolor=p.accent, focussolid=True)
    s.configure("Surface.TFrame", background=p.surface)
    s.configure("TSizegrip", background=p.surface)
    s.configure("Line.TFrame", background=p.line)
    s.configure("Focus.TFrame", background=p.accent)
    s.configure("Sidebar.TFrame", background=p.subtle)
    s.configure("SidebarHint.TLabel", background=p.text, foreground=p.surface, padding=(7, 4),
                font=("Segoe UI", 9))
    s.configure("Title.TLabel", background=p.canvas, font=("Segoe UI Semibold", 24))
    s.configure("Subtitle.TLabel", background=p.canvas, foreground=p.muted)
    s.configure("Empty.TLabel", background=p.surface, font=("Segoe UI Semibold", 13))
    s.configure("Brand.TLabel", background=p.surface, font=("Segoe UI Semibold", 15))
    s.configure("Muted.TLabel", foreground=p.muted)
    s.configure("EditorError.TLabel", foreground=p.error)
    s.configure("TEntry", fieldbackground=p.surface, foreground=p.text, insertcolor=p.text,
                selectbackground=p.selection, selectforeground=p.text, padding=5)
    s.configure("TCombobox", fieldbackground=p.surface, foreground=p.text, insertcolor=p.text,
                selectbackground=p.selection, selectforeground=p.text, padding=(8, 6),
                postoffset=(0, 1, 0, -2))  # Overlap the field outline by 1 px in either direction.
    # Blend clam's bevel and inner focus ring into the fill. Only the outer
    # border changes color on hover or keyboard focus for every dropdown.
    s.configure("TMenubutton", padding=(10, 7, "23p", 7), background=p.surface, borderwidth=1,
                bordercolor=p.line, lightcolor=p.surface, darkcolor=p.surface, focuscolor=p.surface)
    menu_fill = [("disabled", p.subtle), ("selected", p.surface), ("pressed", p.selection), ("active", p.subtle)]
    s.map("TMenubutton", background=menu_fill, lightcolor=menu_fill, darkcolor=menu_fill,
          focuscolor=menu_fill, foreground=[("disabled", p.muted)],
          bordercolor=[("disabled", p.line), ("selected", p.line), ("focus", p.accent), ("active", p.accent)])
    s.configure("DropdownArrow.TLabel", font=HEADING_FONT, foreground=p.muted, background=p.surface)
    s.map("DropdownArrow.TLabel", background=menu_fill)
    style_dropdowns(s, p)
    s.configure("More.TMenubutton", space=6)
    s.configure("LanguagePicker.TMenubutton", space=9, font=("Segoe UI Semibold", 10))
    s.layout("LanguageItem.TButton", [("Button.border", {"sticky": "nsew", "border": 1,
              "children": [("Button.padding", {"sticky": "nsew"})]})])
    item_fill = [("selected", p.selection), ("focus", p.subtle)]
    s.configure("LanguageItem.TButton", padding=(3, 4), background=p.surface, bordercolor=p.surface,
                lightcolor=p.surface, darkcolor=p.surface, relief="solid", borderwidth=1)
    s.map("LanguageItem.TButton", background=item_fill, lightcolor=item_fill, darkcolor=item_fill,
          bordercolor=[("focus", p.accent), ("selected", p.selection)])
    for style, face, size, ink in (("Native", "Segoe UI Semibold", 10, p.text),
                                   ("English", "Segoe UI", 9, p.muted),
                                   ("Check", "Segoe UI Symbol", 11, p.accent)):
        label = f"{style}.Language.TLabel"
        s.configure(label, font=(face, size), foreground=ink, background=p.surface)
        s.map(label, background=item_fill)
    s.layout("Search.TEntry", [("Entry.padding", {"sticky": "nswe", "children": [
        ("Entry.textarea", {"sticky": "nswe"})
    ]})])
    # Leave room for the clear icon inside the entry's right edge.
    s.configure("Search.TEntry", background=p.surface, fieldbackground=p.surface, padding=(2, 5, 26, 5))
    style_editor_tabs(s, p)
    s.configure("Small.TLabel", background=p.surface, foreground=p.muted, font=("Segoe UI", 9))
    progress_fill = p.tab_indicator or p.accent
    s.configure("Download.Horizontal.TProgressbar", background=progress_fill, troughcolor=p.subtle,
                bordercolor=p.line, lightcolor=progress_fill, darkcolor=progress_fill, thickness=8)
    s.configure("Detail.TLabel", background=p.surface, font=("Segoe UI Semibold", 13))
    s.configure("TButton", background=p.surface, padding=(12, 7), borderwidth=1, relief="flat")
    s.map("TButton", background=[("disabled", p.subtle), ("pressed", p.selection), ("active", p.subtle)],
          foreground=[("disabled", p.muted)], bordercolor=[("focus", p.accent)])
    s.configure("Primary.TButton", background=p.accent, foreground=p.on_accent, width=-1, space=7,
                font=("Segoe UI Semibold", 10), bordercolor=p.accent, lightcolor=p.accent, darkcolor=p.accent)
    primary_fill = [("disabled", p.subtle), ("pressed", p.hover_accent), ("active", p.hover_accent)]
    s.map("Primary.TButton", background=primary_fill, foreground=[("disabled", p.muted)],
          bordercolor=[("focus", p.text), *primary_fill],
          lightcolor=primary_fill, darkcolor=primary_fill)

    # Use flat labels inside command and navigation buttons. Keep the focus
    # element so keyboard navigation still has a visible outline.
    flat_button = [("Button.focus", {"sticky": "nswe", "children": [
        ("Button.padding", {"sticky": "nswe", "children": [
            ("Button.label", {"sticky": "nswe"})
        ]})
    ]})]
    s.layout("Command.TButton", flat_button)
    s.configure("Command.TButton", width=-1, background=p.surface, space=6, padding=(8, 7))
    s.map("Command.TButton", background=[("disabled", p.surface), ("pressed", p.selection), ("active", p.subtle)],
          foreground=[("disabled", p.muted)])
    s.layout("AppMenu.TButton", [("Button.padding", {"sticky": "nswe", "children": [
        ("Button.label", {"sticky": "nswe"})
    ]})])
    s.configure("AppMenu.TButton", anchor="w", width=-1, padding=(11, 7, 22, 7), space=10,
                background=p.surface, foreground=p.text)
    s.map("AppMenu.TButton", background=[("disabled", p.surface), ("focus", p.selection)],
          foreground=[("disabled", p.muted)])
    style_checkboxes(s, p)
    s.configure("AppMenu.TCheckbutton", padding=(7, 6, 16, 6), background=p.surface,
                foreground=p.text, focuscolor=p.selection)
    s.map("AppMenu.TCheckbutton", background=[("focus", p.selection), ("active", p.selection)],
          foreground=[("disabled", p.muted)])
    s.configure("Options.TCheckbutton", padding=(0, 3))
    s.layout("Nav.TButton", flat_button)
    # Reserve space for the overlaid count while allowing translated labels to grow.
    s.configure("Nav.TButton", background=p.subtle, anchor="w", width=-1, space=9, padding=(12, 9, 38, 9))
    s.configure("Compact.Nav.TButton", anchor="center", width=-1, padding=(9, 9))
    nav_background = [("disabled", p.subtle), ("selected", p.selection), ("pressed", p.selection), ("active", p.surface)]
    nav_foreground = [("disabled", p.line), ("selected", p.text)]
    s.map("Nav.TButton", background=nav_background, foreground=nav_foreground)
    s.configure("Count.Nav.TLabel", background=p.subtle, foreground=p.muted, font=("Segoe UI Semibold", 9))
    s.map("Count.Nav.TLabel", background=nav_background, foreground=nav_foreground)
    s.configure("Theme.Toolbutton", background=p.subtle, foreground=p.muted,
                padding=(11, 5), space=6, relief="flat", borderwidth=0)
    s.map("Theme.Toolbutton", background=[("selected", p.accent), ("active", p.selection)],
          foreground=[("selected", p.on_accent), ("active", p.text)], relief=[("selected", "flat")],
          focuscolor=[("selected", p.on_accent), ("focus", p.text)])
    s.configure("Palette.Theme.Toolbutton", anchor="w", padding=(8, 7), space=8,
                background=p.surface, foreground=p.text)
    # The enclosing card supplies the one-pixel border around the table.
    s.layout("Treeview", [("Treeview.treearea", {"sticky": "nsew"})])
    s.configure("Treeview", rowheight=42, background=p.surface, foreground=p.text)
    s.configure("Categories.Treeview", rowheight=32, background=p.subtle, fieldbackground=p.subtle)
    s.configure("Treeview.Cell", padding=(TREE_TEXT_PAD, 0))
    s.configure("Categories.Treeview.Cell", padding=0)
    # Center app icons and checklist boxes in the leading column of these flat tables.
    s.layout("Treeview.Item", [("Treeitem.image", {"sticky": ""})])
    s.layout("Categories.Treeview.Item", [("Treeitem.padding", {"sticky": "nsew", "children": [
        ("Treeitem.image", {"sticky": ""})]})])
    s.configure("Categories.Treeview.Item", padding=(16, 0, 0, 0))
    s.layout("Compact.Categories.Treeview.Item", [("Treeitem.image", {"sticky": ""})])
    # Keep tagged error text visible on the selection background.
    s.map("Treeview", background=[("selected", p.selection)], foreground=[])
    style_heading_dividers(s, p.line)
    s.configure("Treeview.Heading", font=HEADING_FONT, background=p.canvas,
                foreground=p.muted, padding=(TREE_TEXT_PAD, 8), relief="flat", borderwidth=0)
    s.map("Treeview.Heading", background=[("active", p.subtle)])
    s.configure("TScrollbar", background=p.subtle, troughcolor=p.surface, arrowcolor=p.muted,
                arrowsize=11, borderwidth=0, lightcolor=p.subtle, darkcolor=p.subtle)
    s.map("TScrollbar", background=[("active", p.line)])
