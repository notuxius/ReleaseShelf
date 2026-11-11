"""Exercise the CLI with publisher fixtures or an optional loopback server."""

import hashlib
import io
import json
import os
import runpy
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from email.message import Message
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from urllib.response import addinfourl
import zipfile

from fetch_sources import Recipe, copy_with_progress, windows_product_version
from fetch_store import digest, write_state
from releaseshelf import execute


PROJECT = Path(__file__).resolve().parent


class Publisher(SimpleHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append(self.path)
        super().do_GET()

    def log_message(self, *args):
        pass


class ReleaseShelfTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.publisher = base / "publisher"
        self.publisher.mkdir()
        self.dest = base / "downloads"
        self.requests = []
        self.use_http = os.environ.get("RELEASESHELF_HTTP_TEST", os.environ.get("PORTABLE_FETCH_HTTP_TEST")) == "1"
        url = "https://publisher.example"
        if self.use_http:
            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Publisher, directory=self.publisher))
            server.requests = self.requests
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(thread.join)
            self.addCleanup(server.shutdown)
            url = f"http://127.0.0.1:{server.server_port}"
        self.recipe = {
            "id": "sample-tool", "previous_ids": ["Example.SampleTool"],
            "category": "Utilities", "description": "Test utility.",
            "page": url + "/page.html", "version_pattern": r"SampleTool v([0-9.]+)",
            "download": url + "/sample-tool.zip", "extract": ["SampleTool.exe", "help.txt", "notes.txt"],
        }
        self.recipe_file = base / "recipe.json"
        self.recipe_file.write_text(json.dumps(self.recipe))
        self.page = self.publisher / "page.html"
        self.page.write_text("<h1>SampleTool v1.27</h1>")
        self.archive = self.publisher / "sample-tool.zip"
        with zipfile.ZipFile(self.archive, "w") as package:
            for name in self.recipe["extract"]:
                package.writestr(name, ("fixture:" + name).encode())
        self.job = self.dest / self.recipe["id"]
        self.manifest = self.job / "current.json"

    def reply(self, request, timeout):
        path = urlsplit(request.full_url).path
        self.requests.append(path)
        data = (self.publisher / path.lstrip("/")).read_bytes()
        headers = Message()
        headers["Content-Length"] = str(len(data))
        return addinfourl(io.BytesIO(data), headers, request.full_url, 200)

    def cli(self, action, *, success=True, batch=None, expected_code=None):
        selection = ["--all", "--catalog", str(batch)] if batch else [str(self.recipe_file)]
        args = [str(PROJECT / "releaseshelf.py"), action, *selection, "--dest", str(self.dest)]
        if self.use_http:
            result = subprocess.run([sys.executable, *args], capture_output=True, text=True, timeout=15)
            if expected_code is not None:
                self.assertEqual(result.returncode, expected_code, result.stdout + result.stderr)
                return result.stdout + result.stderr
            self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
            return result.stdout + result.stderr
        output = io.StringIO()
        with (patch("urllib.request.OpenerDirector.open", side_effect=self.reply),
              patch.object(sys, "argv", args), redirect_stdout(output), redirect_stderr(output),
              self.assertRaises(SystemExit) as result):
            runpy.run_path(args[0], run_name="__main__")
        if expected_code is not None:
            self.assertEqual(result.exception.code, expected_code, output.getvalue())
            return output.getvalue()
        self.assertEqual(result.exception.code == 0, success, str(result.exception))
        return output.getvalue() + str(result.exception)

    @unittest.skipUnless(os.name == "nt", "Uses Windows tar and Shell shortcuts")
    def test_folder_publication_and_shortcut_repair(self):
        recipe = Recipe.from_data({**self.recipe, "extract_folder": "Tool v{version}",
                                   "extract": ["tool.exe"], "shortcut": "Tool"})
        link = Path(self.temp.name) / "Programs" / "Tool.lnk"
        live = self.job / "current"

        def package(version, files):
            self.page.write_text(f"SampleTool v{version}")
            with zipfile.ZipFile(self.archive, "w") as z:
                for name, contents in files.items():
                    z.writestr(f"Tool v{version}/{name}", contents)

        def run(action):
            return execute(action, recipe, self.dest, {})

        with (patch("urllib.request.OpenerDirector.open", side_effect=self.reply),
              patch("fetch_store.shortcut_path", return_value=link)):
            package("1.0", {"tool.exe": b"exe", "settings/default.ini": b"first", "old.txt": b"old"})
            self.assertEqual(run("check").status, "Update available")
            self.assertFalse(link.exists())
            self.assertEqual(run("fetch").status, "Updated")
            self.assertEqual(run("verify").status, "Verified")
            (live / "user.log").write_bytes(b"keep")
            (live / "settings/default.ini").unlink()
            self.assertEqual(run("verify").status, "Repair needed")
            self.assertEqual(run("fetch").status, "Cached")
            self.assertEqual((live / "settings/default.ini").read_bytes(), b"first")
            link.unlink()
            self.assertEqual(run("check").status, "Repair needed")
            with patch("fetch_store.write_shortcut", side_effect=PermissionError("locked shortcut")):
                self.assertEqual(run("fetch").status, "Pending")
            self.assertEqual(run("fetch").status, "Cached")
            self.assertTrue(link.is_file())
            self.assertEqual(self.requests.count("/sample-tool.zip"), 1)
            package("1.1", {"tool.exe": b"exe", "settings/default.ini": b"second"})
            self.assertEqual(run("fetch").status, "Updated")
            self.assertFalse((live / "old.txt").exists())
            self.assertEqual((live / "settings/default.ini").read_bytes(), b"second")
            self.assertEqual((live / "user.log").read_bytes(), b"keep")
            package("1.2", {"tool.exe": b"changed", "../outside": b"invalid"})
            self.assertEqual(run("fetch").status, "Error")
            self.assertEqual((live / "tool.exe").read_bytes(), b"exe")
            self.assertEqual(run("verify").status, "Verified")

    def test_fetch_repeat_and_bad_update(self):
        self.assertIn(f"Missing manifest: {self.manifest}", self.cli("check"))
        self.assertFalse(self.dest.exists())
        self.assertIn("Updated: 1.27", self.cli("fetch"))
        saved = self.manifest.read_bytes()
        state = json.loads(saved)
        folder = self.job / "releases" / state["release"]
        original = self.archive.read_bytes()
        self.assertEqual((folder / self.archive.name).read_bytes(), original)
        with zipfile.ZipFile(io.BytesIO(original)) as package:
            for name in self.recipe["extract"]:
                self.assertEqual((folder / "files" / name).read_bytes(), package.read(name))
        self.assertIn("Latest: 1.27", self.cli("fetch"))
        self.assertEqual(self.requests.count("/sample-tool.zip"), 1)
        self.page.write_text("SampleTool v1.28")
        self.archive.write_bytes(b"incomplete zip")
        self.assertIn("Error:", self.cli("fetch", success=False))
        self.assertEqual(self.manifest.read_bytes(), saved)
        for name, checksum in state["sha256"].items():
            self.assertEqual(hashlib.sha256((folder / name).read_bytes()).hexdigest(), checksum)
        self.assertEqual(list(self.job.glob(".stage-*")), [])
        self.assertEqual(list((self.job / "releases").iterdir()), [folder])

    def test_fetch_reports_progress_only_for_network_transfer(self):
        job = Recipe.read(self.recipe_file)
        payload = self.archive.read_bytes()
        for known_length in (True, False):
            with self.subTest(known_length=known_length):
                destination = self.dest / str(known_length)
                response = io.BytesIO(payload)
                response.headers = {"Content-Length": str(len(payload))} if known_length else {}
                progress = []
                with (
                    patch("fetch_sources.read_page", return_value="SampleTool v1.27"),
                    patch("fetch_sources.open_url", return_value=response) as network,
                    patch("fetch_sources.zipfile.ZipFile", wraps=zipfile.ZipFile) as validate,
                ):
                    def report(received, total):
                        self.assertFalse(validate.called)
                        progress.append((received, total))

                    result = execute("fetch", job, destination, {}, report)
                    self.assertEqual(result.status, "Updated")
                    total = len(payload) if known_length else None
                    self.assertEqual(progress, [(0, total), (len(payload), total), (None, None)])
                    self.assertTrue(validate.called)
                    progress.clear()
                    self.assertEqual(execute("fetch", job, destination, {}, report).status, "Latest")
                    published = Path(json.loads((destination / job.id / "current.json").read_text())["published"]["path"])
                    published.unlink()
                    self.assertEqual(execute("fetch", job, destination, {}, report).status, "Cached")
                    self.assertEqual(execute("check", job, destination, {}, report).status, "Latest")
                    self.assertEqual(execute("verify", job, destination, {}, report).status, "Verified")
                    self.assertEqual(progress, [])
                    network.assert_called_once()

    def test_fast_download_progress_is_throttled_and_finishes_at_actual_size(self):
        payload = b"x" * (4 * 1024 * 1024)
        progress = []
        with patch("fetch_sources.monotonic", side_effect=[1, 1.01, 1.02, 1.03]):
            target = io.BytesIO()
            copy_with_progress(io.BytesIO(payload), target, len(payload),
                               lambda received, total: progress.append((received, total)))
        self.assertEqual(target.getvalue(), payload)
        self.assertEqual(progress, [(0, len(payload)), (1024 * 1024, len(payload)),
                                    (len(payload), len(payload)), (None, None)])

    def test_slow_download_reports_available_bytes_before_buffer_fills(self):
        response = io.BytesIO(b"download")
        progress = []
        with (
            patch.object(response, "read1", side_effect=lambda size: response.read(2)),
            patch("fetch_sources.monotonic", side_effect=[1, 1.2, 1.4, 1.6]),
        ):
            copy_with_progress(response, io.BytesIO(), None,
                               lambda received, total: progress.append((received, total)))
        self.assertEqual(progress, [(0, None), (2, None), (4, None), (6, None),
                                    (8, None), (None, None)])

    def test_failed_download_ends_progress_and_cleans_staging(self):
        job = Recipe.read(self.recipe_file)
        for interrupted in (True, False):
            with self.subTest(interrupted=interrupted):
                destination = self.dest / str(interrupted)
                response = io.BytesIO(b"partial")
                response.headers = {"Content-Length": "100"}
                reads = [b"partial", OSError("Transfer interrupted") if interrupted else b""]
                progress = []
                with (
                    patch("fetch_sources.read_page", return_value="SampleTool v1.27"),
                    patch("fetch_sources.open_url", return_value=response),
                    patch.object(response, "read1", side_effect=reads),
                ):
                    result = execute("fetch", job, destination, {},
                                     lambda received, total: progress.append((received, total)))
                self.assertEqual(result.status, "Error")
                self.assertEqual(progress, [(0, 100), (7, 100), (None, None)])
                root = destination / job.id
                self.assertFalse((root / "current.json").exists())
                self.assertEqual(list(root.glob(".stage-*")), [])
                self.assertEqual(list((root / "releases").iterdir()), [])

    def test_cancel_stalled_publisher_reads_releases_the_job(self):
        job = Recipe.read(self.recipe_file)
        for action in ("check", "fetch"):
            with self.subTest(action=action):
                waiting, resume, closed, cancel = Event(), Event(), Event(), Event()
                response = io.BytesIO(b"partial")
                response.headers = {"Content-Length": "100"}

                def read(size):
                    waiting.set()
                    resume.wait(5)
                    return b""

                def close():
                    io.BytesIO.close(response)
                    closed.set()

                results = []
                progress = []
                worker = Thread(target=lambda: results.append(execute(
                    action, job, self.dest, {}, lambda a, b: progress.append((a, b)), cancel)))
                # A check stalls on its first page; an update stalls after resolving that page.
                version = (patch("fetch_sources.read_page", return_value="SampleTool v1.27")
                           if action == "fetch" else nullcontext())
                with (patch.object(response, "read1", read), patch.object(response, "close", close),
                      patch("fetch_sources.open_url", return_value=response), version):
                    worker.start()
                    try:
                        self.assertTrue(waiting.wait(2))
                        cancel.set()
                        worker.join(1)
                        self.assertFalse(worker.is_alive())
                        self.assertEqual([result.status for result in results], ["Canceled"])
                        self.assertFalse(self.manifest.exists())
                        self.assertFalse((self.job / "pending.json").exists())
                        self.assertEqual(list(self.job.glob(".stage-*")), [])
                        if action == "fetch":
                            self.assertEqual(list((self.job / "releases").iterdir()), [])
                    finally:
                        resume.set()
                        worker.join(2)
                        self.assertTrue(closed.wait(2))
                if action == "fetch" and progress:
                    self.assertEqual(progress[-1], (None, None))
        self.assertIn("Updated", self.cli("fetch"))

    def test_cancel_before_or_after_publication_commit(self):
        self.cli("fetch")
        previous = self.manifest.read_bytes()
        state = json.loads(previous)
        target = Path(state["published"]["path"])
        previous_output = target.read_bytes()
        self.page.write_text("SampleTool v1.28")
        with zipfile.ZipFile(self.archive, "w") as package:
            for name in self.recipe["extract"]:
                package.writestr(name, b"replacement")
        job = Recipe.read(self.recipe_file)
        cancel = Event()

        def cancel_before_commit(path):
            checksum = digest(path)
            if path.name.startswith(".releaseshelf-"):
                cancel.set()
            return checksum

        def cancel_after_journal(path, data):
            write_state(path, data)
            if path.name == "pending.json":
                cancel.set()

        with patch("urllib.request.OpenerDirector.open", side_effect=self.reply):
            with patch("fetch_store.digest", side_effect=cancel_before_commit):
                result = execute("fetch", job, self.dest, {}, cancel_event=cancel)
            self.assertEqual(result.status, "Canceled")
            self.assertEqual(self.manifest.read_bytes(), previous)
            self.assertEqual(target.read_bytes(), previous_output)
            self.assertEqual([p.name for p in (self.job / "releases").iterdir()], [state["release"]])
            self.assertFalse((self.job / "pending.json").exists())
            self.assertEqual(list(target.parent.glob(".releaseshelf-*")), [])

            cancel.clear()
            with patch("fetch_store.write_state", side_effect=cancel_after_journal):
                result = execute("fetch", job, self.dest, {}, cancel_event=cancel)
            self.assertEqual(result.status, "Updated")
            self.assertEqual(target.read_bytes(), b"replacement")
            self.assertEqual(json.loads(self.manifest.read_bytes())["version"], "1.28")
            self.assertFalse((self.job / "pending.json").exists())

    def test_unknown_version_and_missing_file_repair(self):
        self.cli("fetch")
        state = json.loads(self.manifest.read_text())
        (self.job / "releases" / state["release"] / "files" / "SampleTool.exe").unlink()
        missing = self.job / "releases" / state["release"] / "files" / "SampleTool.exe"
        self.assertIn(f"Missing file: {missing}", self.cli("check"))
        self.assertIn(f"Fetch needed: Missing file: {missing}", self.cli("fetch"))
        self.assertIn("Latest", self.cli("check"))
        self.page.write_text("Version unavailable")
        self.assertIn("unknown", self.cli("check", success=False))
        self.assertIn("unknown", self.cli("fetch", success=False))
        self.assertEqual(self.requests.count("/sample-tool.zip"), 2)

    def test_catalog_metadata_keeps_existing_release_current(self):
        self.recipe.pop("category")
        self.recipe.pop("description")
        self.recipe_file.write_text(json.dumps(self.recipe))
        self.cli("fetch")
        saved = self.manifest.read_bytes()
        self.recipe.update(category="Hardware", description="Reports battery health.")
        self.recipe_file.write_text(json.dumps(self.recipe))
        self.assertIn("Verified: 1.27", self.cli("verify"))
        self.assertIn("Latest: 1.27", self.cli("fetch"))
        self.assertEqual(self.manifest.read_bytes(), saved)
        self.assertEqual(self.requests.count("/sample-tool.zip"), 1)

    def test_reject_unsafe_destination(self):
        self.recipe["extract"] = ["../outside.exe"]
        self.recipe_file.write_text(json.dumps(self.recipe))
        self.assertIn("Invalid filename", self.cli("fetch", success=False))
        self.assertFalse(self.dest.exists())

    def test_restored_app_id_reuses_previous_cache_manifest_and_custom_target(self):
        current_id = "sample-tool"
        old_id = "Example.SampleTool"
        self.assertEqual(self.recipe["id"], current_id)
        self.recipe_file.write_text(json.dumps(dict(self.recipe, id=old_id, previous_ids=[current_id])))
        target = Path(self.temp.name) / "tools" / "SampleTool.exe"
        self.dest.mkdir()
        mappings = self.dest / "targets.json"
        mappings.write_text(json.dumps({old_id.upper(): str(target)}))
        self.cli("fetch")
        manifest = self.dest / old_id / "current.json"
        saved = manifest.read_bytes()

        self.recipe_file.write_text(json.dumps(self.recipe))
        self.assertIn(f"SHA-256 verified: {target}", self.cli("verify"))
        self.assertIn("Latest: 1.27", self.cli("check"))
        self.assertIn("Latest: 1.27", self.cli("fetch"))
        self.assertEqual(manifest.read_bytes(), saved)
        self.assertFalse((self.dest / current_id).exists())

        mappings.write_text(json.dumps({current_id: str(target)}))
        target.unlink()
        self.assertIn("Cached: 1.27", self.cli("fetch"))
        self.assertIn(f"SHA-256 verified: {target}", self.cli("verify"))
        self.assertEqual(self.requests.count("/sample-tool.zip"), 1)

    def test_conflicting_cache_and_target_ids_stop_before_fetch(self):
        current_id = self.recipe["id"]
        old_id, = self.recipe["previous_ids"]
        (self.dest / current_id).mkdir(parents=True)
        (self.dest / old_id).mkdir()
        self.assertIn("Multiple cache paths", self.cli("fetch", expected_code=1))
        (self.dest / old_id).rmdir()
        (self.dest / "targets.json").write_text(json.dumps({
            key: str(Path(self.temp.name) / f"{key}.exe") for key in (old_id, current_id)
        }))
        self.assertIn("Multiple target mappings", self.cli("fetch", expected_code=1))
        self.assertEqual(self.requests, [])

    def test_repeat_checks_explain_changed_file_and_missing_release(self):
        self.cli("fetch")
        state = json.loads(self.manifest.read_text())
        folder = self.job / "releases" / state["release"]
        archive = folder / self.archive.name
        archive.write_bytes(b"changed")
        self.assertIn(f"SHA-256 mismatch: {archive}", self.cli("check"))
        folder.rename(folder.with_name("moved"))
        self.assertIn(f"Missing release folder: {folder}", self.cli("check"))
        self.assertEqual(self.requests.count("/sample-tool.zip"), 1)

    def test_archive_only_with_fixed_filename_and_new_version(self):
        self.recipe.pop("extract")
        self.recipe["filename"] = "SampleTool-32bit.zip"
        self.recipe_file.write_text(json.dumps(self.recipe))
        self.cli("fetch")
        previous = json.loads(self.manifest.read_text())["release"]
        self.page.write_text("SampleTool v1.28")
        self.assertIn("Updated: 1.28", self.cli("fetch"))
        state = json.loads(self.manifest.read_text())
        self.assertNotEqual(previous, state["release"])
        for release in [previous, state["release"]]:
            folder = self.job / "releases" / release
            self.assertEqual([p.name for p in folder.iterdir()], [self.recipe["filename"]])
        self.assertIn("Latest: 1.28", self.cli("check"))

    def test_versioned_iso_and_reissued_publisher_checksum(self):
        base_url = self.recipe["page"].rsplit("/", 1)[0]
        self.recipe_file.write_text(json.dumps({
            "id": "live-image", "page": self.recipe["page"], "version_pattern": r"version=(\S+)",
            "download": base_url + "/images/{version}/live-{version}.iso",
            "filename": "Live {version}.iso", "sha256_pattern": r"sha256=(\S+)",
        }))
        image = self.publisher / "images" / "1.0" / "live-1.0.iso"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"first image")
        self.page.write_text("version=1.0 sha256=" + hashlib.sha256(image.read_bytes()).hexdigest())
        self.assertIn("Updated: 1.0", self.cli("fetch"))
        self.assertEqual(self.requests.count("/images/1.0/live-1.0.iso"), 1)
        published = self.dest / "live-image" / "current" / "Live 1.0.iso"
        self.assertEqual(published.read_bytes(), b"first image")
        self.assertIn("Latest: 1.0", self.cli("check"))

        replacement = b"reissued image"
        self.page.write_text("version=1.0 sha256=" + hashlib.sha256(replacement).hexdigest())
        self.assertIn("Update available: 1.0", self.cli("check"))
        self.assertIn("SHA-256 mismatch", self.cli("fetch", success=False))
        self.assertEqual(published.read_bytes(), b"first image")
        image.write_bytes(replacement)
        self.assertIn("Updated: 1.0", self.cli("fetch"))
        self.assertEqual(published.read_bytes(), replacement)
        self.assertIn("Verified: 1.0", self.cli("verify"))
        self.page.write_text("version=1.0 sha256=unfinished")
        self.assertIn("Invalid SHA-256", self.cli("fetch", success=False))
        self.assertEqual(self.requests.count("/images/1.0/live-1.0.iso"), 3)

    def test_expanded_archive_name_is_validated_before_download(self):
        recipe = Recipe.from_data({
            "id": "live-image", "page": self.recipe["page"], "version_pattern": r"version=(\S+)",
            "download": "https://publisher.example/latest.iso", "filename": "{version}.iso",
        })
        with patch("fetch_sources.read_page", return_value="version=../outside"):
            with self.assertRaises(ValueError):
                recipe.resolve()

    def test_windows_link_is_requested_only_for_an_actual_download(self):
        recipe = Recipe.from_data({
            "id": "test-image", "page": "https://www.microsoft.com/en-us/software-download/windows11",
            "version_pattern": r"version=(\S+)", "sha256_pattern": r"sha256=(\S+)",
            "download_resolver": "windows-11-english-x64", "filename": "image-{version}.iso",
        })
        payload = b"Windows installation media fixture"
        response = io.BytesIO(payload)
        response.headers = {"Content-Length": str(len(payload))}
        checksum = hashlib.sha256(payload).hexdigest()
        page = "version=26H2 sha256=" + checksum
        with (patch("fetch_sources.read_page", return_value=page),
              patch("fetch_sources.open_windows_iso", return_value=response) as open_iso,
              patch("fetch_sources.open_url") as ordinary_download):
            self.assertEqual(execute("check", recipe, self.dest, {}).status, "Update available")
            open_iso.assert_not_called()
            self.assertEqual(execute("fetch", recipe, self.dest, {}).status, "Updated")
            open_iso.assert_called_once_with("26H2", checksum)
            self.assertEqual(execute("fetch", recipe, self.dest, {}).status, "Latest")
            self.assertEqual(execute("verify", recipe, self.dest, {}).status, "Verified")
            state = json.loads((self.dest / recipe.id / "current.json").read_text())
            published = Path(state["published"]["path"])
            self.assertEqual(published.read_bytes(), payload)
            published.unlink()
            self.assertEqual(execute("fetch", recipe, self.dest, {}).status, "Cached")
            open_iso.assert_called_once()
            ordinary_download.assert_not_called()

    def test_adopt_original_manifest_and_repair_live_file_without_downloading(self):
        self.cli("fetch")
        state = json.loads(self.manifest.read_text())
        live = Path(state["published"]["path"])
        original = live.read_bytes()
        live.unlink()
        old_fields = ("version", "release", "recipe", "sha256")
        self.manifest.write_text(json.dumps({key: state[key] for key in old_fields}))
        self.assertIn("Cached: 1.27", self.cli("fetch"))
        self.assertEqual(live.read_bytes(), original)
        live.write_bytes(b"stale portable application")
        self.assertIn("Repair needed", self.cli("check"))
        self.assertIn("Cached: 1.27", self.cli("fetch"))
        self.assertIn("Latest: 1.27", self.cli("fetch"))
        self.assertEqual(live.read_bytes(), original)
        self.assertEqual(json.loads(self.manifest.read_text())["release"], state["release"])
        self.assertEqual(self.requests.count("/sample-tool.zip"), 1)

    def test_quick_check_and_offline_full_verification(self):
        self.cli("fetch")
        live = Path(json.loads(self.manifest.read_text())["published"]["path"])
        info = live.stat()
        changed = bytearray(live.read_bytes())
        changed[0] ^= 1
        live.write_bytes(changed)
        os.utime(live, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertIn("Latest: 1.27", self.cli("check"))
        before = list(self.requests)
        self.assertIn("SHA-256 mismatch", self.cli("verify", expected_code=1))
        self.assertEqual(self.requests, before)
        self.cli("fetch")
        self.assertIn("Verified: 1.27", self.cli("verify"))

    @unittest.skipUnless(os.name == "nt", "Windows sharing semantics")
    def test_locked_custom_target_retries_cached_release(self):
        live = Path(self.temp.name) / "installed" / "my-tool.exe"
        live.parent.mkdir()
        self.dest.mkdir()
        (self.dest / "targets.json").write_text(json.dumps({self.recipe["id"]: str(live)}))
        self.cli("fetch")
        old = live.read_bytes()
        old_state = self.manifest.read_bytes()
        self.page.write_text("SampleTool v1.28")
        with zipfile.ZipFile(self.archive, "w") as package:
            for name in self.recipe["extract"]:
                package.writestr(name, "updated")
        with live.open("rb"):
            self.assertIn("Pending", self.cli("fetch", expected_code=2))
            self.assertEqual(live.read_bytes(), old)
            self.assertEqual(self.manifest.read_bytes(), old_state)
            self.assertIn("Pending", self.cli("check", expected_code=2))
            self.assertIn("Pending", self.cli("verify", expected_code=1))
        self.assertIn("Cached: 1.28", self.cli("fetch"))
        self.assertEqual(live.read_bytes(), b"updated")
        self.assertFalse((self.job / "pending.json").exists())
        self.assertEqual(self.requests.count("/sample-tool.zip"), 2)

    @unittest.skipUnless(os.name == "nt", "Windows version resource")
    def test_publish_exe_rejects_stale_executable(self):
        self.recipe.update(version_pattern=r"<b>([\d.]+)</b>\s*\[",
                           extract=["sample.exe"], publish_exe="sample.exe")
        self.recipe_file.write_text(json.dumps(self.recipe))
        # Windows Python supplies an actual PE resource without a publisher download.
        exe = Path(sys.executable)
        version = windows_product_version(exe)
        self.page.write_text(f"Latest release notes <b>{version}</b> [release date]")
        with zipfile.ZipFile(self.archive, "w") as package:
            package.write(exe, "sample.exe")
        self.cli("fetch")
        manifest = self.job / "current.json"
        previous = manifest.read_bytes()
        self.page.write_text("Latest release notes <b>99.9.9.9</b> [release date]")
        result = self.cli("fetch", expected_code=1)
        self.assertIn(f"expected 99.9.9.9, got {version}", result)
        self.assertEqual(manifest.read_bytes(), previous)
        self.assertEqual((manifest.parent / "bin" / "sample.exe").read_bytes(), exe.read_bytes())

    @unittest.skipUnless(os.name == "nt", "Windows version resource")
    def test_legacy_publish_exe_uses_named_member_and_original_cache(self):
        exe = Path(sys.executable)
        version = windows_product_version(exe)
        self.recipe.update(extract=["notes.txt", "portable.exe"], publish_exe="portable.exe")
        self.recipe_file.write_text(json.dumps(self.recipe))
        self.page.write_text(f"SampleTool v{version}")
        with zipfile.ZipFile(self.archive, "w") as package:
            package.writestr("notes.txt", "release notes")
            package.write(exe, "portable.exe")
        self.cli("fetch")
        state = json.loads(self.manifest.read_text())
        legacy_fields = {key: self.recipe[key] for key in
                         ("id", "page", "version_pattern", "download", "extract", "publish_exe")}
        legacy_fields["archive"] = self.archive.name
        legacy_hash = hashlib.sha256(json.dumps(legacy_fields, sort_keys=True).encode()).hexdigest()
        self.assertEqual(state["recipe"], legacy_hash)
        old = {key: state[key] for key in ("version", "release", "recipe", "sha256")}
        self.manifest.write_text(json.dumps(old))
        live = self.job / "bin" / "portable.exe"
        live.write_bytes(b"older installed executable")
        self.assertIn("Cached", self.cli("fetch"))
        self.assertIn("Latest", self.cli("fetch"))
        self.assertIn("Verified", self.cli("verify"))
        self.assertEqual(live.read_bytes(), exe.read_bytes())
        self.assertEqual(json.loads(self.manifest.read_text())["release"], old["release"])
        self.assertEqual(self.requests.count("/sample-tool.zip"), 1)
        self.assertFalse((self.job / "current").exists())

    def test_batch_isolates_failures_and_returns_actionable_exit_codes(self):
        catalog = Path(self.temp.name) / "catalog"
        catalog.mkdir()
        (catalog / "good.json").write_text(self.recipe_file.read_text())
        self.assertIn("Update available", self.cli("check", batch=catalog, expected_code=2))
        (catalog / "broken.json").write_text("{")
        result = self.cli("fetch", batch=catalog, expected_code=1)
        self.assertIn("broken.json", result)
        self.assertIn("Updated", result)
        (catalog / "broken.json").unlink()
        self.assertIn("Latest", self.cli("check", batch=catalog, expected_code=0))
        self.page.write_text("no version")
        self.assertIn("1.27\t-\tSource error", self.cli("check", batch=catalog, expected_code=1))
        self.assertIn("Verified", self.cli("verify", batch=catalog, expected_code=0))

    def test_duplicate_recipe_ids_are_rejected_before_fetch(self):
        catalog = Path(self.temp.name) / "catalog"
        catalog.mkdir()
        for name in ("first", "second"):
            (catalog / (name + ".json")).write_text(self.recipe_file.read_text())
        self.assertEqual(self.cli("fetch", batch=catalog, expected_code=1).count("Duplicate recipe ID"), 2)
        self.assertFalse(self.dest.exists())

    def test_github_release_filename_checksum_and_ambiguous_selection(self):
        if self.use_http:
            self.skipTest("GitHub API fixture uses the in-process transport")
        api = self.publisher / "repos" / "owner" / "tool" / "releases" / "latest"
        api.parent.mkdir(parents=True)
        self.recipe_file.write_text(json.dumps({"id": "github-tool", "github": "owner/tool",
                                                "asset_pattern": "*-win.zip"}))
        name = "Tool-v1.0-win.zip"
        asset = {"name": name, "browser_download_url": self.recipe["download"],
                 "digest": "sha256:" + hashlib.sha256(self.archive.read_bytes()).hexdigest()}
        release = {"tag_name": "v1.0", "assets": [asset]}
        api.write_text(json.dumps(release))
        self.cli("fetch")
        live = self.dest / "github-tool" / "current" / name
        self.assertEqual(live.read_bytes(), self.archive.read_bytes())
        self.assertIn("Latest", self.cli("check"))
        release["assets"].append(dict(asset, name="Tool-arm-win.zip"))
        api.write_text(json.dumps(release))
        result = self.cli("fetch", expected_code=1)
        self.assertIn(name, result)
        self.assertIn("Tool-arm-win.zip", result)
        self.assertEqual(self.requests.count("/sample-tool.zip"), 1)
        release["assets"].pop()
        asset["digest"] = "sha256:" + "0" * 64
        api.write_text(json.dumps(release))
        self.assertIn("Publisher SHA-256 mismatch", self.cli("fetch", expected_code=1))
        self.assertEqual(live.read_bytes(), self.archive.read_bytes())


if __name__ == "__main__":
    unittest.main()
