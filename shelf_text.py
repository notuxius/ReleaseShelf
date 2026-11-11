"""Fit single-line display text to the space allocated by Tk."""

from tkinter import ttk


TREE_TEXT_PAD = 10
# Tk 8.6 DrawCells insets the cell by four pixels per side before applying the theme.
CELL_TEXT_INSETS = 2 * (TREE_TEXT_PAD + 4)


def fit_text(widget, font, text, width, suffix=""):
    def fits(value):
        return widget.tk.getint(widget.tk.call("font", "measure", font, "-displayof", widget, value)) <= width

    line, newline, _ = text.replace("\r\n", "\n").replace("\r", "\n").partition("\n")
    if not newline and fits(line + suffix):
        return line + suffix
    ending = "..." + suffix
    if not fits(ending):
        ending = "..."
    if not fits(ending):
        return next((dots for dots in ("..", ".") if fits(dots)), "")
    low, high = 0, len(line)
    while low < high:
        middle = (low + high + 1) // 2
        if fits(line[:middle] + ending):
            low = middle
        else:
            high = middle - 1
    return line[:low] + ending


class FittedLabel(ttk.Label):
    def __init__(self, master, **options):
        super().__init__(master, width=1, **options)
        self.full_text = ""
        self.bind("<Configure>", self.fit)
        self.bind("<<ThemeChanged>>", self.fit)

    def set_text(self, text):
        self.full_text = text
        self.fit()

    def fit(self, _event=None):
        text = self.full_text
        if self.winfo_width() > 1:
            style = ttk.Style(self)
            font = style.lookup(self["style"] or "TLabel", "font")
            # The clam label has a one-pixel border on each side.
            text = fit_text(self, font, text, self.winfo_width() - 2)
        self.configure(text=text)


class FittedTreeview(ttk.Treeview):
    """Keep the original captions so wider columns can reveal the full text."""

    def __init__(self, master, **options):
        super().__init__(master, **options)
        self.rows = {}
        self.captions = {}
        self.widths = ()
        self.pending_fit = None
        self.style = ttk.Style(self)
        for event in ("<Map>", "<Configure>", "<B1-Motion>", "<ButtonRelease-1>"):
            self.bind(event, self.schedule_fit, add="+")
        self.bind("<<ThemeChanged>>", self.theme_changed)

    def theme_changed(self, _event):
        self.widths = ()
        self.schedule_fit()

    def column(self, column, option=None, **options):
        result = super().column(column, option, **options)
        if options:
            self.schedule_fit()
        return result

    def destroy(self):
        if self.pending_fit is not None:
            self.after_cancel(self.pending_fit)
        super().destroy()

    def schedule_fit(self, _event=None):
        if self.pending_fit is None:
            self.pending_fit = self.after_idle(self.fit_columns)

    def fit_columns(self):
        self.pending_fit = None
        if not self.winfo_ismapped():
            return
        widths = tuple(self.column(col, "width") for col in self["columns"])
        if widths == self.widths:
            return
        self.widths = widths
        self.row_font = self.style.lookup(self["style"] or "Treeview", "font")
        self.heading_font = self.style.lookup("Treeview.Heading", "font")
        for row, values in self.rows.items():
            self.draw_row(row, values)
        for col, (text, suffix) in self.captions.items():
            self.draw_heading(col, text, suffix)

    def set_values(self, row, values):
        self.rows[row] = values
        self.draw_row(row, values)

    def draw_row(self, row, values):
        if self.widths:
            values = tuple(fit_text(self, self.row_font, str(value), width - CELL_TEXT_INSETS)
                           for value, width in zip(values, self.widths, strict=True))
        self.item(row, values=values)

    def set_caption(self, col, text, suffix=""):
        self.captions[col] = (text, suffix)
        self.draw_heading(col, text, suffix)

    def draw_heading(self, col, text, suffix):
        caption = text + suffix
        if self.widths:
            width = self.widths[self["columns"].index(col)]
            caption = fit_text(self, self.heading_font, text, width - 2 * TREE_TEXT_PAD, suffix)
        self.heading(col, text=caption)

    def delete(self, *items):
        super().delete(*items)
        for row in items:
            self.rows.pop(row, None)
