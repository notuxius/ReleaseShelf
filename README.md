# ReleaseShelf

ReleaseShelf keeps portable Windows apps, archives, and disk images up to date. Start with an empty catalog and choose **Add item** to track your first download.

ReleaseShelf shows its version at the right end of the bottom status row.

While downloading, a progress bar appears beneath **Downloading…** in the item’s **Status** cell. The bottom status row counts files that need a download, for example **2 of 4 downloads**. Items already up to date and files restored from a local copy are excluded. The bottom row and the open item info dialog also show the percentage received. When the publisher does not provide a file size, the bars animate until the transfer finishes.

## Run it

Download `ReleaseShelf.exe` from [GitHub Releases](https://github.com/notuxius/ReleaseShelf/releases) once a release is available. Place it in a writable folder and double-click it. The executable includes Python. Saving your first item creates a `recipes` folder beside it. Keep that folder when replacing the EXE.

To run from source, use Python 3.13 or newer. Double-click `releaseshelf_ui.pyw` or open PowerShell in this folder and run:

```powershell
python .\releaseshelf_ui.pyw
```

Open **Catalog** to see all tracked items. **Available updates** shows items with a newer release or a pending publication. **In progress** shows the item currently being checked, updated, downloaded, or verified. Its count updates as each item starts and finishes. **Needs attention** shows errors, files that need repair, and pending publications. The counts beside these views update as checks finish.

Under **Categories** in the sidebar, select a category to see its items. Each count shows how many items belong to that category. The list updates when you save an item or reload the catalog. In the collapsed sidebar, click the folder icon to open the category list.

Click **Search catalog** or press **Ctrl+F** to focus the field. Type or paste up to 64 characters to filter the current view by name, category, or ID. Clicking outside keeps the filter active. Click the **×** or press **Esc** in the field to clear the search.

The catalog table starts sorted by **Category** in ascending order. Click **Name**, **Category**, or **Status** to sort by that column. Click the same header again to reverse the order. The arrow shows the current direction.

Right-click any column header to show or hide columns in the catalog table. The menu lists every column in table order. ReleaseShelf remembers your choices across views and restarts. At least one text column stays visible.

Select **Item icon** in the column menu or **Show item icons** in Options to display the icon column before **Name**. Both controls share the same setting. ISO images show a CD icon, even before download when the recipe specifies an ISO filename. Other rows show the local executable’s icon when available and the ReleaseShelf window icon from the column header otherwise.

**Check for updates** compares local versions with publisher versions without downloading files. **Update all items** downloads available updates and repairs missing or damaged files. Both commands act on the whole catalog, including items hidden by the current view or search. The button changes to **Cancel** while the batch runs. Click it to stop the current operation and skip the remaining items. You can update without checking first. **More → Verify all items** checks the catalog offline. To stop verification, reopen **More** and choose **Cancel**. Use **More → Reload catalog** after changing recipe files externally.

Double-click an item or press **Enter** on a selected row to open **Edit item** on the **Catalog** tab. Its **Item info** tab shows the description, category, item ID, versions, source, cache location, and latest result. Right-click the row or press **Shift+F10** for **Check for updates**, **Get updates**, **Verify files**, or **Edit item**. You can also run these individual actions from **Item info**. Choose **Cancel** in a running or queued item’s right-click menu to cancel just that item and let the rest of the batch continue. The window reads local manifests on startup and contacts publishers when you check or fetch.

Use the button at the top of the sidebar to switch to icons only or restore the labels and counts. Hover over an icon for its label.

Open **Options → Language** at the bottom of the sidebar. The picker shows native and English names for Arabic, Bengali, Simplified Chinese, Egyptian Arabic, English, French, German, Hindi, Indonesian, Japanese, Nigerian Pidgin, Brazilian Portuguese, Russian, Spanish, Ukrainian, and Urdu. The checkmark shows the current language. Select a language to apply it immediately. ReleaseShelf remembers your choice the next time you open it. English is the default.

**Options** also lets you choose an appearance and a palette. Each palette has a **Light** and a **Dark** variant. **System** switches between them with the Windows app color setting. Choose from **OLED mint**, **Pure carbon**, **Soft graphite**, **Blue ink**, **Warm amber**, **Slate mint**, **Orchid**, and **Rosewood**. Changes apply immediately and are saved. The defaults are **System** and **Slate mint**.

Use **Add item** to set up an item from a publisher page or GitHub release. In the **Catalog** tab, choose a unique **Item ID**, such as `my-tool`. ReleaseShelf uses this ID for the item’s cache and target mapping. If you edit an existing item’s ID here, ReleaseShelf remembers the old one so downloads and shortcuts keep working.

On the **Catalog** tab, enter an optional **Name** and **Category**. Items without a name display their ID. Items without a category appear as **Uncategorized**. To change the description shown in **Item info**, edit `description` in the recipe JSON. **Download options** lets you specify ZIP members, an archive name, a checksum, and executable settings. Use **Save** to write your settings and return to the catalog.

The window accepts the same `--catalog`, `--dest`, and `--targets` paths as the CLI and uses Tkinter, included in the standard Windows Python installer.

For the command line:

```powershell
python .\releaseshelf.py check --all
python .\releaseshelf.py fetch --all
python .\releaseshelf.py verify --all
```

Use one recipe when testing a change.

```powershell
python .\releaseshelf.py fetch .\recipes\my-tool.json
```

| Command | Behavior |
| --- | --- |
| `check` | Looks up the publisher version and checks the cached files and published file. Matching size and modification time allow a quick check. It hashes files when their metadata differs or an older manifest lacks that metadata. It creates no files. |
| `fetch` | Resolves the publisher version, verifies existing bytes using SHA-256, downloads when needed, and publishes the primary file. A damaged or missing published file can be repaired from a verified cached release. |
| `verify` | Works offline. Reads every cached file and the published file, compares their SHA-256 hashes with the saved manifest, and reports any mismatch or pending publication. |

Batch mode prints a row per recipe and continues past individual failures. Use `--catalog PATH` to select another recipe folder. Exit code `0` means the command completed successfully. Batch `check` returns `2` for an available update, repair, or pending publication. A pending `fetch` also returns `2`. Errors and failed offline verification return `1` and take precedence. Single-recipe `check` retains its original successful exit status for an ordinary available update or repair.

## Where the files go

A recipe with a shortcut name creates a shortcut under the current user's Start menu Programs folder. The shortcut opens the published executable with its folder as the working directory. If a shortcut is missing or changed, **Get updates** can recreate it from a verified local release. **Check for updates** and **Verify files** only report its condition.

ReleaseShelf uses Python’s ZIP reader for selected archive members and Windows `tar.exe` for full-folder extraction. A full-folder recipe publishes supporting files alongside the executable. User-added files, such as logs, carry over when the folder is replaced.

For ZIPs with selected members, the first member in `extract` is the published file unless the recipe names an executable with `publish_exe`. Additional members and the original archive are kept together in the release cache. For archive-only recipes, the archive itself is published.

If the previous default cache folder `%USERPROFILE%\Downloads\PortableFetch` exists, ReleaseShelf uses it so existing shortcuts and target mappings keep working. The previous `portable_fetch.py` command also remains available.

To change the cache root, pass the same `--dest PATH` for every command. To update an existing portable executable at its usual path, put a machine-local `targets.json` inside that cache root. For example:

```json
{
  "my-tool": "D:\\PortableTools\\My Tool\\MyTool.exe"
}
```

Set the value to the **actual executable path used by your shortcut**. You can also pass `--targets PATH` to select a settings file explicitly. Custom targets are absolute filenames outside the cache root. A fetch can replace that file. Each recipe owns one target.

An update is staged and validated before publication. ReleaseShelf writes a temporary file alongside the target, verifies it, and atomically replaces the target. When Windows holds the executable open, the command reports `Pending`. Close the application and repeat `fetch` to finish using the cached bytes. Fetch checks the publisher again on retry. Old release directories remain available under `releases`.

Older ReleaseShelf manifests are supported. The first fetch with the same recipe and publisher version verifies the saved release and creates the published file. Fetch reports `Updated` when it downloads a fresh copy, `Cached` when it uses a verified saved copy to finish publication, and `Latest` when the files already match.

## Write a recipe

Set `shortcut` to a Start menu shortcut name, without the `.lnk` extension, to link to the recipe's primary executable. For a complete app inside an archive folder, set `extract_folder` to the folder name (it can include `{version}`) and list the primary executable first in `extract`. All files below that folder are published together. Complete-folder recipes use their managed app directory. Custom target mappings apply to single-file publications.

For a publisher page, select **Add item → Source → Publisher page** and choose a unique item ID. If you change an existing recipe’s `id` by hand, keep its former IDs in the `previous_ids` array. Set the HTTPS `page`, a `version_pattern` with one capture group, and the HTTPS `download` URL. Use `filename` for a fixed archive name and `extract` for selected top-level ZIP members in publication order.

Publisher recipes can use `{version}` in the download path, query, or archive filename. An optional `sha256_pattern` with one capture group reads the expected SHA-256 from the same release page. ReleaseShelf verifies that digest before publishing the download. The optional Windows ISO resolver checks Microsoft's release and English x64 checksum on **Check for updates**. When **Get updates** needs the image, it reuses the ISO recorded by Ketarin if its checksum matches. Otherwise it verifies the signature of the Fido PowerShell script and uses it to request a fresh Microsoft download link.

For a public GitHub repository with release assets, use this recipe shape and substitute the repository and its asset pattern:

```json
{
  "id": "my-tool",
  "github": "OWNER/REPOSITORY",
  "asset_pattern": "MyTool-*-windows-x64.zip",
  "extract": ["MyTool.exe"]
}
```

ReleaseShelf reads the latest release, matches the asset name using a case-sensitive glob, and requires exactly one match. It shows the candidate names when selection fails. The downloaded archive keeps the author's filename by default. For versioned archive-only downloads, a local target mapping gives the published copy a fixed path.

A recipe may include a hex `sha256` to pin an expected archive digest. GitHub's asset SHA-256 is checked automatically when available. For a Windows EXE inside a ZIP, set `product_version` to its member name to compare its embedded ProductVersion string with the publisher version before publication. A leading lowercase `v` in the publisher tag is ignored. The `publish_exe` field also checks ProductVersion and publishes the named EXE under `bin`.

## Versioning and builds

Set the ReleaseShelf version in `shelf_version.py` before each release. Use `MAJOR.MINOR.PATCH` and bump the major number for breaking changes, the minor number for compatible features, or the patch number for compatible fixes. Reset lower numbers to zero when the major or minor number increases.

On Windows with PyInstaller installed, run `python -m PyInstaller ReleaseShelf.spec` to build `dist\ReleaseShelf.exe`. The footer, publisher request headers, and Windows file properties all read the same release version.

## Run the tests

```powershell
python -m unittest discover -v
```

The default suite supplies publisher responses at the HTTP boundary. It exercises the CLI, real filesystem writes, Windows executable metadata and Windows file locking. On a host that permits loopback connections, set `RELEASESHELF_HTTP_TEST=1` to exercise local HTTP and CLI subprocesses.
