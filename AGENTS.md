# ReleaseShelf updates

For small UI changes, run only the tests that cover the affected behavior. For example, a change to the More menu can be checked with `python run_tests.py -v test_shelf_window.DesktopTest.test_more_menu_opens_with_mouse_and_keyboard`. Run local tests in the background using `run_tests.py`. On Windows the runner keeps Tk windows on a private desktop. Monitor the process and report its exit status and any failures.

After updating ReleaseShelf, rebuild `ReleaseShelf.exe` if needed and restart the executable from this project folder with its window visible. Before reporting completion, verify that the bottom status row shows the version from `shelf_version.py`.

When the user says **release**, publish the current ReleaseShelf changes to GitHub. Choose the next semantic version for the changes since the last release (or use the version the user specifies), update `shelf_version.py`, run the full suite with `python run_tests.py`, and wait for it to pass. Then rebuild the local executable and verify its visible version. Commit the source changes and push the commit and its matching `vMAJOR.MINOR.PATCH` tag to `origin`. The tag starts the Windows release workflow. Wait for it to finish, verify the published release has a downloadable `ReleaseShelf.exe`, and return the release link. Reserve release tags for release requests.

Use “app list” for the list of saved items and “General” for the first item editor tab in user-facing text.

Keep personal app entries, target mappings, cached downloads, and third-party applications out of the repository and release assets. A fresh installation starts with an empty app list.
