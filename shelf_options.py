"""Language and appearance controls for the ReleaseShelf desktop app."""

import tkinter as tk
from tkinter import ttk

from shelf_i18n import LANGUAGES, tr
from shelf_icons import make_icon
from shelf_language_menu import LanguageMenu
from shelf_paint import stage_window
from shelf_theme import PALETTES, THEME_MODES, initial_geometry, style_titlebar


def read_language(path):
    try:
        code = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return "en"
    return code if code in LANGUAGES else "en"


class OptionsWindow(tk.Toplevel):
    def __init__(self, parent, mode, palette_name, on_change, palette, *, language, on_language_change,
                 show_app_icons, on_app_icons_change):
        super().__init__(parent)
        stage_window(self)
        self.transient(parent)
        self.resizable(False, False)
        self.palette = palette
        self.mode_buttons = {}
        self.mode_images = {}
        self.palette_buttons = {}
        self.swatches = {}
        body = ttk.Frame(self, padding=(20, 16))
        body.pack(fill="both", expand=True)
        heading_font = ("Segoe UI Semibold", 11)

        self.language_heading = ttk.Label(body, font=heading_font)
        self.language_heading.pack(anchor="w")
        self.language_button = ttk.Button(body, style="LanguagePicker.TMenubutton", compound="left")
        self.language_button.pack(fill="x", pady=(6, 16))
        self.language_menu = LanguageMenu(self.language_button, on_language_change)

        self.appearance_heading = ttk.Label(body, font=heading_font)
        self.appearance_heading.pack(anchor="w")
        modes = ttk.Frame(body, style="Line.TFrame", padding=1)
        modes.pack(fill="x", pady=(6, 8))
        for column, name in enumerate(THEME_MODES):
            modes.columnconfigure(column, weight=1, uniform="mode")
            button = ttk.Radiobutton(modes, value=name, variable=mode, command=on_change,
                                     style="Theme.Toolbutton", compound="left", takefocus=True)
            button.grid(row=0, column=column, sticky="ew")
            self.mode_buttons[name] = button

        self.app_icons_button = ttk.Checkbutton(body, variable=show_app_icons, command=on_app_icons_change,
                                               style="Options.TCheckbutton")
        self.app_icons_button.pack(anchor="w", pady=(0, 12))

        self.palette_heading = ttk.Label(body, font=heading_font)
        self.palette_heading.pack(anchor="w", pady=(0, 5))
        choices = ttk.Frame(body)
        choices.pack(fill="x")
        for column in (0, 1):
            choices.columnconfigure(column, weight=1, uniform="palette")
        for index, name in enumerate(PALETTES):
            swatch = tk.PhotoImage(master=self, width=46, height=24)
            self.swatches[name] = swatch
            button = ttk.Radiobutton(choices, image=swatch, compound="left",
                                     variable=palette_name, value=name, command=on_change,
                                     style="Palette.Theme.Toolbutton", takefocus=True)
            row, column = divmod(index, 2)
            button.grid(row=row, column=column, sticky="ew", padx=(0, 6) if column == 0 else 0, pady=3)
            self.palette_buttons[name] = button

        self.close_button = ttk.Button(body, compound="left", command=self.destroy)
        self.close_button.pack(anchor="e", pady=(14, 0))
        self.bind("<Escape>", lambda _: self.destroy())
        self.bind("<Map>", lambda event: style_titlebar(self, self.palette.is_dark)
                  if event.widget is self else None)
        self.apply_language(language)
        self.apply_theme(palette)
        initial_geometry(self, 460, self.winfo_reqheight(), parent=parent)
        self.grab_set()
        self.mode_buttons[mode.get()].focus_set()

    def apply_language(self, code):
        self.title(tr("Options"))
        self.language_heading.configure(text=tr("Language"))
        self.language_button.configure(text=LANGUAGES[code][0])
        self.language_menu.select(code)
        self.appearance_heading.configure(text=tr("Appearance"))
        self.app_icons_button.configure(text=tr("Show item icons"))
        self.palette_heading.configure(text=tr("Palette"))
        self.close_button.configure(text=tr("Close"))
        for name, button in (*self.mode_buttons.items(), *self.palette_buttons.items()):
            button.configure(text=tr(name))
        if self.winfo_ismapped():
            self.update_idletasks()
            if self.winfo_reqwidth() > self.winfo_width() or self.winfo_reqheight() > self.winfo_height():
                initial_geometry(self, self.winfo_width(), self.winfo_height(), parent=self.master)

    def apply_theme(self, palette):
        self.palette = palette
        self.configure(background=palette.canvas)
        self.close_icon = make_icon(self, "cancel", palette.text)
        self.close_button.configure(image=self.close_icon)
        self.language_icon = make_icon(self, "language", palette.accent)
        self.language_button.configure(image=self.language_icon)
        self.language_menu.apply_theme(palette)
        mode = "Dark" if palette.is_dark else "Light"
        for name, swatch in self.swatches.items():
            colors = PALETTES[name][mode]
            swatch.put(colors.line, to=(0, 0, 46, 24))
            swatch.put(colors.canvas, to=(1, 1, 45, 23))
            swatch.put(colors.surface, to=(3, 3, 43, 21))
            swatch.put(colors.accent, to=(5, 5, 20, 10))
            swatch.put(colors.subtle, to=(22, 5, 35, 10))
            swatch.put(colors.selection, to=(3, 13, 43, 18))
        for name, button in self.mode_buttons.items():
            icon_name = name.lower()
            normal = make_icon(self, icon_name, palette.muted)
            selected = make_icon(self, icon_name, palette.on_accent)
            active = make_icon(self, icon_name, palette.text)
            button.configure(image=(normal, "selected", selected, "active", active))
            self.mode_images[name] = (normal, selected, active)
        if self.winfo_ismapped():
            style_titlebar(self, palette.is_dark)
