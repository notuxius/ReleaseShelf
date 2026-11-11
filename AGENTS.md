# ReleaseShelf updates

After updating ReleaseShelf, rebuild `ReleaseShelf.exe` if needed and restart the executable from this project folder with its window visible. Before reporting completion, verify that the bottom status row shows the version from `shelf_version.py`.

When the user says **release**, publish the current ReleaseShelf changes to GitHub. Choose the next semantic version for the changes since the last release (or use the version the user specifies), update `shelf_version.py`, run the tests, rebuild the local executable, and verify its visible version. Commit the source changes and push the commit and its matching `vMAJOR.MINOR.PATCH` tag to `origin`. The tag starts the Windows release workflow. Wait for it to finish, verify the published release has a downloadable `ReleaseShelf.exe`, and return the release link. Reserve release tags for release requests.

Keep personal catalog entries, target mappings, cached downloads, and third-party applications out of the repository and release assets. A fresh installation starts with an empty catalog.
