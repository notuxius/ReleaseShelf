"""ReleaseShelf: check publishers, fetch portable apps, or verify local files."""

import argparse
import json
import re
import sys
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass, replace
from http.client import HTTPException
from pathlib import Path
from time import sleep

from fetch_sources import Recipe, Release, filename
from fetch_store import Result, fetch_lock, matches, read_state, run_release, verify
from shelf_cancel import JobCanceled, cancel_scope, check_canceled, publisher_items
from shelf_checklist import Checklist, LocalItem, catalog_item
from shelf_message import error_detail, message


ERRORS = (OSError, ValueError, LookupError, TypeError, AttributeError, re.error,
          zipfile.BadZipFile, HTTPException, RuntimeError)
APP_DIR = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
DEFAULT_CATALOG = APP_DIR / "recipes"
MAX_PARALLEL_ITEMS = 3
UPDATE_RETRY_DELAYS = (1, 2, 4)


def batch_map(action, worker, items):
    if action == "verify":
        yield from map(worker, items)
    else:
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_ITEMS) as pool:
            yield from pool.map(worker, items)


def updates_disabled(job):
    return isinstance(job, Recipe) and job.updates_disabled


def default_destination():
    downloads = Path.home() / "Downloads"
    legacy = downloads / "PortableFetch"
    return legacy if legacy.is_dir() else downloads / "ReleaseShelf"


def load_targets(path, dest):
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        if path == dest / "targets.json":
            return {}
        raise
    if not isinstance(data, dict):
        raise ValueError("Targets must be an object mapping recipe IDs to absolute file paths")
    targets = {}
    owners = {}
    for job, value in data.items():
        key = filename(job).casefold()
        target = Path(value)
        if not target.is_absolute() or not target.name:
            raise ValueError(message("Use an absolute target filename for {job}", job=job))
        filename(target.name)
        target = target.resolve()
        if target.is_relative_to(dest) or target == path.resolve():
            raise ValueError(message("Custom targets must be outside the managed download folder and settings file: "
                                     "{target}", target=target))
        if key in targets or target in owners:
            raise ValueError(message("Duplicate recipe ID or target in {path}: {job} -> {target}",
                                     path=path, job=job, target=target))
        targets[key] = target
        owners[target] = job
    return targets


def run_job(action, recipe, dest, target, on_progress=None, *, prepared=None, defer_download=False):
    root = recipe.cache_root(dest)
    if action == "verify":
        return verify(root, recipe, target)
    with fetch_lock(root) if action == "fetch" else nullcontext():
        state = read_state(root, "current.json")
        local = state["version"] if state else "-"
        if prepared is not None and prepared.can_reuse_release(root, state):
            release = prepared.release
        else:
            try:
                release, = publisher_items(lambda: (recipe.resolve(),))
            except ERRORS as exc:
                check_canceled()
                return Result("Source error", local, detail=error_detail(exc))
        try:
            result = run_release(action, root, recipe, release, target, on_progress, defer_download=defer_download)
            if isinstance(result, Release):
                pending = read_state(root, "pending.json")
                generations = tuple(entry["release"] if entry else None for entry in (state, pending))
                return PreparedDownload(recipe, dest, target, release, generations)
            return result
        except ERRORS as exc:
            return Result("Error", local, release.version, error_detail(exc))


def exit_code(action, result, batch):
    if result.status in {"Source error", "Error"} or (action == "verify" and result.status not in {"Verified", "Local only"}):
        return 1
    if result.status == "Pending" or (batch and result.status in {"Repair needed", "Update available"}):
        return 2
    return 0


def add_recipes_argument(parser):
    parser.add_argument("--recipes", type=Path, default=DEFAULT_CATALOG, help="folder containing recipe files")
    parser.add_argument("--catalog", dest="recipes", type=Path, help=argparse.SUPPRESS)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check", "fetch", "verify"])
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("recipe", type=Path, nargs="?")
    selection.add_argument("--all", action="store_true", help="run every JSON recipe in the recipe folder")
    add_recipes_argument(parser)
    parser.add_argument("--dest", type=Path)
    parser.add_argument("--targets", type=Path, help="machine-local output paths (default: DEST/targets.json)")
    args = parser.parse_args(argv)
    dest = (args.dest or default_destination()).resolve()
    targets = load_targets(args.targets or dest / "targets.json", dest)
    paths = sorted(args.recipes.glob("*.json")) if args.all else [args.recipe]
    if not paths:
        raise ValueError(f"No JSON recipes in {args.recipes}")
    jobs = read_jobs(paths)
    codes = []
    if args.all:
        print("Recipe\tLocal\tAvailable\tResult\tDetails")
        jobs = [(path, job) for path, job in jobs if not isinstance(job, (Checklist, LocalItem))
                and (args.action == "verify" or not updates_disabled(job))]

    def process(item):
        path, job = item
        delays = UPDATE_RETRY_DELAYS if args.all and args.action == "fetch" else ()
        return path, job, execute(args.action, job, dest, targets, retry_delays=delays)

    results = batch_map(args.action, process, jobs) if args.all else map(process, jobs)
    for path, job, result in results:
        name = path.name if isinstance(job, Result) else job.id
        if args.all:
            detail = " | ".join(result.detail.splitlines())
            print(f"{name}\t{result.local}\t{result.available}\t{result.status}\t{detail}")
        else:
            version = result.available if result.available != "-" else result.local
            label = "Source Error" if result.status == "Source error" else result.status
            print(f"{label}: {version}\n{result.detail}")
        codes.append(exit_code(args.action, result, args.all))
    return 1 if 1 in codes else max(codes, default=0)


