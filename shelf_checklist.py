"""Local setup checklists and their explicit, per-step Windows actions."""

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess

from fetch_sources import Recipe, companion_actions, filename
from shelf_theme import write_preference


@dataclass(frozen=True)
class SetupStep:
    label: str
    description: str
    powershell: str
    selected: bool = False


@dataclass(frozen=True)
class Checklist:
    id: str
    name: str
    category: str
    steps: tuple
    software_folder: str

    def id_keys(self):
        return (self.id.casefold(),)


class ChecklistHistory:
    """Last completed run of each adjustment for the current Windows user."""

    def __init__(self, directory, catalog):
        self.directory = directory
        self.catalog = os.path.normcase(str(catalog.resolve()))

    def path(self, item_id, folder, step):
        identity = json.dumps([self.catalog, item_id.casefold(), folder, step.label, step.powershell])
        return self.directory / (sha256(identity.encode()).hexdigest() + ".txt")

    def status(self, item_id, folder, step):
        try:
            status = self.path(item_id, folder, step).read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return "Not applied"
        except (OSError, UnicodeError):
            return "Error"
        return status if status in ("Applied", "Error") else "Error"

    def record(self, item_id, folder, step, status):
        write_preference(self.path(item_id, folder, step), status + "\n")

    def summary(self, item):
        statuses = {self.status(item.id, item.software_folder, step) for step in item.steps}
        if "Error" in statuses:
            return "Error"
        return statuses.pop() if len(statuses) == 1 else "Partially applied"


@dataclass(frozen=True)
class LocalItem:
    id: str
    name: str
    category: str
    path: Path

    def id_keys(self):
        return (self.id.casefold(),)


def catalog_item(data, *, path=None):
    if isinstance(data, dict) and "local_path" in data:
        if any(key in data for key in ("type", "github", "page", "download", "direct_download")):
            raise ValueError("Choose one source for a local item.")
        item_id = filename(data["id"])
        if item_id.casefold() == "targets.json":
            raise ValueError("targets.json is reserved for local settings")
        name, category, location = (data.get(key, "") for key in ("name", "category", "local_path"))
        if not all(isinstance(value, str) for value in (name, category, location)) or not location.strip():
            raise ValueError("Enter a local file or folder path.")
        location = Path(os.path.expandvars(location))
        if not location.is_absolute():
            location = ((path.parent if path else Path.cwd()) / location).resolve()
        return LocalItem(item_id, name.strip(), category.strip(), location)
    if not isinstance(data, dict) or data.get("type") != "checklist":
        return Recipe.from_data(data, path=path)
    companion_actions(data)
    item_id = filename(data["id"])
    if item_id.casefold() == "targets.json":
        raise ValueError("targets.json is reserved for local settings")
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("A checklist needs at least one step.")
    items = []
    for step in steps:
        if not isinstance(step, dict) or not all(isinstance(step.get(key), str) and step[key].strip()
                                                 for key in ("label", "description", "powershell")):
            raise ValueError("Each setup step needs a label, description and PowerShell action.")
        selected = step.get("selected", False)
        if not isinstance(selected, bool):
            raise ValueError("Step selection must be true or false.")
        items.append(SetupStep(step["label"], step["description"], step["powershell"], selected))
    name, category, folder = (data.get(field, "") for field in ("name", "category", "software_folder"))
    if not all(isinstance(value, str) for value in (name, category, folder)):
        raise ValueError("Checklist names, categories and folder paths must be text.")
    return Checklist(item_id, name.strip(), category.strip(), tuple(items), folder)


def read_catalog_item(path):
    return catalog_item(json.loads(path.read_text(encoding="utf-8-sig")), path=path)


def apply_step(step, software_folder, directory):
    """Run only the chosen local script and return its output or an actionable error."""
    if os.name != "nt":
        raise OSError("Setup actions require Windows.")
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    command = (
        "$ErrorActionPreference = 'Stop'\n"
        "[Console]::InputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n"
        "try { & ([ScriptBlock]::Create([Console]::In.ReadToEnd())) } "
        "catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }"
    )
    folder = os.path.expandvars(software_folder)
    if folder:
        folder = str((directory / folder).resolve())
    env = {**os.environ, "RELEASESHELF_SOFTWARE_FOLDER": folder}
    result = subprocess.run([str(shell), "-NoLogo", "-NoProfile", "-NonInteractive",
                             "-ExecutionPolicy", "Bypass", "-Command", command],
                            input=step.powershell, encoding="utf-8", errors="replace", capture_output=True,
                            cwd=directory, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise ValueError(result.stderr.strip() or result.stdout.strip() or f"PowerShell exited with {result.returncode}")
    return result.stdout.strip()


def run_checklist_step(item, step, folder, directory, history):
    try:
        output = apply_step(step, folder, directory)
    except Exception as exc:
        status, output = "Error", str(exc)
    else:
        status = "Applied"
    try:
        history.record(item.id, folder, step, status)
    except OSError as exc:
        return status, output, str(exc)
    return status, output, ""
