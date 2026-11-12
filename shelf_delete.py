"""Plan and confirm catalog deletion against the same paths that will be removed."""

from contextlib import nullcontext
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil

from fetch_sources import CONFIG_SUFFIXES, Recipe, filename
from fetch_store import check_link, fetch_lock, read_state, tree_members
from releaseshelf import APP_DIR, load_targets, read_jobs
from shelf_checklist import LocalItem


DELETE = "Delete item"
DATA = "Delete item with data"
FOLDER = "Delete entire app folder"


@dataclass(frozen=True)
class Deletion:
    files: tuple = ()
    folders: tuple = ()
    manifests: tuple = ()
    cache: Path | None = None


def check_location(path):
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Expected an absolute path: {path}")
    for part in (*reversed(path.parents), path):
        check_link(part)


def deletion_plan(mode, path, job, dest, targets):
    if mode == DELETE:
        return Deletion()
    if mode not in (DATA, FOLDER):
        raise ValueError(f"Unknown delete command: {mode}")
    files, folders, manifests, app_folders = set(), set(), [], set()
    cache = None
    if isinstance(job, LocalItem):
        # Relative local references are resolved by the catalog loader. Inspect the
        # original path as well so a junction cannot redirect a recursive deletion.
        source = json.loads(path.read_text(encoding="utf-8-sig"))["local_path"]
        reference = path.parent / os.path.expandvars(source)
        for part in (*reversed(reference.parents), reference):
            check_link(part)
        local = reference.resolve()
        if local.is_dir():
            app_folders.add(local)
            if mode == DATA:
                raise ValueError("Use Delete entire app folder to remove a local folder.")
        elif local.is_file():
            files.add(local)
            app_folders.add(local.parent)
    elif isinstance(job, Recipe):
        root = job.cache_root(dest)
        check_location(root)
        root = root.resolve()
        cache = root if root.is_dir() else None
        releases = root / "releases"
        check_link(releases)
        if releases.is_dir():
            folders.update(child for child in releases.iterdir()
                           if re.fullmatch(r"[0-9a-f]{32}", child.name) and child.is_dir())
        for name in ("current.json", "pending.json"):
            manifest = root / name
            check_link(manifest)
            state = read_state(root, name)
            if state is None:
                continue
            manifests.append(manifest)
            published = state.get("published", {})
            if not published.get("path"):
                continue
            primary = Path(published["path"])
            check_location(primary)
            primary = primary.resolve()
            filename(primary.name)
            managed = primary.parent in (root / "current", root / "bin")
            if not managed:
                mappings = load_targets(targets, dest)
                if not any(mappings.get(key) == primary for key in job.id_keys()):
                    raise ValueError(f"Published path no longer matches the item's target mapping: {primary}")
            app_folders.add(primary.parent)
            if primary.is_file():
                files.add(primary)
            if managed and mode == DATA:
                companions = {Path(p) for p in published.get("companions", [])}
                for member in tree_members(state):
                    relative = Path(member[6:])
                    if (member in published.get("stats", {}) and relative not in companions
                            and relative.suffix.lower() not in CONFIG_SUFFIXES and not job.is_portable_setting(relative)):
                        candidate = primary.parent / relative
                        if candidate.is_file():
                            files.add(candidate)
        if mode == FOLDER and (root / ".previous-app").is_dir():
            folders.add(root / ".previous-app")
    else:
        raise ValueError("This item has no app folder.")

    if mode == FOLDER:
        if not app_folders and isinstance(job, Recipe):
            raise ValueError("This item has no app folder.")
        folders.update(folder for folder in app_folders if folder.is_dir())
        files.clear()
    protected = [APP_DIR, path.parent, dest, targets.resolve(), Path.home()]
    if os.environ.get("SystemRoot"):
        protected.append(Path(os.environ["SystemRoot"]))
    protected = tuple(p.resolve() for p in protected)
    for target in (*files, *folders, *manifests):
        check_location(target)
        resolved = target.resolve()
        if (resolved == Path(resolved.anchor) or any(p.is_relative_to(resolved) for p in protected)
                or (resolved.parent == path.parent.resolve() and resolved.suffix.lower() == ".json")):
            raise ValueError(f"Cannot delete ReleaseShelf storage or a protected folder: {target}")
    return Deletion(tuple(sorted(files)), tuple(sorted(folders)), tuple(manifests), cache)


def delete_item(mode, path, job, original, plan, dest, targets):
    """Delete the recipe last so locked files and partial cleanup can be retried."""
    with fetch_lock(path.parent):
        check_link(path)
        if (path.read_bytes() != original
                or dict(read_jobs(path.parent.glob("*.json"))).get(path) != job):
            raise ValueError("This recipe changed on disk. Cancel and reopen it to load the latest version.")
        if plan.cache:
            check_location(plan.cache / ".fetch.lock")
        with fetch_lock(plan.cache) if plan.cache else nullcontext():
            if deletion_plan(mode, path, job, dest, targets) != plan:
                raise ValueError("Reload the app list and try again.")
            for file in plan.files:
                file.unlink()
            for folder in plan.folders:
                shutil.rmtree(folder)
            for manifest in plan.manifests:
                manifest.unlink()
            path.unlink()