class ChecklistLoadError(Result):
    """An invalid entry whose JSON still identifies it as a checklist."""


def read_jobs(paths):
    jobs = []
    for path in paths:
        data = None
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            jobs.append((path, catalog_item(data, path=path)))
        except ERRORS as exc:
            kind = ChecklistLoadError if isinstance(data, dict) and data.get("type") == "checklist" else Result
            jobs.append((path, kind("Error", detail=error_detail(exc))))
    counts = Counter(key for _, recipe in jobs if not isinstance(recipe, Result) for key in recipe.id_keys())
    shortcuts = Counter(recipe.shortcut.casefold() for _, recipe in jobs if isinstance(recipe, Recipe) and recipe.shortcut)
    for index, (path, recipe) in enumerate(jobs):
        if not isinstance(recipe, Result):
            duplicates = [key for key in recipe.id_keys() if counts[key] > 1]
            if duplicates:
                detail = message("Duplicate recipe ID {ids} in {path}", ids=", ".join(duplicates), path=path)
                kind = ChecklistLoadError if isinstance(recipe, Checklist) else Result
                jobs[index] = (path, kind("Error", detail=detail))
            elif isinstance(recipe, Recipe) and recipe.shortcut and shortcuts[recipe.shortcut.casefold()] > 1:
                jobs[index] = (path, Result("Error", detail=f"Duplicate Start menu shortcut: {recipe.shortcut}"))
    return jobs


@dataclass(frozen=True)
class PreparedDownload:
    job: Recipe
    dest: Path
    target: Path | None
    release: Release
    generations: tuple
    retry_delays: tuple = ()

    def can_reuse_release(self, root, current):
        # A competing fetch may have published or journaled a newer release while this one waited.
        pending = read_state(root, "pending.json")
        for old_id, entry in zip(self.generations, (current, pending)):
            if entry and entry["release"] != old_id and not matches(entry, self.job, self.release):
                return False
        return True

    def run(self, on_progress=None, *, cancel_event=None, on_retry=None):
        try:
            with cancel_scope(cancel_event):
                return fetch_with_retries(self.job, self.dest, self.target, on_progress, cancel_event,
                                          self.retry_delays, prepared=self, on_retry=on_retry)
        except JobCanceled:
            return Result("Canceled")
        except ERRORS as exc:
            return Result("Error", detail=error_detail(exc))


def fetch_with_retries(job, dest, target, on_progress, cancel_event, delays, *,
                       prepared=None, defer_download=False, on_retry=None):
    while True:
        check_canceled()
        try:
            result = run_job("fetch", job, dest, target, on_progress, prepared=prepared, defer_download=defer_download)
        except ERRORS as exc:
            result = Result("Error", detail=error_detail(exc))
        if isinstance(result, PreparedDownload):
            return replace(result, retry_delays=delays)
        if result.status not in {"Source error", "Error"} or not delays:
            return result
        delay, delays = delays[0], delays[1:]
        if on_retry is not None:
            on_retry()
        if cancel_event is None:
            sleep(delay)
        else:
            cancel_event.wait(delay)
        # Refresh the publisher URL in case the previous download link expired.
        prepared = None


def prepare_fetch(job, dest, targets, *, cancel_event=None, retry_delays=(), on_retry=None):
    """Complete local work now, or queue the resolved release for a network download."""
    return execute("fetch", job, dest, targets, cancel_event=cancel_event,
                   defer_download=True, retry_delays=retry_delays, on_retry=on_retry)


def execute(action, job, dest, targets, on_progress=None, cancel_event=None, *,
            defer_download=False, retry_delays=(), on_retry=None):
    try:
        with cancel_scope(cancel_event):
            if isinstance(job, Result):
                return job
            if isinstance(job, Checklist):
                return Result("Error", detail="Open the setup checklist in ReleaseShelf to choose adjustments.")
            if isinstance(job, LocalItem):
                return local_result(job)
            if action in ("check", "fetch") and job.updates_disabled:
                return Result("Disabled")
            keys = [key for key in job.id_keys() if key in targets]
            if len(keys) > 1:
                raise ValueError(message("Multiple target mappings for {app}: {keys}. "
                                         "Keep one mapping for this item in the targets file.",
                                         app=job.id, keys=", ".join(keys)))
            target = targets[keys[0]] if keys else None
            if action == "fetch":
                return fetch_with_retries(job, dest, target, on_progress, cancel_event, retry_delays,
                                          defer_download=defer_download, on_retry=on_retry)
            return run_job(action, job, dest, target, on_progress)
    except JobCanceled:
        return Result("Canceled")
    except ERRORS as exc:
        return Result("Error", detail=error_detail(exc))


def local_result(job):
    if job.path.exists():
        return Result("Local only", detail=str(job.path))
    return Result("Error", detail=message("Missing file: {path}", path=job.path))


def run_cli():
    try:
        sys.exit(main())
    except ERRORS as error:
        sys.exit(f"Error: {error}")


if __name__ == "__main__":
    run_cli()
