"""Resolve publisher versions and validate downloads for ReleaseShelf."""

import ctypes
import fnmatch
import hashlib
import json
import ntpath
import os
import re
import subprocess
import tarfile
import tempfile
import zipfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from urllib.parse import quote, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from shelf_cancel import check_canceled, file_sha256, publisher_items, read_chunks
from shelf_message import message
from shelf_version import __version__
from shelf_windows_iso import open_windows_iso


WINDOWS_ISO_RESOLVER = "windows-11-english-x64"


def filename(value):
    if (not isinstance(value, str) or not value or value in {".", ".."}
            or ntpath.isreserved(value) or any(c in value for c in '/\\:')
            or value.endswith((".", " "))):
        raise ValueError(message("Invalid filename: {value!r}", value=value))
    return value


def source_url(url):
    parsed = urlsplit(url)
    local = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    if not parsed.hostname or not (parsed.scheme == "https" or (parsed.scheme == "http" and local)):
        raise ValueError("Sources require HTTPS (HTTP is supported for loopback testing).")
    return url


class CheckedRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, source_url(newurl))


def open_url(url):
    request = Request(source_url(url), headers={"User-Agent": f"ReleaseShelf/{__version__}"})
    return build_opener(CheckedRedirect()).open(request, timeout=30)


def read_page(url):
    with open_url(url) as response:
        body = bytearray()
        for chunk in read_chunks(response):
            body.extend(chunk)
            if len(body) > 2_000_000:
                raise ValueError("Version page exceeds 2 MB")
        return body.decode(response.headers.get_content_charset() or "utf-8", errors="replace")


def digest(path):
    with path.open("rb") as stream:
        return file_sha256(stream)


