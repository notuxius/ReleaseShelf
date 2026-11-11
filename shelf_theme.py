"""Appearance settings and ttk palettes for the ReleaseShelf desktop window."""

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
from pathlib import Path
import tempfile

THEME_MODES = ("System", "Light", "Dark")
WINDOW_SIDE_PADDING = 22


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


def work_area(window):
    if os.name == "nt":
        class MonitorInfo(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                        ("work", wintypes.RECT), ("flags", wintypes.DWORD)]

        user32 = ctypes.windll.user32
        user32.MonitorFromWindow.restype = wintypes.HANDLE
        monitor = user32.MonitorFromWindow(wintypes.HWND(window.winfo_id()), 2)  # Nearest monitor
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
    line = "ReleaseShelfHeadingLine"
    if element not in style.element_names():
        # Named images share the theme's lifetime across catalog windows.
        blank = "ReleaseShelfHeadingBlank"
        for image in (blank, line):
            style.tk.call("image", "create", "photo", image, "-width", 1, "-height", 1)
        style.element_create(element, "image", blank, ("alternate", line))
        style.element_create(bottom, "image", line)
        # Each heading has either a caption or an icon. Center both in the same space.
        style.layout("Treeview.Heading", [
            ("Treeheading.cell", {"sticky": "nsew"}),
            ("Treeheading.border", {"sticky": "nsew", "children": [
                ("Treeheading.padding", {"sticky": "nsew", "children": [
                    ("Treeheading.text", {"sticky": "we"}),
                    ("Treeheading.image", {"sticky": ""})]})]}),
            (element, {"sticky": "nse"}),
            (bottom, {"sticky": "sew"})])
    style.tk.call(line, "put", color)


def style_dropdown_arrows(style, color):
    indicator = "ReleaseShelf.dropdownIndicator"
    down, up = "ReleaseShelfDropdownDown", "ReleaseShelfDropdownUp"
    if indicator not in style.element_names():
        # Leave 8 px before the triangle and 12 px between it and the button edge.
        for image in (down, up):
            style.tk.call("image", "create", "photo", image, "-width", 31, "-height", 6)
        style.element_create(indicator, "image", down, ("selected", up))
        style.layout("TMenubutton", [("Menubutton.border", {"sticky": "nswe", "children": [
            ("Menubutton.focus", {"sticky": "nswe", "children": [
                (indicator, {"side": "right", "sticky": ""}),
                ("Menubutton.padding", {"sticky": "we", "children": [
                    ("Menubutton.label", {"side": "left", "sticky": ""})]})]})]})])
    for row in range(6):
        for image, y in ((down, row), (up, 5 - row)):
            style.tk.call(image, "put", color, "-to", 8 + row, y, 19 - row, y + 1)


