"""Exercise the desktop buttons against an isolated catalog and cache."""

import gc
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from string import Formatter
from threading import Event
from time import monotonic, sleep
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

from fetch_store import Result
from shelf_window import ShelfWindow
from shelf_i18n import LANGUAGES, catalog, tr
from shelf_options import read_language
from shelf_theme import DARK, LIGHT, PALETTES, read_appearance


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def right_edge(widget):
    return widget.winfo_rootx() + widget.winfo_width()


class GatedPage:
    def __init__(self):
        self.entered = Event()
        self.resume = Event()

    def __call__(self, *args, **kwargs):
        self.entered.set()
        if not self.resume.wait(15):
            raise TimeoutError("Publisher response was never released")
        return "1"


class GatedDownload(io.BytesIO):
    """Hold the second chunk until the UI has displayed the first one."""

    def __init__(self, *, content_length=True, fail=False):
        super().__init__(b"download")
        self.headers = {"Content-Length": "8"} if content_length else {}
        self.waiting = Event()
        self.resume = Event()
        self.fail = fail

    def read1(self, size=-1):
        if self.tell():
            self.waiting.set()
            if not self.resume.wait(15):
                raise TimeoutError("The download test did not release the response")
            if self.fail:
                raise OSError("Interrupted download")
        return super().read(4 if size < 0 else min(size, 4))


