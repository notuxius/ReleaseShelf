"""Grid layout for the catalog and the editor's scrollable fields."""

from tkinter import ttk


class _Scrollbar(ttk.Scrollbar):
    def set(self, first, last):
        super().set(first, last)
        if float(first) <= 0 and float(last) >= 1:
            self.grid_remove()
        elif not self.winfo_manager():
            self.grid()


def grid_scrollable(widget, *, horizontal=False, reserve_space=True):
    parent = widget.master
    parent.rowconfigure(0, weight=1)
    parent.columnconfigure(0, weight=1)
    widget.grid(row=0, column=0, sticky="nsew")
    vertical = _Scrollbar(parent, orient="vertical", command=widget.yview)
    vertical.grid(row=0, column=1, sticky="nsew")
    widget.configure(yscrollcommand=vertical.set)
    if horizontal:
        bar = _Scrollbar(parent, orient="horizontal", command=widget.xview)
        bar.grid(row=1, column=0, sticky="nsew")
        widget.configure(xscrollcommand=bar.set)

    def reserve_gutters():
        # Keeping the viewport size stable prevents word wrapping from changing
        # the need for a scrollbar when it appears or disappears.
        parent.columnconfigure(1, minsize=vertical.winfo_reqwidth())
        if horizontal:
            parent.rowconfigure(1, minsize=bar.winfo_reqheight())

    # Let ttk apply the window's theme before measuring the controls.
    if reserve_space:
        parent.after_idle(reserve_gutters)
