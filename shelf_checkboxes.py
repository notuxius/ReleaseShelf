"""Shared checkbox indicators for ttk controls and checklist tables."""

import tkinter as tk

from shelf_icons import tinted_strip

MIXED = "ReleaseShelfCheckboxMixed"


def checkbox_image(checked=False, disabled=False):
    state = "On" if checked else "Off"
    return "ReleaseShelfCheckbox" + state + ("Disabled" if disabled else "")


def header_image(checked, total, disabled=False):
    if 0 < checked < total:
        return MIXED + ("Disabled" if disabled else "") + "Header"
    return checkbox_image(total > 0 and checked == total, disabled or not total) + "Header"


def style_checkboxes(style, palette):
    # Named photos keep table cells and ttk elements in sync when the palette changes.
    names = style.tk.call("image", "names")
    accent = palette.tab_indicator or palette.accent
    for suffix, outline, fill in (("", palette.muted, accent), ("Disabled", palette.line, palette.muted)):
        for index, (state, color) in enumerate((("Off", outline), ("On", fill), ("Mixed", fill))):
            name = "ReleaseShelfCheckbox" + state + suffix
            if name not in names:
                style.tk.call("image", "create", "photo", name)
            strip = tk.PhotoImage(master=style.master, data=tinted_strip("checkboxes.png", color), format="png")
            style.tk.call(name, "copy", str(strip), "-from", index * 24, 0, (index + 1) * 24, 20,
                          "-compositingrule", "set")
            # Headings already supply padding. Trim the control's margins so its box lines up with the rows.
            if name + "Header" not in names:
                style.tk.call("image", "create", "photo", name + "Header")
            style.tk.call(name + "Header", "copy", name, "-from", 4, 2, 20, 18, "-compositingrule", "set")
    indicator = "ReleaseShelf.checkbox"
    if indicator not in style.element_names():
        style.element_create(indicator, "image", checkbox_image(),
                             ("disabled", "alternate", MIXED + "Disabled"),
                             ("disabled", "selected", checkbox_image(True, True)),
                             ("disabled", checkbox_image(False, True)),
                             ("alternate", MIXED), ("selected", checkbox_image(True)))
        style.layout("TCheckbutton", [("Checkbutton.padding", {"sticky": "nsew", "children": [
            (indicator, {"side": "left"}),
            ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
                ("Checkbutton.label", {"sticky": "nsew"})]})]})])
    style.configure("TCheckbutton", background=palette.canvas, foreground=palette.text)
    style.map("TCheckbutton", background=[("active", palette.canvas)],
              foreground=[("disabled", palette.muted)])