class DesktopTest(unittest.TestCase):
    def setUp(self):
        # Dispose of previous Tk interpreters on their owning thread before starting workers.
        gc.collect()
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"No Tk display: {exc}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        catalog = Path(tmp.name) / "catalog"
        catalog.mkdir()
        self.dest = Path(tmp.name) / "cache"
        for name in ("alpha", "beta"):
            (catalog / f"{name}.json").write_text(json.dumps({
                "id": name, "page": "https://publisher.example", "version_pattern": "(1)",
                "download": f"https://publisher.example/{name}.bin",
            }))
        self.appearance = Path(tmp.name) / "settings" / "appearance.txt"
        self.ui = ShelfWindow(self.root, catalog, self.dest, self.dest / "targets.json",
                              appearance_file=self.appearance)
        self.keys = self.ui.tree.get_children()

    def edit_selected(self, tab_name=None):
        self.ui.edit_button.invoke()
        editor = self.ui.editor
        if tab_name is not None:
            tabs = editor.tabs
            tabs.select(next(tab for tab in tabs.tabs() if tabs.tab(tab, "text") == tr(tab_name)))
        return editor

    def right_click_app(self, key):
        self.root.deiconify()
        self.root.update()
        tree = self.ui.tree
        x, y, width, height = tree.bbox(key, "Name")
        tree.event_generate("<Button-3>", x=x + width // 2, y=y + height // 2)
        self.root.update()
        self.assertTrue(self.ui.app_menu.winfo_viewable())
        return self.ui.app_menu

    def click(self, button):
        # An editor closed during this test may still have cyclic Tk widget references.
        gc.collect()
        button.invoke()
        self.wait_until_idle()

    def wait_until_idle(self):
        deadline = monotonic() + 5
        while self.ui.busy and monotonic() < deadline:
            self.root.update()
            sleep(.005)
        self.assertFalse(self.ui.busy)

    def wait_for(self, condition):
        deadline = monotonic() + 5
        while not condition() and monotonic() < deadline:
            self.root.update()
            sleep(.005)
        self.assertTrue(condition())

    def test_language_selector_translates_live_and_survives_restart(self):
        key = self.keys[1]
        self.ui.search.set("beta")
        self.ui.column_menu.buttons["Source"].invoke()
        with patch("fetch_sources.read_page", return_value="1"):
            self.click(self.ui.all_buttons["check"])
        self.ui.options_button.invoke()
        options = self.ui.options
        expected = {"ar", "arz", "bn", "de", "en", "es", "fr", "hi", "id", "ja", "pcm", "pt", "ru", "uk", "ur", "zh"}
        self.assertEqual(set(LANGUAGES), expected)
        self.assertEqual(set(options.language_menu.buttons), expected)
        for code, (native, _) in LANGUAGES.items():
            with self.subTest(code=code):
                options.language_menu.buttons[code].invoke()
                self.root.update()
                self.assertEqual(options.language_button.cget("text"), native)
                self.assertEqual(read_language(self.ui.language_file), code)
                self.assertEqual(options.language_heading.cget("text"), tr("Language"))
                self.assertEqual(self.ui.page_title.get(), tr("Catalog"))
                if code != "en":
                    self.assertNotEqual(self.ui.page_title.get(), "Catalog")
                self.assertEqual(self.ui.tree.heading("Name", "text"), tr("Name"))
                self.assertEqual(self.ui.column_menu.buttons["Item icon"].cget("text"), tr("Item icon"))
                self.assertEqual(self.ui.tree.get_children(), (key,))
                self.assertEqual(self.ui.tree.selection(), (key,))
                self.assertNotIn("Source", self.ui.tree["displaycolumns"])
                self.assertEqual(self.ui.tree.set(key, "Status"), tr("Update available"))
                self.assertEqual(self.ui.results[key].status, "Update available")
        options.language_menu.buttons["ur"].invoke()
        options.destroy()
        self.ui.edit_button.invoke()
        editor = self.ui.editor
        self.assertEqual(editor.title(), tr("Edit item"))
        self.assertEqual(editor.fields["id"].get(), "beta")
        self.assertIn(tr("Missing manifest: {path}", path=self.dest / "beta" / "current.json"),
                      editor.detail.get("1.0", "end"))
        editor.destroy()
        second = tk.Toplevel(self.root)
        self.addCleanup(second.destroy)
        reopened = ShelfWindow(second, self.ui.catalog, self.dest, self.ui.targets, appearance_file=self.appearance)
        self.assertEqual(reopened.page_title.get(), catalog("ur")["Catalog"])
        reopened.options_button.invoke()
        self.assertEqual(reopened.options.language_button.cget("text"), "اردو")

    def assert_dropdown_join(self, trigger, menu, *, below):
        self.assertTrue(trigger.instate(["selected"]))
        self.assertTrue(menu.join.winfo_viewable())
        shared_y = trigger.winfo_rooty() + (trigger.winfo_height() - 1 if below else 0)
        self.assertEqual(menu.join.winfo_rooty(), shared_y)
        self.assertEqual(menu.join.winfo_height(), 1)
        self.assertEqual(menu.join.winfo_rootx(), menu.winfo_rootx() + 1)
        self.assertEqual(right_edge(menu.join), right_edge(menu) - 1)

    def test_keyboard_language_picker_previews_cancels_and_commits(self):
        self.root.deiconify()
        self.ui.open_options()
        options = self.ui.options
        self.root.update()
        trigger, menu = options.language_button, options.language_menu
        trigger.focus_force()
        x, y = trigger.winfo_rootx(), trigger.winfo_rooty()
        # Exercise both attachment edges independently of the host monitor size.
        with patch("shelf_menu.work_area", return_value=(0, y, x + 1000, y + 1000)):
            trigger.event_generate("<Down>")
        self.root.update()
        self.assertTrue(menu.winfo_viewable())
        self.assert_dropdown_join(trigger, menu, below=True)
        self.assertIs(menu.focus_get(), menu.buttons["en"])
        menu.focus_get().event_generate("<KeyPress-j>")
        self.root.update()
        self.assertIs(menu.focus_get(), menu.buttons["ja"])
        menu.focus_get().event_generate("<Escape>")
        self.root.update()
        self.assertFalse(menu.winfo_viewable())
        self.assertTrue(trigger.instate(["!selected"]))
        self.assertEqual(self.ui.language, "en")
        with patch("shelf_menu.work_area", return_value=(0, 0, x + 1000, y + trigger.winfo_height())):
            trigger.invoke()
        self.root.update()
        self.assert_dropdown_join(trigger, menu, below=False)
        menu.focus_get().event_generate("<KeyPress-d>")
        self.root.update()
        self.assertIs(menu.focus_get(), menu.buttons["de"])
        menu.focus_get().event_generate("<Return>")
        self.root.update()
        self.assertFalse(menu.winfo_viewable())
        self.assertTrue(trigger.instate(["!selected"]))
        self.assertEqual(trigger.cget("text"), "Deutsch")
        self.assertEqual(read_language(self.ui.language_file), "de")

    def test_invalid_language_preference_uses_english(self):
        self.ui.language_file.parent.mkdir(parents=True, exist_ok=True)
        for contents in (b"unknown", b"", b"\xff"):
            with self.subTest(contents=contents):
                self.ui.language_file.write_bytes(contents)
                self.assertEqual(read_language(self.ui.language_file), "en")

    def test_language_catalogs_preserve_format_arguments(self):
        formatter = Formatter()

        def fields(template):
            return sorted((name, spec, conversion) for _, name, spec, conversion in formatter.parse(template)
                          if name is not None)

        reference = catalog("uk")
        for code in LANGUAGES.keys() - {"en"}:
            translations = catalog(code)
            self.assertEqual(translations.keys(), reference.keys(), code)
            for source, translation in translations.items():
                with self.subTest(code=code, source=source):
                    self.assertTrue(translation.strip())
                    self.assertEqual(fields(translation), fields(source))

    def test_right_click_menu_runs_on_the_clicked_app(self):
        self.root.deiconify()
        self.root.update()
        tree = self.ui.tree
        first, second = self.keys
        self.assertEqual(tree.selection(), (first,))
        x, y, width, height = tree.bbox(second, "Status")
        tree.event_generate("<Button-3>", x=x + width // 2, y=y + height // 2)
        self.root.update()
        self.assertEqual(tree.selection(), (second,))
        self.assertEqual(tree.focus(), second)
        menu = self.ui.app_menu
        self.assertTrue(menu.winfo_viewable())
        for label in ("Check for updates", "Get updates", "Verify files", "Edit item"):
            button = menu.buttons[label]
            self.assertTrue(button.instate(["!disabled"]))
            self.assertEqual(str(button.cget("compound")), "left")
            self.assertIn(button.cget("image")[0], self.root.image_names())
        tree.selection_set(first)
        menu.buttons["Verify files"].invoke()
        self.wait_until_idle()
        self.assertFalse(menu.winfo_viewable())
        self.assertEqual(self.ui.results[second].status, "Repair needed")
        self.assertEqual(self.ui.results[first].status, "Not checked")

    def test_right_click_below_rows_leaves_selection_alone(self):
        self.root.deiconify()
        self.root.update()
        tree = self.ui.tree
        x, y, _, height = tree.bbox(self.keys[-1], "Name")
        with (patch.object(self.ui.app_menu, "popup") as row_popup,
              patch.object(self.ui.column_menu, "popup") as column_popup):
            tree.event_generate("<Button-3>", x=x + 10, y=y + height + 5)
            row_popup.assert_not_called()
            column_popup.assert_not_called()
        self.assertEqual(tree.selection(), (self.keys[0],))

    def test_more_menu_opens_with_mouse_and_keyboard(self):
        self.root.deiconify()
        self.root.update()
        trigger, menu = self.ui.more_button, self.ui.more_menu
        x, y = trigger.winfo_rootx(), trigger.winfo_rooty()
        with patch("shelf_menu.work_area", return_value=(0, y, x + 1000, y + 1000)):
            trigger.event_generate("<ButtonPress-1>", x=8, y=8)
            trigger.event_generate("<ButtonRelease-1>", x=8, y=8)
        self.root.update()
        self.assertTrue(menu.winfo_viewable())
        self.assert_dropdown_join(trigger, menu, below=True)
        self.assertEqual(tuple(menu.buttons), ("Verify all items", "Reload catalog"))
        row_style = self.ui.app_menu.buttons["Edit item"].cget("style")
        for button in menu.buttons.values():
            self.assertEqual(button.cget("style"), row_style)
            self.assertEqual(str(button.cget("compound")), "left")
            self.assertIn(button.cget("image")[0], self.root.image_names())
        menu.buttons["Reload catalog"].invoke()
        self.root.update()
        self.assertFalse(menu.winfo_viewable())
        self.assertTrue(trigger.instate(["!selected"]))
        trigger.focus_force()
        with patch("shelf_menu.work_area", return_value=(0, 0, x + 1000, y + trigger.winfo_height())):
            trigger.event_generate("<Down>")
        self.root.update()
        self.assertTrue(menu.winfo_viewable())
        self.assert_dropdown_join(trigger, menu, below=False)
        first = menu.buttons["Verify all items"]
        self.assertIs(self.root.focus_get(), first)
        first.event_generate("<Escape>")
        self.root.update()
        self.assertFalse(menu.winfo_viewable())
        self.assertTrue(trigger.instate(["!selected"]))
        self.assertIs(self.root.focus_get(), trigger)

    def test_dropdowns_indicate_focus_with_a_single_outer_border(self):
        self.ui.options_button.invoke()
        palette = self.ui.palette
        for button in (self.ui.more_button, self.ui.options.language_button):
            style = button.cget("style")
            for state, border in (((), palette.line), (("focus",), palette.accent),
                                  (("active",), palette.accent), (("active", "pressed", "focus"), palette.accent),
                                  (("disabled", "focus"), palette.line)):
                with self.subTest(style=style, state=state):
                    lookup = self.ui.style.lookup
                    self.assertEqual(lookup(style, "bordercolor", state), border)
                    self.assertEqual(int(lookup(style, "borderwidth", state)), 1)
                    fill = lookup(style, "background", state)
                    for option in ("lightcolor", "darkcolor", "focuscolor"):
                        self.assertEqual(lookup(style, option, state), fill)

    def click_header(self, column):
        tree = self.ui.tree
        x, y, width, _ = tree.bbox(tree.get_children()[0], column)
        point = {"x": x + width // 2, "y": y // 2}
        tree.event_generate("<ButtonPress-1>", **point)
        tree.event_generate("<ButtonRelease-1>", **point)
        self.root.update()

    def test_header_sort_defaults_to_category_and_survives_filtering(self):
        catalog = self.ui.catalog
        recipe = json.loads((catalog / "alpha.json").read_text())
        for app_id, name, category in (("alpha", "Zulu Tool", "Utilities"),
                                       ("beta", "bravo Tool", "audio"),
                                       ("gamma", "alpha Viewer", "Media")):
            (catalog / f"{app_id}.json").write_text(json.dumps({
                **recipe, "id": app_id, "name": name, "category": category,
            }))
        self.ui.reload()
        self.root.deiconify()
        self.root.update()
        tree = self.ui.tree
        alpha, beta, gamma = (str(catalog / f"{app_id}.json") for app_id in ("alpha", "beta", "gamma"))
        self.assertEqual(tree.get_children(), (beta, gamma, alpha))
        tree.selection_set(beta)
        self.click_header("Category")
        self.assertEqual(tree.get_children(), (alpha, gamma, beta))
        self.click_header("Category")
        self.assertEqual(tree.get_children(), (beta, gamma, alpha))
        self.click_header("Name")
        self.assertEqual(tree.get_children(), (gamma, beta, alpha))
        self.click_header("Name")
        self.assertEqual(tree.get_children(), (alpha, beta, gamma))
        self.ui.search.set("tool")
        self.assertEqual(tree.get_children(), (alpha, beta))
        self.ui.search.set("")
        self.assertEqual(tree.get_children(), (alpha, beta, gamma))
        self.assertEqual(tree.selection(), (beta,))

    def test_status_header_sorts_and_reorders_after_an_operation(self):
        self.root.deiconify()
        self.root.update()
        tree = self.ui.tree
        alpha, beta = self.keys
        self.ui.start("verify", key=alpha)
        self.wait_until_idle()
        self.assertEqual(tree.set(alpha, "Status"), "Repair needed")
        self.click_header("Status")
        self.assertEqual(tree.get_children(), (beta, alpha))
        self.click_header("Status")
        self.assertEqual(tree.get_children(), (alpha, beta))
        with patch("shelf_window.execute", return_value=Result("Latest")):
            self.ui.start("check", key=alpha)
            self.wait_until_idle()
        self.assertEqual(tree.set(alpha, "Status"), "Latest")
        self.assertEqual(tree.get_children(), (beta, alpha))

    def test_header_menu_hides_and_restores_a_column_even_in_an_empty_view(self):
        self.root.deiconify()
        self.root.update()
        tree, menu = self.ui.tree, self.ui.column_menu
        columns = tree["columns"]
        tree.selection_set(self.keys[1])
        tree.event_generate("<ButtonPress-3>", x=15, y=5, time=1000)
        tree.event_generate("<ButtonRelease-3>", x=15, y=5, time=1030)
        self.root.update()
        self.assertTrue(menu.winfo_viewable())
        self.assertFalse(self.ui.app_menu.winfo_viewable())
        self.assertEqual(tuple(menu.buttons), ("Item icon", *columns))
        self.assertTrue(menu.buttons["Item icon"].instate(["!selected", "!disabled"]))
        for name in columns:
            self.assertTrue(menu.buttons[name].instate(["selected"]))
        category = menu.buttons["Category"]
        category.invoke()
        self.root.update()
        self.assertEqual(tree["displaycolumns"], tuple(col for col in columns if col != "Category"))
        self.assertEqual(tree.selection(), (self.keys[1],))
        self.assertEqual(tree.set(self.keys[1], "Category"), "Uncategorized")
        self.ui.search.set("uncategorized")
        self.assertEqual(tree.get_children(), self.keys)
        self.ui.search.set("no matches")
        self.assertEqual(tree.get_children(), ())
        menu.dismiss()
        self.root.update()
        tree.event_generate("<ButtonPress-3>", x=15, y=5, time=3000)
        tree.event_generate("<ButtonRelease-3>", x=15, y=5, time=3030)
        self.root.update()
        self.assertTrue(menu.winfo_viewable())
        self.assertTrue(category.instate(["!selected"]))
        category.invoke()
        self.assertEqual(tree["displaycolumns"], columns)

    def test_app_icons_toggle_preserves_filtered_rows_and_survives_restart(self):
        self.root.geometry("760x430")
        self.root.deiconify()
        tree = self.ui.tree
        self.ui.column_menu.buttons["Source"].invoke()
        columns = tree["displaycolumns"]
        self.ui.search.set("beta")
        key = self.keys[1]
        self.ui.options_button.invoke()
        button = self.ui.options.app_icons_button
        icon_column = self.ui.column_menu.buttons["Item icon"]
        self.root.update()
        self.assertTrue(button.instate(["!selected"]))
        self.assertEqual(tree.bbox(key, "Name")[0], 0)

        icon_column.invoke()
        self.root.update()
        self.assertTrue(button.instate(["selected"]))
        self.assertTrue(icon_column.instate(["selected"]))
        self.assertTrue(self.ui.show_app_icons.get())
        self.assertEqual(tree.get_children(), (key,))
        self.assertEqual(tree.selection(), (key,))
        self.assertEqual(tree["displaycolumns"], columns)
        self.assertEqual(tree.heading("#0", "text"), "")
        self.assertEqual(tree.tk.splitlist(tree.heading("#0", "image")), (str(self.ui.app_column_icon),))
        for name in tree["columns"]:
            self.assertEqual(str(tree.heading(name, "anchor")), "center")
            self.assertEqual(str(tree.column(name, "anchor")), "center")
        x, _, width, _ = tree.bbox(key, "#0")
        self.assertEqual(x, 0)
        self.assertGreater(width, 0)
        self.assertEqual(tree.bbox(key, "Name")[0], width)
        self.assertEqual(tree.xview(), (0.0, 1.0))
        x, _, width, _ = tree.bbox(key, columns[-1])
        self.assertEqual(x + width, tree.winfo_width())

        self.ui.search.set("")
        self.assertEqual(tree.get_children(), self.keys)
        self.assertEqual(tree.selection(), (key,))
        for row in self.keys:
            image, = tree.item(row, "image")
            self.assertIn(image, self.root.image_names())
            self.assertGreater(self.root.tk.call("image", "width", image), 0)

        self.ui.options.destroy()
        second = tk.Toplevel(self.root)
        self.addCleanup(second.destroy)
        reopened = ShelfWindow(second, self.ui.catalog, self.dest, self.ui.targets, appearance_file=self.appearance)
        reopened.options_button.invoke()
        restored_button = reopened.options.app_icons_button
        restored_column = reopened.column_menu.buttons["Item icon"]
        self.assertTrue(restored_button.instate(["selected"]))
        self.assertTrue(restored_column.instate(["selected"]))
        self.assertIn(reopened.tree.item(key, "image")[0], self.root.image_names())
        restored_button.invoke()
        self.root.update()
        self.assertFalse(reopened.show_app_icons.get())
        self.assertTrue(restored_column.instate(["!selected", "!disabled"]))
        self.assertEqual(reopened.tree.bbox(key, "Name")[0], 0)
        self.assertEqual(reopened.tree["displaycolumns"], columns)

    def test_app_icon_column_uses_the_heading_icon_for_missing_icons(self):
        local_icon = tk.PhotoImage(master=self.root, width=16, height=16)
        self.ui.show_app_icons.set(True)
        with patch("shelf_window.load_app_icon", side_effect=(None, local_icon)):
            self.ui.change_app_icons()
        tree = self.ui.tree
        for mode, language in (("Light", "en"), ("Dark", "fr")):
            with self.subTest(mode=mode, language=language):
                self.ui.mode.set(mode)
                self.ui.refresh_theme()
                self.ui.change_language(language)
                self.root.update_idletasks()
                self.assertEqual(tree.heading("#0", "text"), "")
                image, = tree.tk.splitlist(tree.heading("#0", "image"))
                self.assertIn(image, self.root.image_names())
                self.assertEqual(tree.item(self.keys[0], "image"), (image,))
                self.assertEqual(tree.item(self.keys[1], "image"), (str(local_icon),))
                self.assertEqual(tree.heading("Name", "text"), tr("Name"))
                self.assertEqual(tree.set(self.keys[0], "Name"), "alpha")
                self.assertEqual(tree.item(self.keys[0], "text"), "")

    def test_iso_recipes_show_disc_icons_before_download(self):
        for key, source in zip(self.keys, (
                {"page": "https://publisher.example", "version_pattern": "(1)",
                 "download": "https://publisher.example/install-{version}.ISO"},
                {"github": "owner/project", "asset_pattern": "live-*.iso"})):
            path = Path(key)
            path.write_text(json.dumps({"id": path.stem, **source}))
        self.ui.reload()
        self.ui.options_button.invoke()
        self.ui.options.app_icons_button.invoke()

        for key in self.keys:
            icon = self.ui.app_images[key]
            self.assertIsNotNone(icon)
            self.assertEqual(self.ui.tree.item(key, "image"), (str(icon),))
            self.assertEqual((icon.width(), icon.height()), (16, 16))
            self.assertTrue(icon.transparency_get(8, 8))
            self.assertFalse(icon.transparency_get(3, 8))

    def test_icon_column_supports_row_context_menu_and_double_click(self):
        self.root.deiconify()
        self.ui.options_button.invoke()
        self.ui.options.app_icons_button.invoke()
        self.ui.options.destroy()
        self.root.update()
        tree = self.ui.tree
        key = self.keys[1]
        left, top, width, height = tree.bbox(key, "#0")
        x, y = left + width // 2, top + height // 2
        self.assertEqual(tree.identify_region(x, y), "tree")
        with patch.object(self.ui.app_menu, "popup") as popup:
            tree.event_generate("<Button-3>", x=x, y=y)
            popup.assert_called_once()
        self.assertEqual(self.ui.app_menu_key, key)
        self.assertEqual(tree.selection(), (key,))
        tree.selection_set(self.keys[0])
        with patch.object(self.ui, "open_editor") as edit:
            for timestamp in (1000, 1070):
                tree.event_generate("<ButtonPress-1>", x=x, y=y, time=timestamp)
                tree.event_generate("<ButtonRelease-1>", x=x, y=y, time=timestamp + 20)
            edit.assert_called_once_with(Path(key))

    def assert_columns_fill_table(self, tree):
        self.root.update()
        self.assertEqual(tree.xview(), (0.0, 1.0))
        self.assertEqual(right_edge(tree), right_edge(tree.master))
        columns = tree["displaycolumns"]
        key = tree.get_children()[0]
        self.assertEqual(tree.bbox(key, columns[0])[0], 0)
        x, _, width, _ = tree.bbox(key, columns[-1])
        self.assertEqual(x + width, tree.winfo_width())
        bars = (child for child in tree.master.winfo_children() if isinstance(child, ttk.Scrollbar))
        self.assertFalse(any(bar.winfo_ismapped() for bar in bars))

    def test_visible_columns_fill_table_after_hiding_and_resizing(self):
        self.root.geometry("1120x680")
        self.root.deiconify()
        self.root.update()
        tree = self.ui.tree
        buttons = self.ui.column_menu.buttons
        buttons["Source"].invoke()
        buttons["Status"].invoke()
        self.assert_columns_fill_table(tree)
        for name in ("Category", "Local version", "Available version"):
            buttons[name].invoke()
        self.assert_columns_fill_table(tree)
        self.root.geometry("960x540")
        self.assert_columns_fill_table(tree)
        buttons["Category"].invoke()
        self.assert_columns_fill_table(tree)

    def test_all_columns_fit_in_a_narrow_window(self):
        self.root.geometry("760x430")
        self.root.deiconify()
        tree = self.ui.tree
        self.assert_columns_fill_table(tree)
        self.ui.change_language("fr")
        buttons = self.ui.column_menu.buttons
        buttons["Source"].invoke()
        buttons["Source"].invoke()
        self.assert_columns_fill_table(tree)
        # A vertical scrollbar takes width away from these same columns.
        for _ in range(40):
            tree.insert("", "end")
        bar = self.assert_scrollbar_visible(tree, True)
        self.assertEqual(right_edge(tree), bar.winfo_rootx())
        self.assertEqual(tree.xview(), (0.0, 1.0))

    def test_last_visible_column_can_restore_others_after_restart(self):
        tree = self.ui.tree
        columns = tree["columns"]
        for name in columns[:-1]:
            self.ui.column_menu.buttons[name].invoke()
        self.assertEqual(tree["displaycolumns"], ("Source",))

        # Older releases persisted the Name column as "App name".
        prefs = self.ui.columns_file
        prefs.write_text(prefs.read_text(encoding="utf-8").replace("Name\n", "App name\n"), encoding="utf-8")
        second = tk.Toplevel(self.root)
        self.addCleanup(second.destroy)
        reopened = ShelfWindow(second, self.ui.catalog, self.dest, self.ui.targets,
                               appearance_file=self.appearance)
        second.geometry("1120x680")
        self.assertEqual(reopened.tree["displaycolumns"], ("Source",))
        self.assert_columns_fill_table(reopened.tree)
        buttons = reopened.column_menu.buttons
        self.assertTrue(buttons["Source"].instate(["selected", "disabled"]))
        buttons["Item icon"].invoke()
        self.assertTrue(buttons["Item icon"].instate(["selected", "!disabled"]))
        self.assertTrue(buttons["Source"].instate(["selected", "disabled"]))
        buttons["Source"].invoke()
        self.assertEqual(reopened.tree["displaycolumns"], ("Source",))
        buttons["Item icon"].invoke()
        buttons["Name"].invoke()
        self.assertEqual(reopened.tree["displaycolumns"], ("Name", "Source"))
        self.assertTrue(buttons["Source"].instate(["!disabled"]))
        self.assert_columns_fill_table(reopened.tree)
        for name in columns[1:-1]:
            buttons[name].invoke()
        self.assert_columns_fill_table(reopened.tree)

    def test_page_title_already_fits_when_a_view_is_clicked(self):
        self.root.deiconify()
        title = self.ui.title_label
        for language, geometry in (("en", "1120x680"), ("es", "760x430")):
            self.root.geometry(geometry)
            self.ui.change_language(language)
            self.ui.nav_buttons["all"].invoke()
            self.root.update()
            for view in ("attention", "progress", "updates", "all"):
                with self.subTest(language=language, view=view):
                    width, height = title.winfo_width(), title.winfo_height()
                    button = self.ui.nav_buttons[view]
                    button.invoke()
                    self.assertEqual(title.cget("text"), button.cget("text"))
                    self.assertGreaterEqual(width, title.winfo_reqwidth())
                    self.assertGreaterEqual(height, title.winfo_reqheight())
                    self.root.update_idletasks()
                    self.assertEqual((title.winfo_width(), title.winfo_height()), (width, height))

    def test_sidebar_toggle_sits_beside_options_and_stacks_above_when_collapsed(self):
        self.root.deiconify()
        options = self.ui.options_button
        toggle = self.ui.sidebar_toggle
        all_apps = self.ui.nav_buttons["all"]
        for mode, geometry in (("Light", "760x430"), ("Dark", "1120x680")):
            with self.subTest(mode=mode, geometry=geometry):
                self.ui.mode.set(mode)
                self.ui.refresh_theme()
                self.root.geometry(geometry)
                self.root.update()
                self.assertEqual(options.winfo_rootx(), all_apps.winfo_rootx())
                self.assertLessEqual(right_edge(options), toggle.winfo_rootx())
                self.assertEqual(right_edge(toggle), right_edge(all_apps))
                self.assertEqual(toggle.winfo_rooty(), options.winfo_rooty())
                self.assertEqual(options.winfo_height(), all_apps.winfo_height())
                self.assertIn(options.cget("image")[0], self.root.image_names())
                expanded = (options.winfo_geometry(), toggle.winfo_geometry())

                toggle.invoke()
                self.root.update()
                self.assertEqual(toggle.winfo_rootx(), options.winfo_rootx())
                self.assertEqual(right_edge(toggle), right_edge(options))
                gap = options.winfo_rooty() - (toggle.winfo_rooty() + toggle.winfo_height())
                self.assertGreaterEqual(gap, 0)
                self.assertLess(gap, toggle.winfo_height())

                toggle.invoke()
                self.root.update()
                self.assertEqual((options.winfo_geometry(), toggle.winfo_geometry()), expanded)

    def test_sidebar_categories_filter_search_and_follow_catalog_changes(self):
        ui = self.ui
        first, second = self.keys
        path = Path(first)
        recipe = json.loads(path.read_text())
        recipe["category"] = "Hardware"
        path.write_text(json.dumps(recipe))
        ui.reload()
        self.root.deiconify()
        self.root.update()
        listing = ui.category_list
        hardware, unassigned = listing.get_children()
        self.assertEqual(listing.set(hardware, "name"), "Hardware")
        self.assertEqual(listing.set(hardware, "count"), "1")
        self.assertEqual(listing.set(unassigned, "name"), "Uncategorized")
        x, y, width, height = listing.bbox(hardware, "name")
        listing.event_generate("<ButtonPress-1>", x=x + width // 2, y=y + height // 2)
        listing.event_generate("<ButtonRelease-1>", x=x + width // 2, y=y + height // 2)
        self.root.update()
        self.assertEqual(ui.tree.get_children(), (first,))
        self.assertEqual(ui.page_title.get(), "Hardware")
        ui.search.set("beta")
        self.assertFalse(ui.tree.get_children())
        self.assertEqual(ui.empty_title.get(), "No matching items")
        ui.sidebar_toggle.invoke()
        self.root.update()
        self.assertTrue(ui.categories_button.winfo_viewable())
        ui.categories_button.invoke()
        self.root.update()
        self.assertEqual(listing.selection(), (hardware,))
        listing.selection_set(unassigned)
        self.root.update()
        self.assertEqual(ui.tree.get_children(), (second,))
        ui.change_language("uk")
        self.assertEqual(ui.page_title.get(), tr("Uncategorized"))
        self.assertEqual(listing.selection(), (unassigned,))
        ui.change_language("en")
        ui.search.set("")
        editor = self.edit_selected()
        editor.fields["category"].set("Hardware")
        editor.save()
        self.root.update()
        self.assertEqual(ui.view.get(), "all")
        self.assertEqual(listing.get_children(), (hardware,))
        self.assertEqual(listing.set(hardware, "count"), "2")
        self.assertEqual(set(ui.tree.get_children()), set(self.keys))

    def test_sidebar_toggle_reclaims_space_and_restores_labels_and_counts(self):
        self.root.deiconify()
        self.ui.search_entry.insert(0, "alpha")
        nav = self.ui.nav_buttons
        nav["updates"].invoke()
        self.root.update()
        sidebar, tree = nav["all"].master, self.ui.tree
        sidebar_width, tree_width = sidebar.winfo_width(), tree.winfo_width()
        buttons = [*nav.values(), self.ui.options_button]
        placements = {badge: badge.place_info() for badge in self.ui.nav_counts.values()}

        self.ui.sidebar_toggle.invoke()
        self.root.update()
        self.assertLess(sidebar.winfo_width(), sidebar_width)
        self.assertGreater(tree.winfo_width(), tree_width)
        for button in buttons:
            self.assertEqual(str(button.cget("compound")), "image")
            self.assertIn(button.cget("image")[0], self.root.image_names())
        for badge in placements:
            self.assertFalse(badge.winfo_viewable())
        self.assertEqual(self.ui.search.get(), "alpha")
        self.assertTrue(nav["updates"].instate(["selected"]))
        nav["attention"].invoke()
        self.assertEqual(self.ui.view.get(), "attention")

        self.ui.sidebar_toggle.invoke()
        self.root.update()
        self.assertEqual(sidebar.winfo_width(), sidebar_width)
        self.assertEqual(tree.winfo_width(), tree_width)
        for button in buttons:
            self.assertEqual(str(button.cget("compound")), "left")
        for badge, placement in placements.items():
            self.assertTrue(badge.winfo_viewable())
            self.assertEqual(badge.place_info(), placement)
        self.assertEqual(self.ui.search.get(), "alpha")
        self.assertTrue(nav["attention"].instate(["selected"]))

    def test_keyboard_app_menu_respects_selection_and_action_availability(self):
        valid, invalid = self.keys
        Path(invalid).write_text("{")
        self.ui.reload()
        self.root.deiconify()
        tree, menu = self.ui.tree, self.ui.app_menu
        tree.focus_force()
        self.root.update()
        with patch.object(menu, "popup") as popup:
            tree.selection_remove(*tree.selection())
            tree.event_generate("<Shift-F10>")
            popup.assert_not_called()

            tree.selection_set(invalid)
            tree.event_generate("<Shift-F10>")
            popup.assert_called_once()
            for label in ("Check for updates", "Get updates", "Verify files"):
                self.assertTrue(menu.buttons[label].instate(["disabled"]))
            self.assertTrue(menu.buttons["Edit item"].instate(["!disabled"]))
            with patch.object(self.ui, "open_editor") as edit:
                menu.buttons["Edit item"].invoke()
                edit.assert_called_once_with(Path(invalid))

            tree.selection_set(valid)
            self.ui.busy = True
            tree.event_generate("<Shift-F10>")
            self.assertEqual(popup.call_count, 2)
            for label in ("Check for updates", "Get updates", "Verify files", "Edit item"):
                self.assertTrue(menu.buttons[label].instate(["disabled"]))

    def test_keyboard_app_menu_navigation_and_dismissal(self):
        self.root.deiconify()
        tree, menu = self.ui.tree, self.ui.app_menu
        tree.focus_force()
        self.root.update()
        tree.event_generate("<Shift-F10>")
        self.root.update()
        first, second = menu.buttons["Check for updates"], menu.buttons["Get updates"]
        self.assertTrue(menu.winfo_viewable())
        self.assertIs(self.root.focus_get(), first)
        first.event_generate("<Down>")
        self.root.update()
        self.assertIs(self.root.focus_get(), second)
        second.event_generate("<Escape>")
        self.root.update()
        self.assertFalse(menu.winfo_viewable())
        self.assertIsNone(self.root.grab_current())
        self.assertIs(self.root.focus_get(), tree)

    def test_theme_switch_persistence_and_system_changes(self):
        self.ui.tree.selection_set(self.keys[1])
        with patch("shelf_window.system_mode", return_value="Dark") as windows:
            self.ui.options_button.invoke()
            options = self.ui.options
            options.mode_buttons["System"].invoke()
            self.assertIs(self.ui.palette, DARK)
            options.palette_buttons["OLED mint"].invoke()
            mint = PALETTES["OLED mint"]
            self.assertIs(self.ui.palette, mint["Dark"])
            windows.return_value = "Light"
            deadline = monotonic() + 3
            while self.ui.palette is mint["Dark"] and monotonic() < deadline:
                self.root.update()
                sleep(.01)
            self.assertIs(self.ui.palette, mint["Light"])
            self.assertEqual(self.ui.palette_name.get(), "OLED mint")
            self.assertEqual(read_appearance(self.appearance), ("System", "OLED mint"))
            options.mode_buttons["Dark"].invoke()
            self.assertIs(self.ui.palette, mint["Dark"])
            self.assertIsNone(self.ui.theme_timer)
            self.assertEqual(self.ui.tree.selection(), (self.keys[1],))
            options.destroy()
            second = tk.Toplevel(self.root)
            reopened = ShelfWindow(second, self.ui.catalog, self.dest, self.ui.targets,
                                   appearance_file=self.appearance)
            self.assertEqual(reopened.mode.get(), "Dark")
            self.assertEqual(reopened.palette_name.get(), "OLED mint")
            reopened.edit_button.invoke()
            self.assertEqual(reopened.editor.detail.cget("background"), mint["Dark"].surface)
            reopened.options_button.invoke()
            self.assertTrue(reopened.options.mode_buttons["Dark"].instate(["selected"]))
            reopened.options.mode_buttons["Light"].invoke()
            self.assertEqual(reopened.editor.detail.cget("background"), mint["Light"].surface)
            second.destroy()

    def test_options_previews_applies_and_saves_each_palette_in_both_modes(self):
        for widget in descendants(self.root):
            if isinstance(widget, ttk.Radiobutton):
                self.assertNotIn(str(widget.cget("value")), {"System", "Light", "Dark"})
        self.ui.options_button.invoke()
        options = self.ui.options
        names = {"OLED mint", "Pure carbon", "Soft graphite", "Blue ink", "Warm amber", "Slate mint",
                 "Orchid", "Rosewood"}
        headings = {w.cget("text") for w in descendants(options) if isinstance(w, ttk.Label)}
        self.assertIn("Palette", headings)
        self.assertEqual(set(options.palette_buttons), names)
        self.assertEqual(set(options.mode_buttons), {"System", "Light", "Dark"})
        button_style = self.ui.app_menu.buttons["Edit item"].cget("style")
        for mode in ("Dark", "Light"):
            options.mode_buttons[mode].invoke()
            for name in sorted(names):
                with self.subTest(mode=mode, name=name):
                    palette = PALETTES[name][mode]
                    swatch = options.swatches[name]
                    accent = tuple(channel // 257 for channel in self.root.winfo_rgb(palette.accent))
                    self.assertEqual(swatch.get(5, 5), accent)
                    options.palette_buttons[name].invoke()
                    self.assertIs(self.ui.palette, palette)
                    self.assertEqual(self.ui.style.lookup("Primary.TButton", "background"), palette.accent)
                    self.assertEqual(self.ui.style.lookup("Treeview", "background", ["selected", "focus"]),
                                     palette.selection)
                    self.assertEqual(self.ui.style.lookup(button_style, "background", ["focus"]), palette.selection)
                    for menu in (self.ui.app_menu, self.ui.column_menu, self.ui.more_menu):
                        self.assertEqual(self.ui.style.lookup(menu.border.cget("style"), "background"), palette.line)
                    self.assertEqual(read_appearance(self.appearance), (mode, name))
        options.palette_buttons["Pure carbon"].invoke()
        options.destroy()
        self.ui.options_button.invoke()
        self.assertTrue(self.ui.options.palette_buttons["Pure carbon"].instate(["selected"]))
        self.ui.options.mode_buttons["Dark"].invoke()
        self.assertIs(self.ui.palette, PALETTES["Pure carbon"]["Dark"])

    def test_legacy_mode_preferences_use_the_original_default_palette(self):
        self.assertEqual(read_appearance(self.appearance), ("System", "Slate mint"))
        self.appearance.parent.mkdir(parents=True, exist_ok=True)
        for mode in ("Light", "Dark", "System"):
            with self.subTest(mode=mode):
                self.appearance.write_text(mode + "\n", encoding="utf-8")
                self.assertEqual(read_appearance(self.appearance), (mode, "Slate mint"))
                self.appearance.write_text(f"{mode}\nCurrent mint\n", encoding="utf-8")
                self.assertEqual(read_appearance(self.appearance), (mode, "Slate mint"))
        self.assertIs(PALETTES["Slate mint"]["Light"], LIGHT)
        self.assertIs(PALETTES["Slate mint"]["Dark"], DARK)
        self.appearance.write_text("Dark\nRetired palette", encoding="utf-8")
        self.assertEqual(read_appearance(self.appearance), ("Dark", "Slate mint"))

    @unittest.skipUnless(sys.platform == "win32", "Window opacity is staged on Windows")
    def test_editor_is_populated_and_positioned_when_revealed(self):
        path = Path(self.keys[0])
        recipe = json.loads(path.read_text())
        recipe.update(name="Alpha utility", category="Tools")
        path.write_text(json.dumps(recipe))
        self.root.deiconify()
        self.root.update()
        revealed = []
        fields = []
        attributes = tk.Toplevel.attributes

        def geometry(window):
            return window.geometry(), [w.winfo_geometry() for w in fields]

        def observe(window, *args):
            if args == ("-alpha", 1):
                self.assertEqual(float(attributes(window, "-alpha")), 0)
                catalog = window.nametowidget(window.tabs.select())
                fields.extend(w for w in catalog.winfo_children() if isinstance(w, ttk.Entry) and w.winfo_viewable())
                labels = {w.cget("text") for w in catalog.winfo_children()
                          if isinstance(w, ttk.Label) and w.winfo_viewable()}
                self.assertTrue({"Item ID", "Name", "Category"} <= labels)
                self.assertEqual([w.get() for w in fields], ["alpha", "Alpha utility", "Tools"])
                self.assertEqual(window.detail_title.cget("text"), "Alpha utility")
                self.assertIn("Item ID: alpha", window.detail.get("1.0", "end"))
                revealed.append(geometry(window))
            return attributes(window, *args)

        with patch.object(tk.Toplevel, "attributes", observe):
            self.ui.open_editor(path)
            self.root.update()
        editor = self.ui.editor
        self.assertEqual(revealed, [geometry(editor)])
        self.assertIs(editor.focus_lastfor(), fields[0])
        editor.cancel_button.invoke()
        self.assertIsNone(self.ui.editor)

    def test_editor_footer_buttons_share_the_larger_size(self):
        self.root.deiconify()
        self.root.update()
        self.ui.add_button.invoke()
        save = self.ui.editor.save_button
        cancel = next(w for w in save.master.winfo_children() if w.cget("text") == tr("Cancel"))
        for label in (tr("Save"), "Save the application settings"):
            with self.subTest(label=label):
                save.configure(text=label)
                self.root.update()
                self.assertGreaterEqual(save.winfo_width(), max(save.winfo_reqwidth(), cancel.winfo_reqwidth()))
                self.assertEqual(cancel.winfo_width(), save.winfo_width())
                self.assertEqual(cancel.winfo_height(), save.winfo_height())

    def test_details_and_actions_appear_only_in_the_editor(self):
        labels = {"Check for updates", "Get updates", "Verify files"}
        for widget in descendants(self.root):
            if widget.winfo_toplevel() != self.root:
                continue
            if isinstance(widget, ttk.Button):
                if widget.cget("text") == "Check for updates":
                    self.assertIs(widget, self.ui.all_buttons["check"])
                else:
                    self.assertNotIn(widget.cget("text"), labels)
            elif isinstance(widget, tk.Text):
                self.assertNotIn(str(self.dest / "alpha"), widget.get("1.0", "end"))
        editor = self.edit_selected()
        tabs = next(w for w in descendants(editor) if isinstance(w, ttk.Notebook))
        self.assertEqual(tabs.tab(tabs.select(), "text"), "Catalog")
        self.assertEqual([tabs.tab(tab, "text") for tab in tabs.tabs()],
                         ["Catalog", "Download options", "Source", "Item info"])
        self.assertEqual({button.cget("text") for button in editor.buttons.values()}, labels)
        detail = editor.detail.get("1.0", "end")
        self.assertIn("https://publisher.example", detail)
        self.assertIn(str(self.dest / "alpha"), detail)
        self.assertIn("Not checked", detail)
        self.assertEqual(str(editor.detail.cget("state")), "disabled")

    def test_editor_save_and_cancel_share_size_for_long_labels(self):
        self.root.deiconify()
        editor = self.edit_selected()
        save = editor.save_button
        cancel = next(w for w in descendants(editor) if isinstance(w, ttk.Button) and w.cget("text") == tr("Cancel"))
        for label in (tr("Save"), "Save changes to this application"):
            with self.subTest(label=label):
                save.configure(text=label)
                self.root.update()
                self.assertTrue(save.winfo_viewable())
                self.assertEqual(cancel.winfo_width(), save.winfo_width())
                self.assertEqual(cancel.winfo_height(), save.winfo_height())
                self.assertGreaterEqual(save.winfo_width(), max(save.winfo_reqwidth(), cancel.winfo_reqwidth()))

    def assert_scrollbar_visible(self, widget, visible, orient="vertical"):
        self.root.update()
        bar = next(w for w in widget.master.winfo_children()
                   if isinstance(w, ttk.Scrollbar) and str(w.cget("orient")) == orient)
        self.assertTrue(widget.winfo_viewable())
        self.assertEqual(bool(bar.winfo_ismapped()), visible)
        if visible:
            self.assertEqual(bar.get(), widget.yview() if orient == "vertical" else widget.xview())
        return bar

    def test_scrollbar_alignment_and_visibility_on_resize(self):
        self.root.deiconify()
        tree = self.ui.tree
        self.assert_scrollbar_visible(tree, False)
        extra = [tree.insert("", "end") for _ in range(80)]
        table_bar = self.assert_scrollbar_visible(tree, True)
        right_margin = right_edge(self.root) - right_edge(table_bar)
        tree.delete(*extra)
        self.assert_scrollbar_visible(tree, False)

        editor = self.edit_selected("Item info")
        editor.set_app_info("alpha", "\n".join(["line"] * 12))
        editor.geometry("720x300")
        detail_bar = self.assert_scrollbar_visible(editor.detail, True)
        self.assertEqual(right_edge(editor) - right_edge(detail_bar), right_margin)
        editor.detail.yview_moveto(1)
        self.assert_scrollbar_visible(editor.detail, True)
        editor.geometry("840x700")
        self.assert_scrollbar_visible(editor.detail, False)

    def test_scrollbars_follow_edits_to_multiline_fields(self):
        self.root.deiconify()
        for tab_name in ("Download options", "Recipe JSON"):
            with self.subTest(tab=tab_name):
                if tab_name == "Recipe JSON":
                    Path(self.keys[0]).write_text("{")
                    self.ui.reload()
                editor = self.edit_selected()
                tabs = next(w for w in descendants(editor) if isinstance(w, ttk.Notebook))
                tabs.select(next(tab for tab in tabs.tabs() if tabs.tab(tab, "text") == tab_name))
                field = editor.text_area
                self.assert_scrollbar_visible(field, False)
                field.insert("end", "W" * 300 + "\nfilename" * 80)
                self.assert_scrollbar_visible(field, True)
                if tab_name == "Download options":
                    self.assert_scrollbar_visible(field, True, "horizontal")
                field.delete("1.0", "end")
                self.assert_scrollbar_visible(field, False)
                if tab_name == "Download options":
                    self.assert_scrollbar_visible(field, False, "horizontal")
                editor.destroy()

    def test_selection_fetch_and_offline_batch_verification(self):
        editor = self.edit_selected()
        # Pending selection changes behind the dialog must not redirect its actions.
        self.ui.tree.selection_set(self.keys[1])
        self.root.update()
        with patch("fetch_sources.read_page", return_value="1") as page:
            self.click(editor.buttons["check"])
            page.assert_called_once()
            self.assertFalse(self.dest.exists())
            self.assertEqual([self.ui.results[k].status for k in self.keys],
                             ["Update available", "Not checked"])
            self.assertIn("Update available", editor.detail.get("1.0", "end"))
            reply = io.BytesIO(b"fixture")
            reply.headers = {"Content-Length": "7"}
            with patch("fetch_sources.open_url", return_value=reply):
                self.click(editor.buttons["fetch"])
        live = self.dest / "alpha" / "current" / "alpha.bin"
        self.assertEqual(live.read_bytes(), b"fixture")
        live.write_bytes(b"damaged")
        with patch("fetch_sources.open_url") as network:
            self.click(editor.buttons["verify"])
            self.assertIn("SHA-256 mismatch", editor.detail.get("1.0", "end"))
            self.assertEqual(self.ui.results[self.keys[1]].status, "Not checked")
            editor.destroy()
            self.ui.more_menu.buttons["Verify all items"].invoke()
            self.wait_until_idle()
            network.assert_not_called()
        self.assertEqual(self.ui.results[self.keys[1]].status, "Repair needed")
        self.assertIn("2 of 2 processed", self.ui.status.get())

    def test_update_all_counts_only_files_needing_download(self):
        def cached_file(url):
            response = io.BytesIO(b"cached")
            response.headers = {"Content-Length": "6"}
            return response

        with patch("fetch_sources.read_page", return_value="1"):
            with patch("fetch_sources.open_url", side_effect=cached_file):
                self.click(self.ui.all_buttons["fetch"])
            self.assertEqual([self.ui.results[key].status for key in self.keys], ["Updated", "Updated"])
            (self.dest / "beta" / "current" / "beta.bin").unlink()

            # The two utilities already have valid archives. Insert four new ISO recipes around them.
            names = ("amber", "delta", "epsilon", "gamma")
            for name in names:
                (self.ui.catalog / f"{name}.json").write_text(json.dumps({
                    "id": name, "page": "https://publisher.example", "version_pattern": "(1)",
                    "download": f"https://publisher.example/{name}.iso",
                }))
            self.ui.reload()
            responses = [GatedDownload() for _ in names]
            with patch("fetch_sources.open_url", side_effect=responses) as download:
                self.ui.all_buttons["fetch"].invoke()
                try:
                    for index, (name, response) in enumerate(zip(names, responses), start=1):
                        self.wait_for(lambda: response.waiting.is_set() and self.ui.downloading
                                      and self.ui.working_key == str(self.ui.catalog / f"{name}.json"))
                        self.assertIn(f"{index} of 4 downloads", self.ui.status.get())
                        response.resume.set()
                finally:
                    for response in responses:
                        response.resume.set()
                    self.wait_until_idle()
            self.assertEqual([call.args[0] for call in download.call_args_list],
                             [f"https://publisher.example/{name}.iso" for name in names])
            self.assertEqual([self.ui.results[key].status for key in self.keys], ["Latest", "Cached"])

    def test_update_all_excludes_matching_local_windows_iso_from_downloads(self):
        image = self.ui.catalog.parent / "local.iso"
        image.write_bytes(b"existing Windows image")
        checksum = hashlib.sha256(image.read_bytes()).hexdigest()
        release_page = f"version=25H2 sha256={checksum}"
        windows_key, _ = self.keys
        Path(windows_key).write_text(json.dumps({
            "id": "test-image", "page": "https://www.microsoft.com/en-us/software-download/windows11",
            "version_pattern": r"version=(\S+)", "sha256_pattern": r"sha256=(\S+)",
            "download_resolver": "windows-11-english-x64", "filename": "image-{version}.iso",
        }))
        self.ui.reload()

        response = GatedDownload()
        with (
            patch("shelf_windows_iso.ketarin_windows_image", return_value=image),
            patch("shelf_windows_iso.windows_iso_url") as microsoft_download,
            patch("fetch_sources.read_page", return_value=release_page),
            patch("fetch_sources.open_url", return_value=response) as download,
        ):
            self.ui.all_buttons["fetch"].invoke()
            try:
                self.wait_for(lambda: response.waiting.is_set() and self.ui.downloading)
                self.assertEqual(self.ui.working_key, self.keys[1])
                self.assertTrue(self.ui.status.get().endswith("1 of 1 download"), self.ui.status.get())
            finally:
                response.resume.set()
                self.wait_until_idle()
            microsoft_download.assert_not_called()
            download.assert_called_once_with("https://publisher.example/beta.bin")
        installed = self.dest / "test-image" / "current" / "image-25H2.iso"
        self.assertEqual(installed.read_bytes(), image.read_bytes())

    def test_fetch_displays_percentage_and_cached_fetch_keeps_progress_hidden(self):
        self.root.deiconify()
        self.ui.change_language("uk")
        editor = self.edit_selected("Item info")
        self.root.update()
        gc.collect()
        progress_widgets = (self.ui.download_progress, editor.download_progress)
        row_bar, tree, key = self.ui.row_progress, self.ui.tree, self.keys[0]

        def bar_in_status_cell():
            cell = tree.bbox(key, "Status")
            if not cell or not row_bar.winfo_ismapped():
                return False
            x, y, width, height = cell
            left, top = row_bar.winfo_x(), row_bar.winfo_y()
            return (x <= left < left + row_bar.winfo_width() <= x + width
                    and y <= top < top + row_bar.winfo_height() <= y + height)

        response = GatedDownload()
        with (
            patch("fetch_sources.read_page", return_value="1"),
            patch("fetch_sources.open_url", return_value=response),
        ):
            editor.buttons["fetch"].invoke()
            try:
                self.wait_for(lambda: response.waiting.is_set() and all(
                    progress.winfo_manager() == "grid"
                    and float(progress.bar["value"]) == 50
                    for progress in progress_widgets
                ))
                self.assertEqual(self.ui.tree.set(self.keys[0], "Status"),
                                 tr("{action}…", action=tr("Downloading")))
                for progress in progress_widgets:
                    self.assertEqual(str(progress.bar["mode"]), "determinate")
                    self.assertEqual(float(progress.bar["maximum"]), 100)
                    self.assertEqual(progress.percent.cget("text"), "50%")

                self.wait_for(bar_in_status_cell)
                self.assertEqual(str(row_bar["mode"]), "determinate")
                self.assertAlmostEqual(float(row_bar["value"]) / float(row_bar["maximum"]), 0.5)
                self.ui.column_menu.buttons["Status"].invoke()
                self.wait_for(lambda: not row_bar.winfo_ismapped())
                self.ui.column_menu.buttons["Status"].invoke()
                self.wait_for(bar_in_status_cell)
                self.ui.search.set("beta")
                self.wait_for(lambda: not row_bar.winfo_ismapped())
                self.ui.search.set("")
                self.wait_for(bar_in_status_cell)

                self.root.geometry("760x430")
                self.root.update()
                self.wait_for(bar_in_status_cell)
                extra = [tree.insert("", "end") for _ in range(40)]
                tree.yview_moveto(1)
                self.wait_for(lambda: not row_bar.winfo_ismapped())
                tree.see(key)
                self.wait_for(bar_in_status_cell)
                tree.delete(*extra)
            finally:
                response.resume.set()
                self.wait_until_idle()

            target = self.dest / "alpha" / "current" / "alpha.bin"
            self.assertEqual(target.read_bytes(), b"download")
            self.assertEqual(self.ui.results[self.keys[0]].status, "Updated")
            self.assertTrue(all(not progress.winfo_manager() for progress in progress_widgets))
            self.assertFalse(row_bar.winfo_manager())

            # Repairing publication from the verified cache performs no transfer.
            target.unlink()
            shown = []
            for progress in (*progress_widgets, row_bar):
                progress.bind("<Map>", lambda event: shown.append(event.widget))
            with patch("fetch_sources.open_url") as network:
                self.click(editor.buttons["fetch"])
                self.root.update()
                network.assert_not_called()
            self.assertEqual(self.ui.results[self.keys[0]].status, "Cached")
            self.assertEqual(target.read_bytes(), b"download")
            self.assertEqual(shown, [])
            self.assertTrue(all(not progress.winfo_manager() for progress in progress_widgets))

    def test_unknown_download_length_animates_and_error_clears_progress(self):
        self.root.deiconify()
        editor = self.edit_selected("Item info")
        self.root.update()
        gc.collect()
        progress_widgets = (self.ui.download_progress, editor.download_progress)
        response = GatedDownload(content_length=False, fail=True)
        with (
            patch("fetch_sources.read_page", return_value="1"),
            patch("fetch_sources.open_url", return_value=response),
        ):
            editor.buttons["fetch"].invoke()
            try:
                self.wait_for(lambda: response.waiting.is_set() and all(
                    progress.winfo_manager() == "grid" for progress in progress_widgets
                ))
                self.assertTrue(self.ui.status.get().endswith("1 of 1 download"), self.ui.status.get())
                self.assertEqual(self.ui.tree.set(self.keys[0], "Status"),
                                 tr("{action}…", action=tr("Downloading")))
                for progress in progress_widgets:
                    self.assertEqual(str(progress.bar["mode"]), "indeterminate")
                    self.assertEqual(progress.percent.cget("text"), "")
                row_bar = self.ui.row_progress
                self.wait_for(row_bar.winfo_ismapped)
                self.assertEqual(str(row_bar["mode"]), "indeterminate")
                bars = [*(progress.bar for progress in progress_widgets), row_bar]
                values = [float(bar["value"]) for bar in bars]
                self.wait_for(lambda: all(
                    float(bar["value"]) != value for bar, value in zip(bars, values)
                ))
            finally:
                response.resume.set()
                self.wait_until_idle()

        result = self.ui.results[self.keys[0]]
        self.assertEqual(result.status, "Error")
        self.assertIn("Interrupted download", result.detail)
        self.assertEqual(self.ui.tree.set(self.keys[0], "Status"), tr("Error"))
        self.assertTrue(all(not progress.winfo_manager() for progress in progress_widgets))
        self.assertFalse(row_bar.winfo_manager())
        self.assertFalse((self.dest / "alpha" / "current" / "alpha.bin").exists())
        self.assertFalse(editor.buttons["fetch"].instate(["disabled"]))

    def test_editor_drafts_and_closing_during_a_check(self):
        editor = self.edit_selected()
        path = Path(self.keys[0])
        saved = path.read_bytes()
        with patch("fetch_sources.read_page", return_value="1") as page:
            editor.fields["id"].set("draft")
            editor.buttons["check"].invoke()
            self.assertFalse(self.ui.busy)
            page.assert_not_called()
            self.assertIn("save", editor.error.get().lower())
            self.assertEqual(editor.fields["id"].get(), "draft")

            editor.fields["id"].set("alpha")
            editor.buttons["check"].invoke()
            self.assertTrue(self.ui.busy)
            self.assertTrue(all(b.instate(["disabled"]) for b in editor.buttons.values()))
            editor.save()
            self.assertTrue(editor.winfo_exists())
            self.assertEqual(path.read_bytes(), saved)
            editor.destroy()
            self.wait_until_idle()
            page.assert_called_once()
        self.assertEqual(self.ui.tree.set(self.keys[0], "Status"), "Update available")
        self.assertIn("Update available", self.edit_selected().detail.get("1.0", "end"))

    def test_check_all_without_a_selection(self):
        self.ui.tree.selection_remove(*self.ui.tree.selection())
        self.root.update()
        with patch("fetch_sources.read_page", return_value="1") as page:
            self.ui.all_buttons["check"].invoke()
            self.assertTrue(self.ui.busy)
            self.assertEqual(self.ui.all_buttons["check"].cget("text"), "Cancel")
            self.assertTrue(self.ui.all_buttons["check"].instate(["!disabled"]))
            for button in (self.ui.all_buttons["fetch"], self.ui.edit_button):
                self.assertTrue(button.instate(["disabled"]))
            for label in ("Verify all items", "Reload catalog"):
                self.assertTrue(self.ui.more_menu.buttons[label].instate(["disabled"]))
            self.wait_until_idle()
            self.assertEqual(page.call_count, 2)
        self.assertFalse(self.dest.exists())
        self.assertEqual([self.ui.results[k].status for k in self.keys],
                         ["Update available", "Update available"])
        self.assertEqual(self.ui.tree.selection(), ())

    def test_cancel_bulk_work_stops_active_app_and_skips_rest(self):
        self.root.deiconify()
        self.root.update()
        previous = self.ui.results.copy()
        for action, label in (("check", "Check for updates"), ("fetch", "Update all items")):
            with self.subTest(action=action):
                publisher = GatedPage()
                with (patch("fetch_sources.read_page", side_effect=publisher) as page,
                      patch("fetch_sources.open_url") as download):
                    button = self.ui.all_buttons[action]
                    original_size = button.winfo_width(), button.winfo_height()
                    button.invoke()
                    try:
                        self.wait_for(publisher.entered.is_set)
                        self.root.update_idletasks()
                        self.assertEqual(button.cget("text"), "Cancel")
                        self.assertEqual((button.winfo_width(), button.winfo_height()), original_size)
                        self.assertTrue(button.instate(["!disabled"]))
                        button.invoke()
                        self.wait_until_idle()
                    finally:
                        publisher.resume.set()
                        self.wait_until_idle()
                    self.assertEqual(page.call_count, 1)
                    download.assert_not_called()
                self.assertEqual(self.ui.results, previous)
                self.assertEqual(button.cget("text"), label)
                self.assertEqual((button.winfo_width(), button.winfo_height()), original_size)
                self.assertTrue(all(b.instate(["!disabled"]) for b in self.ui.all_buttons.values()))
                self.assertIn("0 of 2 processed, 2 canceled", self.ui.status.get())
                self.assertEqual(self.ui.view_counts["progress"].get(), "0")

    def test_more_menu_cancels_bulk_verification_and_keeps_its_size(self):
        from shelf_cancel import check_canceled

        self.root.deiconify()
        self.ui.change_language("fr")
        self.root.update()
        trigger, menu = self.ui.more_button, self.ui.more_menu
        verify, reload_apps = menu.buttons["Verify all items"], menu.buttons["Reload catalog"]
        trigger.invoke()
        self.root.update()
        self.assertTrue(menu.winfo_viewable())
        self.assertEqual(verify.cget("text"), tr("Verify all items"))
        menu_size = menu.winfo_width(), menu.winfo_height()
        button_size = verify.winfo_width(), verify.winfo_height()
        previous = self.ui.results.copy()
        resume = Event()

        def gated_verify(root, recipe, target):
            if not resume.wait(10):
                raise TimeoutError("The test did not release verification")
            check_canceled()
            return Result("Verified")

        with patch("releaseshelf.verify", side_effect=gated_verify) as worker:
            verify.invoke()
            try:
                self.wait_for(lambda: self.ui.working_key == self.keys[0] and worker.called)
                self.assertFalse(menu.winfo_viewable())
                self.assertTrue(trigger.instate(["!disabled"]))
                trigger.invoke()
                self.root.update()
                self.assertTrue(menu.winfo_viewable())
                self.assertEqual(verify.cget("text"), tr("Cancel"))
                self.assertTrue(verify.instate(["!disabled"]))
                self.assertTrue(reload_apps.instate(["disabled"]))
                self.assertEqual((verify.winfo_width(), verify.winfo_height()), button_size)
                self.assertEqual((menu.winfo_width(), menu.winfo_height()), menu_size)
                verify.invoke()
            finally:
                resume.set()
                self.wait_until_idle()
            worker.assert_called_once()

        self.assertEqual(self.ui.results, previous)
        self.assertEqual(self.ui.status.get(), tr("{label}  ·  {finished} of {total} processed, {canceled} canceled",
                                                 label=tr("Verify all items"), finished=0, total=2, canceled=2))
        trigger.invoke()
        self.root.update()
        self.assertTrue(menu.winfo_viewable())
        self.assertEqual(verify.cget("text"), tr("Verify all items"))
        self.assertTrue(verify.instate(["!disabled"]))
        self.assertTrue(reload_apps.instate(["!disabled"]))
        self.assertEqual((verify.winfo_width(), verify.winfo_height()), button_size)
        self.assertEqual((menu.winfo_width(), menu.winfo_height()), menu_size)
        menu.dismiss()

    def test_right_click_cancel_interrupts_one_download_and_continues_batch(self):
        alpha, beta = self.keys
        with patch("fetch_sources.read_page", return_value="1"):
            self.click(self.ui.all_buttons["check"])
            previous = self.ui.results[alpha]
            blocked = GatedDownload()
            second = io.BytesIO(b"second")
            second.headers = {}
            with patch("fetch_sources.open_url", side_effect=(blocked, second)):
                self.ui.all_buttons["fetch"].invoke()
                try:
                    self.wait_for(lambda: blocked.waiting.is_set() and self.ui.downloading)
                    self.ui.tree.selection_set(beta)
                    button = self.right_click_app(alpha).buttons["Cancel"]
                    self.assertTrue(button.instate(["!disabled"]))
                    button.invoke()
                    self.wait_until_idle()
                finally:
                    blocked.resume.set()
                    self.wait_until_idle()
        self.assertEqual(self.ui.results[alpha], previous)
        self.assertEqual(self.ui.results[beta].status, "Updated")
        self.assertFalse((self.dest / "alpha" / "current.json").exists())
        self.assertFalse((self.dest / "alpha" / "current" / "alpha.bin").exists())
        self.assertFalse(list((self.dest / "alpha").glob(".stage-*")))
        self.assertEqual((self.dest / "beta" / "current" / "beta.bin").read_bytes(), b"second")
        self.assertEqual(self.ui.view_counts["progress"].get(), "0")
        self.assertFalse(self.ui.download_progress.winfo_manager())
        self.assertIn("1 of 2 processed, 1 canceled", self.ui.status.get())

    def test_right_click_cancels_queued_app_and_single_check(self):
        alpha, beta = self.keys
        for bulk in (True, False):
            with self.subTest(bulk=bulk):
                publisher = GatedPage()
                previous = self.ui.results.copy()
                with patch("fetch_sources.read_page", side_effect=publisher) as page:
                    if bulk:
                        self.ui.all_buttons["check"].invoke()
                    else:
                        self.right_click_app(beta).buttons["Check for updates"].invoke()
                    try:
                        self.wait_for(publisher.entered.is_set)
                        if not bulk:
                            menu = self.right_click_app(alpha)
                            self.assertTrue(menu.buttons["Cancel"].instate(["disabled"]))
                            menu.dismiss()
                        button = self.right_click_app(beta).buttons["Cancel"]
                        self.assertTrue(button.instate(["!disabled"]))
                        button.invoke()
                    finally:
                        publisher.resume.set()
                        self.wait_until_idle()
                    self.assertEqual(page.call_count, 1)
                if bulk:
                    self.assertEqual(self.ui.results[alpha].status, "Update available")
                else:
                    self.assertEqual(self.ui.results[alpha], previous[alpha])
                self.assertEqual(self.ui.results[beta], previous[beta])
                expected = "1 of 2 processed" if bulk else "0 of 1 processed"
                self.assertIn(f"{expected}, 1 canceled", self.ui.status.get())

    def test_search_by_display_name_and_id_keeps_catalog_wide_check(self):
        editor = self.edit_selected()
        editor.fields["name"].set("Desk Utility")
        editor.save()

        entry, tree = self.ui.search_entry, self.ui.tree
        for query in ("uTiLiTy", "ALPHA"):
            entry.delete(0, "end")
            entry.insert(0, query)
            self.root.update()
            self.assertEqual(tree.get_children(), (self.keys[0],))
            self.assertEqual(tree.set(self.keys[0], "Source"), "publisher.example")

        entry.insert("end", " missing")
        self.root.update()
        self.assertEqual(tree.get_children(), ())
        self.assertEqual(tree.selection(), ())
        self.assertTrue(self.ui.edit_button.instate(["disabled"]))
        with patch("fetch_sources.read_page", return_value="1") as page:
            self.click(self.ui.all_buttons["check"])
            self.assertEqual(page.call_count, 2)
        self.assertEqual(tree.get_children(), ())
        self.assertEqual(self.ui.view_counts["updates"].get(), "2")

    def test_updates_view_tracks_results_and_attention_includes_recipe_errors(self):
        valid, invalid = self.keys
        Path(invalid).write_text("{")
        self.ui.more_menu.buttons["Reload catalog"].invoke()
        self.ui.nav_buttons["updates"].invoke()
        self.root.update()
        tree = self.ui.tree
        self.assertEqual(tree.get_children(), ())
        with patch("fetch_sources.read_page", return_value="1"):
            self.click(self.ui.all_buttons["check"])
            self.assertEqual(tree.get_children(), (valid,))
            reply = io.BytesIO(b"fixture")
            reply.headers = {}
            with patch("fetch_sources.open_url", return_value=reply):
                self.click(self.ui.all_buttons["fetch"])
        self.assertEqual(tree.get_children(), ())
        self.assertEqual(self.ui.view_counts["updates"].get(), "0")
        self.ui.nav_buttons["attention"].invoke()
        self.root.update()
        self.assertEqual(tree.get_children(), (invalid,))
        self.assertEqual(tree.set(invalid, "Status"), "Error")
        self.ui.nav_buttons["all"].invoke()
        self.root.update()
        self.assertEqual(set(tree.get_children()), set(self.keys))

    def test_pending_publication_appears_in_both_actionable_views(self):
        root = self.dest / "alpha"
        root.mkdir(parents=True)
        (root / "pending.json").write_text(json.dumps({
            "version": "2", "release": "1" * 32, "sha256": {},
        }))
        self.ui.more_menu.buttons["Reload catalog"].invoke()
        for view in ("updates", "attention"):
            self.ui.nav_buttons[view].invoke()
            self.root.update()
            self.assertEqual(self.ui.tree.get_children(), (self.keys[0],))
            self.assertEqual(self.ui.tree.set(self.keys[0], "Status"), "Pending")

        self.ui.nav_buttons["progress"].invoke()
        self.assertEqual(self.ui.tree.get_children(), ())
        self.assertEqual(self.ui.view_counts["progress"].get(), "0")
        self.assertEqual(self.ui.empty_title.get(), "No operations in progress")

    def test_progress_view_tracks_active_batch_item_and_clears_on_completion(self):
        resume = {name: Event() for name in ("alpha", "beta")}

        def execute(action, job, dest, targets, **kwargs):
            if not resume[job.id].wait(5):
                raise TimeoutError("The progress view test did not release the worker")
            return Result("Verified")

        self.ui.nav_buttons["progress"].invoke()
        self.assertEqual(self.ui.tree.get_children(), ())
        self.assertEqual(self.ui.view_counts["progress"].get(), "0")
        self.assertEqual(self.ui.empty_title.get(), "No operations in progress")
        with patch("shelf_window.execute", side_effect=execute):
            self.ui.more_menu.buttons["Verify all items"].invoke()
            try:
                self.wait_for(lambda: self.ui.working_key == self.keys[0]
                              and self.ui.view_counts["progress"].get() == "1")
                self.assertEqual(self.ui.tree.get_children(), (self.keys[0],))
                self.assertEqual(self.ui.tree.set(self.keys[0], "Status"), "Verifying…")
                resume["alpha"].set()
                self.wait_for(lambda: self.ui.working_key == self.keys[1])
                self.assertEqual(self.ui.tree.get_children(), (self.keys[1],))
                self.assertEqual(self.ui.view_counts["progress"].get(), "1")
                self.assertEqual(self.ui.tree.set(self.keys[1], "Status"), "Verifying…")
                self.assertEqual(self.ui.results[self.keys[0]].status, "Verified")
            finally:
                for event in resume.values():
                    event.set()
                self.wait_until_idle()
        self.assertEqual(self.ui.view.get(), "progress")
        self.assertEqual(self.ui.tree.get_children(), ())
        self.assertEqual(self.ui.view_counts["progress"].get(), "0")
        self.assertEqual(self.ui.empty_title.get(), "No operations in progress")

    def test_progress_view_preserves_search_and_counts_filtered_active_app(self):
        resume = Event()

        def execute(*args, **kwargs):
            if not resume.wait(5):
                raise TimeoutError("The progress view test did not release the worker")
            return Result("Update available")

        self.ui.nav_buttons["progress"].invoke()
        self.ui.search.set("beta")
        with patch("shelf_window.execute", side_effect=execute):
            self.ui.start("check", key=self.keys[0])
            try:
                self.wait_for(lambda: self.ui.working_key == self.keys[0]
                              and self.ui.view_counts["progress"].get() == "1")
                self.assertEqual(self.ui.search.get(), "beta")
                self.assertEqual(self.ui.tree.get_children(), ())
                self.assertEqual(self.ui.empty_title.get(), "No matching items")
                self.ui.search.set("alpha")
                self.assertEqual(self.ui.tree.get_children(), (self.keys[0],))
                self.assertEqual(self.ui.view_counts["progress"].get(), "1")
                self.ui.search.set("beta")
            finally:
                resume.set()
                self.wait_until_idle()
        self.assertEqual(self.ui.search.get(), "beta")
        self.assertEqual(self.ui.tree.get_children(), ())
        self.assertEqual(self.ui.view_counts["progress"].get(), "0")
        self.assertEqual(self.ui.empty_title.get(), "No operations in progress")

    def test_progress_view_clears_active_item_after_worker_failure(self):
        resume = Event()

        def execute(*args, **kwargs):
            if not resume.wait(5):
                raise TimeoutError("The progress view test did not release the worker")
            raise RuntimeError("Worker failed")

        self.ui.nav_buttons["progress"].invoke()
        with patch("shelf_window.execute", side_effect=execute):
            self.ui.all_buttons["check"].invoke()
            try:
                self.wait_for(lambda: self.ui.working_key == self.keys[0]
                              and self.ui.view_counts["progress"].get() == "1")
                self.assertEqual(self.ui.tree.get_children(), (self.keys[0],))
            finally:
                resume.set()
                self.wait_until_idle()
        self.assertEqual(self.ui.tree.get_children(), ())
        self.assertEqual(self.ui.view_counts["progress"].get(), "0")
        self.assertEqual(self.ui.empty_title.get(), "No operations in progress")
        self.assertIn("Worker failed", self.ui.status.get())
        self.assertTrue(self.ui.all_buttons["check"].instate(["!disabled"]))

    def test_search_keeps_its_width_and_filter_when_clicking_the_title_or_heading_gap(self):
        self.root.deiconify()
        tree, entry = self.ui.tree, self.ui.search_entry
        border, title = self.ui.search_border, self.ui.title_label
        heading = title.master
        tree.focus_force()
        self.root.update()
        search_width = border.winfo_width()
        self.assertGreater(entry.winfo_width(), self.ui.search_hint.winfo_reqwidth())

        for target in (title, heading):
            for query in ("", "beta"):
                with self.subTest(target=target, query=query):
                    border.event_generate("<Button-1>")
                    self.root.update()
                    self.assertEqual(border.winfo_width(), search_width)
                    entry.delete(0, "end")
                    entry.insert(0, query)
                    self.root.update()
                    self.assertEqual(border.winfo_width(), search_width)

                    # The gap between the title and the search field belongs to the heading frame.
                    x = 5 if target is title else border.winfo_rootx() - heading.winfo_rootx() - 3
                    target.event_generate("<Button-1>", x=x, y=target.winfo_height() // 2)
                    self.root.update()
                    self.assertEqual(border.winfo_width(), search_width)
                    self.assertEqual(entry.get(), query)
                    self.assertEqual(tree.get_children(), (self.keys[1],) if query else self.keys)

                    border.event_generate("<Button-1>")
                    self.root.update()
                    self.assertEqual(self.root.focus_get(), entry)
                    self.assertEqual(border.winfo_width(), search_width)
                    self.assertEqual(entry.get(), query)

    def test_clear_button_and_escape_restore_the_list_and_keep_the_search_width(self):
        self.root.deiconify()
        tree, entry = self.ui.tree, self.ui.search_entry
        border, clear = self.ui.search_border, self.ui.search_clear
        self.root.update()
        search_width = border.winfo_width()

        for action, query in (("click", "beta"), ("escape", "beta"), ("escape", "")):
            with self.subTest(action=action, query=query):
                tree.focus_force()
                self.root.update()
                border.event_generate("<Button-1>")
                self.root.update()
                entry.insert(0, query)
                self.root.update()
                self.assertEqual(border.winfo_width(), search_width)
                self.assertEqual(tree.get_children(), (self.keys[1],) if query else self.keys)
                self.assertEqual(clear.winfo_ismapped(), bool(query))
                if action == "click":
                    clear.event_generate("<ButtonPress-1>", x=3, y=3)
                    clear.event_generate("<ButtonRelease-1>", x=3, y=3)
                else:
                    entry.event_generate("<Escape>")
                self.root.update()
                self.assertEqual(entry.get(), "")
                self.assertEqual(tree.get_children(), self.keys)
                self.assertEqual(border.winfo_width(), search_width)
                self.assertFalse(clear.winfo_ismapped())

    def test_keyboard_search_clear_and_open_catalog_tab(self):
        self.root.deiconify()
        tree = self.ui.tree
        tree.focus_force()
        self.root.update()
        tree.event_generate("<Control-f>")
        self.root.update()
        entry = self.ui.search_entry
        self.assertEqual(self.root.focus_get(), entry)
        entry.insert(0, "beta")
        self.root.update()
        self.assertEqual(tree.get_children(), (self.keys[1],))
        entry.event_generate("<Escape>")
        self.root.update()
        self.assertEqual(entry.get(), "")
        self.assertEqual(tree.get_children(), self.keys)
        tree.selection_set(self.keys[1])
        tree.focus_force()
        tree.event_generate("<Return>")
        self.root.update()
        editor = self.ui.editor
        self.assertEqual(editor.path, Path(self.keys[1]))
        self.assertEqual(editor.tabs.tab(editor.tabs.select(), "text"), "Catalog")

    def test_get_all_without_a_prior_check_and_skip_current_downloads(self):
        self.ui.tree.selection_set(self.keys[1])

        def download(url):
            response = io.BytesIO(url.rsplit("/", 1)[-1].encode())
            response.headers = {}
            return response

        with (
            patch("fetch_sources.read_page", return_value="1"),
            patch("fetch_sources.open_url", side_effect=download) as archive,
        ):
            self.click(self.ui.all_buttons["fetch"])
            self.assertEqual(archive.call_count, 2)
            for name in ("alpha", "beta"):
                target = self.dest / name / "current" / f"{name}.bin"
                self.assertEqual(target.read_bytes(), target.name.encode())
            archive.reset_mock()
            self.click(self.ui.all_buttons["fetch"])
            archive.assert_not_called()
        self.assertEqual([self.ui.results[k].status for k in self.keys], ["Latest", "Latest"])
        self.assertEqual(self.ui.tree.selection(), (self.keys[1],))
        self.assertIn("2 of 2 processed", self.ui.status.get())

    def test_invalid_targets_leave_controls_available_for_retry(self):
        self.dest.mkdir()
        self.ui.targets.write_text("{")
        editor = self.edit_selected()
        self.click(editor.buttons["fetch"])
        self.assertIn("Could not finish", self.ui.status.get())
        self.assertEqual(editor.error.get(), self.ui.status.get())
        self.ui.targets.unlink()
        self.click(editor.buttons["verify"])
        self.assertEqual(editor.error.get(), "")
        self.assertEqual(self.ui.results[self.keys[0]].status, "Repair needed")

    def test_add_github_then_edit_source_and_keep_download_settings(self):
        self.ui.add_button.invoke()
        editor = self.ui.editor
        editor.source.set("GitHub release")
        editor.show_source()
        editor.fields["id"].set("new-tool")
        editor.fields["github"].set("owner/tool")
        editor.fields["asset_pattern"].set("*-win.zip")
        editor.fields["publish_exe"].set("tool.exe")
        editor.text_area.insert("1.0", "notes.txt\ntool.exe")
        editor.save()
        path = self.ui.catalog / "new-tool.json"
        self.assertEqual(self.ui.tree.selection(), (str(path),))
        data = json.loads(path.read_text())
        data["description"] = "Extra catalog metadata"
        path.write_text(json.dumps(data))
        self.ui.edit_button.invoke()
        editor = self.ui.editor
        editor.source.set("Publisher page")
        editor.show_source()
        for key, value in {"page": "https://example.org", "version_pattern": "v([0-9.]+)",
                           "download": "https://example.org/files.zip"}.items():
            editor.fields[key].set(value)
        editor.save()
        changed = json.loads(path.read_text())
        self.assertNotIn("github", changed)
        self.assertNotIn("asset_pattern", changed)
        for key in ("publish_exe", "extract", "description"):
            self.assertEqual(changed[key], data[key])
        with patch("fetch_sources.read_page", return_value="v4.2"):
            self.click(self.edit_selected().buttons["check"])
        self.assertEqual(self.ui.results[str(path)].available, "4.2")

    def test_catalog_app_name_round_trip_and_fallback(self):
        key = self.keys[0]
        tree = self.ui.tree
        self.assertEqual(tree.heading("#1", "text"), "Name")
        self.assertEqual(tree.set(key, "Name"), "alpha")

        editor = self.edit_selected()
        self.assertEqual(editor.fields["name"].get(), "")
        editor.fields["name"].set("Alpha Utility")
        editor.save()
        saved = json.loads(Path(key).read_text(encoding="utf-8"))
        self.assertEqual(saved["id"], "alpha")
        self.assertEqual(saved["name"], "Alpha Utility")
        self.assertNotIn("description", saved)
        self.assertEqual(tree.set(key, "Name"), "Alpha Utility")

        editor = self.edit_selected()
        self.assertEqual(editor.fields["name"].get(), "Alpha Utility")
        editor.fields["name"].set("  ")
        editor.save()
        self.assertEqual(tree.set(key, "Name"), "alpha")

    def test_renaming_to_a_winget_id_keeps_cached_version_and_verification(self):
        key = self.keys[0]
        editor = self.edit_selected()
        archive = io.BytesIO(b"alpha release")
        archive.headers = {}
        with (patch("fetch_sources.read_page", return_value="1"),
              patch("fetch_sources.open_url", return_value=archive)):
            self.click(editor.buttons["fetch"])
        editor.fields["id"].set("Vendor.Alpha")
        editor.save()

        self.ui.reload()
        self.assertEqual(self.ui.tree.set(key, "Local version"), "1")
        editor = self.edit_selected()
        detail = editor.detail.get("1.0", "end-1c")
        self.assertIn("Item ID: Vendor.Alpha", detail)
        self.assertIn(f"Cache: {self.dest / 'alpha'}", detail)
        self.assertEqual(json.loads(Path(key).read_text())["previous_ids"], ["alpha"])
        self.click(editor.buttons["verify"])
        self.assertEqual(self.ui.results[key].status, "Verified")

        editor.fields["id"].set("alpha")
        editor.save()
        saved = json.loads(Path(key).read_text())
        self.assertEqual(saved["id"], "alpha")
        self.assertEqual(saved["previous_ids"], ["Vendor.Alpha"])
        self.click(self.edit_selected().buttons["verify"])
        self.assertEqual(self.ui.results[key].status, "Verified")

    def test_editor_rejects_id_claimed_by_another_apps_previous_id(self):
        first, second = self.keys
        editor = self.edit_selected()
        editor.fields["id"].set("Vendor.Alpha")
        editor.save()
        self.assertEqual(self.ui.jobs[first].id, "Vendor.Alpha")

        self.ui.tree.selection_set(second)
        editor = self.edit_selected()
        original = Path(second).read_bytes()
        editor.fields["id"].set("ALPHA")
        editor.save()
        self.assertIn("already used", editor.error.get())
        self.assertEqual(Path(second).read_bytes(), original)

    def test_repairing_a_duplicate_id_preserves_only_unclaimed_history(self):
        first, second = self.keys
        path = Path(first)
        data = json.loads(path.read_text())
        data.update(id="BETA", previous_ids=["alpha"])
        path.write_text(json.dumps(data))
        self.ui.reload()
        self.assertEqual(self.ui.results[first].status, "Error")

        editor = self.edit_selected()
        editor.fields["id"].set("Vendor.Alpha")
        editor.save()
        self.assertEqual(json.loads(path.read_text())["previous_ids"], ["alpha"])
        self.assertEqual(self.ui.jobs[first].id, "Vendor.Alpha")
        self.assertEqual(self.ui.jobs[second].id, "beta")

    def test_editing_category_preserves_the_description_shown_in_app_info(self):
        key, other = self.keys
        path = Path(key)
        data = json.loads(path.read_text(encoding="utf-8"))
        description = "  Shows the battery's charge.\nIncludes capacity history.  "
        path.write_text(json.dumps(dict(data, description=description)), encoding="utf-8")
        self.ui.reload()
        tree = self.ui.tree
        self.assertEqual(tree.set(key, "Category"), "Uncategorized")
        with patch("fetch_sources.read_page", return_value="1"):
            self.click(self.ui.all_buttons["check"])
        editor = self.edit_selected()
        catalog = next(tab for tab in editor.tabs.tabs() if editor.tabs.tab(tab, "text") == tr("Catalog"))
        catalog_widgets = list(descendants(editor.nametowidget(catalog)))
        labels = [w.cget("text") for w in catalog_widgets if isinstance(w, ttk.Label)]
        self.assertNotIn(tr("Description"), labels)
        for widget in catalog_widgets:
            if isinstance(widget, tk.Text):
                self.assertNotIn(description.strip(), widget.get("1.0", "end"))
        editor.fields["category"].set("System tools")
        editor.save()

        self.assertEqual(tree.set(key, "Category"), "System tools")
        self.assertEqual(tree.set(key, "Status"), "Update available")
        self.assertNotIn(description.strip(), tree.item(key, "values"))
        self.assertEqual(tree.set(other, "Category"), "Uncategorized")
        self.ui.search_entry.insert(0, "SYSTEM TOOLS")
        self.root.update()
        self.assertEqual(tree.get_children(), (key,))
        editor = self.edit_selected()
        detail = editor.detail.get("1.0", "end-1c")
        self.assertIn("Category: System tools", detail)
        self.assertIn(description.strip(), detail)
        self.assertIn("Update available", detail)
        editor.fields["category"].set("")
        editor.save()
        self.assertEqual(tree.get_children(), ())
        self.ui.search_entry.delete(0, "end")
        self.root.update()
        self.assertEqual(tree.set(key, "Category"), "Uncategorized")
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("category", saved)
        self.assertEqual(saved["description"], description)

    def test_edit_rejects_invalid_duplicate_and_stale_changes(self):
        self.ui.edit_button.invoke()
        editor = self.ui.editor
        path = Path(self.keys[0])
        original = path.read_bytes()
        editor.fields["version_pattern"].set("no group")
        editor.save()
        self.assertIn("capture group", editor.error.get())
        editor.fields["version_pattern"].set("(1)")
        editor.fields["id"].set("BETA")
        editor.save()
        self.assertIn("already used", editor.error.get())
        self.assertEqual(path.read_bytes(), original)
        editor.fields["id"].set("alpha")
        path.write_bytes(original + b"\n")
        with patch("fetch_sources.read_page") as page:
            editor.buttons["check"].invoke()
            self.assertIn("changed on disk", editor.error.get())
            self.assertFalse(self.ui.busy)
            page.assert_not_called()
        editor.save()
        self.assertIn("changed on disk", editor.error.get())
        editor.destroy()
        self.assertEqual(path.read_bytes(), original + b"\n")

    def test_repair_malformed_json_and_add_to_empty_catalog(self):
        for key in self.keys:
            Path(key).unlink()
        self.ui.reload()
        self.assertTrue(self.ui.edit_button.instate(["disabled"]))
        self.assertTrue(all(button.instate(["disabled"]) for button in self.ui.all_buttons.values()))
        self.ui.add_button.invoke()
        self.assertTrue(self.ui.editor.winfo_exists())
        self.assertFalse(self.ui.editor.buttons)
        self.assertIsNone(self.ui.editor.detail)
        self.ui.editor.destroy()
        path = Path(self.keys[0])
        path.write_text("{")
        self.ui.reload()
        self.ui.edit_button.invoke()
        editor = self.ui.editor
        self.assertTrue(all(button.instate(["disabled"]) for button in editor.buttons.values()))
        self.assertEqual(editor.tabs.tab(editor.tabs.select(), "text"), "Recipe JSON")
        self.assertEqual(editor.tabs.tab(editor.tabs.tabs()[-1], "text"), "Item info")
        self.assertEqual(editor.text_area.get("1.0", "end-1c"), "{")
        editor.text_area.insert("end-1c", '"id":"fixed","github":"org/tool","asset_pattern":"*.zip"}')
        editor.save()
        self.assertEqual(self.ui.jobs[str(path)].id, "fixed")

    def test_iso_source_settings_round_trip_and_switch_to_direct_download(self):
        path = Path(self.keys[0])
        editor = self.edit_selected()
        editor.fields["page"].set("https://www.microsoft.com/en-us/software-download/windows11")
        editor.fields["version_pattern"].set(r"Version (\d{2}H[12])")
        pattern = r"English x64 ([0-9A-Fa-f]{64})"
        editor.fields["sha256_pattern"].set(pattern)
        editor.fields["filename"].set("Windows 11 {version}.iso")
        editor.windows_iso_button.invoke()
        self.assertTrue(editor.download_entry.instate(["disabled"]))
        editor.save()
        saved = json.loads(path.read_text())
        self.assertNotIn("download", saved)
        self.assertEqual(saved["download_resolver"], "windows-11-english-x64")

        editor = self.edit_selected()
        self.assertFalse(editor.raw_editor)
        self.assertTrue(editor.download_entry.instate(["disabled"]))
        self.assertEqual(editor.form_data(), saved)
        editor.windows_iso_button.invoke()
        self.assertTrue(editor.download_entry.instate(["!disabled"]))
        download = "https://example.org/system-{version}.iso"
        editor.fields["download"].set(download)
        editor.save()
        saved = json.loads(path.read_text())
        self.assertNotIn("download_resolver", saved)
        self.assertEqual(saved["download"], download)
        self.assertEqual(saved["filename"], "Windows 11 {version}.iso")
        self.assertEqual(saved["sha256_pattern"], pattern)

        editor = self.edit_selected()
        editor.fields["sha256_pattern"].set("")
        editor.save()
        self.assertNotIn("sha256_pattern", json.loads(path.read_text()))

    def test_switching_to_github_removes_page_hash_pattern_and_resolver(self):
        path = Path(self.keys[0])
        data = json.loads(path.read_text())
        del data["download"]
        data.update(page="https://www.microsoft.com/en-us/software-download/windows11",
                    download_resolver="windows-11-english-x64", sha256_pattern=r"English x64 (\w{64})",
                    filename="image.iso")
        path.write_text(json.dumps(data))
        self.ui.reload()
        editor = self.edit_selected()
        editor.source.set("GitHub release")
        editor.show_source()
        editor.fields["github"].set("example/live-image")
        editor.fields["asset_pattern"].set("live-*.iso")
        editor.save()
        self.assertEqual(json.loads(path.read_text()),
                         {"id": "alpha", "github": "example/live-image", "asset_pattern": "live-*.iso",
                          "filename": "image.iso"})
