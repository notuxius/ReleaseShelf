"""Shared popup menus for ReleaseShelf commands and choices."""

import tkinter as tk
from tkinter import ttk

from shelf_icons import make_icon
from shelf_i18n import tr
from shelf_theme import work_area


class AppMenu(tk.Toplevel):
    def __init__(self, owner):
        super().__init__(owner, borderwidth=0)
        self.withdraw()
        self.overrideredirect(True)
        self.transient(owner.winfo_toplevel())
        self.border = ttk.Frame(self, style="Line.TFrame", padding=1)
        self.border.pack(fill="both", expand=True)
        self.panel = ttk.Frame(self.border, style="Surface.TFrame", padding=4)
        self.panel.pack(fill="both", expand=True)
        self.buttons = {}
        self.icon_names = {}
        self.images = {}
        self.bind("<ButtonPress>", self.outside_press)
        self.bind("<FocusOut>", self.focus_left)
        self.bind("<Escape>", self.dismiss)
        self.bind("<Tab>", self.dismiss)
        self.bind("<Return>", self.invoke_focused)
        self.bind("<KP_Enter>", self.invoke_focused)
        self.bind("<Down>", lambda _: self.move_focus(1))
        self.bind("<Up>", lambda _: self.move_focus(-1))
        self.protocol("WM_DELETE_WINDOW", self.dismiss)

    def add_action(self, label, command, *, icon):
        def choose():
            self.dismiss()
            command()

        button = ttk.Button(self.panel, text=label, command=choose, style="AppMenu.TButton",
                            compound="left", takefocus=False)
        button.state(["disabled"])
        self.icon_names[button] = icon
        self.add_button(label, button)

    def refresh_icons(self, palette):
        for button, name in self.icon_names.items():
            self.refresh_icon(button, name, palette)

    def refresh_icon(self, button, name, palette):
        normal = make_icon(self, name, palette.text)
        disabled = make_icon(self, name, palette.muted)
        button.configure(image=(normal, "disabled", disabled))
        self.images[button] = (normal, disabled)

    def set_action_icon(self, button, name, palette):
        if self.icon_names[button] != name:
            self.icon_names[button] = name
            self.refresh_icon(button, name, palette)

    def refresh_labels(self):
        for label, button in self.buttons.items():
            button.configure(text=tr(label))

    def add_checkbutton(self, label, variable, command):
        button = ttk.Checkbutton(self.panel, text=label, variable=variable, command=command,
                                 style="AppMenu.TCheckbutton", takefocus=False)
        self.add_button(label, button)

    def add_button(self, label, button):
        button.pack(fill="x")
        button.bind("<Enter>", lambda _: self.highlight(button))
        button.bind("<Motion>", lambda _: self.highlight(button))
        button.bind("<Leave>", lambda _: self.focus_set() if self.winfo_viewable() else None)
        self.buttons[label] = button

    def add_separator(self):
        ttk.Frame(self.panel, height=1, style="Line.TFrame").pack(fill="x", padx=10, pady=4)

    def highlight(self, button):
        target = self if button.instate(["disabled"]) else button
        if self.focus_get() != target:
            target.focus_set()

    def move_focus(self, step):
        enabled = [button for button in self.buttons.values() if button.instate(["!disabled"])]
        if enabled:
            current = self.focus_get()
            index = enabled.index(current) + step if current in enabled else (0 if step > 0 else -1)
            enabled[index % len(enabled)].focus_set()
        return "break"

    def invoke_focused(self, event):
        if event.widget in self.buttons.values():
            event.widget.invoke()
        return "break"

    def popup(self, screen_x, screen_y, *, keyboard=False):
        self.update_idletasks()
        width, height = self.winfo_reqwidth(), self.winfo_reqheight()
        left, top, right, bottom = work_area(self.master)
        x = max(left, min(screen_x, right - width))
        y = screen_y
        if y + height > bottom:
            y = max(top, screen_y - height)
        self.show_at(x, y, keyboard=keyboard)

    def show_at(self, x, y, *, keyboard=False):
        self.geometry(f"+{x}+{y}")
        self.deiconify()
        self.lift()
        self.update_idletasks()
        self.grab_set()
        self.focus_force()
        if keyboard:
            self.move_focus(1)

    def outside_press(self, event):
        x, y = event.x_root - self.winfo_rootx(), event.y_root - self.winfo_rooty()
        if not (0 <= x < self.winfo_width() and 0 <= y < self.winfo_height()):
            return self.dismiss()

    def focus_left(self, event):
        if event.widget is self:
            self.after_idle(self.dismiss_if_unfocused)

    def dismiss_if_unfocused(self):
        focused = self.focus_displayof()
        if self.winfo_viewable() and (focused is None or focused.winfo_toplevel() != self):
            self.dismiss(restore_focus=False)

    def dismiss(self, event=None, *, restore_focus=True):
        if self.winfo_viewable():
            self.grab_release()
            self.withdraw()
            if restore_focus:
                self.master.focus_set()
        return "break"


class DropdownMenu(AppMenu):
    def __init__(self, owner):
        super().__init__(owner)
        self.join = ttk.Frame(self, style="Surface.TFrame", height=1)
        owner.configure(command=self.open_below)
        for key in ("<Down>", "<Return>", "<KP_Enter>", "<space>"):
            owner.bind(key, self.open_below)

    def open_below(self, event=None):
        if self.winfo_viewable():
            return self.dismiss()
        if self.master.instate(["!disabled"]):
            self.update_idletasks()
            self.show_attached(work_area(self.master), keyboard=event is not None)
        return "break"

    def show_attached(self, bounds, *, keyboard=False):
        owner = self.master
        left, top, right, bottom = bounds
        width, height = self.winfo_reqwidth(), self.winfo_reqheight()
        owner_x, owner_y = owner.winfo_rootx(), owner.winfo_rooty()
        owner_right = owner_x + owner.winfo_width()
        owner_bottom = owner_y + owner.winfo_height()
        x = max(left, min(owner_right, right) - width)
        below = owner_bottom - 1 + height <= bottom
        y = owner_bottom - 1 if below else max(top, owner_y + 1 - height)
        # Keep the joining edge clear even when a menu is wider than its trigger.
        self.join.place(x=1, y=0 if below else height - 1, relwidth=1, width=-2, height=1)
        owner.state(["selected"])
        self.show_at(x, y, keyboard=keyboard)

    def dismiss(self, event=None, *, restore_focus=True):
        self.master.state(["!selected"])
        return super().dismiss(event, restore_focus=restore_focus)