def style_editor_tabs(style, palette):
    tab = "Editor.TNotebook.Tab"
    face = "ReleaseShelf.editorTab"
    normal, hover, selected = "ReleaseShelfTabNormal", "ReleaseShelfTabHover", "ReleaseShelfTabSelected"
    if face not in style.element_names():
        # Stretch the face with the caption but keep the three-pixel underline at the bottom.
        for image in (normal, hover, selected):
            style.tk.call("image", "create", "photo", image, "-width", 1, "-height", 4)
        style.element_create(face, "image", normal, ("selected", selected), ("active", hover),
                             border=(0, 0, 0, 3), padding=(0, 0, 0, 3), sticky="nsew")
        style.layout(tab, [(face, {"sticky": "nsew", "children": [
            ("Notebook.padding", {"sticky": "nsew", "children": [
                ("Notebook.focus", {"sticky": "nsew", "children": [
                    ("Notebook.label", {"sticky": ""})]})]})]})])
    for image, fill in ((normal, palette.canvas), (hover, palette.subtle), (selected, palette.selection)):
        style.tk.call(image, "put", fill, "-to", 0, 0, 1, 4)
    style.tk.call(selected, "put", palette.tab_indicator or palette.accent, "-to", 0, 1, 1, 4)
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
    s.configure("Line.TFrame", background=p.line)
    s.configure("Focus.TFrame", background=p.accent)
    s.configure("Sidebar.TFrame", background=p.subtle)
    s.configure("SidebarHeading.TLabel", background=p.subtle, foreground=p.muted, font=("Segoe UI Semibold", 9))
    s.configure("SidebarHint.TLabel", background=p.text, foreground=p.surface, padding=(7, 4),
                font=("Segoe UI", 9))
    s.configure("Title.TLabel", background=p.canvas, font=("Segoe UI Semibold", 24))
    s.configure("Subtitle.TLabel", background=p.canvas, foreground=p.muted)
    s.configure("Empty.TLabel", background=p.surface, font=("Segoe UI Semibold", 13))
    s.configure("Brand.TLabel", background=p.surface, font=("Segoe UI Semibold", 15))
    s.configure("Muted.TLabel", foreground=p.muted)
    s.configure("EditorError.TLabel", foreground="#f5afa6" if p.is_dark else "#a12d26")
    s.configure("TEntry", fieldbackground=p.surface, foreground=p.text, insertcolor=p.text,
                selectbackground=p.selection, selectforeground=p.text, padding=5)
    # Blend clam's bevel and inner focus ring into the fill. Only the outer
    # border changes color on hover or keyboard focus for every dropdown.
    s.configure("TMenubutton", padding=(10, 7), background=p.surface, borderwidth=1,
                bordercolor=p.line, lightcolor=p.surface, darkcolor=p.surface, focuscolor=p.surface)
    menu_fill = [("disabled", p.subtle), ("selected", p.surface), ("pressed", p.selection), ("active", p.subtle)]
    s.map("TMenubutton", background=menu_fill, lightcolor=menu_fill, darkcolor=menu_fill,
          focuscolor=menu_fill, foreground=[("disabled", p.muted)],
          bordercolor=[("disabled", p.line), ("selected", p.line), ("focus", p.accent), ("active", p.accent)])
    style_dropdown_arrows(s, p.muted)
    s.configure("More.TMenubutton", padding=(10, 7, 0, 7), space=6)
    s.configure("LanguagePicker.TMenubutton", padding=(12, 8), space=9, font=("Segoe UI Semibold", 10))
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
    s.configure("AppMenu.TCheckbutton", padding=(10, 6, 16, 6), background=p.surface,
                foreground=p.text, indicatorbackground=p.surface, indicatorforeground=p.on_accent,
                upperbordercolor=p.muted, lowerbordercolor=p.muted, focuscolor=p.selection)
    s.map("AppMenu.TCheckbutton", background=[("focus", p.selection), ("active", p.selection)],
          foreground=[("disabled", p.muted)], indicatorbackground=[("selected", p.accent)])
    s.configure("Options.TCheckbutton", padding=(0, 3), indicatorbackground=p.surface,
                indicatorforeground=p.on_accent, upperbordercolor=p.muted, lowerbordercolor=p.muted)
    s.map("Options.TCheckbutton", background=[("active", p.canvas)],
          indicatorbackground=[("selected", p.accent), ("!selected", p.surface)])
    s.layout("Nav.TButton", flat_button)
    # Reserve space for the overlaid count while allowing translated labels to grow.
    s.configure("Nav.TButton", background=p.subtle, anchor="w", width=-1, space=9, padding=(12, 9, 38, 9))
    s.configure("Compact.Nav.TButton", anchor="center", width=-1, padding=(9, 9))
    nav_background = [("selected", p.selection), ("pressed", p.selection), ("active", p.surface)]
    nav_foreground = [("selected", p.text)]
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
    s.configure("Treeview.Cell", padding=(10, 0))
    # The catalog is flat. Its optional tree column contains only the app icon.
    s.layout("Treeview.Item", [("Treeitem.image", {"sticky": ""})])
    s.map("Treeview", background=[("selected", p.selection)], foreground=[("selected", p.text)])
    style_heading_dividers(s, p.line)
    s.configure("Treeview.Heading", font=("Segoe UI Semibold", 9), background=p.canvas,
                foreground=p.muted, padding=(10, 8), relief="flat", borderwidth=0)
    s.map("Treeview.Heading", background=[("active", p.subtle)])
    s.configure("TScrollbar", background=p.subtle, troughcolor=p.surface, arrowcolor=p.muted,
                arrowsize=11, borderwidth=0, lightcolor=p.subtle, darkcolor=p.subtle)
    s.map("TScrollbar", background=[("active", p.line)])