def sha256(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise ValueError(message("Invalid SHA-256: {value!r}", value=value))
    return value.lower()


@dataclass(frozen=True)
class Release:
    version: str
    download: str
    archive: str
    checksum: str | None


@dataclass(frozen=True)
class Recipe:
    id: str
    page: str
    version_pattern: str
    download: str
    archive: str
    extract: tuple
    github: str
    asset_pattern: str
    product_version: str
    checksum: str | None
    publish_exe: str = ""
    name: str = ""
    category: str = ""
    description: str = ""
    previous_ids: tuple = ()
    sha256_pattern: str = ""
    download_resolver: str = ""
    extract_folder: str = ""
    shortcut: str = ""

    @classmethod
    def read(cls, path):
        return cls.from_data(json.loads(path.read_text(encoding="utf-8-sig")))

    @classmethod
    def from_data(cls, data):
        if not isinstance(data, dict):
            raise ValueError("A recipe must be a JSON object")
        members = data.get("extract", [])
        if not isinstance(members, list):
            raise ValueError("extract must be a list of ZIP member filenames")
        names = tuple(filename(item) for item in members)
        extract_folder = filename(data["extract_folder"]) if "extract_folder" in data else ""
        shortcut = filename(data["shortcut"]) if "shortcut" in data else ""
        if extract_folder and not names:
            raise ValueError("A folder recipe must list its primary executable first in extract")
        if len({name.casefold() for name in names}) != len(names):
            raise ValueError("Duplicate extraction filenames")
        publish_exe = data.get("publish_exe") or ""
        if publish_exe and (publish_exe not in names or not publish_exe.lower().endswith(".exe")):
            raise ValueError("publish_exe must name an extracted .exe file")
        product = data.get("product_version", publish_exe)
        if publish_exe and product != publish_exe:
            raise ValueError("product_version and publish_exe must name the same executable")
        if product and product not in names:
            raise ValueError("product_version must name one of the extracted files")
        if shortcut and not (publish_exe or (names and names[0].lower().endswith(".exe"))):
            raise ValueError("A Start menu shortcut requires a published executable")
        checksum = sha256(data["sha256"]) if "sha256" in data else None
        github = data.get("github", "")
        page = pattern = download = asset = archive = checksum_pattern = resolver = ""
        if "github" in data:
            if not re.fullmatch(r"[\w.-]+/[\w.-]+", github, re.ASCII) or any(
                    part in {".", ".."} for part in github.split("/")):
                raise ValueError("github must be owner/repository")
            if any(field in data for field in ("page", "version_pattern", "download", "download_resolver", "sha256_pattern")):
                raise ValueError("Use one source per recipe")
            asset = data["asset_pattern"]
            if not isinstance(asset, str) or not asset:
                raise ValueError("asset_pattern must be a filename glob")
            if "filename" in data:
                archive = filename(data["filename"])
        else:
            page = source_url(data["page"])
            pattern = data["version_pattern"]
            if re.compile(pattern).groups != 1:
                raise ValueError("version_pattern must have one version capture group")
            if "sha256_pattern" in data:
                checksum_pattern = data["sha256_pattern"]
                if re.compile(checksum_pattern).groups != 1:
                    raise ValueError("sha256_pattern must have one checksum capture group")
            if "download_resolver" in data:
                resolver = data["download_resolver"]
                if resolver != WINDOWS_ISO_RESOLVER or "download" in data:
                    raise ValueError("Choose one supported download source")
                if page != "https://www.microsoft.com/en-us/software-download/windows11" or not checksum_pattern:
                    raise ValueError("Windows 11 requires the Microsoft English download page and a SHA-256 pattern")
            else:
                download = source_url(data["download"])
                if "{version}" in urlsplit(download).netloc:
                    raise ValueError("Use {version} in the download path or query")
            archive = filename(data.get("filename", unquote(urlsplit(download).path.rsplit("/", 1)[-1])))
        if names and archive.casefold() == "files":
            raise ValueError("The archive filename conflicts with the extraction folder")
        job_id = filename(data["id"])
        previous = data.get("previous_ids", [])
        if not isinstance(previous, list):
            raise ValueError("previous_ids must be a list of previous item IDs")
        previous_ids = tuple(filename(value) for value in previous)
        all_ids = (job_id, *previous_ids)
        if any(value.casefold() == "targets.json" for value in all_ids):
            raise ValueError("targets.json is reserved for local settings")
        if len(set(all_ids)) != len(all_ids):
            raise ValueError("The recipe contains a repeated item ID")
        metadata = {}
        for key, label in (("name", "Name"), ("category", "Category"), ("description", "Description")):
            value = data.get(key, "")
            if not isinstance(value, str):
                raise ValueError(message("{label} must be text", label=message(label)))
            metadata[key] = value.strip()
        return cls(job_id, page, pattern, download, archive, names, github, asset, product,
                   checksum, publish_exe, previous_ids=previous_ids, sha256_pattern=checksum_pattern,
                   download_resolver=resolver, extract_folder=extract_folder, shortcut=shortcut, **metadata)

    def id_keys(self):
        return tuple(dict.fromkeys(value.casefold() for value in (self.id, *self.previous_ids)))

    def cache_root(self, dest):
        root = dest / self.id
        if not self.previous_ids:
            return root
        paths = dict.fromkeys([root, *(dest / value for value in self.previous_ids)])
        existing = [path for path in paths if path.exists()]
        if len(existing) > 1:
            conflicts = ", ".join(map(str, existing))
            raise ValueError(message("Multiple cache paths exist for {app}: {paths}. "
                                     "Move unused caches outside the download folder before continuing.",
                                     app=self.id, paths=conflicts))
        return existing[0] if existing else root

    def fingerprint(self, recipe_id=None):
        # Preserve the original fingerprint for unchanged page recipes and older manifests.
        fields = {key: getattr(self, key) for key in
                  ("page", "version_pattern", "download", "archive", "extract")}
        fields["id"] = self.id if recipe_id is None else recipe_id
        fields.update({key: getattr(self, key) for key in
                       ("github", "asset_pattern", "checksum", "sha256_pattern", "download_resolver", "extract_folder")
                       if getattr(self, key)})
        if self.publish_exe:
            fields["publish_exe"] = self.publish_exe
        elif self.product_version:
            fields["product_version"] = self.product_version
        return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()

    def matches_fingerprint(self, saved):
        return any(saved == self.fingerprint(value) for value in (self.id, *self.previous_ids))

    def resolve(self):
        if not self.github:
            page = read_page(self.page)
            match = re.search(self.version_pattern, page)
            version = (match.group(1) or "").strip() if match else ""
            if not version:
                raise ValueError("Available version unknown: version_pattern did not match the page")
            checksum = self.checksum
            if self.sha256_pattern:
                match = re.search(self.sha256_pattern, page)
                if not match:
                    raise ValueError("Publisher SHA-256 pattern did not match the page")
                published = sha256(match.group(1))
                if checksum and checksum != published:
                    raise ValueError("Publisher and recipe checksums disagree")
                checksum = published
            archive = filename(self.archive.replace("{version}", version))
            if self.extract and archive.casefold() == "files":
                raise ValueError("The archive filename conflicts with the extraction folder")
            download = self.download.replace("{version}", quote(version, safe=""))
            return Release(version, download, archive, checksum)

        release = json.loads(read_page(f"https://api.github.com/repos/{self.github}/releases/latest"))
        version = release["tag_name"].strip()
        if not version:
            raise ValueError("Empty GitHub release tag")
        assets = [item for item in release["assets"]
                  if item.get("state", "uploaded") == "uploaded"
                  and fnmatch.fnmatchcase(item["name"], self.asset_pattern)]
        if len(assets) != 1:
            candidates = ", ".join(item["name"] for item in (assets or release["assets"])) or message("(none)")
            raise ValueError(message("Expected one asset matching {pattern!r}, found {count}. Assets: {assets}",
                                     pattern=self.asset_pattern, count=len(assets), assets=candidates))
        chosen = assets[0]
        archive = self.archive or filename(chosen["name"])
        if self.extract and archive.casefold() == "files":
            raise ValueError("The archive filename conflicts with the extraction folder")
        checksum = self.checksum
        if chosen.get("digest"):
            algorithm, _, value = chosen["digest"].partition(":")
            if algorithm != "sha256":
                raise ValueError(message("Unsupported asset digest: {algorithm}", algorithm=algorithm))
            published = sha256(value)
            if checksum and checksum != published:
                raise ValueError("GitHub and recipe checksums disagree")
            checksum = published
        return Release(version, source_url(chosen["browser_download_url"]), archive, checksum)

    def expected_files(self, archive):
        return [archive, *["files/" + name for name in self.extract]]

    def primary(self, archive):
        if self.publish_exe:
            return "files/" + self.publish_exe
        return "files/" + self.extract[0] if self.extract else archive


def windows_product_version(path):
    """Read the translated ProductVersion string from a PE without running it."""
    if os.name != "nt":
        raise ValueError("ProductVersion validation requires Windows")
    api = ctypes.WinDLL("version", use_last_error=True)
    size = api.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        raise ValueError(message("No Windows version resource in {file}", file=path.name))
    data = ctypes.create_string_buffer(size)
    if not api.GetFileVersionInfoW(str(path), 0, size, data):
        raise ctypes.WinError(ctypes.get_last_error())
    pointer = ctypes.c_void_p()
    length = ctypes.c_uint()

    def query(key):
        return api.VerQueryValueW(data, key, ctypes.byref(pointer), ctypes.byref(length))

    if query(r"\VarFileInfo\Translation"):
        words = (ctypes.c_ushort * (length.value // 2)).from_address(pointer.value)
        for lang, codepage in zip(words[::2], words[1::2]):
            if query(fr"\StringFileInfo\{lang:04x}{codepage:04x}\ProductVersion") and length.value:
                value = ctypes.wstring_at(pointer.value, length.value).strip("\x00 ")
                if value:
                    return value
    raise ValueError(message("No ProductVersion string in {file}", file=path.name))


def copy_with_progress(response, target, total, on_progress):
    """Report transferred bytes, then (None, None) when the transfer ends."""
    write_chunks(read_chunks(response), target, total, on_progress)


def write_chunks(chunks, target, total, on_progress):
    received = reported = 0
    next_report = 0
    try:
        if on_progress:
            on_progress(0, total)
        for chunk in chunks:
            check_canceled()
            target.write(chunk)
            if not on_progress:
                continue
            received += len(chunk)
            now = monotonic()
            if now >= next_report:
                on_progress(received, total)
                reported = received
                next_report = now + 0.1
        if on_progress and received != reported:
            on_progress(received, total)
    finally:
        if on_progress:
            on_progress(None, None)


def download_chunks(recipe, release):
    stream = (open_windows_iso(release.version, release.checksum) if recipe.download_resolver
              else open_url(release.download))
    with stream as response:
        yield from response_chunks(response)


def response_chunks(response):
    length = response.headers.get("Content-Length")
    local = getattr(response, "url", "").startswith("file:")
    yield (int(length) if length is not None else None), local
    yield from read_chunks(response)


def extract_windows_archive(recipe, release, archive, destination):
    """Read the Windows tar stream and create only validated directories and regular files."""
    tar = Path(os.environ["SystemRoot"]) / "System32" / "tar.exe"
    prefix = filename(recipe.extract_folder.replace("{version}", release.version)) if recipe.extract_folder else ""
    destination.mkdir()
    found = set()
    with tempfile.TemporaryFile() as errors, subprocess.Popen(
        [str(tar), "-cf", "-", "--format", "pax", "@" + str(archive.resolve())],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors,
        creationflags=subprocess.CREATE_NO_WINDOW,
    ) as process:
        try:
            with tarfile.open(fileobj=process.stdout, mode="r|") as stream:
                for entry in stream:
                    check_canceled()
                    parts = entry.name.split("/")
                    if prefix:
                        if parts[0] != prefix:
                            continue
                        parts = parts[1:]
                        if not parts and entry.isdir():
                            continue
                    elif entry.name not in recipe.extract:
                        continue
                    path = destination.joinpath(*(filename(part) for part in parts))
                    if not parts or path == destination or path in found:
                        raise ValueError(f"Duplicate or invalid archive entry: {entry.name}")
                    found.add(path)
                    if entry.isdir():
                        path.mkdir(parents=True, exist_ok=True)
                    elif entry.isreg():
                        path.parent.mkdir(parents=True, exist_ok=True)
                        with stream.extractfile(entry) as source, path.open("xb") as target:
                            for chunk in read_chunks(source):
                                target.write(chunk)
                    else:
                        raise ValueError(f"Unsupported archive entry: {entry.name}")
            # Drain tar padding before waiting for the producer.
            for _ in read_chunks(process.stdout):
                pass
            if process.wait():
                errors.seek(0)
                raise ValueError("Windows tar: " + errors.read(2048).decode(errors="replace").strip())
        except tarfile.TarError as exc:
            raise ValueError(f"Windows tar could not read {archive.name}: {exc}") from exc
        finally:
            if process.poll() is None:
                process.kill()
            process.stdout.close()
    for name in recipe.extract:
        if not (destination / name).is_file():
            raise ValueError(f"Missing archive member: {name}")


def stage_release(recipe, release, folder, on_progress=None, *, local_source=None):
    archive = folder / release.archive
    source = (response_chunks(local_source) if local_source is not None
              else publisher_items(lambda: download_chunks(recipe, release)))
    with closing(source) as chunks:
        expected, local = next(chunks)
        with archive.open("xb") as target:
            write_chunks(chunks, target, expected, None if local else on_progress)
    check_canceled()
    size = archive.stat().st_size
    if not size or (expected is not None and size != expected):
        raise ValueError("Incomplete or empty download")
    if release.checksum and digest(archive) != release.checksum:
        raise ValueError(message("Publisher SHA-256 mismatch for {file}", file=release.archive))
    if recipe.extract_folder or (recipe.extract and archive.suffix.lower() != ".zip" and not zipfile.is_zipfile(archive)):
        extract_windows_archive(recipe, release, archive, folder / "files")
    elif recipe.extract or archive.suffix.lower() == ".zip":
        with zipfile.ZipFile(archive) as package:
            if not recipe.extract:
                for member in package.infolist():
                    try:
                        with package.open(member) as src:
                            for _ in read_chunks(src):
                                pass  # Reading to EOF validates this member's CRC.
                    except zipfile.BadZipFile as exc:
                        raise ValueError(message("Invalid ZIP checksum for {file}", file=member.filename)) from exc
                return
            extracted = folder / "files"
            extracted.mkdir()
            entries = package.namelist()
            for member in recipe.extract:
                if entries.count(member) != 1:
                    raise ValueError(message("Expected exactly one ZIP member named {file}", file=member))
                with package.open(member) as src, (extracted / member).open("xb") as dst:
                    for chunk in read_chunks(src):
                        dst.write(chunk)
    if recipe.product_version:
        actual = windows_product_version(folder / "files" / recipe.product_version)
        expected = release.version.removeprefix("v")
        if actual != expected:
            raise ValueError(message("{file} ProductVersion: expected {expected}, got {actual}",
                                     file=recipe.product_version, expected=expected, actual=actual))
