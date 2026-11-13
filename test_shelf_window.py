"""Exercise the desktop buttons against an isolated catalog and cache."""

import gc
import hashlib
import io
import json
import stat
from pathlib import Path
import subprocess
import sys
import tempfile
from string import Formatter
from threading import Event
from time import monotonic, sleep
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch
import zipfile

from fetch_store import Result
from shelf_categories import MOVE, RENAME, CategoryDialog, change_category
from shelf_delete import DELETE, FOLDER, delete_item, deletion_plan
from shelf_checkboxes import header_image
from shelf_window import ShelfWindow, deletion_dialog
from shelf_i18n import LANGUAGES, catalog, tr
from shelf_options import read_language
from shelf_theme import DARK, LIGHT, PALETTES, category_picker, read_appearance


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def download_fixture(url):
    response = io.BytesIO(b"fixture")
    response.headers = {"Content-Length": "7"}
    return response


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
        tmp = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
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

    def right_click_app(self, key, *, cancel_for=None):
        self.root.deiconify()
        self.root.update()
        tree = self.ui.tree
        x, y, width, height = tree.bbox(key, "Name")
        tree.event_generate("<Button-3>", x=x + width // 2, y=y + height // 2)
        self.root.update()
        menu = self.ui.app_menu
        self.assertTrue(menu.winfo_viewable())
        toggle = "Enable updates" if getattr(self.ui.jobs[key], "updates_disabled", False) else "Disable updates"
        labels = ("Get updates", "Verify files", toggle,
                  "Install", "Open app folder", MOVE, "Edit item", DELETE, FOLDER)
        self.assertEqual([button.cget("text") for button in menu.buttons.values()],
                         [tr("Cancel" if label == cancel_for else label) for label in labels])
        return menu

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

    def test_right_click_opens_clicked_apps_published_folder(self):
        first, second = self.keys
        root = self.ui.jobs[second].cache_root(self.dest)
        root.mkdir(parents=True)
        folder = self.dest / "custom output"
        folder.mkdir()
        (root / "current.json").write_text(json.dumps({
            "version": "1", "release": "a" * 32, "sha256": {}, "published": {"path": str(folder / "app.exe")},
        }))
        self.ui.tree.selection_set(first)
        with patch("shelf_window.os.startfile", create=True) as launch:
            self.right_click_app(second).buttons["Open app folder"].invoke()
            launch.assert_called_once_with(folder)
            self.right_click_app(first).buttons["Open app folder"].invoke()
            launch.assert_called_once()
            self.assertIn("Get updates", self.ui.status.get())

    def test_install_opens_clicked_published_package(self):
        first, second = self.keys
        root = self.ui.jobs[second].cache_root(self.dest)
        root.mkdir(parents=True)
        package = self.dest / "Setup package.MSI"
        package.touch()
        (root / "current.json").write_text(json.dumps({
            "version": "1", "release": "a" * 32, "sha256": {}, "published": {"path": str(package)},
        }))
        self.ui.tree.selection_set(first)
        with patch("shelf_window.os.startfile", create=True) as launch:
            self.right_click_app(second).buttons["Install"].invoke()
            launch.assert_called_once_with(package, cwd=package.parent)
            self.assertTrue(self.right_click_app(first).buttons["Install"].instate(["disabled"]))
            self.ui.app_menu.dismiss()
            package.unlink()
            self.assertTrue(self.right_click_app(second).buttons["Install"].instate(["!disabled"]))

    def test_install_fetches_clicked_item_then_reuses_verified_cache(self):
        first, second = self.keys
        recipe = Path(second)
        recipe.write_text(json.dumps({**json.loads(recipe.read_text()), "filename": "Setup.MSI"}))
        self.ui.reload()
        package = self.dest / "beta" / "current" / "Setup.MSI"
        response = GatedDownload()
        with (
            patch("fetch_sources.read_page", return_value="1") as check,
            patch("fetch_sources.open_url", return_value=response) as download,
            patch("shelf_window.os.startfile", create=True) as launch,
        ):
            self.right_click_app(second).buttons["Install"].invoke()
            try:
                self.wait_for(response.waiting.is_set)
                self.ui.tree.selection_set(first)
                launch.assert_not_called()
            finally:
                response.resume.set()
                self.wait_until_idle()
            self.assertEqual(package.read_bytes(), b"download")
            launch.assert_called_once_with(package, cwd=package.parent)
            package.unlink()
            self.click(self.right_click_app(second).buttons["Install"])
            self.assertEqual(self.ui.results[second].status, "Cached")
            self.assertEqual(launch.call_count, 2)
            download.assert_called_once()
            check.reset_mock()
            self.right_click_app(second).buttons["Install"].invoke()
            self.assertEqual(launch.call_count, 3)
            check.assert_not_called()

    def test_failed_or_canceled_install_does_not_launch(self):
        key = self.keys[0]
        recipe = Path(key)
        recipe.write_text(json.dumps({**json.loads(recipe.read_text()), "filename": "Setup.exe"}))
        self.ui.reload()
        with patch("shelf_window.os.startfile", create=True) as launch:
            with patch("fetch_sources.read_page", side_effect=OSError("Publisher unavailable")):
                self.click(self.right_click_app(key).buttons["Install"])
                self.assertIn("Publisher unavailable", self.ui.status.get())
            response = GatedDownload()
            with patch("fetch_sources.read_page", return_value="1"), patch("fetch_sources.open_url", return_value=response):
                self.right_click_app(key).buttons["Install"].invoke()
                try:
                    self.wait_for(response.waiting.is_set)
                    self.right_click_app(key, cancel_for="Install").buttons["Install"].invoke()
                finally:
                    response.resume.set()
                    self.wait_until_idle()
            launch.assert_not_called()

    def test_install_local_file(self):
        path = self.ui.catalog / "local.json"
        for name, enabled in (("setup.exe", True), ("download.zip", False)):
            package = self.ui.catalog / name
            package.touch()
            path.write_text(json.dumps({"id": "local", "local_path": name}))
            self.ui.reload()
            self.ui.select_view("local")
            button = self.right_click_app(str(path)).buttons["Install"]
            self.assertTrue(button.instate(["!disabled" if enabled else "disabled"]))
            if enabled:
                with patch("shelf_window.os.startfile", side_effect=OSError("Launch failed"), create=True) as launch:
                    button.invoke()
                    launch.assert_called_once_with(package, cwd=package.parent)
                    self.assertIn("Launch failed", self.ui.status.get())
            self.ui.app_menu.dismiss()

    @unittest.skipUnless(sys.platform == "win32", "Windows installation")
    def test_local_post_install_restore_and_saved_opt_out(self):
        path = self.ui.catalog / "local.json"
        package = self.ui.catalog / "setup.msi"
        package.touch()
        path.write_text(json.dumps({"id": "local", "local_path": "setup.msi", "restore_script": "profile.ps1",
                                    "restore_as_admin": True}))
        self.ui.reload()
        self.ui.select_view("local")
        with (patch("companion_buttons.subprocess.run") as run,
              patch("companion_buttons.open_companion") as restore,
              patch("shelf_window.os.startfile", create=True) as launch):
            run.return_value.returncode = 1602
            run.return_value.stderr = ""
            self.right_click_app(str(path)).buttons["Install"].invoke()
            self.wait_for(lambda: not self.ui.installing)
            self.assertIn("1602", self.ui.status.get())
            restore.assert_not_called()
            run.return_value.returncode = 3010
            self.right_click_app(str(path)).buttons["Install"].invoke()
            self.wait_for(lambda: not self.ui.installing)
            restore.assert_called_once_with(path.parent / "companions" / path.stem, Path("profile.ps1"), "run", True,
                                            app_folder=package.parent)
            self.ui.open_editor(path)
            editor = self.ui.editor
            checkbox = next(w for w in descendants(editor) if isinstance(w, ttk.Checkbutton)
                            and w.cget("text") == "Run restore script after installation")
            self.assertTrue(checkbox.instate(["selected"]))
            checkbox.invoke()
            editor.save_button.invoke()
            self.ui.open_editor(path)
            self.assertFalse(self.ui.editor.restore_after_install.get())
            self.ui.editor.destroy()
            self.right_click_app(str(path)).buttons["Install"].invoke()
            launch.assert_called_once_with(package, cwd=package.parent)
            self.assertEqual(run.call_count, 2)

    @unittest.skipUnless(sys.platform == "win32", "Windows installation")
    def test_fetched_install_restores_portable_config_without_a_script(self):
        path = Path(self.keys[1])
        path.write_text(json.dumps({**json.loads(path.read_text()), "filename": "Setup.exe"}))
        seeds = self.ui.catalog / "companions" / path.stem
        seeds.mkdir(parents=True)
        (seeds / "tool.ini").write_text("saved")
        self.ui.reload()
        live = self.dest / "beta" / "current" / "tool.ini"
        def finish_installer(*args, **kwargs):
            self.assertFalse(live.exists())
            live.write_text("defaults")
            return subprocess.CompletedProcess(args, 0)
        with (patch("fetch_sources.read_page", return_value="1"),
              patch("fetch_sources.open_url", side_effect=download_fixture),
              patch("companion_buttons.subprocess.run", side_effect=finish_installer) as run):
            self.right_click_app(str(path)).buttons["Install"].invoke()
            self.wait_for(lambda: not self.ui.busy and not self.ui.installing)
            self.assertEqual(live.read_text(), "saved", self.ui.status.get())
            run.assert_called_once()

    def test_disable_and_enable_from_sidebar(self):
        ui = self.ui
        first, second = self.keys
        ui.results[first] = Result("Update available")
        self.right_click_app(first).buttons["Disable updates"].invoke()
        self.assertEqual(ui.view_counts["updates"].get(), "0")
        self.assertEqual(ui.nav_buttons["disabled"].winfo_manager(), "grid")
        ui.nav_buttons["disabled"].invoke()
        self.assertEqual(ui.tree.get_children(), (first,))
        self.assertEqual(ui.view_counts["disabled"].get(), "1")
        ui.search.set("beta")
        self.assertFalse(ui.tree.get_children())
        self.assertEqual(ui.nav_buttons["disabled"].winfo_manager(), "grid")
        ui.search.set("")
        ui.reload()
        self.assertEqual(ui.tree.get_children(), (first,))
        self.assertEqual(ui.tree.set(first, "Status"), "Disabled")
        ui.start("fetch", key=first)
        self.assertFalse(ui.busy)
        ui.open_editor(Path(first))
        self.assertTrue(ui.editor.buttons["fetch"].instate(["disabled"]))
        self.assertTrue(ui.editor.buttons["verify"].instate(["!disabled"]))
        ui.editor.destroy()
        with patch("shelf_window.prepare_fetch", return_value=Result("Latest")) as prepare:
            self.click(ui.all_buttons["fetch"])
            self.assertEqual([call.args[0] for call in prepare.call_args_list], [ui.jobs[second]])
        menu = self.right_click_app(first)
        self.assertTrue(menu.buttons["Get updates"].instate(["disabled"]))
        self.assertTrue(menu.buttons["Verify files"].instate(["!disabled"]))
        menu.buttons["Disable updates"].invoke()
        self.assertFalse(ui.nav_buttons["disabled"].winfo_manager())
        self.assertEqual(ui.view.get(), "all")
        self.assertEqual(ui.tree.get_children(), self.keys)
        ui.reload()
        self.assertFalse(ui.jobs[first].updates_disabled)

    def test_local_view_includes_missing_paths(self):
        self.assertFalse(self.ui.nav_buttons["local"].winfo_manager())
        path = self.ui.catalog / "local.json"
        path.write_text(json.dumps({"id": "local", "local_path": "missing"}))
        self.ui.reload()
        self.assertEqual(self.ui.tree.get_children(), self.keys)
        self.assertEqual(self.ui.view_counts["all"].get(), "2")
        self.assertEqual(self.ui.page_title.get(), "Tracked")
        nav = self.ui.nav_buttons
        self.assertEqual(int(nav["local"].grid_info()["row"]), int(nav["all"].grid_info()["row"]) + 1)
        nav["attention"].invoke()
        self.assertIn(str(path), self.ui.tree.get_children())
        nav["local"].invoke()
        self.assertEqual(self.ui.tree.get_children(), (str(path),))
        self.assertEqual(self.ui.view_counts["local"].get(), "1")
        self.assertEqual(self.ui.tree.set(str(path), "Status"), "Error")
        self.assertTrue(self.right_click_app(str(path)).buttons["Disable updates"].instate(["disabled"]))
        self.ui.search.set("no matching item")
        self.assertEqual(self.ui.tree.get_children(), ())
        self.assertEqual(nav["local"].winfo_manager(), "grid")
        path.unlink()
        self.ui.reload()
        self.assertFalse(nav["local"].winfo_manager())
        self.assertEqual(self.ui.view.get(), "all")

    def test_tracked_examples_open_and_save_in_a_local_only_catalog(self):
        for key in self.keys:
            Path(key).unlink()
        local = self.ui.catalog / "local.json"
        local.write_text(json.dumps({"id": "local", "local_path": "."}))
        self.ui.reload()
        self.root.deiconify()
        self.root.update()
        self.assertTrue(self.ui.example_choices.winfo_viewable())
        buttons = self.ui.example_choices.winfo_children()
        self.assertEqual([b.cget("text") for b in buttons], ["Process Explorer", "Autoruns"])
        buttons[0].invoke()
        self.assertEqual(self.ui.editor.fields["id"].get(), "process-explorer")
        self.ui.editor.cancel_button.invoke()
        self.assertEqual(list(self.ui.catalog.glob("*.json")), [local])
        buttons[1].invoke()
        self.ui.editor.save_button.invoke()
        key, = self.ui.tree.get_children()
        self.assertEqual(self.ui.tree.set(key, "Name"), "Autoruns")
        self.assertEqual(self.ui.jobs[key].download, "https://download.sysinternals.com/files/Autoruns.zip")
        self.assertEqual(self.ui.view_counts["local"].get(), "1")

    def test_local_item_opens_folder_and_edits_without_enabling_updates(self):
        path = self.ui.catalog / "local.json"
        path.write_text(json.dumps({"id": "local", "category": "Hardware", "local_path": "."}))
        self.ui.reload(saved=str(path))
        self.ui.select_view("local")
        key = str(path)
        self.assertEqual(self.ui.tree.set(key, "Status"), "Local only")
        with patch("shelf_window.os.startfile", create=True) as launch:
            menu = self.right_click_app(key)
            self.assertTrue(menu.buttons["Get updates"].instate(["disabled"]))
            menu.buttons["Open app folder"].invoke()
            launch.assert_called_once_with(self.ui.catalog)
        self.ui.open_editor(path)
        editor = self.ui.editor
        self.assertEqual(editor.source.get(), "Local file or folder")
        editor.fields["category"].set("Utilities")
        editor.save()
        self.assertEqual(json.loads(path.read_text()), {"id": "local", "category": "Utilities", "local_path": "."})

    def test_apply_custom_settings_checkbox(self):
        path = Path(self.keys[0])
        self.ui.tree.selection_set(str(path))
        seeds = self.ui.catalog / "companions" / path.stem
        seeds.mkdir(parents=True)
        (seeds / "app.ini").write_text("saved configuration")
        editor = self.edit_selected()
        self.assertFalse(editor.apply_settings.get())
        editor.settings_button.invoke()
        editor.save_button.invoke()
        self.assertIn("Get updates", self.ui.status.get())
        job = self.ui.jobs[str(path)]
        root = job.cache_root(self.dest)
        live = root / "current"
        live.mkdir(parents=True)
        target = live / "app.exe"
        target.write_bytes(b"app")
        (root / "current.json").write_text(json.dumps({"version": "1", "release": "a" * 32,
            "sha256": {}, "recipe": job.fingerprint(), "published": {"path": str(target)}}))
        (live / "app.ini").write_text("old")
        editor = self.edit_selected()
        self.assertTrue(editor.apply_settings.get())
        editor.save_button.invoke()
        self.assertEqual((live / "app.ini").read_text(), "saved configuration")
        self.assertIn("applied", self.ui.status.get())
        editor = self.edit_selected()
        editor.settings_button.invoke()
        (live / "app.ini").write_text("local edit")
        editor.save_button.invoke()
        self.assertEqual((live / "app.ini").read_text(), "local edit")
        (seeds / "app.ini").unlink()
        (seeds / "restore.ps1").touch()
        self.assertFalse(hasattr(self.edit_selected(), "settings_button"))

    def test_language_selector_translates_live_and_survives_restart(self):
        key = self.keys[1]
        self.ui.search.set("beta")
        self.ui.column_menu.buttons["Source"].invoke()
        self.click(self.ui.bulk_buttons["verify"])
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
                self.assertEqual(options.language_button.winfo_reqheight(), self.ui.more_button.winfo_reqheight())
                self.assertEqual(read_language(self.ui.language_file), code)
                self.assertEqual(options.language_heading.cget("text"), tr("Language"))
                self.assertEqual(self.ui.page_title.get(), tr("Tracked"))
                self.assertEqual(self.ui.tree.heading("Name", "text"), tr("Name"))
                self.assertEqual(self.ui.column_menu.buttons["Item icon"].cget("text"), tr("Item icon"))
                self.assertEqual(self.ui.tree.get_children(), (key,))
                self.assertEqual(self.ui.tree.selection(), (key,))
                self.assertNotIn("Source", self.ui.tree["displaycolumns"])
                self.assertEqual(self.ui.tree.set(key, "Status"), tr("Repair needed"))
                self.assertEqual(self.ui.results[key].status, "Repair needed")
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
        self.assertEqual(reopened.page_title.get(), catalog("ur")["Tracked"])
        reopened.options_button.invoke()
        self.assertEqual(reopened.options.language_button.cget("text"), "اردو")

    def assert_dropdown_join(self, trigger, menu, *, below):
        self.assertTrue(trigger.instate(["selected"]))
        self.assertEqual(menu.arrow.cget("text"), "▲")
        self.assertTrue(menu.join.winfo_viewable())
        shared_y = trigger.winfo_rooty() + (trigger.winfo_height() - 1 if below else 0)
        self.assertEqual(menu.join.winfo_rooty(), shared_y)
        self.assertEqual(menu.join.winfo_height(), 1)
        self.assertEqual(menu.join.winfo_rootx(), menu.winfo_rootx() + 1)
        self.assertEqual(right_edge(menu.join), right_edge(menu) - 1)

    @unittest.skipUnless(sys.platform == "win32", "Native window activation requires Windows")
    def test_clicking_behind_options_keeps_the_dialog_active(self):
        import ctypes
        from ctypes import wintypes

        user = ctypes.WinDLL("user32")
        user.GetActiveWindow.restype = wintypes.HWND
        self.root.deiconify()
        self.root.update()
        self.root.focus_force()
        self.ui.open_options()
        self.root.update()
        dialog = self.ui.options
        parent = wintypes.HWND(int(self.root.frame(), 0))
        frame = int(dialog.frame(), 0)
        focused = dialog.focus_get()
        self.assertIs(focused.winfo_toplevel(), dialog)

        # Windows only asks an enabled window whether a click should activate it.
        self.assertTrue(user.IsWindowEnabled(parent))
        for hit in (1, 2):  # HTCLIENT and HTCAPTION
            with self.subTest(hit=hit):
                result = user.SendMessageW(parent, 0x21, parent, (0x201 << 16) | hit)  # WM_MOUSEACTIVATE
                self.assertIn(result, (3, 4))  # MA_NOACTIVATE or MA_NOACTIVATEANDEAT
                self.ui.add_button.event_generate("<ButtonPress-1>", x=5, y=5)
                self.ui.add_button.event_generate("<ButtonRelease-1>", x=5, y=5)
                self.root.update()
                self.assertIsNone(self.ui.editor)
                self.assertEqual(user.GetActiveWindow(), frame)
                self.assertIs(dialog.focus_get(), focused)
                self.assertIs(self.root.grab_current(), dialog)

        dialog.close_button.invoke()
        self.ui.add_button.event_generate("<ButtonPress-1>", x=5, y=5)
        self.ui.add_button.event_generate("<ButtonRelease-1>", x=5, y=5)
        self.root.update()
        self.assertIsNotNone(self.ui.editor)

    def test_keyboard_language_picker_previews_cancels_and_commits(self):
        self.root.deiconify()
        self.ui.open_options()
        options = self.ui.options
        self.root.update()
        trigger, menu = options.language_button, options.language_menu
        self.assertIs(self.root.grab_current(), options)
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
        self.assertIs(self.root.grab_current(), options)
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
        self.assertIs(self.root.grab_current(), options)
        options.close_button.invoke()
        self.assertIsNone(self.root.grab_current())

    @unittest.skipUnless(sys.platform == "win32", "Shell actions require Windows")
    def test_companion_buttons_are_explicit_and_reveal_the_selected_manual(self):
        path = Path(self.keys[0])
        data = json.loads(path.read_text())
        data.update(restore_script="Restore.ps1", manual_files=["Settings/Instructions.txt"])
        path.write_text(json.dumps(data))
        folder = self.ui.catalog / "companions" / path.stem
        manual = folder / data["manual_files"][0]
        manual.parent.mkdir(parents=True)
        manual.write_text("Example")
        (folder / data["restore_script"]).write_text("Write-Output 'Example'")
        with patch("companion_buttons.os.startfile", create=True) as launch:
            self.ui.open_editor(path)
            dialog = self.ui.editor
            buttons = {str(w.cget("text")): w for w in descendants(dialog) if isinstance(w, ttk.Button)}
            launch.assert_not_called()
            for label in ("Open companion files", "Open app folder", "Open instructions"):
                self.assertNotIn(label, buttons)
            buttons["Show in folder"].invoke()
            self.assertEqual(launch.call_args.kwargs["arguments"], f'/select,"{manual}"')
            buttons["Edit restore script"].invoke()
            self.assertEqual(launch.call_args.args[0].name, "notepad.exe")
            self.assertEqual(launch.call_args.kwargs["arguments"], subprocess.list2cmdline([str(folder / "Restore.ps1")]))
            self.assertTrue(any(str(w.cget("text")) == "Restore.ps1" for w in descendants(dialog)
                                if isinstance(w, ttk.Label)))
            buttons["Run restore script"].invoke()
            self.assertEqual(launch.call_args.args[1], "open")
            self.assertEqual(launch.call_args.args[3], str(folder))
            manual.unlink()
            buttons["Show in folder"].invoke()
            self.assertIn("Missing companion file", dialog.error.get())
            dialog.save_button.invoke()
            self.assertEqual(launch.call_count, 3)

    def test_companion_actions_reject_escaping_paths_and_mismatched_file_types(self):
        from fetch_sources import companion_actions
        for data in ({"restore_script": "../other.ps1"}, {"restore_script": "C:/other.ps1"},
                     {"restore_script": "other.exe"}, {"manual_files": ["other.ps1"]},
                     {"restore_script": "folder/../x.ps1"}, {"restore_as_admin": True},
                     {"restore_after_install": "false"}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                companion_actions(data)

    @unittest.skipUnless(sys.platform == "win32", "Local PowerShell actions require Windows")
    def test_checklist_only_applies_explicit_choices(self):
        from releaseshelf import main

        path = self.ui.catalog / "setup.json"
        log = self.ui.catalog / "actions.txt"
        steps = [{"label": name, "description": name, "powershell": script} for name, script in (
            ("one", "'one' | Add-Content actions.txt"),
            ("skip", "'skip' | Add-Content actions.txt"),
            ("failure", "throw 'sample failure'"),
            ("last", "'last' | Add-Content actions.txt"))]
        path.write_text(json.dumps({"type": "checklist", "id": "setup", "name": "Setup",
                                    "category": "Tools", "steps": steps}))
        self.ui.reload(saved=str(path))
        self.assertEqual(self.ui.tree.get_children(), self.keys)
        self.assertEqual(self.ui.view_counts["all"].get(), "2")
        catalog_columns = self.ui.tree["displaycolumns"]
        self.assertFalse(any(str(path) in keys for keys in self.ui.category_groups.values()))
        checklist = self.ui.nav_buttons["checklist"]
        self.assertLess(int(checklist.grid_info()["row"]), int(self.ui.nav_buttons["all"].grid_info()["row"]))
        checklist.invoke()
        self.assertEqual(self.ui.view_counts["checklist"].get(), "1")
        self.assertEqual(self.ui.tree.get_children(), (str(path),))
        self.assertEqual(self.ui.tree["displaycolumns"], ("Name", "Status"))
        self.assertEqual(self.ui.tree.set(str(path), "Status"), "Not applied")
        self.assertEqual(self.ui.count.cget("text"), "1 item")
        self.root.deiconify()
        self.ui.open_editor(path)
        dialog = self.ui.editor
        self.assertTrue(dialog.apply_button.instate(["disabled"]))
        self.assertEqual(dialog.selected, [False] * 4)
        dialog.tabs.select(1)
        self.root.update()
        self.click_checkbox_header(dialog.listing)
        self.assertEqual(dialog.selected, [True] * 4)
        self.click_checkbox_header(dialog.listing)
        self.assertEqual(dialog.selected, [False] * 4)
        self.click_checkbox_header(dialog.listing)
        dialog.toggle(1)
        self.assertEqual(dialog.listing.tk.splitlist(dialog.listing.heading("#0", "image")), (header_image(3, 4),))
        dialog.save_button.invoke()
        with patch("shelf_window.execute", return_value=Result("Verified")) as verify:
            self.click(self.ui.bulk_buttons["verify"])
            self.assertEqual(verify.call_count, 2)
        with patch("releaseshelf.execute", return_value=Result("Verified")) as verify:
            self.assertEqual(main(["verify", "--all", "--catalog", str(self.ui.catalog)]), 0)
            self.assertEqual(verify.call_count, 2)
        self.assertFalse(log.exists())
        self.ui.open_editor(path)
        dialog = self.ui.editor
        self.assertEqual(dialog.selected, [True, False, True, True])
        dialog.tabs.select(1)
        self.root.update()
        dialog.apply_button.invoke()
        self.assertTrue(dialog.running)
        self.assertTrue(dialog.apply_button.instate(["disabled"]))
        self.wait_for(lambda: not dialog.running)
        self.assertEqual(log.read_text().splitlines(), ["one", "last"])
        self.assertEqual(dialog.listing.set("2", "result"), "Error")
        self.assertIn("sample failure", dialog.outputs[2])
        self.assertEqual(dialog.listing.set("1", "result"), "Not applied")
        self.assertEqual(dialog.listing.set("3", "result"), "Applied")
        self.assertEqual(self.ui.tree.set(str(path), "Status"), "Error")
        self.assertEqual(self.ui.view_counts["attention"].get(), "0")
        self.ui.select_view("all")
        self.assertEqual(self.ui.tree["displaycolumns"], catalog_columns)
        self.assertEqual(self.ui.tree.get_children(), self.keys)
        checklist.invoke()
        dialog.toggle(0)
        dialog.toggle(3)
        with patch("shelf_checklist.apply_step", return_value="ok"):
            dialog.apply_button.invoke()
            self.wait_for(lambda: not dialog.running)
            self.assertEqual(self.ui.tree.set(str(path), "Status"), "Partially applied")
            dialog.toggle(2)
            dialog.toggle(1)
            dialog.apply_button.invoke()
            self.wait_for(lambda: not dialog.running)
        self.ui.reload()
        self.assertEqual(self.ui.tree.set(str(path), "Status"), "Applied")
        second = tk.Toplevel(self.root)
        self.addCleanup(second.destroy)
        reopened = ShelfWindow(second, self.ui.catalog, self.dest, self.ui.targets, appearance_file=self.appearance)
        reopened.select_view("checklist")
        self.assertEqual(reopened.tree.set(str(path), "Status"), "Applied")
        path.write_text(path.read_text() + "\n")
        dialog.apply_button.invoke()
        self.assertFalse(dialog.running)
        self.assertIn("changed on disk", dialog.error.get())
        self.assertEqual(log.read_text().splitlines(), ["one", "last"])

    def test_checklist_edit_save_and_apply(self):
        path = self.ui.catalog / "setup.json"
        step = {"label": "First", "description": "Original", "powershell": "'one'", "extra": 42}
        data = {"id": "setup", "type": "checklist", "steps": [step], "software_folder": "old"}
        path.write_text(json.dumps(data))
        self.ui.open_editor(path)
        dialog = self.ui.editor
        self.assertEqual(dialog.tabs.tab("current", "text"), "General")
        self.assertIsNone(dialog.picker)
        dialog.label.set("Discard")
        dialog.close_button.invoke()
        self.assertEqual(json.loads(path.read_bytes()), data)

        data["steps"].append({**step, "selected": True})
        path.write_text(json.dumps(data))
        self.ui.open_editor(path)
        dialog = self.ui.editor
        dialog.name.set("Setup tools")
        dialog.folder.set("tools")
        dialog.label.set("First draft")
        dialog.picker.current(1)
        dialog.picker.event_generate("<<ComboboxSelected>>")
        dialog.script.delete("1.0", "end")
        dialog.save_button.invoke()
        self.assertIn("PowerShell", dialog.error.get())
        self.assertEqual(json.loads(path.read_bytes()), data)
        dialog.script.insert("1.0", "'two'\n'new'")
        dialog.description.insert("end", " revised")
        dialog.tabs.select(1)
        self.root.update()
        self.assertEqual(dialog.listing.rows["0"][0], "First draft")
        dialog.listing.selection_set("1")
        self.root.update()
        self.assertEqual(dialog.detail.get(), "Original revised")
        with patch("shelf_checklist.apply_step", return_value="done") as run:
            dialog.save_button.invoke()
            run.assert_not_called()
            saved = json.loads(path.read_bytes())
            self.assertEqual(saved["name"], "Setup tools")
            self.assertEqual(saved["steps"][0], {**step, "label": "First draft", "selected": False})
            self.ui.open_editor(path)
            dialog = self.ui.editor
            dialog.picker.current(1)
            dialog.picker.event_generate("<<ComboboxSelected>>")
            dialog.script.insert("end", "\n'latest'")
            dialog.tabs.select(1)
            self.root.update()
            dialog.apply_button.invoke()
            self.wait_for(lambda: not dialog.running)
            run.assert_called_once()
            action, folder, directory = run.call_args.args
            self.assertEqual((action.powershell, folder, directory), ("'two'\n'new'\n'latest'", "tools", path.parent))

    def test_checklist_toolbar_uses_checked_rows_and_all_rows_across_search(self):
        ui, tree = self.ui, self.ui.tree
        keys = []
        for name in ("first", "second"):
            path = ui.catalog / f"{name}.json"
            steps = [{"label": name + suffix, "description": name, "powershell": "unused"}
                     for suffix in ("1", "2")]
            path.write_text(json.dumps({"id": name, "type": "checklist", "steps": steps}))
            keys.append(str(path))
        ui.reload()
        ui.select_view("checklist")
        self.root.deiconify()
        self.root.update()
        selected, apply_all = (ui.all_buttons[action] for action in ("apply_selected", "apply_all"))
        self.assertNotIn("check", ui.all_buttons)
        self.assertFalse(ui.all_buttons["fetch"].winfo_ismapped())
        self.assertTrue(selected.winfo_ismapped() and apply_all.winfo_ismapped())
        self.assertTrue(selected.instate(["disabled"]))
        x, y, w, h = tree.bbox(keys[0], "#0")
        self.assertIn("image", tree.identify_element(x + w // 2, y + h // 2))
        tree.event_generate("<Button-1>", x=x + w // 2, y=y + h // 2)
        tree.event_generate("<ButtonRelease-1>", x=x + w // 2, y=y + h // 2)
        self.assertTrue(tree.tag_has("checked", keys[0]))
        tree.focus_force()
        self.root.update()
        tree.event_generate("<space>")
        self.assertTrue(selected.instate(["disabled"]))
        tree.event_generate("<space>")
        ui.search.set("second")
        self.assertEqual(tree.get_children(), (keys[1],))
        with patch("shelf_checklist.apply_step", return_value="ok") as apply:
            self.click(selected)
            self.assertEqual([c.args[0].label for c in apply.call_args_list], ["first1", "first2"])
            self.assertEqual(tree.set(keys[0], "Status"), "Applied")
            self.assertEqual(tree.set(keys[1], "Status"), "Not applied")
            apply.reset_mock()
            self.click(apply_all)
            self.assertEqual([c.args[0].label for c in apply.call_args_list],
                             ["first1", "first2", "second1", "second2"])
        self.assertEqual(tree.set(keys[1], "Status"), "Applied")
        self.assertEqual(ui.checked_items, {keys[0]})
        ui.show_app_icons.set(True)
        ui.change_app_icons()
        self.assertEqual(tree.tk.splitlist(tree.heading("#0", "image")), (header_image(0, 1),))
        ui.select_view("all")
        self.root.update()
        self.assertTrue(ui.all_buttons["fetch"].winfo_ismapped())
        self.assertFalse(selected.winfo_ismapped())
        ui.select_view("checklist")
        ui.search.set("")
        self.assertTrue(tree.tag_has("checked", keys[0]))
        self.assertEqual(tree.tk.splitlist(tree.heading("#0", "image")), (header_image(1, 2),))
        self.click_checkbox_header(tree)
        self.assertEqual(ui.checked_items, set(keys))
        self.assertEqual(tree.tk.splitlist(tree.heading("#0", "image")), (header_image(2, 2),))
        ui.search.set("second")
        self.click_checkbox_header(tree)
        self.assertEqual(ui.checked_items, {keys[0]})
        ui.search.set("no match")
        self.assertEqual(tree.tk.splitlist(tree.heading("#0", "image")), (header_image(0, 0),))
        ui.search.set("")
        self.click_checkbox_header(tree)
        self.click_checkbox_header(tree)
        self.assertFalse(ui.checked_items)

    def click_checkbox_header(self, tree):
        self.root.update()
        x = tree.column("#0", "width") // 2
        self.assertEqual(tree.identify_region(x, 10), "heading")
        tree.event_generate("<Motion>", x=x, y=10)
        tree.event_generate("<Button-1>", x=x, y=10)
        tree.event_generate("<ButtonRelease-1>", x=x, y=10)
        self.root.update()

    def test_cancel_checklist_batch_finishes_current_step_and_records_partial_status(self):
        path = self.ui.catalog / "setup.json"
        path.write_text(json.dumps({"id": "setup", "type": "checklist", "steps": [
            {"label": name, "description": name, "powershell": "unused"} for name in ("one", "two")]}))
        self.ui.reload()
        self.ui.select_view("checklist")
        step = GatedPage()
        button = self.ui.all_buttons["apply_all"]
        with patch("shelf_checklist.apply_step", side_effect=step) as apply:
            button.invoke()
            try:
                self.wait_for(step.entered.is_set)
                self.assertEqual(button.cget("text"), "Cancel")
                button.invoke()
            finally:
                step.resume.set()
                self.wait_until_idle()
            self.assertEqual([c.args[0].label for c in apply.call_args_list], ["one"])
        self.assertEqual(self.ui.tree.set(str(path), "Status"), "Partially applied")
        self.assertIn("1 canceled", self.ui.status.get())

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
        for label in ("Get updates", "Verify files", "Edit item"):
            button = menu.buttons[label]
            self.assertTrue(button.instate(["!disabled"]))
            self.assertEqual(str(button.cget("compound")), "left")
            self.assertIn(button.cget("image")[0], self.root.image_names())
        tree.selection_set(first)
        menu.buttons["Verify files"].invoke()
        self.wait_until_idle()
        self.assertFalse(menu.winfo_viewable())
        self.assertEqual(self.ui.results[second].status, "Repair needed")
        self.assertEqual(self.ui.results[first].status, "Unknown")

    def test_delete_local_file_scope_and_confirmation(self):
        path = self.ui.catalog / "local.json"
        app = self.dest.parent / "portable" / "tool.exe"
        folder = app.parent
        settings = folder / "settings.ini"
        for mode in (DELETE, FOLDER):
            with self.subTest(mode=mode):
                folder.mkdir(exist_ok=True)
                app.write_bytes(b"app")
                settings.write_text("settings")
                path.write_text(json.dumps({"id": "local", "local_path": str(app)}))
                self.ui.reload()
                self.ui.select_view("local")
                with patch("shelf_window.deletion_dialog", return_value=False) as confirm:
                    self.right_click_app(str(path)).buttons[mode].invoke()
                    self.assertTrue(path.exists() and app.exists() and settings.exists())
                    if mode == FOLDER:
                        self.assertIn(folder, confirm.call_args.args[-1])
                with patch("shelf_window.deletion_dialog", return_value=True):
                    self.right_click_app(str(path)).buttons[mode].invoke()
                self.assertNotIn(str(path), self.ui.jobs)
                self.assertFalse(path.exists())
                self.assertEqual(app.exists(), mode == DELETE)
                self.assertEqual(settings.exists(), mode != FOLDER)
                self.assertEqual(folder.exists(), mode != FOLDER)
                self.assertEqual(self.ui.view.get(), "all")
                self.assertEqual(self.ui.tree.get_children(), self.keys)

    def test_delete_dialog_actions_and_safe_default(self):
        self.root.deiconify()
        self.root.update()
        paths = [Path("folder") / f"file-{i}.txt" for i in range(30)]
        for action in ("Return", "Escape", "Delete item with data"):
            observed = {}

            def choose():
                dialog = self.root.grab_current()
                buttons = {w.cget("text"): w for w in descendants(dialog) if isinstance(w, ttk.Button)}
                listing = next(w for w in descendants(dialog) if isinstance(w, tk.Text))
                # The private desktop may have no active window. Check which control
                # receives focus on activation before sending a keyboard event.
                observed["focus"] = dialog.focus_lastfor()
                observed["cancel"] = buttons["Cancel"]
                observed["icons"] = all(w.cget("image") for w in buttons.values())
                observed["paths"] = listing.get("1.0", "end-1c").splitlines()
                if action == "Delete item with data":
                    buttons[action].invoke()
                else:
                    buttons["Cancel"].focus_force()
                    buttons["Cancel"].event_generate(f"<{action}>")

            def timeout_close():
                observed["timeout"] = True
                self.root.grab_current().destroy()

            self.root.after(100, choose)
            timeout = self.root.after(5000, timeout_close)
            confirmed = deletion_dialog(self.root, self.ui.palette, FOLDER, "Sample", "Files", paths)
            self.root.after_cancel(timeout)
            self.assertNotIn("timeout", observed)
            self.assertEqual(confirmed, action == "Delete item with data")
            self.assertIs(observed["focus"], observed["cancel"])
            self.assertTrue(observed["icons"])
            self.assertEqual(observed["paths"], list(map(str, paths)))

    def test_delete_download_removes_app_folder_and_cache(self):
        path = Path(self.keys[0])
        root = self.dest / "old-id"
        live = root / "current"
        live.mkdir(parents=True)
        payload = ("tool.exe", "support.dll", "settings.ini", "manual.txt")
        for name in (*payload, "user.log"):
            (live / name).write_text(name)
        raw = json.loads(path.read_text())
        raw["previous_ids"] = ["old-id"]
        path.write_text(json.dumps(raw))
        companions = self.ui.catalog / "companions" / path.stem
        companions.mkdir(parents=True)
        (companions / "manual.txt").write_text("instructions")
        release = "a" * 32
        saved = root / "releases" / release
        saved.mkdir(parents=True)
        (saved / "download.zip").write_bytes(b"cache")
        hashes = {"files/" + name: "hash" for name in payload}
        manifest = {"release": release, "version": "1", "sha256": hashes,
                    "published": {"path": str(live / "tool.exe"), "stats": hashes, "companions": ["manual.txt"]}}
        (root / "current.json").write_text(json.dumps(manifest))
        self.ui.reload()
        with patch("shelf_window.deletion_dialog", return_value=True):
            self.right_click_app(str(path)).buttons["Delete item with data"].invoke()
        self.assertFalse(path.exists() or saved.exists() or (root / "current.json").exists())
        self.assertFalse(live.exists())
        self.assertTrue((companions / "manual.txt").exists())

    def test_stale_delete_confirmation_keeps_replacement(self):
        path = Path(self.keys[0])

        def replace(*args, **kwargs):
            if kwargs.get("blocked"):
                return False
            path.write_text(json.dumps({"id": "replacement", "local_path": str(self.dest)}))
            return True

        with patch("shelf_window.deletion_dialog", side_effect=replace):
            self.right_click_app(str(path)).buttons[DELETE].invoke()
        self.assertEqual(json.loads(path.read_text())["id"], "replacement")
        self.assertEqual(self.ui.jobs[str(path)].id, "replacement")

    def test_external_download_folder_and_retry_after_locked_file(self):
        path = Path(self.keys[0])
        job = self.ui.jobs[str(path)]
        root = self.dest / job.id
        root.mkdir(parents=True)
        app = self.dest.parent / "mapped" / "tool.exe"
        app.parent.mkdir()
        app.write_bytes(b"app")
        settings = app.with_suffix(".ini")
        settings.write_text("personal")
        targets = self.ui.targets
        targets.write_text(json.dumps({job.id: str(app)}))
        manifest = root / "current.json"
        manifest.write_text(json.dumps({"release": "a" * 32, "version": "1", "sha256": {},
                                        "published": {"path": str(app)}}))
        original = path.read_bytes()
        full = deletion_plan(FOLDER, path, job, self.dest, targets)
        self.assertEqual(full.folders, (app.parent,))
        with patch("shelf_delete.shutil.rmtree", side_effect=PermissionError("locked")), self.assertRaises(PermissionError):
            delete_item(FOLDER, path, job, original, full, self.dest, targets)
        self.assertTrue(path.exists() and manifest.exists() and settings.exists())
        delete_item(FOLDER, path, job, original, full, self.dest, targets)
        self.assertFalse(app.parent.exists() or path.exists() or manifest.exists())
        self.assertEqual(json.loads(targets.read_text()), {job.id: str(app)})

    def test_delete_with_data_opens_a_dialog_for_local_folders_and_files(self):
        path = Path(self.keys[0])
        folder = self.dest.parent / "portable"
        folder.mkdir()
        payload = folder / "tool.exe"
        payload.touch()
        for target in (folder, payload):
            path.write_text(json.dumps({"id": "local", "local_path": str(target)}))
            self.ui.reload()
            self.ui.select_view("local")
            menu = self.right_click_app(str(path))

            observed = {}

            def dismiss(dialog):
                widgets = list(descendants(dialog))
                buttons = {w.cget("text"): w for w in widgets if isinstance(w, ttk.Button)}
                observed.update(title=dialog.title(), modal=dialog.grab_current() == dialog,
                                buttons=set(buttons), icons=all(b.cget("image") for b in buttons.values()),
                                labels=[w.cget("text") for w in widgets if isinstance(w, ttk.Label)],
                                lists=[w.get("1.0", "end-1c").splitlines() for w in widgets if isinstance(w, tk.Text)])
                buttons["Cancel"].invoke()

            with patch.object(self.root, "wait_window", side_effect=dismiss) as wait:
                menu.buttons["Delete item with data"].invoke()
                wait.assert_called_once()
            self.assertEqual(observed["title"], "Delete entire app folder")
            self.assertTrue(observed["modal"] and observed["icons"])
            self.assertEqual(observed["buttons"], {"Cancel", "Delete item with data"})
            self.assertIn("Everything inside the listed folders will be lost.", "\n".join(observed["labels"]))
            self.assertIn(str(folder), observed["lists"][0])
            self.assertTrue(payload.exists() and path.exists())

    def test_delete_local_folder_with_readonly_settings(self):
        path = Path(self.keys[0])
        folder = self.dest.parent / "local folder"
        folder.mkdir()
        settings = folder / "Settings"
        settings.mkdir()
        config = settings / "config.ini"
        config.touch()
        if sys.platform == "win32":
            for entry in (config, settings, folder):
                entry.chmod(stat.S_IREAD)
        path.write_text(json.dumps({"id": "local", "local_path": str(folder)}))
        self.ui.reload()
        job = self.ui.jobs[str(path)]
        full = deletion_plan(FOLDER, path, job, self.dest, self.ui.targets)
        delete_item(FOLDER, path, job, path.read_bytes(), full, self.dest, self.ui.targets)
        self.assertFalse(folder.exists() or path.exists())

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
            menu.arrow.event_generate("<ButtonPress-1>", x=4, y=4)
            menu.arrow.event_generate("<ButtonRelease-1>", x=4, y=4)
        self.root.update()
        self.assertTrue(menu.winfo_viewable())
        self.assert_dropdown_join(trigger, menu, below=True)
        self.assertEqual(tuple(menu.buttons), ("Reload", "Verify all items"))
        row_style = self.ui.app_menu.buttons["Edit item"].cget("style")
        for button in menu.buttons.values():
            self.assertEqual(button.cget("style"), row_style)
            self.assertEqual(str(button.cget("compound")), "left")
            self.assertIn(button.cget("image")[0], self.root.image_names())
        menu.buttons["Reload"].invoke()
        self.root.update()
        self.assertFalse(menu.winfo_viewable())
        self.assertTrue(trigger.instate(["!selected"]))
        trigger.focus_force()
        with patch("shelf_menu.work_area", return_value=(0, 0, x + 1000, y + trigger.winfo_height())):
            trigger.event_generate("<Down>")
        self.root.update()
        self.assertTrue(menu.winfo_viewable())
        self.assert_dropdown_join(trigger, menu, below=False)
        first = menu.buttons["Reload"]
        second = menu.buttons["Verify all items"]
        self.assertIs(self.root.focus_get(), first)
        px, py = trigger.winfo_pointerxy()
        # Tk can send crossing and motion events as the popup appears under a still pointer.
        for sequence in ("<Enter>", "<Motion>", "<Leave>"):
            second.event_generate(sequence, rootx=px, rooty=py)
            self.root.update()
            self.assertIs(self.root.focus_get(), first)
        second.event_generate("<Motion>", rootx=px + 1, rooty=py)
        self.root.update()
        self.assertIs(self.root.focus_get(), second)
        second.event_generate("<Escape>")
        self.root.update()
        self.assertFalse(menu.winfo_viewable())
        self.assertTrue(trigger.instate(["!selected"]))
        self.assertIs(self.root.focus_get(), trigger)

    def test_dropdowns_indicate_focus_with_a_single_outer_border(self):
        self.ui.options_button.invoke()
        palette = self.ui.palette
        for menu in (self.ui.more_menu, self.ui.options.language_menu):
            button = menu.master
            self.assertEqual(menu.arrow.cget("text"), "▼")
            self.assertEqual(self.ui.style.lookup(menu.arrow.cget("style"), "font"),
                             self.ui.style.lookup("Treeview.Heading", "font"))
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
        self.assertEqual(tree.get_children(), (alpha, beta))
        self.click_header("Status")
        self.assertEqual(tree.get_children(), (beta, alpha))
        with patch("shelf_window.prepare_fetch", return_value=Result("Updated")):
            self.ui.start("fetch", key=alpha)
            self.wait_until_idle()
        self.assertEqual(tree.set(alpha, "Status"), "Updated")
        self.assertEqual(tree.get_children(), (alpha, beta))

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

    @unittest.skipUnless(sys.platform == "win32", "Executable icons require Windows")
    def test_zip_app_icons_use_a_shallow_executable_and_skip_missing_icons(self):
        key, local_key = self.keys
        job = self.ui.jobs[key]
        root = job.cache_root(self.dest)
        release = "1" * 32
        folder = root / "releases" / release
        folder.mkdir(parents=True)
        archive = folder / "bundle.ZIP"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("nested/bin/helper.exe", b"helper")
            package.writestr("app/empty.exe", b"no icon")
            package.write(sys.executable, "app/main.EXE")
        (root / "current.json").write_text(json.dumps({
            "version": "1", "release": release, "archive": archive.name,
            "sha256": {archive.name: hashlib.sha256(archive.read_bytes()).hexdigest()},
        }))
        Path(local_key).write_text(json.dumps({"id": "local", "local_path": str(archive)}))
        from shelf_app_icons import _executable_icon
        expected = _executable_icon(self.root, Path(sys.executable))
        self.assertIsNotNone(expected)
        self.ui.show_app_icons.set(True)
        with patch("shelf_app_icons._executable_icon", wraps=_executable_icon) as extract:
            self.ui.reload()
        self.assertEqual(extract.call_count, 4)
        for item in (key, local_key):
            icon = self.ui.app_images[item]
            self.assertEqual(icon.data(format="png"), expected.data(format="png"))
            self.assertEqual(self.ui.tree.item(item, "image"), (str(icon),))
        self.assertFalse(any(call.args[1].exists() for call in extract.call_args_list))

    def test_catalog_icon_supplies_artwork_for_an_app_without_an_embedded_icon(self):
        key, other = self.keys
        folder = self.ui.catalog / "icons"
        folder.mkdir()
        icon = tk.PhotoImage(master=self.root, width=16, height=16)
        icon.put("#eb3456", to=(0, 0, 16, 16))
        icon.write(str(folder / (Path(key).stem + ".png")), format="png")
        (folder / (Path(other).stem + ".png")).write_bytes(b"incomplete PNG")
        self.ui.show_app_icons.set(True)
        self.ui.reload()
        self.assertEqual(self.ui.app_images[key].get(0, 0), (235, 52, 86))
        self.assertEqual(self.ui.tree.item(key, "image"), (str(self.ui.app_images[key]),))
        self.assertEqual(self.ui.tree.item(other, "image"), (str(self.ui.app_column_icon),))

    @unittest.skipUnless(sys.platform == "win32", "Windows icon resources")
    def test_zip_supplies_packaged_ico_without_an_executable(self):
        from shelf_app_icons import _executable_icon, _zip_icon
        source = Path(__file__).parent / "assets" / "releaseshelf.ico"
        archive = self.ui.catalog / "scripts.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.write(source, "resources/app.ico")
            package.writestr("start.cmd", "echo ready")
        icon = _zip_icon(self.root, archive)
        self.assertEqual(icon.data(format="png"), _executable_icon(self.root, source).data(format="png"))

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

    def test_store_installer_logo_uses_product_id_and_survives_offline_reload(self):
        key = self.keys[0]
        path = Path(key)
        recipe = json.loads(path.read_text())
        product = "9P4CLT2RJ1RS"
        recipe["download"] = f"https://get.microsoft.com/installer/download/{product}"
        path.write_text(json.dumps(recipe))
        self.ui.reload()
        photo = tk.PhotoImage(master=self.root, width=16, height=16)
        photo.put("#ff9900", to=(0, 0, 16, 16))
        png = photo.tk.call(str(photo), "data", "-format", "png")
        images = [{"ImagePurpose": "Screenshot", "Uri": "//store-images.s-microsoft.com/image/screen"},
                  {"ImagePurpose": "Logo", "Uri": "//store-images.s-microsoft.com/image/bee"}]
        catalog = {"Product": {"LocalizedProperties": [{"Images": images}]}}
        with patch("shelf_app_icons.read_page", return_value=json.dumps(catalog)) as lookup, \
             patch("shelf_app_icons.open_url", return_value=io.BytesIO(png)) as download:
            self.ui.show_app_icons.set(True)
            self.ui.change_app_icons()
            until = monotonic() + 5
            while self.ui.store_icon_queue and monotonic() < until:
                self.root.update()
                sleep(0.01)
            self.assertFalse(self.ui.store_icon_queue)
            self.assertEqual(self.ui.app_images[key].get(8, 8), (255, 153, 0))
            self.ui.reload()
            self.assertEqual(self.ui.app_images[key].get(8, 8), (255, 153, 0))
            self.assertEqual(self.ui.tree.item(key, "image"), (str(self.ui.app_images[key]),))
            lookup.assert_called_once_with(
                f"https://displaycatalog.mp.microsoft.com/v7.0/products/{product}?market=US&languages=en-US")
            download.assert_called_once_with("https://store-images.s-microsoft.com/image/bee?w=16&h=16&format=png")

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
        self.assertEqual(right_edge(tree), right_edge(tree.master) - 1)
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

    def test_clipped_catalog_text_refits_and_full_values_remain_searchable(self):
        ui = self.ui
        path = Path(self.keys[0])
        name = "Desktop application with a searchable suffix"
        category = "Long category name " * 5 + "Utilities"
        data = {**json.loads(path.read_text()), "name": name, "category": category}
        path.write_text(json.dumps(data))
        ui.reload()
        self.root.geometry("760x430")
        self.root.deiconify()
        self.root.update()

        tree = ui.tree
        self.assertTrue(tree.set(str(path), "Name").endswith("..."))
        self.assertTrue(ui.category_list.set("category:" + category, "name").endswith("..."))
        ui.search.set("searchable suffix")
        self.assertEqual(tree.get_children(), (str(path),))
        for column in tree["columns"][1:]:
            ui.column_menu.buttons[column].invoke()
        self.root.geometry("1120x680")
        self.root.update()
        self.assertEqual(tree.set(str(path), "Name"), name)
        self.assertEqual(ui.name(str(path)), name)

    def test_hover_reveals_only_clipped_text_in_the_displayed_column(self):
        self.root.geometry("760x430")
        self.root.deiconify()
        tree = self.ui.tree
        tree.configure(displaycolumns=("Status", "Name"))
        self.root.update()
        row = self.keys[0]
        name = "Very long application name " * 10
        values = list(tree.rows[row])
        values[tree["columns"].index("Name")] = name
        tree.set_values(row, values)
        caption = "A very long heading " * 10
        tree.set_caption("Name", caption, " ^")
        x, y, width, height = tree.bbox(row, "Name")
        x += width // 2
        self.assertEqual(tree.clipped_text_at(x, 5), caption + " ^")
        tree.event_generate("<Motion>", x=x, y=y + height // 2)
        self.root.after(650, self.root.quit)
        self.root.mainloop()
        tips = [w for w in self.root.winfo_children()
                if isinstance(w, ttk.Label) and w.winfo_ismapped()]
        self.assertEqual([w["text"] for w in tips], [name])
        tree.event_generate("<Leave>")
        self.assertFalse(tips[0].winfo_exists())
        values[tree["columns"].index("Name")] = "Done..."
        tree.set_values(row, values)
        self.assertIsNone(tree.clipped_text_at(x, y + height // 2))

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
                    self.assertEqual(title.full_text, self.ui.view_title(view))
                    displayed = title.cget("text")
                    if displayed != title.full_text:
                        self.assertTrue(displayed.endswith("..."))
                        self.assertTrue(title.full_text.startswith(displayed[:-3]))
                    self.assertEqual(title.master.winfo_height(), title.winfo_reqheight())
                    self.root.update_idletasks()
                    self.assertEqual((title.winfo_width(), title.winfo_height()), (width, height))

    def test_category_header_stays_one_line_when_resized(self):
        category = "OS Setup and Recovery"
        path = Path(self.keys[0])
        recipe = json.loads(path.read_text())
        path.write_text(json.dumps({**recipe, "category": category}))
        self.ui.reload()
        self.root.deiconify()
        self.ui.select_view(f"category:{category}")
        title = self.ui.title_label
        for width in (1400, 760, 1400):
            self.root.geometry(f"{width}x680")
            self.root.update()
            if width == 760:
                self.assertTrue(title.cget("text").endswith("..."))
                self.assertEqual(title.master.winfo_height(), full_height)
            else:
                self.assertEqual(title.cget("text"), category)
                full_height = title.master.winfo_height()

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
                checklist = self.ui.nav_buttons["checklist"]
                nav_gap = all_apps.winfo_rooty() - (checklist.winfo_rooty() + checklist.winfo_height())
                self.assertEqual(gap, nav_gap)

                toggle.invoke()
                self.root.update()
                self.assertEqual((options.winfo_geometry(), toggle.winfo_geometry()), expanded)

    def test_compact_category_arrows_scroll_when_icons_overflow(self):
        ui = self.ui
        listing = ui.category_list
        up, down = ui.category_scroll_arrows
        self.root.deiconify()
        self.root.update()
        ui.sidebar_toggle.invoke()
        self.root.update()
        self.assertFalse(up.winfo_viewable() or down.winfo_viewable())

        recipe = json.loads(Path(self.keys[0]).read_text())
        for number in range(15):
            recipe.update(id=f"tool-{number}", category=f"Group {number:02}")
            (ui.catalog / f"tool-{number}.json").write_text(json.dumps(recipe))
        ui.reload()
        self.root.geometry("1120x430")
        self.root.update()
        rail_width = ui.sidebar.winfo_width()
        self.assertFalse(ui.category_scrollbar.winfo_viewable())
        self.assertTrue(up.winfo_viewable() and down.winfo_viewable())
        self.assertLessEqual(up.winfo_rooty() + up.winfo_height(), listing.winfo_rooty())
        self.assertLessEqual(listing.winfo_rooty() + listing.winfo_height(), down.winfo_rooty())
        self.assertTrue(up.instate(["disabled"]))
        self.assertTrue(down.instate(["!disabled"]))
        for mode in ("Light", "Dark"):
            ui.mode.set(mode)
            ui.refresh_theme()
            style = up.cget("style")
            for hover in ((), ("active",)):
                disabled = (*up.state(), *hover)
                self.assertNotEqual(ui.style.lookup(style, "foreground", disabled),
                                    ui.style.lookup(style, "foreground", down.state()))
                self.assertEqual(ui.style.lookup(style, "background", disabled), ui.palette.subtle)
        down.invoke()
        self.root.update()
        self.assertGreater(listing.yview()[0], 0)
        self.assertTrue(up.instate(["!disabled"]) and down.instate(["!disabled"]))
        up.invoke()
        self.root.update()
        self.assertEqual(listing.yview()[0], 0)
        listing.yview_moveto(1)
        self.root.update()
        self.assertTrue(down.instate(["disabled"]))
        self.assertTrue(up.instate(["!disabled"]))

        self.root.geometry("1120x900")
        self.root.update()
        self.assertFalse(up.winfo_viewable() or down.winfo_viewable())
        self.assertTrue(up.instate(["disabled"]) and down.instate(["disabled"]))
        self.assertEqual(ui.sidebar.winfo_width(), rail_width)
        self.root.geometry("1120x430")
        ui.sidebar_toggle.invoke()
        self.root.update()
        self.assertFalse(up.winfo_viewable() or down.winfo_viewable())
        self.assertTrue(ui.category_scrollbar.winfo_viewable())

    def test_options_keeps_navigation_gap_at_bottom_after_toggle_scroll_or_resize(self):
        self.root.deiconify()
        listing = self.ui.category_list
        recipe = json.loads(Path(self.keys[0]).read_text())
        for count in (1, 24):
            for number in range(1, count):
                recipe.update(id=f"tool-{number}", category=f"Group {number:02}")
                (self.ui.catalog / f"tool-{number}.json").write_text(json.dumps(recipe))
            self.ui.reload()
            for height in (680, 557, 430):
                with self.subTest(categories=count, height=height):
                    self.root.geometry(f"1120x{height}")
                    self.root.update()
                    options = self.ui.options_button
                    header = self.ui.categories_button
                    header.invoke()
                    self.root.update()
                    self.assertFalse(listing.winfo_viewable())
                    rail = self.ui.sidebar_body.master
                    rail_bottom = rail.winfo_rooty() + rail.winfo_height()
                    tracked = self.ui.nav_buttons["all"]
                    checklist = self.ui.nav_buttons["checklist"]
                    nav_gap = tracked.winfo_rooty() - checklist.winfo_rooty() - checklist.winfo_height()
                    self.assertEqual(rail_bottom - options.winfo_rooty() - options.winfo_height(), nav_gap)
                    header.invoke()
                    self.root.update()
                    self.assertTrue(listing.winfo_viewable())
                    listing.yview_moveto(1)
                    self.root.update()
                    last = listing.get_children()[-1]
                    _, y, _, h = listing.bbox(last)
                    bottom = y + h
                    if count == 24:
                        self.assertEqual(listing.winfo_height(), self.ui.sidebar_body.winfo_height()
                                         - self.ui.sidebar_footer.winfo_height())
                        self.assertLess(listing.winfo_height() - bottom, h)
                        self.assertLessEqual(bottom, listing.winfo_height())
                    else:
                        self.assertEqual(bottom, listing.winfo_height())
                    gap_above = options.winfo_rooty() - (listing.winfo_rooty() + bottom)
                    gap_below = rail_bottom - (options.winfo_rooty() + options.winfo_height())
                    self.assertEqual(listing.winfo_rooty() - (header.winfo_rooty() + header.winfo_height()), 2)
                    self.assertGreaterEqual(gap_above, nav_gap)
                    self.assertEqual(gap_below, nav_gap)
                    bar = self.assert_scrollbar_visible(listing, count == 24)
                    self.assertEqual(right_edge(listing), right_edge(header))
                    if bar.winfo_ismapped():
                        self.assertEqual(right_edge(bar), right_edge(header))
                        self.assertIs(bar.winfo_containing(bar.winfo_rootx() + 1, bar.winfo_rooty() + 1), bar)
                    expected_right = bar.winfo_rootx() if bar.winfo_ismapped() else right_edge(header)
                    x, _, width, _ = listing.bbox(last, "count")
                    self.assertEqual(expected_right - (listing.winfo_rootx() + x + width), 8)

    def test_narrow_sidebar_fits_every_caption_between_its_icon_and_badge(self):
        ui = self.ui
        self.root.deiconify()
        self.root.update()
        for language in ("en", "uk"):
            ui.change_language(language)
            for name, button in ui.nav_buttons.items():
                ui.view_counts[name].set("105")
                button.grid()
            for width in (150, 340):
                ui.panes.sash_place(0, width, 0)
                self.root.update()
                for button, (label, badge) in ui.sidebar_labels.items():
                    with self.subTest(language=language, width=width, label=label):
                        text = button["text"]
                        self.assertTrue(button.winfo_viewable())
                        self.assertLessEqual(button.winfo_reqwidth(), button.winfo_width())
                        self.assertEqual(button.identify(13, button.winfo_height() // 2), "label")
                        if text != tr(label):
                            self.assertEqual(width, 150)
                            self.assertTrue(text.endswith("..."))
                            self.assertTrue(tr(label).startswith(text[:-3]))
                            button.event_generate("<Enter>")
                            self.assertEqual(ui.sidebar_hint["text"], tr(label))
                            button.event_generate("<Leave>")
                        if badge is not None:
                            self.assertLessEqual(right_edge(badge), right_edge(button))
                if width == 150:
                    self.assertTrue(ui.options_button["text"].endswith("..."))
                    if language == "en":
                        self.assertTrue(ui.categories_button["text"].endswith("..."))
                    listing = ui.category_list
                    for row in listing.get_children():
                        self.assertTrue(listing.set(row, "name").strip("."))
                        self.assertEqual(listing.set(row, "count"), "2")

    def test_sidebar_category_ellipsis_at_text_boundary(self):
        listing = self.ui.category_list
        name = "Virtualization"
        path = Path(self.keys[0])
        path.write_text(json.dumps({**json.loads(path.read_text()), "category": name}))
        self.ui.reload()
        self.root.deiconify()
        self.root.update()
        row = "category:" + name
        font = self.ui.style.lookup("Categories.Treeview", "font")
        measure = lambda text: int(listing.tk.call("font", "measure", font, "-displayof", listing, text))
        panes = self.ui.panes
        # Each cell has a four-pixel Tk inset on either side.
        fit_at = panes.sash_coord(0)[0] - listing.column("name", "width") + measure(name) + 8
        for delta in (0, -1, 0):
            panes.sash_place(0, fit_at + delta, 0)
            self.root.update()
            self.assertEqual(listing.set(row, "name") == name, delta == 0)
            if delta < 0:
                self.assertTrue(listing.set(row, "name").endswith("..."))
            self.assertEqual(listing.set(row, "count"), "1")
        self.assertEqual(listing.column("count", "width"), measure("1") + 8)

    def test_category_count_column_grows_for_two_digits(self):
        listing = self.ui.category_list
        initial_width = listing.column("count", "width")
        recipe = json.loads(Path(self.keys[0]).read_text())
        for number in range(8):
            recipe["id"] = f"tool-{number}"
            (self.ui.catalog / f"tool-{number}.json").write_text(json.dumps(recipe))
        self.ui.reload()
        self.root.deiconify()
        self.root.update()
        category, = listing.get_children()
        self.assertEqual(listing.set(category, "count"), "10")
        x, _, width, _ = listing.bbox(category, "count")
        self.assertGreater(width, initial_width)
        self.assertLessEqual(x + width, listing.winfo_width())

        recipe.update(id="long-category", category="A long category name " * 4)
        (self.ui.catalog / "long-category.json").write_text(json.dumps(recipe))
        self.ui.reload()
        self.root.update()
        self.assertEqual(listing.xview(), (0.0, 1.0))
        x, _, width, _ = listing.bbox(category, "count")
        self.assertEqual(listing.winfo_width() - (x + width), 8)

    def test_category_picker_shares_one_outline_in_both_directions(self):
        window = tk.Toplevel(self.root)
        window.overrideredirect(True)
        field = category_picker(window, tk.StringVar(window), tuple(range(12)), width=36)
        field.pack()
        call = field.tk.call
        popup = f"{field}.popdown"
        for below, y in ((True, 100), (False, window.winfo_screenheight() - 100)):
            with self.subTest(below=below):
                window.geometry(f"+100+{y}")
                window.update()
                field.event_generate("<ButtonPress-1>", x=field.winfo_width() - 15, y=12)
                window.update()
                popup_y = call("winfo", "rooty", popup)
                height = call("winfo", "height", popup)
                self.assertEqual(popup_y if below else popup_y + height - 1,
                                 field.winfo_rooty() + (field.winfo_height() - 1 if below else 0))
                self.assertEqual(call("winfo", "width", popup), field.winfo_width())
                self.assertEqual(call("winfo", "rooty", f"{popup}.f") - popup_y, 0 if below else 1)
                call("ttk::combobox::Unpost", field)
                window.update()

    def test_edit_item_category_picker_lists_catalog_and_accepts_new_names(self):
        first, second = map(Path, self.keys)
        for path, category in ((first, "Utilities"), (second, "audio")):
            data = json.loads(path.read_text())
            path.write_text(json.dumps({**data, "category": category}))
        self.root.deiconify()
        self.ui.open_editor(first)
        editor = self.ui.editor
        combo = next(w for w in descendants(editor) if isinstance(w, ttk.Combobox))
        self.assertEqual(combo["values"], ("audio", "Utilities"))
        self.assertEqual(combo.get(), "Utilities")
        self.root.update()
        combo.tk.call("ttk::combobox::Post", combo)
        self.root.update()
        listing = f"{combo}.popdown.f.l"
        self.assertEqual(combo.tk.call(listing, "cget", "-background"), self.ui.palette.surface)
        self.assertEqual(combo.tk.call(listing, "cget", "-selectbackground"), self.ui.palette.selection)
        combo.tk.call("event", "generate", listing, "<Up>")
        combo.tk.call("event", "generate", listing, "<Return>")
        self.assertEqual(editor.form_data()["category"], "audio")
        combo.set("New category")
        editor.save()
        self.assertEqual(json.loads(first.read_text())["category"], "New category")

    def test_move_and_rename_categories_from_context_menus(self):
        ui = self.ui
        first, second = self.keys
        local = ui.catalog / "local.json"
        local.write_text(json.dumps({"id": "local", "local_path": ".", "category": "Tools", "extra": 42}))
        ui.reload()
        ui.results[first] = Result("Latest")

        self.right_click_app(first).buttons[MOVE].invoke()
        dialog = self.root.grab_current()
        self.assertIsInstance(dialog, CategoryDialog)
        combo = next(w for w in descendants(dialog) if isinstance(w, ttk.Combobox))
        self.assertIn("Tools", combo["values"])
        dialog.value.set("Tools")
        dialog.save()
        self.assertEqual(ui.tree.set(first, "Category"), "Tools")
        self.assertEqual(ui.results[first].status, "Latest")

        listing = ui.category_list
        self.root.update()
        x, y, width, height = listing.bbox("category:Tools", "name")
        listing.event_generate("<Button-3>", x=x + width // 2, y=y + height // 2)
        self.root.update()
        self.assertTrue(ui.category_menu.winfo_viewable())
        ui.category_menu.buttons[RENAME].invoke()
        dialog = self.root.grab_current()
        self.assertEqual(dialog.value.get(), "Tools")
        ui.search.set("alpha")
        dialog.value.set("Utilities")
        dialog.save()
        self.root.update()
        self.assertEqual(ui.view.get(), "category:Utilities")
        self.assertEqual(ui.tree.get_children(), (first,))
        self.assertEqual(listing.set(ui.view.get(), "count"), "2")
        self.assertEqual(json.loads(local.read_text()), {
            "id": "local", "local_path": ".", "category": "Utilities", "extra": 42})
        self.assertNotIn("category", json.loads(Path(second).read_text()))

        self.right_click_app(first).buttons[MOVE].invoke()
        dialog = self.root.grab_current()
        dialog.value.set("")
        dialog.save()
        self.assertNotIn("category", json.loads(Path(first).read_text()))
        self.assertEqual(listing.set("category:Utilities", "count"), "1")

    def test_category_rename_detects_stale_members_and_rolls_back_failed_write(self):
        paths = [Path(key) for key in self.keys]
        originals = {path: path.read_bytes() for path in paths}
        members = {path: path.stem for path in paths}
        with self.assertRaises(ValueError):
            change_category(self.ui.catalog, {paths[0]: paths[0].stem}, "", "Tools", rename=True)

        replace = Path.replace
        def fail_second(staged, target):
            if target == paths[1]:
                raise PermissionError("File is in use")
            return replace(staged, target)

        with patch.object(Path, "replace", fail_second), self.assertRaises(PermissionError):
            change_category(self.ui.catalog, members, "", "Tools", rename=True)
        self.assertEqual(originals, {path: path.read_bytes() for path in paths})

    def test_main_window_restores_size_and_position_on_reopen(self):
        def reopen():
            window = tk.Toplevel(self.root)
            ui = ShelfWindow(window, self.ui.catalog, self.dest, self.ui.targets, appearance_file=self.appearance)
            window.update()
            return ui

        ui = reopen()
        geometry = "870x510+81+72"
        ui.root.geometry(geometry)
        ui.root.update()
        ui.close()
        ui = reopen()
        self.assertEqual(ui.root.geometry(), geometry)
        if sys.platform == "win32":
            ui.root.state("zoomed")
            ui.root.update()
            ui.close()
            ui = reopen()
            self.assertEqual(ui.root.state(), "zoomed")
            ui.root.state("normal")
            ui.root.update()
            self.assertEqual(ui.root.geometry(), geometry)
        ui.close()

    def test_category_expansion_survives_restart(self):
        ui = self.ui
        self.root.deiconify()
        self.root.update()
        self.assertTrue(ui.category_list.winfo_viewable())
        for expanded, angle in ((False, 90), (True, 0)):
            if expanded:
                ui.categories_button.invoke()
            else:
                ui.categories_arrow.event_generate("<ButtonPress-1>", x=5, y=5)
                ui.categories_arrow.event_generate("<ButtonRelease-1>", x=5, y=5)
            window = tk.Toplevel(self.root)
            self.addCleanup(window.destroy)
            ui = ShelfWindow(window, ui.catalog, self.dest, ui.targets, appearance_file=self.appearance)
            self.root.update()
            self.assertEqual(bool(ui.category_list.winfo_viewable()), expanded)
            self.assertEqual(ui.categories_button.cget("text"), tr("Categories"))
            arrow = ui.categories_arrow
            self.assertEqual(arrow.itemcget(ui.arrow_glyph, "text"), "▼")
            self.assertEqual(float(arrow.itemcget(ui.arrow_glyph, "angle")), angle)
            self.assertEqual(arrow.itemcget(ui.arrow_glyph, "font"), ui.style.lookup("Treeview.Heading", "font"))

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
        # This test checks the full category captions in the resizable sidebar.
        ui.panes.sash_place(0, 300, 0)
        self.root.update()
        listing = ui.category_list
        hardware, unassigned = listing.get_children()
        self.assertEqual(listing.set(hardware, "name"), "Hardware")
        self.assertEqual(listing.set(hardware, "count"), "1")
        self.assertEqual(listing.set(unassigned, "name"), "Uncategorized")
        self.assertEqual(listing.item(hardware, "tags"), ("cpu",))
        self.assertEqual(listing.item(unassigned, "tags"), ("inbox",))
        self.assertTrue(listing.tag_configure("cpu", "image"))
        x, y, width, height = listing.bbox(hardware, "#0")
        listing.event_generate("<ButtonPress-1>", x=x + width // 2, y=y + height // 2)
        listing.event_generate("<ButtonRelease-1>", x=x + width // 2, y=y + height // 2)
        self.root.update()
        self.assertEqual(ui.tree.get_children(), (first,))
        self.assertEqual(ui.page_title.get(), "Hardware")
        self.assertEqual(ui.icon_widgets[ui.title_icon][0], "cpu")
        header = ui.categories_button
        local = ui.nav_buttons["local"]
        self.assertEqual(header.cget("style"), local.cget("style"))
        self.assertEqual(header.winfo_height(), local.winfo_reqheight())
        last_nav = [button for button in ui.nav_buttons.values() if button.winfo_viewable()][-1]
        self.assertEqual(header.winfo_rooty(), last_nav.winfo_rooty() + last_nav.winfo_height() + 4)
        header_position = (header.winfo_rootx(), header.winfo_rooty())
        self.assertEqual(listing.winfo_rootx() + x, header.winfo_rootx())
        header.invoke()
        ui.refresh_categories()
        self.root.update()
        self.assertFalse(listing.winfo_viewable())
        self.assertEqual((header.winfo_rootx(), header.winfo_rooty()), header_position)
        self.assertEqual(ui.view.get(), hardware)
        header.invoke()
        self.root.update()
        self.assertTrue(listing.winfo_viewable())
        self.assertEqual((header.winfo_rootx(), header.winfo_rooty()), header_position)
        self.assertEqual(listing.selection(), (hardware,))
        ui.search.set("beta")
        self.assertFalse(ui.tree.get_children())
        self.assertEqual(ui.empty_title.get(), "No matching items")
        ui.sidebar_toggle.invoke()
        self.root.update()
        compact_width = ui.sidebar.winfo_width()
        self.assertTrue(listing.winfo_viewable())
        self.assertFalse(listing["displaycolumns"])
        self.assertFalse(ui.categories_arrow.winfo_viewable())
        self.assertTrue(header.instate(["selected"]))
        for visible in (False, True):
            header.invoke()
            self.root.update()
            self.assertEqual(bool(listing.winfo_viewable()), visible)
            self.assertFalse(ui.categories_arrow.winfo_viewable())
            self.assertEqual(header.instate(["selected"]), visible)
            self.assertTrue(ui.sidebar_compact)
            self.assertEqual(ui.sidebar.winfo_width(), compact_width)
        self.assertEqual(listing.selection(), (hardware,))
        ui.select_view("all")
        self.assertTrue(header.instate(["selected"]))
        x, y, width, height = listing.bbox(unassigned, "#0")
        x, y = x + width // 2, y + height // 2
        self.assertEqual(listing.clipped_text_at(x, y), "Uncategorized")
        listing.event_generate("<ButtonPress-1>", x=x, y=y)
        listing.event_generate("<ButtonRelease-1>", x=x, y=y)
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

    def test_sidebar_count_matches_button_through_selection(self):
        self.ui.results[self.keys[1]] = Result("Pending")
        self.ui.refresh_view()
        self.root.deiconify()
        self.root.update()
        button = self.ui.nav_buttons["attention"]
        count = self.ui.nav_counts["attention"]

        def background(widget):
            return self.ui.style.lookup(widget.cget("style"), "background", widget.state())

        for target in (button, count):
            self.ui.nav_buttons["all"].invoke()
            target.event_generate("<Enter>")
            target.event_generate("<ButtonPress-1>")
            self.root.update()
            self.assertTrue(button.instate(["pressed", "!selected"]))
            self.assertEqual(background(count), background(button))
            target.event_generate("<ButtonRelease-1>")
            self.root.update()
            self.assertTrue(button.instate(["!pressed", "selected"]))
            self.assertEqual(background(count), background(button))
            target.event_generate("<Leave>")

        self.ui.nav_buttons["all"].invoke()
        button.event_generate("<<Invoke>>")
        self.root.update()
        self.assertTrue(count.instate(["selected", "!pressed"]))
        self.assertEqual(background(count), background(button))

    def test_sidebar_drag_resizes_app_list_and_survives_collapse(self):
        self.root.deiconify()
        self.root.geometry("1120x680")
        self.root.update()
        panes, sidebar = self.ui.panes, self.ui.sidebar

        def drag(distance):
            x, y = panes.sash_coord(0)
            y += 20
            panes.event_generate("<ButtonPress-1>", x=x, y=y)
            panes.event_generate("<B1-Motion>", x=x + distance, y=y)
            panes.event_generate("<ButtonRelease-1>", x=x + distance, y=y)
            self.root.update()

        def reopen():
            window = tk.Toplevel(self.root)
            self.addCleanup(window.destroy)
            ui = ShelfWindow(window, self.ui.catalog, self.dest, self.ui.targets, appearance_file=self.appearance)
            self.root.update()
            return ui

        self.assertEqual(int(panes.cget("sashwidth")), 1)
        old_width = sidebar.winfo_width()
        old_list_width = self.ui.tree.winfo_width()
        grip = self.ui.sidebar_grip
        grip.event_generate("<ButtonPress-1>", x=2, y=20)
        grip.event_generate("<B1-Motion>", x=102, y=20)
        grip.event_generate("<ButtonRelease-1>", x=102, y=20)
        self.root.update()
        self.assertGreater(sidebar.winfo_width(), old_width)
        self.assertLess(self.ui.tree.winfo_width(), old_list_width)
        expanded_width = sidebar.winfo_width()
        reopened = reopen()
        self.assertFalse(reopened.sidebar_compact)
        self.assertEqual(reopened.sidebar.winfo_width(), expanded_width)
        self.ui.sidebar_toggle.invoke()
        self.root.update()
        self.assertLess(sidebar.winfo_width(), old_width)
        reopened = reopen()
        self.assertTrue(reopened.sidebar_compact)
        self.assertEqual(reopened.sidebar.winfo_width(), sidebar.winfo_width())
        reopened.sidebar_toggle.invoke()
        self.root.update()
        self.assertEqual(reopened.sidebar.winfo_width(), expanded_width)
        self.ui.sidebar_toggle.invoke()
        self.root.update()
        self.assertEqual(sidebar.winfo_width(), expanded_width)
        self.root.geometry("760x430")
        self.root.update()
        drag(1000)
        self.assertGreaterEqual(self.ui.table_area.master.winfo_width(), 440)
        drag(-1000)
        self.assertEqual(sidebar.winfo_width(), 150)
        self.assertEqual(reopen().sidebar.winfo_width(), 150)

    def test_sidebar_toggle_reclaims_space_and_restores_labels_and_counts(self):
        self.root.deiconify()
        self.ui.results[self.keys[1]] = Result("Pending")
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
        for view, badge in self.ui.nav_counts.items():
            self.assertEqual(bool(badge.winfo_viewable()), view not in ("progress", "disabled", "local"))
            self.assertEqual(badge.place_info(), placements[badge])
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

            self.ui.select_view("attention")
            tree.selection_set(invalid)
            tree.event_generate("<Shift-F10>")
            popup.assert_called_once()
            for label in ("Get updates", "Verify files"):
                self.assertTrue(menu.buttons[label].instate(["disabled"]))
            self.assertTrue(menu.buttons["Edit item"].instate(["!disabled"]))
            with patch.object(self.ui, "open_editor") as edit:
                menu.buttons["Edit item"].invoke()
                edit.assert_called_once_with(Path(invalid))

            self.ui.select_view("all")
            tree.selection_set(valid)
            self.ui.busy = True
            tree.event_generate("<Shift-F10>")
            self.assertEqual(popup.call_count, 2)
            for label in ("Get updates", "Verify files", "Edit item"):
                self.assertTrue(menu.buttons[label].instate(["disabled"]))

    def test_keyboard_app_menu_navigation_and_dismissal(self):
        self.root.deiconify()
        tree, menu = self.ui.tree, self.ui.app_menu
        tree.focus_force()
        self.root.update()
        tree.event_generate("<Shift-F10>")
        self.root.update()
        first, second = menu.buttons["Get updates"], menu.buttons["Verify files"]
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
        labels = {"Get updates", "Verify files"}
        for widget in descendants(self.root):
            if widget.winfo_toplevel() != self.root:
                continue
            if isinstance(widget, ttk.Button):
                self.assertNotIn(widget.cget("text"), labels | {"Check all items", "Check for updates"})
            elif isinstance(widget, tk.Text):
                self.assertNotIn(str(self.dest / "alpha"), widget.get("1.0", "end"))
        editor = self.edit_selected()
        tabs = next(w for w in descendants(editor) if isinstance(w, ttk.Notebook))
        self.assertEqual(tabs.tab(tabs.select(), "text"), "General")
        self.assertEqual([tabs.tab(tab, "text") for tab in tabs.tabs()],
                         ["General", "Download options", "Source", "Item info"])
        self.assertEqual({button.cget("text") for button in editor.buttons.values()}, labels)
        detail = editor.detail.get("1.0", "end")
        self.assertIn("https://publisher.example", detail)
        self.assertIn(str(self.dest / "alpha"), detail)
        self.assertIn("Unknown", detail)
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

    def test_app_table_shows_partial_rows_on_resize_and_fits_filtered_results(self):
        recipe = json.loads(Path(self.keys[0]).read_text())
        for number in range(24):
            recipe["id"] = f"tool-{number}"
            (self.ui.catalog / f"tool-{number}.json").write_text(json.dumps(recipe))
        self.ui.reload()
        self.root.deiconify()
        tree = self.ui.tree
        card = tree.master.master
        for height, query in ((680, ""), (681, ""), (680, ""), (430, ""), (800, "tool-23"), (800, "")):
            with self.subTest(height=height, query=query):
                self.root.geometry(f"1120x{height}")
                self.ui.search.set(query)
                self.root.update()
                self.assert_scrollbar_visible(tree, not query)
                if not query:
                    self.assertEqual(card.winfo_height(), self.ui.table_area.winfo_height())
                    tree.yview_moveto(0)
                    self.root.update()
                    bottom = tree.winfo_height()
                    _, top, _, row_height = tree.bbox(tree.get_children()[0])
                    visible_pixels = (bottom - top) % row_height
                    if visible_pixels:
                        item = tree.get_children()[(bottom - top) // row_height]
                        self.assertEqual(tree.bbox(item)[1], bottom - visible_pixels)
                tree.yview_moveto(1)
                self.root.update()
                _, y, _, row_height = tree.bbox(tree.get_children()[-1])
                self.assertEqual(row_height, int(self.ui.style.lookup("Treeview", "rowheight")))
                gap = tree.winfo_height() - (y + row_height)
                self.assertGreaterEqual(gap, 0)
                self.assertLess(gap, row_height)
                if query:
                    self.assertEqual(gap, 0)

    def test_scrollbar_alignment_and_visibility_on_resize(self):
        self.root.deiconify()
        tree = self.ui.tree
        self.assert_scrollbar_visible(tree, False)
        card = tree.master.master
        self.assertEqual(right_edge(tree), right_edge(card) - 1)
        right_margin = right_edge(self.root) - right_edge(tree)
        extra = [tree.insert("", "end") for _ in range(80)]
        table_bar = self.assert_scrollbar_visible(tree, True)
        self.assertEqual(right_edge(table_bar), right_edge(card))
        tree.delete(*extra)
        self.assert_scrollbar_visible(tree, False)
        self.assertEqual(right_edge(tree), right_edge(card) - 1)

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
                    self.ui.select_view("attention")
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
            reply = io.BytesIO(b"fixture")
            reply.headers = {"Content-Length": "7"}
            with patch("fetch_sources.open_url", return_value=reply):
                self.click(editor.buttons["fetch"])
            page.assert_called_once()
        live = self.dest / "alpha" / "current" / "alpha.bin"
        self.assertEqual(live.read_bytes(), b"fixture")
        live.write_bytes(b"damaged")
        with patch("fetch_sources.open_url") as network:
            self.click(editor.buttons["verify"])
            self.assertIn("SHA-256 mismatch", editor.detail.get("1.0", "end"))
            self.assertEqual(self.ui.results[self.keys[1]].status, "Unknown")
            editor.destroy()
            self.ui.more_menu.buttons["Verify all items"].invoke()
            self.wait_until_idle()
            network.assert_not_called()
        self.assertEqual(self.ui.results[self.keys[1]].status, "Repair needed")
        self.assertIn("2 of 2 processed", self.ui.status.get())

    def test_update_all_shares_three_retries_across_check_and_download(self):
        from fetch_sources import Recipe

        checks = {"alpha": 0, "beta": 0}
        downloads = {"alpha": 0, "beta": 0}
        retry_labels = {key: [] for key in self.keys}
        paint = self.ui.paint

        def record_row(key):
            paint(key)
            label = self.ui.tree.set(key, "Status")
            if label.startswith("Retry ") and label not in retry_labels[key][-1:]:
                retry_labels[key].append(label)

        resolve = Recipe.resolve

        def check(job):
            checks[job.id] += 1
            if job.id == "alpha" and checks[job.id] == 1:
                raise ValueError("Missing release version")
            return resolve(job)

        def download(url):
            name = Path(url).stem
            downloads[name] += 1
            if name == "beta" or downloads[name] < 3:
                raise OSError(f"Transfer failed on download {downloads[name]}")
            return download_fixture(url)

        with (patch("shelf_window.UPDATE_RETRY_DELAYS", (0, 0, 0)),
              patch.object(self.ui, "paint", side_effect=record_row),
              patch.object(Recipe, "resolve", check),
              patch("fetch_sources.read_page", return_value="1"),
              patch("fetch_sources.open_url", side_effect=download)):
            self.click(self.ui.all_buttons["fetch"])

        alpha, beta = (self.ui.results[key] for key in self.keys)
        self.assertEqual(checks, {"alpha": 4, "beta": 4})
        self.assertEqual(downloads, {"alpha": 3, "beta": 4})
        for key, result in zip(self.keys, (alpha, beta)):
            self.assertEqual(retry_labels[key], ["Retry 1/3", "Retry 2/3", "Retry 3/3"])
            self.assertEqual(self.ui.tree.set(key, "Status"), result.status)
        self.assertEqual(alpha.status, "Updated")
        self.assertEqual((self.dest / "alpha" / "current" / "alpha.bin").read_bytes(), b"fixture")
        self.assertEqual(beta.status, "Error")
        self.assertIn("Transfer failed on download 4", beta.detail)

    def test_cancel_update_all_during_retry_wait(self):
        with (patch("shelf_window.UPDATE_RETRY_DELAYS", (60, 60, 60)),
              patch("fetch_sources.read_page", side_effect=OSError("Offline")) as read):
            self.ui.all_buttons["fetch"].invoke()
            self.wait_for(lambda: all(self.ui.tree.set(key, "Status") == "Retry 1/3" for key in self.keys))
            self.click(self.ui.all_buttons["fetch"])
            self.assertEqual(read.call_count, 2)
        self.assertIn("2 canceled", self.ui.status.get())
        self.assertTrue(all(self.ui.tree.set(key, "Status") == "Unknown" for key in self.keys))

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
            responses = {name: GatedDownload() for name in names}
            def open_download(url):
                return responses[Path(url).stem]

            self.root.deiconify()
            with patch("fetch_sources.open_url", side_effect=open_download) as download:
                queued = str(self.ui.catalog / "gamma.json")
                self.ui.all_buttons["fetch"].invoke()
                try:
                    self.assertEqual(self.ui.tree.set(queued, "Status"), "Pending")
                    self.ui.nav_buttons["progress"].invoke()
                    self.wait_for(lambda: all(responses[name].waiting.is_set() for name in names[:3])
                                  and all(bar.winfo_ismapped() for bar in self.ui.row_bars))
                    self.assertEqual(download.call_count, 3)
                    self.assertEqual(self.ui.tree.set(queued, "Status"), "Pending")
                    self.assertEqual(self.ui.view_counts["attention"].get(), "0")
                    self.assertEqual({Path(key).stem for key in self.ui.tree.get_children()}, set(names[:3]))
                    self.assertIn("1 of 4 downloads", self.ui.status.get())
                    responses["delta"].resume.set()
                    self.wait_for(lambda: responses["gamma"].waiting.is_set() and
                                  {Path(key).stem for key in self.ui.transfers} == {"amber", "epsilon", "gamma"})
                    responses["amber"].resume.set()
                    responses["epsilon"].resume.set()
                    self.wait_for(lambda: "4 of 4 downloads" in self.ui.status.get())
                finally:
                    for response in responses.values():
                        response.resume.set()
                    self.wait_until_idle()
            self.assertCountEqual([call.args[0] for call in download.call_args_list],
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
                self.wait_for(lambda: response.waiting.is_set() and self.ui.transfers)
                self.assertEqual(set(self.ui.transfers), {self.keys[1]})
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
        row_bar, tree, key = self.ui.row_bars[0], self.ui.tree, self.keys[0]

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
                row_bar = self.ui.row_bars[0]
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

    def test_source_error_highlight_follows_theme_and_clears_after_retry(self):
        editor = self.edit_selected("Item info")
        row = self.keys[0]
        with patch("fetch_sources.read_page", return_value="missing version"):
            self.click(editor.buttons["fetch"])
        self.assertEqual(self.ui.results[row].status, "Source error")
        self.assertTrue(self.ui.tree.tag_has("error", row))
        start, end = editor.detail.tag_ranges("error")
        self.assertEqual(editor.detail.get(start, end).strip(), tr(self.ui.results[row].detail))

        for mode in ("Dark", "Light"):
            self.ui.mode.set(mode)
            self.ui.refresh_theme()
            color = self.ui.palette.error
            self.assertEqual(str(self.ui.tree.tag_configure("error", "foreground")), color)
            self.assertEqual(editor.detail.tag_cget("error", "foreground"), color)

        with patch("fetch_sources.read_page", return_value="1"), patch("fetch_sources.open_url", side_effect=download_fixture):
            self.click(editor.buttons["fetch"])
        self.assertFalse(self.ui.tree.tag_has("error", row))
        self.assertFalse(editor.detail.tag_ranges("error"))

    def test_editor_drafts_and_closing_during_an_update(self):
        editor = self.edit_selected()
        path = Path(self.keys[0])
        saved = path.read_bytes()
        with patch("fetch_sources.read_page", return_value="1") as page, patch("fetch_sources.open_url", side_effect=download_fixture):
            editor.fields["id"].set("draft")
            editor.buttons["fetch"].invoke()
            self.assertFalse(self.ui.busy)
            page.assert_not_called()
            self.assertIn("save", editor.error.get().lower())
            self.assertEqual(editor.fields["id"].get(), "draft")

            editor.fields["id"].set("alpha")
            editor.buttons["fetch"].invoke()
            self.assertTrue(self.ui.busy)
            self.assertTrue(all(b.instate(["disabled"]) for b in editor.buttons.values()))
            editor.save()
            self.assertTrue(editor.winfo_exists())
            self.assertEqual(path.read_bytes(), saved)
            editor.destroy()
            self.wait_until_idle()
            page.assert_called_once()
        self.assertEqual(self.ui.tree.set(self.keys[0], "Status"), "Updated")
        self.assertIn("Updated", self.edit_selected().detail.get("1.0", "end"))

    def test_update_all_without_a_selection(self):
        self.ui.tree.selection_remove(*self.ui.tree.selection())
        self.root.update()
        with patch("fetch_sources.read_page", return_value="1") as page, patch("fetch_sources.open_url", side_effect=download_fixture):
            self.ui.all_buttons["fetch"].invoke()
            self.assertTrue(self.ui.busy)
            self.assertEqual(self.ui.all_buttons["fetch"].cget("text"), "Cancel")
            self.assertTrue(self.ui.all_buttons["fetch"].instate(["!disabled"]))
            self.assertTrue(self.ui.edit_button.instate(["disabled"]))
            for label in ("Reload", "Verify all items"):
                self.assertTrue(self.ui.more_menu.buttons[label].instate(["disabled"]))
            self.wait_until_idle()
            self.assertEqual(page.call_count, 2)
        self.assertEqual([self.ui.results[k].status for k in self.keys],
                         ["Updated", "Updated"])
        self.assertEqual(self.ui.tree.selection(), ())

    def test_cancel_bulk_work_stops_active_app_and_skips_rest(self):
        self.root.deiconify()
        self.root.update()
        previous = self.ui.results.copy()
        publisher = GatedPage()
        with (patch("fetch_sources.read_page", side_effect=publisher) as page,
              patch("fetch_sources.open_url") as download):
            button = self.ui.all_buttons["fetch"]
            original_size = button.winfo_width(), button.winfo_height()
            button.invoke()
            try:
                self.wait_for(lambda: page.call_count == 2)
                self.root.update_idletasks()
                self.assertEqual(button.cget("text"), "Cancel")
                self.assertEqual((button.winfo_width(), button.winfo_height()), original_size)
                self.assertTrue(button.instate(["!disabled"]))
                button.invoke()
                self.wait_until_idle()
            finally:
                publisher.resume.set()
                self.wait_until_idle()
            self.assertEqual(page.call_count, 2)
            download.assert_not_called()
        self.assertEqual(self.ui.results, previous)
        self.assertEqual(button.cget("text"), "Update all items")
        self.assertEqual((button.winfo_width(), button.winfo_height()), original_size)
        self.assertTrue(all(b.instate(["!disabled"]) for b in self.ui.all_buttons.values()
                            if b.winfo_ismapped()))
        self.assertIn("0 of 2 processed, 2 canceled", self.ui.status.get())
        self.assertEqual(self.ui.view_counts["progress"].get(), "0")

    def test_more_menu_cancels_bulk_verification_and_keeps_its_size(self):
        from shelf_cancel import check_canceled

        self.root.deiconify()
        self.ui.change_language("fr")
        self.root.update()
        trigger, menu = self.ui.more_button, self.ui.more_menu
        verify, reload_apps = menu.buttons["Verify all items"], menu.buttons["Reload"]
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
                self.wait_for(lambda: self.keys[0] in self.ui.active_items and worker.called)
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
            previous = self.ui.results[alpha]
            blocked = GatedDownload()
            second = io.BytesIO(b"second")
            second.headers = {}
            with patch("fetch_sources.open_url", side_effect=lambda url: blocked if url.endswith("alpha.bin") else second):
                self.ui.all_buttons["fetch"].invoke()
                try:
                    self.wait_for(lambda: blocked.waiting.is_set() and self.ui.transfers)
                    self.ui.tree.selection_set(beta)
                    menu = self.right_click_app(alpha, cancel_for="Get updates")
                    button = menu.buttons["Get updates"]
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

    def test_right_click_cancels_queued_app_and_single_update(self):
        alpha = self.keys[0]
        for name in ("delta", "gamma"):
            (self.ui.catalog / f"{name}.json").write_text(Path(alpha).read_text().replace("alpha", name))
        self.ui.reload()
        queued = str(self.ui.catalog / "gamma.json")
        for bulk in (True, False):
            with self.subTest(bulk=bulk):
                publisher = GatedPage()
                previous = self.ui.results.copy()
                with patch("fetch_sources.read_page", side_effect=publisher) as page, patch("fetch_sources.open_url", side_effect=download_fixture):
                    if bulk:
                        self.ui.all_buttons["fetch"].invoke()
                    else:
                        self.right_click_app(queued).buttons["Get updates"].invoke()
                    try:
                        expected_calls = 3 if bulk else 1
                        self.wait_for(lambda: page.call_count == expected_calls
                                      and len(self.ui.active_items) == expected_calls)
                        if bulk:
                            self.assertNotIn(queued, self.ui.active_items)
                            self.assertEqual(self.ui.tree.set(queued, "Status"), "Pending")
                        if not bulk:
                            menu = self.right_click_app(alpha)
                            self.assertTrue(all(button.instate(["disabled"]) for button in menu.buttons.values()))
                            menu.dismiss()
                        menu = self.right_click_app(queued, cancel_for="Get updates")
                        button = menu.buttons["Get updates"]
                        self.assertTrue(button.instate(["!disabled"]))
                        button.invoke()
                    finally:
                        publisher.resume.set()
                        self.wait_until_idle()
                    self.assertEqual(page.call_count, expected_calls)
                if bulk:
                    self.assertEqual(self.ui.results[alpha].status, "Updated")
                else:
                    self.assertEqual(self.ui.results[alpha], previous[alpha])
                self.assertEqual(self.ui.results[queued], previous[queued])
                self.assertEqual(self.ui.tree.set(queued, "Status"), "Unknown")
                expected = "3 of 4 processed" if bulk else "0 of 1 processed"
                self.assertIn(f"{expected}, 1 canceled", self.ui.status.get())

    def test_right_click_cancel_replaces_the_requested_action_and_restores_its_slot(self):
        key = self.keys[0]
        self.ui.change_language("fr")
        for label, worker in (("Get updates", "shelf_window.prepare_fetch"),
                              ("Verify files", "shelf_window.execute")):
            with self.subTest(action=label):
                entered, resume = Event(), Event()

                def run(*args, cancel_event, **kwargs):
                    entered.set()
                    if not resume.wait(10):
                        raise TimeoutError("The test did not release the worker")
                    return Result("Canceled" if cancel_event.is_set() else "Verified")

                menu = self.right_click_app(key)
                button = menu.buttons[label]
                original_geometry = button.winfo_geometry()
                original_icon = menu.icon_names[button]
                previous = self.ui.results[key]
                with patch(worker, side_effect=run) as operation:
                    button.invoke()
                    try:
                        self.wait_for(lambda: entered.is_set() and key in self.ui.active_items)
                        # An update begins by checking the publisher, but Get updates owns its cancel action.
                        if label == "Get updates":
                            self.assertEqual(self.ui.active_items[key][0], "check")
                        menu = self.right_click_app(key, cancel_for=label)
                        self.assertEqual(menu.icon_names[button], "cancel")
                        self.assertEqual(button.winfo_geometry(), original_geometry)
                        self.assertTrue(button.instate(["!disabled"]))
                        button.invoke()
                    finally:
                        resume.set()
                        self.wait_until_idle()
                    operation.assert_called_once()
                self.assertEqual(self.ui.results[key], previous)
                summary = tr("{label}  ·  {finished} of {total} processed, {canceled} canceled",
                             label=tr(label), finished=0, total=1, canceled=1)
                self.assertEqual(self.ui.status.get(), summary)
                menu = self.right_click_app(key)
                self.assertEqual(menu.icon_names[button], original_icon)
                self.assertEqual(button.winfo_geometry(), original_geometry)
                self.assertTrue(button.instate(["!disabled"]))
                menu.dismiss()

    def test_search_by_display_name_and_id_keeps_catalog_wide_update(self):
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
        with patch("fetch_sources.read_page", return_value="1") as page, patch("fetch_sources.open_url", side_effect=download_fixture):
            self.click(self.ui.all_buttons["fetch"])
            self.assertEqual(page.call_count, 2)
        self.assertEqual(tree.get_children(), ())
        self.assertEqual(self.ui.results[self.keys[1]].status, "Updated")

    def test_updates_view_tracks_results_and_attention_includes_recipe_errors(self):
        valid, invalid = self.keys
        nav = self.ui.nav_buttons
        self.assertEqual([view for view, button in nav.items() if button.winfo_manager()],
                         ["checklist", "all"])
        nav["checklist"].invoke()
        self.assertEqual(self.ui.empty_title.get(), "No checklists yet")
        Path(invalid).write_text("{")
        root = self.ui.jobs[valid].cache_root(self.dest)
        root.mkdir(parents=True)
        (root / "pending.json").write_text(json.dumps({"version": "1", "release": "a" * 32, "sha256": {}}))
        self.ui.more_menu.buttons["Reload"].invoke()
        self.assertEqual(nav["attention"].winfo_manager(), "grid")
        tree = self.ui.tree
        with patch("fetch_sources.read_page", return_value="1"):
            self.assertEqual(nav["updates"].winfo_manager(), "grid")
            nav["updates"].invoke()
            self.assertEqual(tree.get_children(), (valid,))
            (root / "pending.json").unlink()
            reply = io.BytesIO(b"fixture")
            reply.headers = {}
            with patch("fetch_sources.open_url", return_value=reply):
                self.click(self.ui.all_buttons["fetch"])
        self.assertEqual(tree.get_children(), ())
        self.assertFalse(nav["updates"].winfo_manager())
        self.assertEqual(self.ui.view_counts["updates"].get(), "0")
        self.ui.nav_buttons["attention"].invoke()
        self.root.update()
        self.assertEqual(tree.get_children(), (invalid,))
        self.assertEqual(tree.set(invalid, "Status"), "Error")
        self.ui.nav_buttons["all"].invoke()
        self.root.update()
        self.assertEqual(tree.get_children(), (valid,))

    def test_reset_search_reveals_counted_item_in_needs_attention(self):
        ui = self.ui
        invalid = self.keys[1]
        Path(invalid).write_text("{")
        ui.reload()
        ui.search.set("alpha")
        ui.nav_buttons["attention"].invoke()
        self.root.deiconify()
        self.root.update()
        self.assertEqual(ui.view_counts["attention"].get(), "1")
        self.assertEqual(ui.tree.get_children(), ())
        self.assertEqual(ui.empty_title.get(), "No matching items")
        self.assertTrue(ui.reset_search_button.winfo_viewable())

        ui.reset_search_button.invoke()
        self.root.update()
        self.assertEqual(ui.search.get(), "")
        self.assertEqual(ui.view.get(), "attention")
        self.assertEqual(ui.tree.get_children(), (invalid,))
        self.assertFalse(ui.reset_search_button.winfo_viewable())

        ui.nav_buttons["progress"].invoke()
        self.root.update()
        self.assertTrue(ui.empty_panel.winfo_viewable())
        self.assertFalse(ui.reset_search_button.winfo_viewable())

    def test_pending_publication_appears_in_both_actionable_views(self):
        root = self.dest / "alpha"
        root.mkdir(parents=True)
        (root / "pending.json").write_text(json.dumps({
            "version": "2", "release": "1" * 32, "sha256": {},
        }))
        self.ui.more_menu.buttons["Reload"].invoke()
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

        progress = self.ui.nav_buttons["progress"]
        self.assertFalse(progress.winfo_manager())
        self.assertEqual(self.ui.view_counts["progress"].get(), "0")
        with patch("shelf_window.execute", side_effect=execute):
            self.ui.more_menu.buttons["Verify all items"].invoke()
            try:
                self.wait_for(lambda: self.keys[0] in self.ui.active_items
                              and self.ui.view_counts["progress"].get() == "1")
                self.assertEqual(progress.winfo_manager(), "grid")
                progress.invoke()
                self.assertEqual(self.ui.tree.get_children(), (self.keys[0],))
                self.assertEqual(self.ui.tree.set(self.keys[0], "Status"), "Verifying…")
                resume["alpha"].set()
                self.wait_for(lambda: self.keys[1] in self.ui.active_items)
                self.assertEqual(self.ui.tree.get_children(), (self.keys[1],))
                self.assertEqual(self.ui.view_counts["progress"].get(), "1")
                self.assertEqual(self.ui.tree.set(self.keys[1], "Status"), "Verifying…")
                self.assertEqual(self.ui.results[self.keys[0]].status, "Verified")
            finally:
                for event in resume.values():
                    event.set()
                self.wait_until_idle()
        self.assertEqual(self.ui.view.get(), "progress")
        self.assertFalse(progress.winfo_manager())
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
        with patch("shelf_window.prepare_fetch", side_effect=execute):
            self.ui.start("fetch", key=self.keys[0])
            try:
                self.wait_for(lambda: self.keys[0] in self.ui.active_items
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
        with patch("shelf_window.prepare_fetch", side_effect=execute):
            self.ui.all_buttons["fetch"].invoke()
            try:
                self.wait_for(lambda: self.ui.view_counts["progress"].get() == "2")
                self.assertCountEqual(self.ui.tree.get_children(), self.keys)
            finally:
                resume.set()
                self.wait_until_idle()
        self.assertEqual(self.ui.tree.get_children(), ())
        self.assertEqual(self.ui.view_counts["progress"].get(), "0")
        self.assertEqual(self.ui.empty_title.get(), "No operations in progress")
        self.assertIn("Worker failed", self.ui.status.get())
        self.assertTrue(self.ui.all_buttons["fetch"].instate(["!disabled"]))

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
        self.assertEqual(editor.tabs.tab(editor.tabs.select(), "text"), "General")

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
            release = self.ui.jobs[str(path)].resolve()
        self.assertEqual(release.version, "4.2")

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

    def test_app_versions_hide_numeric_tag_prefix_in_list_and_info(self):
        key = self.keys[0]
        editor = self.edit_selected()
        for local, available, shown_local, shown_available in (
            ("v1.2", "V2.0-rc1", "1.2", "2.0-rc1"),
            ("vendor", "2.0", "vendor", "2.0"),
        ):
            with self.subTest(local=local, available=available):
                self.ui.results[key] = Result("Update available", local, available)
                self.ui.paint(key)
                self.assertEqual(self.ui.tree.set(key, "Local version"), shown_local)
                self.assertEqual(self.ui.tree.set(key, "Available version"), shown_available)
                detail = editor.detail.get("1.0", "end-1c")
                self.assertIn(f"Local version: {shown_local}", detail)
                self.assertIn(f"Available version: {shown_available}", detail)

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
        self.ui.select_view("attention")
        self.ui.tree.selection_set(first)

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
        with patch("fetch_sources.read_page", return_value="1"), patch("fetch_sources.open_url", side_effect=download_fixture):
            self.click(self.ui.all_buttons["fetch"])
        editor = self.edit_selected()
        catalog = next(tab for tab in editor.tabs.tabs() if editor.tabs.tab(tab, "text") == tr("General"))
        catalog_widgets = list(descendants(editor.nametowidget(catalog)))
        labels = [w.cget("text") for w in catalog_widgets if isinstance(w, ttk.Label)]
        self.assertNotIn(tr("Description"), labels)
        for widget in catalog_widgets:
            if isinstance(widget, tk.Text):
                self.assertNotIn(description.strip(), widget.get("1.0", "end"))
        editor.fields["category"].set("System tools")
        editor.save()

        self.assertEqual(tree.set(key, "Category"), "System tools")
        self.assertEqual(tree.set(key, "Status"), "Updated")
        self.assertNotIn(description.strip(), tree.item(key, "values"))
        self.assertEqual(tree.set(other, "Category"), "Uncategorized")
        self.ui.search_entry.insert(0, "SYSTEM TOOLS")
        self.root.update()
        self.assertEqual(tree.get_children(), (key,))
        editor = self.edit_selected()
        detail = editor.detail.get("1.0", "end-1c")
        self.assertIn("Category: System tools", detail)
        self.assertIn(description.strip(), detail)
        self.assertIn("Updated", detail)
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
            editor.buttons["fetch"].invoke()
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
        self.ui.select_view("attention")
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
        self.assertFalse(editor.windows_iso_button.winfo_manager())
        editor.fields["page"].set("https://www.microsoft.com/en-us/software-download/windows11")
        self.assertEqual(editor.windows_iso_button.winfo_manager(), "grid")
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
        editor.windows_iso_button.invoke()
        editor.fields["page"].set("https://example.org/releases")
        self.assertFalse(editor.windows_iso_button.winfo_manager())
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
