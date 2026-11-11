"""Keyboard-accessible, two-column language picker for the Options window."""

from time import monotonic
import tkinter as tk
from tkinter import ttk
import unicodedata

from shelf_i18n import LANGUAGES
from shelf_menu import DropdownMenu


def search_text(text):
    return "".join(char for char in unicodedata.normalize("NFKD", text.casefold())
                   if not unicodedata.combining(char))


class LanguageMenu(DropdownMenu):
    def __init__(self, owner, on_change):
        super().__init__(owner)
        self.on_change = on_change
        self.codes = tuple(LANGUAGES)
        self.rows = (len(self.codes) + 1) // 2
        self.selected = self.codes[0]
        self.prefix, self.typed_at = "", 0
        self.opening_pointer = None
        self.labels = {}
        self.search_names = {code: tuple(search_text(name) for name in names)
                             for code, names in LANGUAGES.items()}
        self.canvas = tk.Canvas(self.panel, highlightthickness=0, borderwidth=0, takefocus=False)
        self.scroll = ttk.Scrollbar(self.panel, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.items = ttk.Frame(self.canvas, style="Surface.TFrame")
        window = self.canvas.create_window(0, 0, window=self.items, anchor="nw")
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(window, width=event.width))
        self.items.bind("<Configure>", lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.items.columnconfigure((0, 1), weight=1, uniform="language")
        for row in range(self.rows):
            self.items.rowconfigure(row, weight=1, uniform="language")
        for index, (code, (native, english)) in enumerate(LANGUAGES.items()):
            column, row = divmod(index, self.rows)
            button = ttk.Button(self.items, text=native, style="LanguageItem.TButton", takefocus=False,
                                command=lambda c=code: self.choose(c))
            button.grid(row=row, column=column, sticky="nsew", padx=2, pady=1)
            button.columnconfigure(0, weight=1)
            button.rowconfigure((0, 1), weight=1)
            native_label = ttk.Label(button, text=native, style="Native.Language.TLabel")
            native_label.grid(row=0, column=0, rowspan=2 if native == english else 1,
                              sticky="w", padx=(10, 4), pady=(3, 3 if native == english else 0))
            check = ttk.Label(button, style="Check.Language.TLabel", width=2, anchor="center")
            check.grid(row=0, column=1, rowspan=2, padx=(0, 8))
            labels = [native_label, check]
            if native != english:
                subtitle = ttk.Label(button, text=english, style="English.Language.TLabel")
                subtitle.grid(row=1, column=0, sticky="w", padx=(10, 4), pady=(0, 3))
                labels.append(subtitle)
            self.buttons[code] = button
            self.labels[code] = labels
            for widget in (button, *labels):
                widget.bind("<Motion>", lambda event, b=button: self.hover_choice(event, b))
            for label in labels:
                label.bind("<ButtonRelease-1>", lambda event, b=button: self.release_over(event, b))
            button.bind("<FocusIn>", lambda _, c=code: self.focus_changed(c, True))
            button.bind("<FocusOut>", lambda _, c=code: self.focus_changed(c, False))
            for key in ("<Return>", "<KP_Enter>", "<space>"):
                button.bind(key, self.invoke_focused)
        self.bind("<Left>", lambda _: self.switch_column())
        self.bind("<Right>", lambda _: self.switch_column())
        self.bind("<Home>", lambda _: self.focus_code(self.codes[0]))
        self.bind("<End>", lambda _: self.focus_code(self.codes[-1]))
        self.bind("<KeyPress>", self.type_to_jump)
        self.bind("<MouseWheel>", self.mousewheel)
        self.bind("<Button-4>", lambda _: self.canvas.yview_scroll(-1, "units"))
        self.bind("<Button-5>", lambda _: self.canvas.yview_scroll(1, "units"))

    def apply_theme(self, palette):
        self.canvas.configure(background=palette.surface)

    def select(self, code):
        self.selected = code
        for key, button in self.buttons.items():
            state = ["selected" if key == code else "!selected"]
            button.state(state)
            for label in self.labels[key]:
                label.state(state)
            self.labels[key][1].configure(text="✓" if key == code else "")

    def choose(self, code):
        self.dismiss()
        self.on_change(code)

    def release_over(self, event, button):
        x, y = event.x_root - button.winfo_rootx(), event.y_root - button.winfo_rooty()
        if 0 <= x < button.winfo_width() and 0 <= y < button.winfo_height():
            button.invoke()
        return "break"

    def hover_choice(self, event, button):
        if (event.x_root, event.y_root) != self.opening_pointer:
            self.opening_pointer = None
            self.highlight(button)

    def show_attached(self, bounds, *, keyboard=False):
        owner = self.master
        # Mapping the popup under a stationary pointer can generate a motion event.
        # Keep keyboard focus on the selected language until the pointer moves.
        self.opening_pointer = owner.winfo_pointerxy() if keyboard else None
        self.prefix = ""
        left, top, right, bottom = bounds
        above = owner.winfo_rooty() - top
        owner_bottom = owner.winfo_rooty() + owner.winfo_height()
        below = bottom - owner_bottom
        # Of the 10 framing pixels, one overlaps the trigger.
        height = min(self.items.winfo_reqheight(), max(1, max(above, below) - 9))
        frame_width = 10  # Two 1 px borders and two 4 px insets.
        if height < self.items.winfo_reqheight():
            self.scroll.pack(side="right", fill="y")
            frame_width += self.scroll.winfo_reqwidth()
        else:
            self.scroll.pack_forget()
        width = min(max(owner.winfo_width(), self.items.winfo_reqwidth() + frame_width), right - left)
        self.canvas.configure(width=max(1, width - frame_width), height=height,
                              yscrollincrement=max(1, self.items.winfo_reqheight() // self.rows))
        self.update_idletasks()
        super().show_attached(bounds)
        self.focus_code(self.selected)

    def focus_changed(self, code, focused):
        for label in self.labels[code]:
            label.state(["focus" if focused else "!focus"])
        if focused:
            button = self.buttons[code]
            top = button.winfo_y()
            bottom = top + button.winfo_height()
            visible_top = self.canvas.canvasy(0)
            height = self.canvas.winfo_height()
            if top < visible_top or bottom > visible_top + height:
                start = top if top < visible_top else bottom - height
                self.canvas.yview_moveto(start / max(1, self.items.winfo_height()))

    def focus_code(self, code):
        self.buttons[code].focus_set()
        return "break"

    def focused_index(self):
        current = self.focus_get()
        return next((i for i, button in enumerate(self.buttons.values()) if button is current),
                    self.codes.index(self.selected))

    def move_focus(self, step):
        column, row = divmod(self.focused_index(), self.rows)
        column_codes = self.codes[column * self.rows:(column + 1) * self.rows]
        return self.focus_code(column_codes[(row + step) % len(column_codes)])

    def switch_column(self):
        column, row = divmod(self.focused_index(), self.rows)
        return self.focus_code(self.codes[min((1 - column) * self.rows + row, len(self.codes) - 1)])

    def type_to_jump(self, event):
        if not event.char or not event.char.isprintable() or event.state & 0x000c:
            return
        letter = search_text(event.char)
        now = monotonic()
        prefix = self.prefix + letter if now - self.typed_at < 1 else letter
        self.typed_at = now
        index = self.focused_index()
        for query in (prefix, letter):
            # Keep the current match while typing a longer prefix. Repeated initials cycle.
            offset = index if len(query) > 1 else index + 1
            for code in self.codes[offset:] + self.codes[:offset]:
                if any(name.startswith(query) for name in self.search_names[code]):
                    self.prefix = query
                    return self.focus_code(code)
        self.prefix = ""

    def mousewheel(self, event):
        if event.delta:
            self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"
