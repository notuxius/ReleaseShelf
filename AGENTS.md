# ReleaseShelf updates

Before tagging each release, compare the changes with the previous published release and write a user-facing summary in `release-notes/vMAJOR.MINOR.PATCH.md`. Call out behavior changes that affect existing users and link to the full comparison. Commit the notes with the release and verify that GitHub displays them after publication.

For small UI changes, run only the tests that cover the affected behavior. For example, a change to the More menu can be checked with `python run_tests.py -v test_shelf_window.DesktopTest.test_more_menu_opens_with_mouse_and_keyboard`. Run local tests in the background using `run_tests.py`. On Windows the runner keeps Tk windows on a private desktop. Monitor the process and report its exit status and any failures.

Bump `shelf_version.py` once per completed ReleaseShelf update, including local-only updates. Increment the patch version for fixes, the minor version for new features, and the major version for breaking changes, unless the user specifies a version. Rebuild `ReleaseShelf.exe`, restart the executable from this project folder with its window visible, and verify that the bottom status row shows the new version.

When the user says **release**, publish the current ReleaseShelf changes to GitHub. Use the version assigned to the update unless the user specifies another version. Run the full suite with `python run_tests.py` and wait for it to pass. Then rebuild the local executable and verify its visible version. Commit the source changes and push the commit and its matching `vMAJOR.MINOR.PATCH` tag to `origin`. The tag starts the Windows release workflow. Wait for it to finish, verify the published release has a downloadable `ReleaseShelf.exe`, and return the release link. Reserve release tags for release requests.

Use “app list” for the list of saved items and “General” for the first item editor tab in user-facing text.

Keep personal app entries, target mappings, cached downloads, and third-party applications out of the repository and release assets. A fresh installation starts with an empty app list.
