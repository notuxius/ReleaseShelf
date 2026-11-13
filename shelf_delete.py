"""Plan and confirm catalog deletion against the same paths that will be removed."""

from contextlib import nullcontext
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import stat

from fetch_sources import Recipe, filename
from fetch_store import check_link, fetch_lock, read_state
from releaseshelf import APP_DIR, load_targets, read_jobs
from shelf_checklist import LocalItem


DELETE = "Delete item"
FOLDER = "Delete item with data"


@dataclass(frozen=True)
class Deletion:
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
    if mode != FOLDER:
        raise ValueError(f"Unknown delete command: {mode}")
    folders, manifests, app_folders = set(), [], set()
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
        elif local.is_file():
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
        if (root / ".previous-app").is_dir():
            folders.add(root / ".previous-app")
    else:
        raise ValueError("This item has no app folder.")

    if not app_folders and isinstance(job, Recipe):
        raise ValueError("This item has no app folder.")
    folders.update(folder for folder in app_folders if folder.is_dir())
    protected = [APP_DIR, path.parent, dest, targets.resolve(), Path.home()]
    if os.environ.get("SystemRoot"):
        protected.append(Path(os.environ["SystemRoot"]))
    protected = tuple(p.resolve() for p in protected)
    for target in (*folders, *manifests):
        check_location(target)
        resolved = target.resolve()
        if (resolved == Path(resolved.anchor) or any(p.is_relative_to(resolved) for p in protected)
                or (resolved.parent == path.parent.resolve() and resolved.suffix.lower() == ".json")):
            raise ValueError(f"Cannot delete ReleaseShelf storage or a protected folder: {target}")
    return Deletion(tuple(sorted(folders)), tuple(manifests), cache)


def retry_readonly_removal(remove, path, error):
    if (getattr(error, "winerror", None) != 5 or remove not in (os.unlink, os.rmdir)
            or not os.lstat(path).st_file_attributes & stat.FILE_ATTRIBUTE_READONLY):
        raise error
    os.chmod(path, stat.S_IWRITE, follow_symlinks=False)
    try:
        remove(path)
    except OSError:
        os.chmod(path, stat.S_IREAD, follow_symlinks=False)
        raise


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
            for folder in plan.folders:
                shutil.rmtree(folder, onexc=retry_readonly_removal)
            for manifest in plan.manifests:
                manifest.unlink()
            path.unlink()
